"""
Web dashboard server — serves the interactive architecture UI
alongside the MCP server. Uses aiohttp for async HTTP + WebSocket.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import aiohttp
from aiohttp import web

log = logging.getLogger("arch-viewer.web")


async def start_web_server(arch_mcp, port: int = 3777):
    """Start the web dashboard on the given port."""
    from .scanner import scan_project

    root = arch_mcp.root
    ws_clients: set[web.WebSocketResponse] = set()

    # Register WS broadcast
    async def ws_broadcast(msg: str):
        for ws in list(ws_clients):
            try:
                await ws.send_str(msg)
            except Exception:
                ws_clients.discard(ws)

    arch_mcp._ws_clients.add(ws_broadcast)

    # ─── Routes ───

    async def handle_index(request):
        web_dir = Path(__file__).parent.parent / "web"
        index_path = web_dir / "index.html"
        if index_path.exists():
            return web.FileResponse(index_path)
        # Fallback to old public/index.html
        public_dir = Path(__file__).parent.parent / "public"
        alt_path = public_dir / "index.html"
        if alt_path.exists():
            return web.FileResponse(alt_path)
        return web.Response(text="Dashboard not found", status=404)

    async def handle_api_scan(request):
        arch = arch_mcp.get_architecture()
        if arch:
            return web.json_response(arch.model_dump(), dumps=lambda x: json.dumps(x, default=str))
        return web.json_response({"error": "Not scanned yet"}, status=503)

    async def handle_api_file_read(request):
        rel_path = request.query.get("path", "")
        if not rel_path:
            return web.json_response({"error": "path required"}, status=400)

        abs_path = (root / rel_path).resolve()
        if not str(abs_path).startswith(str(root)):
            return web.json_response({"error": "Access denied"}, status=403)

        if not abs_path.exists():
            return web.json_response({"error": "Not found"}, status=404)

        stat = abs_path.stat()
        if stat.st_size > 2 * 1024 * 1024:
            return web.json_response({"error": "File too large"}, status=413)

        binary_exts = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".zip", ".pdf"}
        if abs_path.suffix.lower() in binary_exts:
            return web.json_response({"path": rel_path, "binary": True, "size": stat.st_size})

        try:
            content = abs_path.read_text(errors="ignore")
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

        return web.json_response({
            "path": rel_path,
            "content": content,
            "size": stat.st_size,
            "modified": stat.st_mtime,
            "binary": False,
        })

    async def handle_api_file_write(request):
        body = await request.json()
        rel_path = body.get("path", "")
        content = body.get("content")
        if not rel_path or content is None:
            return web.json_response({"error": "path and content required"}, status=400)

        abs_path = (root / rel_path).resolve()
        if not str(abs_path).startswith(str(root)):
            return web.json_response({"error": "Access denied"}, status=403)

        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")
        stat = abs_path.stat()
        return web.json_response({"ok": True, "path": rel_path, "size": stat.st_size})

    async def handle_api_search(request):
        query = request.query.get("q", "")
        if not query:
            return web.json_response({"error": "q required"}, status=400)

        results = []
        arch = arch_mcp.get_architecture()
        if not arch:
            return web.json_response({"query": query, "results": [], "total": 0})

        query_lower = query.lower()
        for key, val in _flatten_tree(arch.file_tree):
            if len(results) >= 50:
                break
            finfo = val.get("_file")
            if not finfo:
                continue
            fpath = finfo["path"]
            if finfo.get("size", 0) > 500_000:
                continue
            abs_path = root / fpath
            try:
                text = abs_path.read_text(errors="ignore")
                for i, line in enumerate(text.split("\n")):
                    if query_lower in line.lower():
                        results.append({
                            "file": fpath,
                            "line": i + 1,
                            "text": line.strip()[:200],
                        })
                        if len(results) >= 50:
                            break
            except Exception:
                continue

        return web.json_response({"query": query, "results": results, "total": len(results)})

    async def handle_api_recent(request):
        if arch_mcp._watcher:
            changes = arch_mcp._watcher.get_recent()[:50]
        else:
            changes = []
        return web.json_response({"changes": changes})

    async def handle_api_refresh(request):
        await arch_mcp._scan(run_ai=True)
        arch = arch_mcp.get_architecture()
        return web.json_response({
            "ok": True,
            "version": arch.analysis_version if arch else 0,
        })

    async def handle_api_keys_get(request):
        """Return which providers have keys configured + model catalog."""
        from .agent import load_keys, PROVIDERS, get_selected_model
        data = load_keys(root)
        status = {}
        for provider, info in PROVIDERS.items():
            has_key = bool(data.get(provider)) or bool(os.environ.get(info["env_var"]))
            status[provider] = {
                "configured": has_key,
                "display": info["display"],
                "env_var": info["env_var"],
                "models": info["models"],
                "default_model": info["default_model"],
                "selected_model": get_selected_model(root, provider),
            }
        return web.json_response({
            "providers": status,
            "active_provider": arch_mcp._provider,
            "ai_enabled": arch_mcp._auto_analyze,
        })

    async def handle_api_keys_save(request):
        """Save API keys + model selections, then re-initialize the agent."""
        from .agent import save_keys, inject_keys_to_env, detect_available_provider, get_selected_model
        body = await request.json()
        keys = body.get("keys", {})
        selected_models = body.get("selected_models", {})

        if not keys and not selected_models:
            return web.json_response({"error": "keys or selected_models required"}, status=400)

        # Save keys + model selections to project file
        save_keys(root, keys or {}, selected_models or None)
        inject_keys_to_env(root)

        # Auto-detect provider and re-enable AI if it was disabled
        provider = detect_available_provider(root)
        if provider:
            arch_mcp._provider = provider
            arch_mcp._auto_analyze = True
            arch_mcp._agent = None  # Force re-init with new keys + model

            # Trigger re-analysis
            asyncio.create_task(arch_mcp._scan(run_ai=True))

        return web.json_response({
            "ok": True,
            "active_provider": provider or arch_mcp._provider,
            "selected_model": get_selected_model(root, provider or arch_mcp._provider),
            "ai_enabled": arch_mcp._auto_analyze,
        })

    async def handle_ws(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        ws_clients.add(ws)

        # Send initial architecture
        arch = arch_mcp.get_architecture()
        if arch:
            await ws.send_json({
                "type": "init",
                **arch.model_dump(),
            }, dumps=lambda x: json.dumps(x, default=str))

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        msg_type = data.get("type", "")

                        if msg_type == "read_file":
                            abs_path = (root / data["path"]).resolve()
                            if str(abs_path).startswith(str(root)) and abs_path.exists():
                                content = abs_path.read_text(errors="ignore")
                                await ws.send_json({
                                    "type": "file_content",
                                    "path": data["path"],
                                    "content": content,
                                    "requestId": data.get("requestId"),
                                })

                        elif msg_type == "save_file":
                            abs_path = (root / data["path"]).resolve()
                            if str(abs_path).startswith(str(root)):
                                abs_path.parent.mkdir(parents=True, exist_ok=True)
                                abs_path.write_text(data["content"], encoding="utf-8")
                                await ws.send_json({
                                    "type": "file_saved",
                                    "path": data["path"],
                                    "requestId": data.get("requestId"),
                                })

                        elif msg_type == "rescan":
                            await arch_mcp._scan(run_ai=False)
                            arch = arch_mcp.get_architecture()
                            if arch:
                                await ws.send_json({
                                    "type": "init",
                                    **arch.model_dump(),
                                }, dumps=lambda x: json.dumps(x, default=str))

                        elif msg_type == "refresh_ai":
                            await arch_mcp._scan(run_ai=True)
                            arch = arch_mcp.get_architecture()
                            if arch:
                                await ws.send_json({
                                    "type": "init",
                                    **arch.model_dump(),
                                }, dumps=lambda x: json.dumps(x, default=str))

                    except Exception as e:
                        await ws.send_json({"type": "error", "message": str(e)})

                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break
        finally:
            ws_clients.discard(ws)

        return ws

    # ─── v2 Endpoints: Scoring, Dependency Graph, Anti-Patterns ───

    async def handle_api_score(request):
        """Return architecture health score (0-100) with breakdowns."""
        from .ast_analyzer import analyze_project
        from .scoring import score_architecture

        arch = arch_mcp.get_architecture()
        if not arch:
            return web.json_response({"error": "Not scanned yet"}, status=503)

        analyses = analyze_project(root)
        score = score_architecture(arch, analyses, root)
        return web.json_response(score.to_dict())

    async def handle_api_dep_graph(request):
        """Return dependency graph for visualization."""
        from .ast_analyzer import analyze_project
        from .dep_graph import (
            build_import_graph, build_call_graph,
            build_package_graph, build_component_graph,
            find_hotspots,
        )

        arch = arch_mcp.get_architecture()
        if not arch:
            return web.json_response({"error": "Not scanned yet"}, status=503)

        graph_type = request.query.get("type", "imports")
        analyses = analyze_project(root)

        if graph_type == "calls":
            graph = build_call_graph(analyses)
        elif graph_type == "packages":
            graph = build_package_graph(arch)
        elif graph_type == "components":
            graph = build_component_graph(arch)
        else:
            graph = build_import_graph(analyses, arch)

        result = graph.to_dict()
        result["hotspots"] = find_hotspots(graph)
        return web.json_response(result)

    async def handle_api_anti_patterns(request):
        """Return detected anti-patterns."""
        from .ast_analyzer import analyze_project
        from .scoring import _detect_anti_patterns

        arch = arch_mcp.get_architecture()
        if not arch:
            return web.json_response({"error": "Not scanned yet"}, status=503)

        analyses = analyze_project(root)
        patterns = _detect_anti_patterns(arch, analyses, root)
        return web.json_response({
            "anti_patterns": [
                {
                    "name": ap.name,
                    "severity": ap.severity,
                    "description": ap.description,
                    "file": ap.file,
                    "suggestion": ap.suggestion,
                }
                for ap in patterns
            ],
            "total": len(patterns),
            "by_severity": {
                "critical": sum(1 for ap in patterns if ap.severity == "critical"),
                "warning": sum(1 for ap in patterns if ap.severity == "warning"),
                "info": sum(1 for ap in patterns if ap.severity == "info"),
            },
        })

    async def handle_api_diagram_generate(request):
        """Generate a standalone interactive architecture HTML file."""
        from .diagram_generator import generate_interactive_html

        arch = arch_mcp.get_architecture()
        if not arch:
            return web.json_response({"error": "Not scanned yet"}, status=503)

        try:
            body = await request.json()
        except Exception:
            body = {}
        rel_or_abs = body.get("output_path") or "docs/architecture.html"

        out_path = Path(rel_or_abs)
        if not out_path.is_absolute():
            out_path = root / out_path
        out_path = out_path.resolve()
        if not str(out_path).startswith(str(root)):
            return web.json_response({"error": "output_path must be inside project root"}, status=400)

        project_name = arch.project_name or root.name
        html = generate_interactive_html(arch, project_name)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html, encoding="utf-8")

        # Build a URL the dashboard can link to (served via /static if under /web,
        # otherwise via the file API as a download)
        try:
            rel_to_root = out_path.relative_to(root).as_posix()
        except ValueError:
            rel_to_root = out_path.name
        url = f"/api/file?path={rel_to_root}"

        return web.json_response({
            "ok": True,
            "path": str(out_path),
            "url": url,
            "size": out_path.stat().st_size,
            "components": len(arch.components),
            "data_flows": len(arch.data_flows),
            "project_name": project_name,
        })

    async def handle_api_diagram_html(request):
        """Return the generated HTML directly (for iframe embedding, no file write)."""
        from .diagram_generator import generate_interactive_html
        arch = arch_mcp.get_architecture()
        if not arch:
            return web.Response(text="<h3 style='color:#888;padding:40px;font-family:sans-serif'>Project not scanned yet.</h3>", content_type="text/html")
        project_name = arch.project_name or root.name
        html = generate_interactive_html(arch, project_name)
        return web.Response(text=html, content_type="text/html")

    async def handle_api_memory(request):
        """Return memory store status + recent patterns/corrections."""
        try:
            from .memory import load_memory, get_analysis_history
            from .mem_store import MemStore
            mem = load_memory(root)
            history = get_analysis_history(root, limit=10)

            # Try connecting to Mem0
            mem_info = {"available": False, "vector_count": 0, "backend": "flat-file"}
            try:
                store = MemStore(project_root=root)
                if store.connect():
                    mem_info["available"] = True
                    mem_info["backend"] = "mem0 + qdrant"
                    try:
                        results = store.search("", limit=100) or []
                        mem_info["vector_count"] = len(results)
                    except Exception:
                        pass
            except Exception as e:
                mem_info["error"] = str(e)

            return web.json_response({
                "ok": True,
                "patterns": (mem.get("patterns") or [])[-30:],
                "corrections": (mem.get("corrections") or [])[-30:],
                "history": history,
                "mem0": mem_info,
            })
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})

    async def handle_api_graph_status(request):
        """Check Neo4j connection status + node counts."""
        try:
            from .graph_store import GraphStore
            status = {"available": False, "url": os.environ.get("NEO4J_URI", "bolt://localhost:7687")}
            try:
                gs = GraphStore(project_root=root)
                if gs.connect():
                    status["available"] = True
                    try:
                        # Count nodes for this project
                        with gs._driver.session() as session:
                            comp = session.run("MATCH (c:Component {project: $p}) RETURN count(c) AS n", p=gs.project_id).single()
                            dep = session.run("MATCH (a)-[r:DEPENDS_ON {project: $p}]->(b) RETURN count(r) AS n", p=gs.project_id).single()
                            flow = session.run("MATCH (a)-[r:DATA_FLOW {project: $p}]->(b) RETURN count(r) AS n", p=gs.project_id).single()
                            status["component_count"] = comp["n"] if comp else 0
                            status["dependency_count"] = dep["n"] if dep else 0
                            status["flow_count"] = flow["n"] if flow else 0
                        status["browser_url"] = "http://localhost:7474"
                    except Exception as e:
                        status["query_error"] = str(e)
                    gs.close()
            except Exception as e:
                status["error"] = str(e)
            return web.json_response({"ok": True, "neo4j": status})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})

    # ─── App Setup ───

    app = web.Application()

    # Static files
    web_dir = Path(__file__).parent.parent / "web"
    if web_dir.exists():
        app.router.add_static("/static", web_dir)

    app.router.add_get("/", handle_index)
    app.router.add_get("/ws", handle_ws)
    app.router.add_get("/api/scan", handle_api_scan)
    app.router.add_get("/api/file", handle_api_file_read)
    app.router.add_put("/api/file", handle_api_file_write)
    app.router.add_get("/api/search", handle_api_search)
    app.router.add_get("/api/recent", handle_api_recent)
    app.router.add_post("/api/refresh", handle_api_refresh)
    app.router.add_get("/api/keys", handle_api_keys_get)
    app.router.add_post("/api/keys", handle_api_keys_save)
    app.router.add_get("/api/score", handle_api_score)
    app.router.add_get("/api/dep-graph", handle_api_dep_graph)
    app.router.add_get("/api/anti-patterns", handle_api_anti_patterns)
    app.router.add_post("/api/diagram/generate", handle_api_diagram_generate)
    app.router.add_get("/api/diagram/html", handle_api_diagram_html)
    app.router.add_get("/api/memory", handle_api_memory)
    app.router.add_get("/api/graph/status", handle_api_graph_status)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Web dashboard at http://localhost:%d", port)


def _flatten_tree(tree: dict, prefix: str = "") -> list[tuple[str, dict]]:
    """Flatten nested tree dict into list of (path, node) tuples."""
    items = []
    for key, val in tree.items():
        if key == "_file":
            continue
        path = f"{prefix}/{key}" if prefix else key
        if isinstance(val, dict):
            if "_file" in val:
                items.append((path, val))
            else:
                items.extend(_flatten_tree(val, path))
    return items
