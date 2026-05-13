"""
Dependency Graph Builder — constructs import/call graphs from AST analysis
and outputs visualization-ready data for the web dashboard.

Produces:
  - Import graph (file → file edges based on import statements)
  - Call graph (function → function edges based on call expressions)
  - Package dependency graph (external deps with versions)
  - Cluster graph (component-level connections)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from .ast_analyzer import FileAnalysis
from .models import Architecture, Component

log = logging.getLogger("arch-viewer.dep-graph")


@dataclass
class GraphNode:
    """A node in the dependency graph."""
    id: str
    label: str
    kind: str = ""  # "file", "function", "class", "package", "component"
    group: str = ""  # component name or package category
    size: int = 1  # visual weight (e.g., line count or import count)
    metadata: dict = field(default_factory=dict)


@dataclass
class GraphEdge:
    """An edge in the dependency graph."""
    source: str  # node id
    target: str  # node id
    kind: str = ""  # "imports", "calls", "depends_on", "extends"
    weight: int = 1
    label: str = ""


@dataclass
class DependencyGraph:
    """Complete dependency graph ready for visualization."""
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    clusters: dict[str, list[str]] = field(default_factory=dict)  # group → [node_ids]

    def to_dict(self) -> dict:
        return {
            "nodes": [
                {
                    "id": n.id,
                    "label": n.label,
                    "kind": n.kind,
                    "group": n.group,
                    "size": n.size,
                    "metadata": n.metadata,
                }
                for n in self.nodes
            ],
            "edges": [
                {
                    "source": e.source,
                    "target": e.target,
                    "kind": e.kind,
                    "weight": e.weight,
                    "label": e.label,
                }
                for e in self.edges
            ],
            "clusters": self.clusters,
            "stats": {
                "node_count": len(self.nodes),
                "edge_count": len(self.edges),
                "cluster_count": len(self.clusters),
            },
        }


# ──────────────────────────────────────────
# Graph Builders
# ──────────────────────────────────────────


def build_import_graph(
    analyses: dict[str, FileAnalysis],
    arch: Architecture | None = None,
) -> DependencyGraph:
    """
    Build file-level import graph.
    Nodes = files, Edges = import relationships.
    """
    graph = DependencyGraph()
    file_set = set(analyses.keys())

    # Map module names to file paths for resolution
    module_to_file: dict[str, str] = {}
    for path in analyses:
        # Python module mapping
        if path.endswith(".py"):
            mod = path.replace("/", ".")[:-3]
            if mod.endswith(".__init__"):
                mod = mod[:-9]
            module_to_file[mod] = path
            # Also map the basename
            base = os.path.basename(path)[:-3]
            if base != "__init__":
                module_to_file[base] = path

        # JS/TS module mapping
        for ext in (".js", ".jsx", ".ts", ".tsx", ".mjs"):
            if path.endswith(ext):
                mod = path[:-(len(ext))]
                module_to_file[mod] = path
                # Also map relative paths without extension
                base = os.path.basename(path)[:-(len(ext))]
                module_to_file[base] = path

    # Determine component grouping
    comp_map: dict[str, str] = {}  # file → component name
    if arch:
        for comp in arch.components:
            prefix = comp.path if comp.path != "." else ""
            for fpath in analyses:
                if prefix and fpath.startswith(prefix + "/"):
                    comp_map[fpath] = comp.name
                elif not prefix:
                    comp_map.setdefault(fpath, comp.name)

    # Create nodes
    for path, analysis in analyses.items():
        group = comp_map.get(path, "root")
        graph.nodes.append(GraphNode(
            id=path,
            label=os.path.basename(path),
            kind="file",
            group=group,
            size=max(1, analysis.total_lines // 50),  # scale by file size
            metadata={
                "language": analysis.language,
                "lines": analysis.total_lines,
                "functions": len(analysis.functions),
                "classes": len(analysis.classes),
                "complexity": analysis.complexity,
            },
        ))

        # Track clusters
        if group not in graph.clusters:
            graph.clusters[group] = []
        graph.clusters[group].append(path)

    # Create edges from imports
    for path, analysis in analyses.items():
        for imp in analysis.imports:
            target = _resolve_import_target(path, imp.module, imp.is_relative, module_to_file, file_set)
            if target and target != path:
                graph.edges.append(GraphEdge(
                    source=path,
                    target=target,
                    kind="imports",
                    label=", ".join(imp.names[:3]) if imp.names else imp.module,
                ))

    return graph


def build_call_graph(analyses: dict[str, FileAnalysis]) -> DependencyGraph:
    """
    Build function-level call graph.
    Nodes = functions, Edges = function calls.
    """
    graph = DependencyGraph()

    # Collect all defined functions with their fully qualified names
    func_registry: dict[str, str] = {}  # simple name → file:func id
    all_funcs: dict[str, dict] = {}  # id → metadata

    for path, analysis in analyses.items():
        for func in analysis.functions:
            func_id = f"{path}:{func.name}"
            if func.class_name:
                func_id = f"{path}:{func.class_name}.{func.name}"

            func_registry[func.name] = func_id
            all_funcs[func_id] = {
                "file": path,
                "name": func.name,
                "class": func.class_name,
                "complexity": func.complexity,
                "calls": func.calls,
                "is_async": func.is_async,
            }

            label = func.name
            if func.class_name:
                label = f"{func.class_name}.{func.name}"

            graph.nodes.append(GraphNode(
                id=func_id,
                label=label,
                kind="function",
                group=path,
                size=max(1, func.complexity),
                metadata={
                    "file": path,
                    "line": func.line,
                    "complexity": func.complexity,
                    "is_async": func.is_async,
                    "args": func.args,
                },
            ))

    # Create edges from call expressions
    for func_id, meta in all_funcs.items():
        for call_name in meta["calls"]:
            # Try to resolve the call to a known function
            target_id = func_registry.get(call_name)
            if target_id and target_id != func_id:
                graph.edges.append(GraphEdge(
                    source=func_id,
                    target=target_id,
                    kind="calls",
                ))

    return graph


def build_package_graph(arch: Architecture) -> DependencyGraph:
    """
    Build external dependency graph.
    Nodes = packages, Edges = dependency relationships.
    """
    graph = DependencyGraph()

    # Project node at center
    graph.nodes.append(GraphNode(
        id="project",
        label=arch.project_name,
        kind="project",
        group="root",
        size=5,
    ))

    for dep in arch.dependencies:
        dep_id = f"pkg:{dep.name}"
        graph.nodes.append(GraphNode(
            id=dep_id,
            label=dep.name,
            kind="package",
            group=dep.category,
            size=1,
            metadata={"version": dep.version, "category": dep.category},
        ))

        graph.edges.append(GraphEdge(
            source="project",
            target=dep_id,
            kind="depends_on",
            label=dep.version,
        ))

        if dep.category not in graph.clusters:
            graph.clusters[dep.category] = []
        graph.clusters[dep.category].append(dep_id)

    return graph


def build_component_graph(arch: Architecture) -> DependencyGraph:
    """
    Build component-level architecture graph.
    Nodes = components, Edges = data flows.
    """
    graph = DependencyGraph()

    for comp in arch.components:
        graph.nodes.append(GraphNode(
            id=f"comp:{comp.name}",
            label=comp.name,
            kind="component",
            group=comp.type.value,
            size=max(1, len(comp.files) // 10),
            metadata={
                "type": comp.type.value,
                "tech_stack": comp.tech_stack,
                "file_count": len(comp.files),
                "route_count": len(comp.api_routes),
                "path": comp.path,
            },
        ))

    for flow in arch.data_flows:
        graph.edges.append(GraphEdge(
            source=f"comp:{flow.source}",
            target=f"comp:{flow.target}",
            kind=flow.protocol,
            label=flow.description,
        ))

    return graph


# ──────────────────────────────────────────
# Import Resolution
# ──────────────────────────────────────────


def _resolve_import_target(
    source_file: str,
    module: str,
    is_relative: bool,
    module_to_file: dict[str, str],
    file_set: set[str],
) -> str | None:
    """Try to resolve an import to a project file path."""
    if not module:
        return None

    # Direct module name match
    if module in module_to_file:
        return module_to_file[module]

    # Try as a file path
    candidates = [
        module.replace(".", "/") + ".py",
        module.replace(".", "/") + "/index.js",
        module.replace(".", "/") + "/index.ts",
        module.replace(".", "/") + ".js",
        module.replace(".", "/") + ".ts",
        module.replace(".", "/") + ".tsx",
    ]

    for candidate in candidates:
        if candidate in file_set:
            return candidate

    # Relative import resolution
    if is_relative:
        source_dir = "/".join(source_file.split("/")[:-1])
        for ext in (".py", ".js", ".ts", ".tsx", ".jsx"):
            rel_candidate = f"{source_dir}/{module.replace('.', '/')}{ext}"
            if rel_candidate in file_set:
                return rel_candidate
            # Also check __init__.py
            init_candidate = f"{source_dir}/{module.replace('.', '/')}/__init__.py"
            if init_candidate in file_set:
                return init_candidate

    return None


# ──────────────────────────────────────────
# Graph Analysis Utilities
# ──────────────────────────────────────────


def find_hotspots(graph: DependencyGraph, top_n: int = 10) -> list[dict]:
    """Find the most connected nodes (highest in-degree + out-degree)."""
    in_degree: dict[str, int] = {}
    out_degree: dict[str, int] = {}

    for edge in graph.edges:
        out_degree[edge.source] = out_degree.get(edge.source, 0) + 1
        in_degree[edge.target] = in_degree.get(edge.target, 0) + 1

    node_map = {n.id: n for n in graph.nodes}
    all_ids = set(in_degree.keys()) | set(out_degree.keys())

    hotspots = []
    for nid in all_ids:
        total = in_degree.get(nid, 0) + out_degree.get(nid, 0)
        node = node_map.get(nid)
        hotspots.append({
            "id": nid,
            "label": node.label if node else nid,
            "in_degree": in_degree.get(nid, 0),
            "out_degree": out_degree.get(nid, 0),
            "total_connections": total,
            "kind": node.kind if node else "",
        })

    hotspots.sort(key=lambda x: x["total_connections"], reverse=True)
    return hotspots[:top_n]


def detect_isolated_nodes(graph: DependencyGraph) -> list[str]:
    """Find nodes with zero connections."""
    connected = set()
    for edge in graph.edges:
        connected.add(edge.source)
        connected.add(edge.target)

    return [n.id for n in graph.nodes if n.id not in connected]
