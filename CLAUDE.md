# arch-viewer

MCP-native architecture viewer with AI-powered codebase analysis, AST parsing, architecture scoring, dependency graphs, and interactive dashboard.

## Architecture

- **Python package** (`arch_viewer/`) — MCP server with 17 tools, AI agent (pydantic-ai), AST analyzer, scoring engine, dependency graph builder, aiohttp web server
- **Node.js server** (`src/`) — Zero-dep express dashboard with WebSocket file watching (subset of Python features)
- **Web dashboard** (`web/index.html`) — Single-file enhanced dashboard with settings modal for API keys + model selection

## Key Modules

- `ast_analyzer.py` — Python AST via `ast` module, JS/TS via regex heuristics. Extracts functions, classes, imports, exports, cyclomatic complexity.
- `scoring.py` — 0-100 health score across 4 categories (Modularity, Code Quality, Maintainability, Structure). Anti-pattern detection.
- `dep_graph.py` — Builds import/call/package/component graphs. Hotspot analysis, isolated node detection.
- `agent.py` — pydantic-ai agent with 4 providers (Ollama Cloud, OpenAI, Anthropic, Groq). API keys in `.arch-viewer.keys.json`.
- `mcp_server.py` — 17 MCP tools. MCP mode is default (no `--mcp` flag).
- `scanner.py` — Static scanner. Component detection, route extraction, dependency collection.

## Commands

```bash
# Node.js dashboard
npm start                    # http://localhost:3777

# Python MCP server (full features)
python -m arch_viewer        # MCP + dashboard
python -m arch_viewer --web  # dashboard only
python -m arch_viewer --scan # JSON output
```

## Provider priority

ollama > openai > anthropic > groq (auto-detected from available keys)

## Conventions

- Online providers only — no local model support
- API keys stored in `.arch-viewer.keys.json` (gitignored)
- Python 3.11+, Node 18+
- Ruff for Python linting (line-length 100)
