"""
Standalone interactive architecture diagram generator.

Produces a single self-contained HTML file (no external CSS/JS) styled to match
the Sales Coach reference diagram: pannable/zoomable world, color-tier node
cards with CSS glow, animated cubic-bezier SVG flow edges, layer column labels,
slide-in detail panel, legend, and topbar controls.

Each Component is expanded into multiple sub-nodes (one per route-file, entry
point, or well-known entry file) so a single project produces a rich
multi-column / multi-row diagram instead of a flat 1-node-per-column view.

Public API:
    generate_interactive_html(arch, project_name) -> str
"""

from __future__ import annotations

import json
import os
from collections import OrderedDict
from html import escape
from typing import Iterable

from .models import Architecture, APIRoute, Component, ComponentType, DataFlow


# ─── Layout ──────────────────────────────────────────────────────────────────

# Column order — one column per component type. Types not in this list fall
# through to the trailing "other" column.
COLUMN_ORDER: list[ComponentType] = [
    ComponentType.EXTENSION,
    ComponentType.FRONTEND,
    ComponentType.API_GATEWAY,
    ComponentType.BACKEND,
    ComponentType.MCP_SERVER,
    ComponentType.WORKER,
    ComponentType.QUEUE,
    ComponentType.AUTH,
    ComponentType.CACHE,
    ComponentType.STORAGE,
    ComponentType.DATABASE,
    ComponentType.DOCKER,
    ComponentType.CI_CD,
    ComponentType.CONFIG,
    ComponentType.OTHER,
]

COLUMN_LABELS: dict[ComponentType, str] = {
    ComponentType.FRONTEND: "Frontend",
    ComponentType.EXTENSION: "Extension",
    ComponentType.API_GATEWAY: "API Gateway",
    ComponentType.BACKEND: "Backend API",
    ComponentType.MCP_SERVER: "MCP Servers",
    ComponentType.WORKER: "Workers",
    ComponentType.QUEUE: "Queues",
    ComponentType.AUTH: "Auth",
    ComponentType.CACHE: "Cache",
    ComponentType.STORAGE: "Storage",
    ComponentType.DATABASE: "Databases",
    ComponentType.DOCKER: "Docker",
    ComponentType.CI_CD: "CI / CD",
    ComponentType.CONFIG: "Config",
    ComponentType.OTHER: "Other",
}

# Type -> tier class (drives CSS border/glow color). Aligned with the
# Sales Coach palette: ext=cyan, api=indigo, infra=purple, postcall=yellow,
# critical=red, high=orange, normal=blue, low=gray.
TIER_BY_TYPE: dict[ComponentType, str] = {
    ComponentType.FRONTEND: "normal",       # blue
    ComponentType.EXTENSION: "ext",         # cyan
    ComponentType.API_GATEWAY: "high",      # orange
    ComponentType.BACKEND: "api",           # indigo
    ComponentType.MCP_SERVER: "api",        # indigo
    ComponentType.WORKER: "postcall",       # yellow
    ComponentType.QUEUE: "critical",        # red
    ComponentType.AUTH: "postcall",         # yellow
    ComponentType.CACHE: "high",            # orange
    ComponentType.STORAGE: "infra",         # purple
    ComponentType.DATABASE: "infra",        # purple
    ComponentType.DOCKER: "infra",          # purple
    ComponentType.CI_CD: "low",             # gray
    ComponentType.CONFIG: "low",            # gray
    ComponentType.OTHER: "low",             # gray
}

# Layout constants (CSS px in the "world" coordinate space)
COL_WIDTH = 260
COL_X_START = 60
NODE_X_OFFSET = 30          # node x within column
NODE_Y_START = 120
NODE_Y_STEP = 100
NODE_Y_STEP_TIGHT = 80      # used when a column has many sub-nodes
TIGHT_PACK_THRESHOLD = 6    # >= this many sub-nodes -> use tight pack
LAYER_LABEL_Y = 60
MAX_SUBNODES_PER_COMPONENT = 14

# Well-known entry-file basenames (per language/stack) used as a last-resort
# source of sub-nodes when a component has no routes / entry_points.
WELL_KNOWN_ENTRY_FILES = {
    # JavaScript / TypeScript / Chrome extension
    "manifest.json", "background.js", "background.ts",
    "content.js", "content.ts", "content-script.js",
    "offscreen.js", "offscreen.html",
    "sidepanel.js", "sidepanel.html", "panel.js",
    "popup.js", "popup.html", "options.js", "options.html",
    "service-worker.js", "sw.js",
    "index.js", "index.ts", "index.tsx", "index.jsx",
    "main.js", "main.ts", "main.tsx", "main.jsx",
    "app.js", "app.ts", "app.tsx", "app.jsx",
    "server.js", "server.ts",
    # Python
    "main.py", "app.py", "server.py", "asgi.py", "wsgi.py",
    "__main__.py", "manage.py",
    # Go / Rust / Java
    "main.go", "main.rs", "Main.java",
    # Docker / Infra
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "Makefile",
}


def _safe_id(name: str) -> str:
    """Make a string safe to use as a JS/DOM identifier."""
    out = []
    for ch in name:
        if ch.isalnum() or ch == "_":
            out.append(ch)
        else:
            out.append("_")
    return "n_" + "".join(out)


def _protocol_class(protocol: str) -> str:
    """Map a DataFlow.protocol string to a CSS edge class."""
    p = (protocol or "").lower()
    if any(k in p for k in ("ws", "websocket", "socket")):
        return "ws"
    if "grpc" in p:
        return "grpc"
    if any(k in p for k in ("http", "rest", "api")):
        return "http"
    if any(k in p for k in ("queue", "mq", "kafka", "rabbit", "amqp", "pubsub", "sqs", "sns")):
        return "queue"
    if any(k in p for k in ("db", "sql", "query")):
        return "db"
    return "default"


# ─── Sub-node expansion ──────────────────────────────────────────────────────

def _basename(path: str) -> str:
    """File basename of a route's source file (cross-platform)."""
    if not path:
        return ""
    return os.path.basename(path.replace("\\", "/"))


def _expand_component(comp: Component) -> list[dict]:
    """
    Turn one Component into a list of sub-node *blueprints* (no x/y yet).

    Strategy (first non-empty wins):
      1. api_routes  -> group by file, one sub-node per route-file
      2. entry_points -> one sub-node per entry
      3. well-known entry files in `files`
      4. fallback   -> single sub-node for the whole component

    Each blueprint dict carries:
        label, sub, path, kind ("route_file"|"entry"|"file"|"component"),
        routes (list of APIRoute mini-dicts, may be empty),
        component_name, component_description
    """
    blueprints: list[dict] = []

    # --- 1. routes grouped by file ----------------------------------------
    if comp.api_routes:
        # Use OrderedDict so insertion order is preserved deterministically.
        by_file: "OrderedDict[str, list[APIRoute]]" = OrderedDict()
        for r in comp.api_routes:
            key = r.file or "(unknown)"
            by_file.setdefault(key, []).append(r)
        for fpath, routes in list(by_file.items())[:MAX_SUBNODES_PER_COMPONENT]:
            base = _basename(fpath) or fpath
            blueprints.append({
                "label": base,
                "sub": f"{len(routes)} route" + ("s" if len(routes) != 1 else ""),
                "path": fpath,
                "kind": "route_file",
                "routes": [
                    {"method": r.method, "path": r.path, "file": r.file}
                    for r in routes[:30]
                ],
                "component_name": comp.name,
                "component_description": comp.description or "",
            })
        return blueprints

    # --- 2. entry points --------------------------------------------------
    if comp.entry_points:
        for ep in comp.entry_points[:MAX_SUBNODES_PER_COMPONENT]:
            base = _basename(ep) or ep
            # Build a friendly sub-label from the directory above the file
            parent = os.path.dirname(ep.replace("\\", "/"))
            parent = parent.split("/")[-1] if parent else ""
            blueprints.append({
                "label": base,
                "sub": parent or "Entry point",
                "path": ep,
                "kind": "entry",
                "routes": [],
                "component_name": comp.name,
                "component_description": comp.description or "",
            })
        return blueprints

    # --- 3. well-known entry files within files[] -------------------------
    if comp.files:
        picks: list[str] = []
        for f in comp.files:
            base = _basename(f)
            if base in WELL_KNOWN_ENTRY_FILES:
                picks.append(f)
            if len(picks) >= MAX_SUBNODES_PER_COMPONENT:
                break
        if picks:
            for fpath in picks:
                base = _basename(fpath) or fpath
                parent = os.path.dirname(fpath.replace("\\", "/"))
                parent = parent.split("/")[-1] if parent else ""
                blueprints.append({
                    "label": base,
                    "sub": parent or "File",
                    "path": fpath,
                    "kind": "file",
                    "routes": [],
                    "component_name": comp.name,
                    "component_description": comp.description or "",
                })
            return blueprints

    # --- 4. fallback: one node ------------------------------------------
    blueprints.append({
        "label": comp.name,
        "sub": COLUMN_LABELS.get(comp.type, comp.type.value),
        "path": comp.path or "",
        "kind": "component",
        "routes": [],
        "component_name": comp.name,
        "component_description": comp.description or "",
    })
    return blueprints


def _layout(arch: Architecture) -> tuple[dict, list[dict], list[dict], dict]:
    """
    Position components into columns, expanding each into sub-nodes.

    Returns:
        nodes_by_component_name: {component_name -> primary sub-node dict}
            (used so DataFlow edges referencing the component land on its
            primary sub-node)
        nodes_list: all node dicts in render order
        layer_labels: column labels at the top
        nodes_by_id: {node_id -> node dict}
    """
    # Group components by type, preserving original order within each group
    by_type: dict[ComponentType, list[Component]] = {t: [] for t in COLUMN_ORDER}
    for c in arch.components:
        ct = c.type if c.type in by_type else ComponentType.OTHER
        by_type[ct].append(c)

    nodes_list: list[dict] = []
    nodes_by_component_name: dict[str, dict] = {}
    nodes_by_id: dict[str, dict] = {}
    layer_labels: list[dict] = []

    col_index = 0
    for ctype in COLUMN_ORDER:
        comps = by_type.get(ctype, [])
        if not comps:
            continue

        x = COL_X_START + col_index * COL_WIDTH
        col_y = NODE_Y_START

        # Layer label: column heading. If the column has a single component
        # whose name differs from the generic label, prefer the component name
        # so e.g. "Backend API" or "Extension" shows the actual component
        # name like the Sales Coach reference.
        label_text = COLUMN_LABELS[ctype]
        if len(comps) == 1 and comps[0].name and comps[0].name.lower() != label_text.lower():
            label_text = f"{comps[0].name} ({label_text})" if label_text not in comps[0].name else comps[0].name
        layer_labels.append({
            "text": label_text,
            "x": x + NODE_X_OFFSET,
            "y": LAYER_LABEL_Y,
        })

        for comp in comps:
            blueprints = _expand_component(comp)
            # Choose step size based on how many sub-nodes we're stacking in
            # this column overall — tighter when crowded.
            step = NODE_Y_STEP_TIGHT if len(blueprints) >= TIGHT_PACK_THRESHOLD else NODE_Y_STEP

            primary_node: dict | None = None
            for bp in blueprints:
                node_id = _safe_id(comp.name + "__" + bp["label"] + "__" + str(col_y))
                node = {
                    "id": node_id,
                    "name": bp["label"],
                    "label": bp["label"],
                    "sub": bp["sub"],
                    "tier": TIER_BY_TYPE.get(ctype, "low"),
                    "type": ctype.value,
                    "x": x + NODE_X_OFFSET,
                    "y": col_y,
                    "path": bp["path"],
                    "description": bp["component_description"],
                    "tech_stack": list(comp.tech_stack or []),
                    "files_count": len(comp.files or []),
                    "routes_count": len(bp.get("routes") or []) if bp["kind"] == "route_file" else len(comp.api_routes or []),
                    "api_routes": bp.get("routes") or [],
                    "kind": bp["kind"],
                    "component_name": comp.name,
                }
                nodes_list.append(node)
                nodes_by_id[node_id] = node
                if primary_node is None:
                    primary_node = node
                col_y += step

            # Map this component name to its *primary* sub-node so flow edges
            # referencing the component land on a single anchor point.
            if primary_node is not None:
                nodes_by_component_name[comp.name] = primary_node

            # Small gap between distinct components stacked in the same
            # column (rare, but possible).
            col_y += int(step * 0.4)

        col_index += 1

    return nodes_by_component_name, nodes_list, layer_labels, nodes_by_id


def _build_flow_edges(
    flows: Iterable[DataFlow], nodes_by_component_name: dict[str, dict]
) -> list[dict]:
    edges: list[dict] = []
    for f in flows:
        src = nodes_by_component_name.get(f.source)
        tgt = nodes_by_component_name.get(f.target)
        if not src or not tgt:
            continue
        edges.append({
            "from": src["id"],
            "to": tgt["id"],
            "cls": _protocol_class(f.protocol),
            "protocol": f.protocol or "",
            "description": f.description or "",
            "bidi": (f.direction.value == "bidirectional") if hasattr(f.direction, "value") else False,
        })
    return edges


def _build_cohesion_edges(nodes_list: list[dict]) -> list[dict]:
    """
    Build subtle internal edges between sub-nodes of the same component
    (consecutive vertical pairs). Shows that sub-nodes belong to one logical unit.
    """
    edges: list[dict] = []
    # Group nodes by component_name, preserve order
    by_comp: "OrderedDict[str, list[dict]]" = OrderedDict()
    for n in nodes_list:
        cname = n.get("component_name") or n.get("label", "")
        by_comp.setdefault(cname, []).append(n)
    for cname, subs in by_comp.items():
        if len(subs) < 2:
            continue
        for a, b in zip(subs, subs[1:]):
            edges.append({
                "from": a["id"],
                "to": b["id"],
                "cls": "cohesion",
                "protocol": "",
                "description": f"{cname} internal",
                "bidi": False,
            })
    return edges


def _build_import_edges(
    arch: Architecture, nodes_list: list[dict]
) -> list[dict]:
    """
    Infer file-to-file edges from FileInfo.imports. We only emit an edge
    if BOTH the importer and the imported target map to known sub-nodes
    (by file basename match) so the diagram doesn't explode.
    """
    edges: list[dict] = []

    # Build a flat list of FileInfo from arch.file_tree
    file_infos = _flatten_file_tree(arch.file_tree)
    if not file_infos:
        return edges

    # Map basename -> node id (for nodes whose `path` looks like a file)
    name_to_node: dict[str, str] = {}
    for n in nodes_list:
        bp = _basename(n.get("path") or "")
        if bp and bp not in name_to_node:
            name_to_node[bp] = n["id"]

    seen: set[tuple[str, str]] = set()
    for fi in file_infos:
        from_base = _basename(fi.get("path", ""))
        from_id = name_to_node.get(from_base)
        if not from_id:
            continue
        for imp in (fi.get("imports") or []):
            imp_base = _basename(imp.replace(".", "/")) or imp
            # Try multiple normalizations
            candidates = [imp_base, imp_base + ".py", imp_base + ".js", imp_base + ".ts", imp_base + ".tsx"]
            target_id = None
            for c in candidates:
                if c in name_to_node:
                    target_id = name_to_node[c]
                    break
            if not target_id or target_id == from_id:
                continue
            key = (from_id, target_id)
            if key in seen:
                continue
            seen.add(key)
            edges.append({
                "from": from_id,
                "to": target_id,
                "cls": "import",
                "protocol": "import",
                "description": "",
                "bidi": False,
            })
    return edges


def _flatten_file_tree(tree: dict, prefix: str = "") -> list[dict]:
    """Walk Architecture.file_tree dict and collect all FileInfo-like dicts."""
    out: list[dict] = []
    if not isinstance(tree, dict):
        return out
    for key, val in tree.items():
        if key == "_file" and isinstance(val, dict):
            out.append(val)
        elif isinstance(val, dict):
            if "_file" in val:
                out.append(val["_file"])
            else:
                out.extend(_flatten_file_tree(val, f"{prefix}/{key}"))
    return out


# ─── HTML template ───────────────────────────────────────────────────────────

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TITLE__</title>
<style>
  *,*::before,*::after{margin:0;padding:0;box-sizing:border-box}
  :root{
    --bg:#0a0e17;--surface:#111827;--border:#1e293b;
    --text:#e2e8f0;--muted:#64748b;
    --critical:#ef4444;--high:#f97316;--normal:#3b82f6;--low:#6b7280;
    --accent:#6366f1;--accent2:#8b5cf6;
    --glow-critical:0 0 12px rgba(239,68,68,.5);
    --glow-high:0 0 12px rgba(249,115,22,.45);
    --glow-normal:0 0 12px rgba(59,130,246,.45);
    --glow-accent:0 0 14px rgba(99,102,241,.5);
  }
  html,body{height:100%;overflow:hidden;font-family:'Segoe UI',system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--text)}

  /* canvas */
  #canvas-wrap{position:absolute;inset:0;overflow:hidden;cursor:grab}
  #canvas-wrap.grabbing{cursor:grabbing}
  #world{position:absolute;transform-origin:0 0;will-change:transform}

  /* topbar */
  #topbar{position:fixed;top:0;left:0;right:0;height:52px;display:flex;align-items:center;justify-content:space-between;padding:0 24px;background:linear-gradient(180deg,rgba(10,14,23,.95),rgba(10,14,23,.7));backdrop-filter:blur(10px);z-index:100;border-bottom:1px solid var(--border)}
  #topbar h1{font-size:16px;font-weight:600;letter-spacing:.3px;background:linear-gradient(135deg,#818cf8,#c084fc);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
  #topbar .controls{display:flex;gap:8px}
  #topbar button{background:var(--surface);border:1px solid var(--border);color:var(--text);padding:5px 12px;border-radius:6px;cursor:pointer;font-size:12px;transition:border-color .2s}
  #topbar button:hover{border-color:var(--accent)}

  /* legend */
  #legend{position:fixed;bottom:16px;left:16px;background:rgba(17,24,39,.92);border:1px solid var(--border);border-radius:10px;padding:14px 18px;z-index:100;backdrop-filter:blur(8px);font-size:12px;max-height:70vh;overflow-y:auto}
  #legend h3{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);margin-bottom:8px}
  #legend h3:not(:first-child){margin-top:10px}
  .legend-row{display:flex;align-items:center;gap:8px;margin-bottom:5px}
  .legend-dot{width:10px;height:10px;border-radius:50%}
  .legend-line{width:24px;height:2px;border-radius:1px}

  /* detail panel */
  #detail{position:fixed;top:52px;right:0;width:360px;height:calc(100% - 52px);background:rgba(17,24,39,.96);border-left:1px solid var(--border);backdrop-filter:blur(12px);z-index:100;transform:translateX(100%);transition:transform .3s ease;padding:24px;overflow-y:auto}
  #detail.open{transform:translateX(0)}
  #detail .close-btn{position:absolute;top:12px;right:14px;background:none;border:none;color:var(--muted);font-size:20px;cursor:pointer}
  #detail h2{font-size:16px;font-weight:600;margin-bottom:4px}
  #detail .tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;margin-bottom:12px;color:#fff}
  #detail p{font-size:13px;color:var(--muted);line-height:1.6;margin-bottom:12px}
  #detail h4{font-size:12px;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);margin:16px 0 6px}
  #detail ul{list-style:none;padding:0}
  #detail li{font-size:13px;padding:4px 0;border-bottom:1px solid var(--border);color:var(--text)}
  #detail li:last-child{border:none}
  #detail code{font-family:'Cascadia Code','Fira Code',monospace;font-size:12px;color:#a5b4fc;background:rgba(99,102,241,.12);padding:1px 5px;border-radius:3px}
  #detail .chip{display:inline-block;background:rgba(99,102,241,.12);color:#a5b4fc;border:1px solid rgba(99,102,241,.3);padding:2px 8px;border-radius:10px;font-size:11px;margin:2px 4px 2px 0}

  /* node */
  .node{position:absolute;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 14px;cursor:pointer;transition:box-shadow .25s,border-color .25s,transform .18s;min-width:150px;text-align:center;user-select:none}
  .node:hover{transform:scale(1.05);z-index:10}
  .node .label{font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:200px}
  .node .sub{font-size:10px;color:var(--muted);margin-top:2px}
  .node.selected{border-color:var(--accent);box-shadow:var(--glow-accent)}

  /* tier colours — match Sales Coach palette exactly */
  .node.tier-ext{border-color:#22d3ee;box-shadow:0 0 10px rgba(34,211,238,.25)}
  .node.tier-ext:hover{box-shadow:0 0 18px rgba(34,211,238,.4)}
  .node.tier-api{border-color:var(--accent);box-shadow:0 0 10px rgba(99,102,241,.25)}
  .node.tier-api:hover{box-shadow:0 0 18px rgba(99,102,241,.4)}
  .node.tier-critical{border-color:var(--critical);box-shadow:var(--glow-critical)}
  .node.tier-high{border-color:var(--high);box-shadow:var(--glow-high)}
  .node.tier-normal{border-color:var(--normal);box-shadow:var(--glow-normal)}
  .node.tier-low{border-color:var(--low);box-shadow:0 0 8px rgba(107,114,128,.3)}
  .node.tier-infra{border-color:var(--accent2);box-shadow:0 0 10px rgba(139,92,246,.25)}
  .node.tier-infra:hover{box-shadow:0 0 18px rgba(139,92,246,.4)}
  .node.tier-postcall{border-color:#facc15;box-shadow:0 0 10px rgba(250,204,21,.3)}

  /* layer labels */
  .layer-label{position:absolute;font-size:11px;text-transform:uppercase;letter-spacing:2px;color:var(--muted);font-weight:700;opacity:.55;pointer-events:none}

  /* SVG edges */
  svg.edges{position:absolute;top:0;left:0;pointer-events:none;overflow:visible}
  .edge{fill:none;stroke-width:1.4;opacity:.4}
  .edge.http{stroke:#3b82f6}
  .edge.ws{stroke:#f472b6}
  .edge.grpc{stroke:#a855f7}
  .edge.queue{stroke:#fb7185}
  .edge.db{stroke:#a3e635}
  .edge.default{stroke:#94a3b8}
  .edge.import{stroke:#475569;opacity:.25;stroke-dasharray:2 4}
  .edge.cohesion{stroke:#334155;opacity:.18;stroke-dasharray:1 5;stroke-width:1}

  /* animated dash */
  @keyframes dash{to{stroke-dashoffset:-24}}
  .edge-anim{stroke-dasharray:8 16;animation:dash 1.2s linear infinite;opacity:.6}
  .edge.import.edge-anim, .edge.cohesion.edge-anim{animation:none;stroke-dasharray:2 4}

  /* zoom hint */
  #zoom-hint{position:fixed;bottom:16px;right:16px;font-size:11px;color:var(--muted);z-index:100;background:rgba(17,24,39,.8);padding:6px 12px;border-radius:6px;border:1px solid var(--border)}

  /* empty state */
  #empty{position:fixed;inset:52px 0 0 0;display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:14px;pointer-events:none}
</style>
</head>
<body>

<div id="topbar">
  <h1>__TITLE__</h1>
  <div class="controls">
    <button onclick="resetView()">Reset View</button>
    <button onclick="toggleFlows()">Toggle Flow Animation</button>
  </div>
</div>

<div id="legend">
  <h3>Components</h3>
  __LEGEND_TIERS__
  <h3>Flow Protocols</h3>
  <div class="legend-row"><div class="legend-line" style="background:#3b82f6"></div>HTTP / REST</div>
  <div class="legend-row"><div class="legend-line" style="background:#f472b6"></div>WebSocket</div>
  <div class="legend-row"><div class="legend-line" style="background:#a855f7"></div>gRPC</div>
  <div class="legend-row"><div class="legend-line" style="background:#fb7185"></div>Queue / MQ</div>
  <div class="legend-row"><div class="legend-line" style="background:#a3e635"></div>DB</div>
  <div class="legend-row"><div class="legend-line" style="background:#94a3b8"></div>Other</div>
</div>

<div id="detail">
  <button class="close-btn" onclick="closeDetail()">&times;</button>
  <h2 id="d-title"></h2>
  <span class="tag" id="d-tag"></span>
  <p id="d-desc"></p>
  <h4>Path</h4>
  <p><code id="d-path"></code></p>
  <h4 id="d-tech-h" style="display:none">Tech Stack</h4>
  <div id="d-tech"></div>
  <h4 id="d-routes-h" style="display:none">API Routes</h4>
  <ul id="d-routes"></ul>
  <h4>Connections</h4>
  <ul id="d-conns"></ul>
</div>

<div id="canvas-wrap">
  <div id="world">
    <svg class="edges" id="edge-svg" width="2400" height="1200"></svg>
  </div>
</div>

<div id="zoom-hint">Scroll to zoom &middot; Drag to pan &middot; Click nodes for details</div>

<script>
const NODES = __NODES_JSON__;
const FLOWS = __FLOWS_JSON__;
const LAYER_LABELS = __LAYERS_JSON__;
const TIER_COLORS = {
  ext:'#22d3ee', api:'#6366f1', infra:'#8b5cf6',
  critical:'#ef4444', high:'#f97316', normal:'#3b82f6', low:'#6b7280',
  postcall:'#facc15'
};
const TIER_NAMES = {
  ext:'Extension', api:'Backend API', infra:'Infrastructure',
  critical:'Critical', high:'High', normal:'Normal', low:'Low',
  postcall:'Post-Call'
};

const world  = document.getElementById('world');
const svgEl  = document.getElementById('edge-svg');
const wrap   = document.getElementById('canvas-wrap');
const nodeEls = {};
let selected = null;
let flowsOn  = true;

// Layer labels
LAYER_LABELS.forEach(l => {
  const d = document.createElement('div');
  d.className = 'layer-label';
  d.style.left = l.x + 'px';
  d.style.top  = l.y + 'px';
  d.textContent = l.text;
  world.appendChild(d);
});

// Nodes
NODES.forEach(n => {
  const el = document.createElement('div');
  el.className = 'node tier-' + n.tier;
  el.style.left = n.x + 'px';
  el.style.top  = n.y + 'px';
  el.innerHTML = '<div class="label"></div><div class="sub"></div>';
  el.querySelector('.label').textContent = n.label;
  el.querySelector('.sub').textContent = n.sub;
  el.addEventListener('click', e => { e.stopPropagation(); selectNode(n.id); });
  world.appendChild(el);
  nodeEls[n.id] = el;
});

if (NODES.length === 0) {
  const empty = document.createElement('div');
  empty.id = 'empty';
  empty.textContent = 'No components to display.';
  document.body.appendChild(empty);
}

function nodeCenter(id) {
  const el = nodeEls[id];
  if (!el) return {x:0,y:0};
  return {
    x: parseInt(el.style.left) + el.offsetWidth / 2,
    y: parseInt(el.style.top)  + el.offsetHeight / 2
  };
}

function buildEdges() {
  svgEl.innerHTML = '';
  FLOWS.forEach(f => {
    const a = nodeCenter(f.from);
    const b = nodeCenter(f.to);
    const dx = b.x - a.x;
    const cx1 = a.x + dx * 0.4;
    const cy1 = a.y;
    const cx2 = b.x - dx * 0.4;
    const cy2 = b.y;
    const path = document.createElementNS('http://www.w3.org/2000/svg','path');
    path.setAttribute('d', 'M'+a.x+','+a.y+' C'+cx1+','+cy1+' '+cx2+','+cy2+' '+b.x+','+b.y);
    path.setAttribute('class', 'edge ' + f.cls + (flowsOn ? ' edge-anim' : ''));
    svgEl.appendChild(path);
  });
}

requestAnimationFrame(() => {
  buildEdges();
  resetView();
});

function selectNode(id) {
  const n = NODES.find(x => x.id === id);
  if (!n) return;
  Object.values(nodeEls).forEach(el => el.classList.remove('selected'));
  nodeEls[id].classList.add('selected');
  selected = id;

  document.getElementById('d-title').textContent = n.label;
  const tag = document.getElementById('d-tag');
  tag.textContent = TIER_NAMES[n.tier] || n.tier;
  tag.style.background = TIER_COLORS[n.tier] || '#555';
  document.getElementById('d-desc').textContent = n.description || '(No description)';
  document.getElementById('d-path').textContent = n.path || '(no path)';

  // Tech stack
  const techH = document.getElementById('d-tech-h');
  const techDiv = document.getElementById('d-tech');
  techDiv.innerHTML = '';
  if (n.tech_stack && n.tech_stack.length) {
    techH.style.display = '';
    n.tech_stack.forEach(t => {
      const span = document.createElement('span');
      span.className = 'chip';
      span.textContent = t;
      techDiv.appendChild(span);
    });
  } else {
    techH.style.display = 'none';
  }

  // Routes
  const routesH = document.getElementById('d-routes-h');
  const routesUl = document.getElementById('d-routes');
  routesUl.innerHTML = '';
  if (n.api_routes && n.api_routes.length) {
    routesH.style.display = '';
    n.api_routes.forEach(r => {
      const li = document.createElement('li');
      li.innerHTML = '<code></code> <span class="rpath"></span>';
      li.querySelector('code').textContent = r.method;
      li.querySelector('.rpath').textContent = ' ' + r.path;
      routesUl.appendChild(li);
    });
  } else {
    routesH.style.display = 'none';
  }

  // Connections
  const ul = document.getElementById('d-conns');
  ul.innerHTML = '';
  const outgoing = FLOWS.filter(f => f.from === id).map(f => f.to);
  const incoming = FLOWS.filter(f => f.to === id).map(f => f.from);
  const all = [...new Set([...outgoing, ...incoming])];
  if (!all.length) {
    const li = document.createElement('li');
    li.style.color = 'var(--muted)';
    li.textContent = '(no flows)';
    ul.appendChild(li);
  }
  all.forEach(cid => {
    const cn = NODES.find(x => x.id === cid);
    if (!cn) return;
    const li = document.createElement('li');
    const arrow = outgoing.includes(cid) ? (incoming.includes(cid) ? '↔' : '→') : '←';
    li.textContent = arrow + ' ' + cn.label;
    li.style.cursor = 'pointer';
    li.addEventListener('click', () => selectNode(cid));
    ul.appendChild(li);
  });

  document.getElementById('detail').classList.add('open');
}

function closeDetail() {
  document.getElementById('detail').classList.remove('open');
  Object.values(nodeEls).forEach(el => el.classList.remove('selected'));
  selected = null;
}

wrap.addEventListener('click', e => {
  if (e.target === wrap || e.target === world) closeDetail();
});

// Pan & zoom
let scale = 1, tx = 0, ty = 0;
let dragging = false, dragX = 0, dragY = 0;

function applyTransform() {
  world.style.transform = 'translate('+tx+'px,'+ty+'px) scale('+scale+')';
}

wrap.addEventListener('mousedown', e => {
  if (e.button !== 0) return;
  dragging = true;
  dragX = e.clientX; dragY = e.clientY;
  wrap.classList.add('grabbing');
});
window.addEventListener('mousemove', e => {
  if (!dragging) return;
  tx += e.clientX - dragX;
  ty += e.clientY - dragY;
  dragX = e.clientX; dragY = e.clientY;
  applyTransform();
});
window.addEventListener('mouseup', () => {
  dragging = false;
  wrap.classList.remove('grabbing');
});

wrap.addEventListener('wheel', e => {
  e.preventDefault();
  const rect = wrap.getBoundingClientRect();
  const mx = e.clientX - rect.left;
  const my = e.clientY - rect.top;
  const prevScale = scale;
  scale *= e.deltaY < 0 ? 1.08 : 0.926;
  scale = Math.max(0.25, Math.min(3, scale));
  tx = mx - (mx - tx) * (scale / prevScale);
  ty = my - (my - ty) * (scale / prevScale);
  applyTransform();
}, {passive:false});

function resetView() {
  if (!NODES.length) { tx = 0; ty = 52; scale = 1; applyTransform(); return; }
  let minX=Infinity,minY=Infinity,maxX=-Infinity,maxY=-Infinity;
  NODES.forEach(n => {
    const el = nodeEls[n.id];
    const w = el.offsetWidth || 150;
    const h = el.offsetHeight || 50;
    if (n.x < minX) minX = n.x;
    if (n.y < minY) minY = n.y;
    if (n.x + w > maxX) maxX = n.x + w;
    if (n.y + h > maxY) maxY = n.y + h;
  });
  const pad = 80;
  const bw = maxX - minX + pad*2;
  const bh = maxY - minY + pad*2;
  const vw = wrap.clientWidth;
  const vh = wrap.clientHeight - 52;
  scale = Math.min(vw / bw, vh / bh, 1.2);
  tx = (vw - bw * scale) / 2 - (minX - pad) * scale;
  ty = 52 + (vh - bh * scale) / 2 - (minY - pad) * scale;
  applyTransform();
}

function toggleFlows() {
  flowsOn = !flowsOn;
  buildEdges();
}

window.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeDetail();
});
window.addEventListener('resize', () => { buildEdges(); });
</script>
</body>
</html>
"""


def _legend_tiers_html(tiers_present: list[str]) -> str:
    """Build legend rows for the tiers actually shown in the diagram."""
    color_map = {
        "ext": "#22d3ee",
        "api": "#6366f1",
        "infra": "#8b5cf6",
        "critical": "#ef4444",
        "high": "#f97316",
        "normal": "#3b82f6",
        "low": "#6b7280",
        "postcall": "#facc15",
    }
    label_map = {
        "ext": "Extension",
        "api": "Backend API",
        "infra": "Infrastructure",
        "critical": "Critical",
        "high": "High",
        "normal": "Normal",
        "low": "Low",
        "postcall": "Post-Call",
    }
    rows = []
    for t in tiers_present:
        c = color_map.get(t, "#6b7280")
        n = label_map.get(t, t.title())
        rows.append(
            f'<div class="legend-row"><div class="legend-dot" style="background:{c}"></div>{escape(n)}</div>'
        )
    return "\n  ".join(rows) if rows else '<div class="legend-row" style="color:var(--muted)">(none)</div>'


def generate_interactive_html(arch: Architecture, project_name: str) -> str:
    """
    Render a complete self-contained interactive architecture diagram HTML.

    No external CSS/JS — everything is inlined.

    Args:
        arch: The Architecture model to render.
        project_name: Used in the page title and topbar.

    Returns:
        A complete HTML document as a string.
    """
    nodes_by_component_name, nodes_list, layer_labels, _nodes_by_id = _layout(arch)
    flow_edges = _build_flow_edges(arch.data_flows, nodes_by_component_name)
    cohesion_edges = _build_cohesion_edges(nodes_list)
    import_edges = _build_import_edges(arch, nodes_list)
    edges = flow_edges + import_edges + cohesion_edges

    # Determine which tiers are actually present (for the legend)
    tiers_present: list[str] = []
    seen: set[str] = set()
    for n in nodes_list:
        if n["tier"] not in seen:
            seen.add(n["tier"])
            tiers_present.append(n["tier"])

    title = f"{project_name} — System Architecture"

    html = (
        _HTML_TEMPLATE
        .replace("__TITLE__", escape(title))
        .replace("__NODES_JSON__", json.dumps(nodes_list))
        .replace("__FLOWS_JSON__", json.dumps(edges))
        .replace("__LAYERS_JSON__", json.dumps(layer_labels))
        .replace("__LEGEND_TIERS__", _legend_tiers_html(tiers_present))
    )
    return html
