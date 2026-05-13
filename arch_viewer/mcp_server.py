"""
MCP Server — exposes architecture tools to any MCP client.
Plug this into Claude Code, Cursor, or any MCP-compatible tool.

MCP mode is the default — no flag needed:
  python -m arch_viewer

Usage in .claude/settings.json:
  "mcpServers": {
    "arch-viewer": {
      "command": "python",
      "args": ["-m", "arch_viewer"],
      "env": {
        "ARCH_VIEWER_ROOT": ".",
        "ARCH_VIEWER_PROVIDER": "openai"
      }
    }
  }

API keys are stored in .arch_viewer/keys.json in the project root.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .agent import ArchitectureAgent
from .memory import (
    add_correction,
    add_pattern,
    get_analysis_history,
    load_memory,
)
from .models import Architecture
from .scanner import scan_project
from .watcher import ChangeEvent, ProjectWatcher

log = logging.getLogger("arch-viewer.mcp")


class ArchViewerMCP:
    """
    MCP server that maintains a live architecture model.
    Watches the filesystem, runs AI analysis on changes,
    and exposes tools for querying the architecture.
    """

    def __init__(
        self,
        root: str | Path,
        provider: str = "openai",
        model_name: str | None = None,
        auto_analyze: bool = True,
        web_port: int | None = 3777,
    ):
        self.root = Path(root).resolve()
        self._provider = provider
        self._model_name = model_name
        self._auto_analyze = auto_analyze
        self._web_port = web_port

        # State
        self._arch: Architecture | None = None
        self._watcher: ProjectWatcher | None = None
        self._agent: ArchitectureAgent | None = None
        self._analyzing = False
        self._ws_clients: set = set()
        self._pending_changes: list[ChangeEvent] = []
        self._rescan_lock = asyncio.Lock()

        # MCP server
        self._server = Server("arch-viewer")
        self._register_tools()

    def _register_tools(self):
        """Register all MCP tools."""
        server = self._server

        @server.list_tools()
        async def list_tools():
            return [
                Tool(
                    name="get_architecture",
                    description=(
                        "Returns the complete architecture model for the current project. "
                        "Includes components, data flows, tech stack, file stats, "
                        "and AI-generated descriptions. Use this to understand the project structure."
                    ),
                    inputSchema={"type": "object", "properties": {}, "required": []},
                ),
                Tool(
                    name="get_component",
                    description=(
                        "Get detailed information about a specific component "
                        "(backend, frontend, extension, etc.) including its files, "
                        "API routes, tech stack, and AI-generated description."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Component name (e.g., 'Backend', 'Portal', 'Extension')",
                            },
                        },
                        "required": ["name"],
                    },
                ),
                Tool(
                    name="get_data_flows",
                    description=(
                        "Returns all data flows between components — how they connect, "
                        "what protocols they use, and the direction of communication."
                    ),
                    inputSchema={"type": "object", "properties": {}, "required": []},
                ),
                Tool(
                    name="get_dependencies",
                    description="Lists all project dependencies (Python packages, Node modules, etc.).",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "category": {
                                "type": "string",
                                "description": "Filter by category: python, node-runtime, node-dev, or all",
                            },
                        },
                        "required": [],
                    },
                ),
                Tool(
                    name="get_file_tree",
                    description=(
                        "Returns the project file tree. Optionally filter to a specific path prefix."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Path prefix to filter (e.g., 'backend/app/agents')",
                            },
                        },
                        "required": [],
                    },
                ),
                Tool(
                    name="get_ai_summary",
                    description=(
                        "Returns the AI-generated architecture narrative — a human-readable "
                        "description of the project's architecture, key decisions, and tech highlights."
                    ),
                    inputSchema={"type": "object", "properties": {}, "required": []},
                ),
                Tool(
                    name="refresh_architecture",
                    description=(
                        "Force a full rescan and AI re-analysis of the project. "
                        "Use after major changes like adding new components or restructuring."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "ai": {
                                "type": "boolean",
                                "description": "Run AI analysis (default: true). Set false for fast scan only.",
                            },
                        },
                        "required": [],
                    },
                ),
                Tool(
                    name="get_recent_changes",
                    description="Returns recent file changes detected by the watcher.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "limit": {
                                "type": "integer",
                                "description": "Max number of changes to return (default: 20)",
                            },
                        },
                        "required": [],
                    },
                ),
                Tool(
                    name="get_api_routes",
                    description="Returns all detected API routes across all backend components.",
                    inputSchema={"type": "object", "properties": {}, "required": []},
                ),
                Tool(
                    name="get_architecture_score",
                    description=(
                        "Returns a 0-100 architecture health score with category breakdowns "
                        "(Modularity, Code Quality, Maintainability, Structure), anti-pattern "
                        "detection, and actionable recommendations. Runs AST analysis on all "
                        "supported files (Python, JS/TS)."
                    ),
                    inputSchema={"type": "object", "properties": {}, "required": []},
                ),
                Tool(
                    name="get_dependency_graph",
                    description=(
                        "Returns the import dependency graph showing how files connect to each other. "
                        "Includes nodes (files) and edges (imports) for visualization. "
                        "Also includes hotspot analysis (most connected files)."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "graph_type": {
                                "type": "string",
                                "description": "Type of graph: 'imports' (file-level), 'calls' (function-level), 'packages' (external deps), 'components' (high-level). Default: imports.",
                            },
                        },
                        "required": [],
                    },
                ),
                Tool(
                    name="get_anti_patterns",
                    description=(
                        "Detect architectural anti-patterns: circular imports, god files, "
                        "orphaned modules, excessive complexity, deep nesting, and mixed concerns. "
                        "Returns severity-ranked findings with fix suggestions."
                    ),
                    inputSchema={"type": "object", "properties": {}, "required": []},
                ),
                Tool(
                    name="analyze_file_ast",
                    description=(
                        "Run AST analysis on a specific file — returns functions, classes, "
                        "imports, exports, complexity metrics, and line counts."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Relative file path to analyze (e.g., 'backend/app/main.py')",
                            },
                        },
                        "required": ["path"],
                    },
                ),
                Tool(
                    name="get_memory",
                    description=(
                        "Returns all learned patterns, user corrections, component notes, "
                        "and analysis history from the AI memory store. The AI remembers "
                        "architectural insights across sessions to provide better analysis."
                    ),
                    inputSchema={"type": "object", "properties": {}, "required": []},
                ),
                Tool(
                    name="add_memory_pattern",
                    description=(
                        "Teach the AI a new architecture pattern about this project. "
                        "The pattern is stored locally and used in future analyses. "
                        "Categories: architecture, design-pattern, anti-pattern, relationship, technology."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "category": {
                                "type": "string",
                                "description": (
                                    "Pattern category: architecture, design-pattern, "
                                    "anti-pattern, relationship, or technology"
                                ),
                            },
                            "description": {
                                "type": "string",
                                "description": "What the pattern is (e.g., 'Uses event-driven architecture with FastAPI WebSockets')",
                            },
                            "confidence": {
                                "type": "number",
                                "description": "Confidence score 0.0-1.0 (default: 0.8)",
                            },
                        },
                        "required": ["category", "description"],
                    },
                ),
                Tool(
                    name="add_memory_correction",
                    description=(
                        "Correct something the AI got wrong. The correction is stored and "
                        "applied in all future analyses so the same mistake isn't repeated."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "original": {
                                "type": "string",
                                "description": "What the AI originally said or concluded (the wrong thing)",
                            },
                            "correction": {
                                "type": "string",
                                "description": "What is actually correct",
                            },
                        },
                        "required": ["original", "correction"],
                    },
                ),
            ]

        @server.call_tool()
        async def call_tool(name: str, arguments: dict):
            try:
                result = await self._handle_tool(name, arguments)
                return [TextContent(type="text", text=result)]
            except Exception as e:
                log.error("Tool %s failed: %s", name, e)
                return [TextContent(type="text", text=f"Error: {e}")]

    async def _handle_tool(self, name: str, args: dict) -> str:
        # Ensure we have an architecture model
        if self._arch is None:
            await self._scan(run_ai=False)

        if name == "get_architecture":
            return self._arch.model_dump_json(indent=2)

        elif name == "get_component":
            comp_name = args.get("name", "")
            comp = self._arch.get_component(comp_name)
            if not comp:
                # Fuzzy match
                for c in self._arch.components:
                    if comp_name.lower() in c.name.lower():
                        comp = c
                        break
            if not comp:
                available = [c.name for c in self._arch.components]
                return f"Component '{comp_name}' not found. Available: {available}"
            return comp.model_dump_json(indent=2)

        elif name == "get_data_flows":
            flows = [f.model_dump() for f in self._arch.data_flows]
            return json.dumps(flows, indent=2)

        elif name == "get_dependencies":
            cat = args.get("category", "all")
            deps = self._arch.dependencies
            if cat != "all":
                deps = [d for d in deps if d.category == cat]
            return json.dumps([d.model_dump() for d in deps], indent=2)

        elif name == "get_file_tree":
            path_prefix = args.get("path", "")
            tree = self._arch.file_tree
            if path_prefix:
                for part in path_prefix.strip("/").split("/"):
                    tree = tree.get(part, {})
            return json.dumps(tree, indent=2, default=str)

        elif name == "get_ai_summary":
            if not self._arch.ai_summary:
                return "(No AI summary yet. Run refresh_architecture to generate one.)"
            return self._arch.ai_summary

        elif name == "refresh_architecture":
            run_ai = args.get("ai", True)
            await self._scan(run_ai=run_ai)
            return (
                f"Architecture refreshed (v{self._arch.analysis_version}). "
                f"{len(self._arch.components)} components, "
                f"{self._arch.stats.get('total_files', 0)} files."
            )

        elif name == "get_recent_changes":
            limit = args.get("limit", 20)
            if self._watcher:
                changes = self._watcher.get_recent()[:limit]
            else:
                changes = []
            return json.dumps(changes, indent=2)

        elif name == "get_api_routes":
            routes = []
            for comp in self._arch.components:
                for r in comp.api_routes:
                    routes.append({
                        "component": comp.name,
                        "method": r.method,
                        "path": r.path,
                        "file": r.file,
                        "description": r.description,
                    })
            return json.dumps(routes, indent=2)

        elif name == "get_architecture_score":
            from .ast_analyzer import analyze_project
            from .scoring import score_architecture
            analyses = analyze_project(self.root)
            score = score_architecture(self._arch, analyses, self.root)
            return json.dumps(score.to_dict(), indent=2)

        elif name == "get_dependency_graph":
            from .ast_analyzer import analyze_project
            from .dep_graph import (
                build_import_graph, build_call_graph,
                build_package_graph, build_component_graph,
                find_hotspots,
            )
            graph_type = args.get("graph_type", "imports")
            analyses = analyze_project(self.root)

            if graph_type == "calls":
                graph = build_call_graph(analyses)
            elif graph_type == "packages":
                graph = build_package_graph(self._arch)
            elif graph_type == "components":
                graph = build_component_graph(self._arch)
            else:
                graph = build_import_graph(analyses, self._arch)

            result = graph.to_dict()
            result["hotspots"] = find_hotspots(graph)
            return json.dumps(result, indent=2)

        elif name == "get_anti_patterns":
            from .ast_analyzer import analyze_project
            from .scoring import _detect_anti_patterns
            analyses = analyze_project(self.root)
            patterns = _detect_anti_patterns(self._arch, analyses, self.root)
            return json.dumps([
                {
                    "name": ap.name,
                    "severity": ap.severity,
                    "description": ap.description,
                    "file": ap.file,
                    "suggestion": ap.suggestion,
                }
                for ap in patterns
            ], indent=2)

        elif name == "analyze_file_ast":
            from .ast_analyzer import analyze_file
            rel_path = args.get("path", "")
            abs_path = (self.root / rel_path).resolve()
            if not str(abs_path).startswith(str(self.root)):
                return "Access denied"
            if not abs_path.exists():
                return f"File not found: {rel_path}"
            analysis = analyze_file(str(abs_path))
            if not analysis:
                return f"Unsupported file type: {rel_path}"
            analysis.path = rel_path
            return json.dumps({
                "path": analysis.path,
                "language": analysis.language,
                "total_lines": analysis.total_lines,
                "code_lines": analysis.code_lines,
                "comment_lines": analysis.comment_lines,
                "blank_lines": analysis.blank_lines,
                "complexity": analysis.complexity,
                "max_function_complexity": analysis.max_function_complexity,
                "functions": [
                    {"name": f.name, "line": f.line, "complexity": f.complexity,
                     "is_async": f.is_async, "class_name": f.class_name,
                     "args": f.args, "calls": f.calls}
                    for f in analysis.functions
                ],
                "classes": [
                    {"name": c.name, "line": c.line, "bases": c.bases,
                     "methods": c.methods, "attributes": c.attributes}
                    for c in analysis.classes
                ],
                "imports": [
                    {"module": i.module, "names": i.names, "is_relative": i.is_relative}
                    for i in analysis.imports
                ],
                "exports": [
                    {"name": e.name, "kind": e.kind}
                    for e in analysis.exports
                ],
            }, indent=2)

        elif name == "get_memory":
            memory = load_memory(self.root)
            return json.dumps(memory, indent=2, default=str)

        elif name == "add_memory_pattern":
            category = args.get("category", "architecture")
            description = args.get("description", "")
            confidence = args.get("confidence", 0.8)
            if not description:
                return "Error: description is required"
            entry = add_pattern(
                self.root, category, description, confidence, source="ai",
            )
            return json.dumps({
                "status": "ok",
                "message": f"Pattern added [{category}]",
                "pattern": entry,
            }, indent=2, default=str)

        elif name == "add_memory_correction":
            original = args.get("original", "")
            correction = args.get("correction", "")
            if not original or not correction:
                return "Error: both original and correction are required"
            entry = add_correction(self.root, original, correction)
            return json.dumps({
                "status": "ok",
                "message": "Correction recorded",
                "correction": entry,
            }, indent=2, default=str)

        else:
            return f"Unknown tool: {name}"

    async def _scan(self, run_ai: bool = True):
        """Scan the project and optionally run AI analysis."""
        async with self._rescan_lock:
            log.info("Scanning %s ...", self.root)
            self._arch = scan_project(self.root)

            if run_ai and self._auto_analyze:
                if self._agent is None:
                    self._agent = ArchitectureAgent(
                        provider=self._provider,
                        model_name=self._model_name,
                        project_root=self.root,
                    )
                try:
                    self._analyzing = True
                    self._arch = await self._agent.analyze_architecture(
                        self._arch, self.root
                    )
                    log.info(
                        "AI analysis complete (v%d)", self._arch.analysis_version
                    )
                except Exception as e:
                    log.warning("AI analysis failed (continuing without): %s", e)
                finally:
                    self._analyzing = False

    def _on_file_change(self, event: ChangeEvent):
        """Called by watcher on each file change."""
        self._pending_changes.append(event)
        # Broadcast to web dashboard clients
        msg = json.dumps(event.to_dict())
        for ws_send in list(self._ws_clients):
            try:
                asyncio.get_event_loop().call_soon_threadsafe(
                    lambda s=ws_send, m=msg: asyncio.ensure_future(s(m))
                )
            except Exception:
                self._ws_clients.discard(ws_send)

    def _on_rescan_needed(self):
        """Called by watcher when structural changes require rescan."""
        log.info("Structural change detected — scheduling rescan")
        try:
            loop = asyncio.get_event_loop()
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(self._scan(run_ai=False))
            )
        except Exception:
            pass

    async def run(self):
        """Run the MCP server on stdio."""
        # Initial scan
        await self._scan(run_ai=self._auto_analyze)

        # Start file watcher
        self._watcher = ProjectWatcher(
            root=self.root,
            on_change=self._on_file_change,
            on_rescan_needed=self._on_rescan_needed,
        )
        self._watcher.start()

        # Start web dashboard in background if port specified
        if self._web_port:
            asyncio.create_task(self._start_web_server())

        # Run MCP on stdio
        log.info("MCP server ready on stdio")
        async with stdio_server() as (read_stream, write_stream):
            await self._server.run(read_stream, write_stream, self._server.create_initialization_options())

    async def _start_web_server(self):
        """Start the web dashboard alongside the MCP server."""
        try:
            from .web_server import start_web_server
            await start_web_server(
                arch_mcp=self,
                port=self._web_port,
            )
        except Exception as e:
            log.warning("Web dashboard failed to start: %s", e)

    def get_architecture(self) -> Architecture | None:
        return self._arch
