# arch-viewer

**MCP-native architecture viewer** with AI-powered codebase analysis, AST parsing, architecture scoring, dependency graphs, and an interactive dashboard.

Point it at any codebase and get:
- Live architecture visualization with component detection
- **Architecture health score** (0-100) with category breakdowns
- **AST-based code analysis** — functions, classes, imports, complexity metrics
- **Dependency graphs** — import chains, call graphs, package maps
- **Anti-pattern detection** — circular imports, god files, orphaned modules
- **AI-enriched descriptions** via OpenAI, Anthropic, Groq, or Ollama Cloud
- **Real-time file watching** with WebSocket updates
- **13 MCP tools** for plug-and-play integration with Claude Code, Cursor, etc.

---

## Quick Start

### Node.js Dashboard (zero Python needed)

```bash
npx arch-viewer            # scan current directory
npx arch-viewer ./my-project   # scan a specific project
```

Opens an interactive dashboard at `http://localhost:3777`.

### Python MCP Server (full features)

```bash
pip install arch-viewer
arch-viewer                  # MCP server + dashboard (default)
arch-viewer --web            # dashboard only
arch-viewer --scan           # print architecture JSON
```

### MCP Integration (Claude Code / Cursor)

Add to `.claude/settings.json`:

```json
{
  "mcpServers": {
    "arch-viewer": {
      "command": "python",
      "args": ["-m", "arch_viewer"],
      "env": {
        "ARCH_VIEWER_ROOT": "."
      }
    }
  }
}
```

---

## MCP Tools (13 total)

| Tool | Description |
|------|-------------|
| `get_architecture` | Complete architecture model with components, data flows, stats |
| `get_component` | Deep dive into a specific component |
| `get_data_flows` | How components connect and communicate |
| `get_dependencies` | All project dependencies with versions |
| `get_file_tree` | Nested file tree with filtering |
| `get_ai_summary` | AI-generated architecture narrative |
| `get_api_routes` | Detected HTTP/WS endpoints |
| `get_architecture_score` | 0-100 health score with category breakdowns |
| `get_dependency_graph` | Import/call/package/component graphs |
| `get_anti_patterns` | Circular imports, god files, complexity issues |
| `analyze_file_ast` | AST analysis of a single file |
| `get_recent_changes` | Real-time file change feed |
| `refresh_architecture` | Force rescan + AI re-analysis |

---

## Architecture Scoring

The scoring engine rates your codebase 0-100 across four categories:

| Category | What it measures |
|----------|-----------------|
| **Modularity** (0-25) | Component separation, cohesion, import coupling |
| **Code Quality** (0-25) | Cyclomatic complexity, documentation ratio |
| **Maintainability** (0-25) | File sizes, dependency count, test coverage |
| **Structure** (0-25) | Directory organization, entry points, config hygiene |

Anti-patterns apply additional penalties:

- **Circular Imports** — A imports B, B imports A
- **God Files** — 30+ functions or 5+ classes in one file
- **Excessive Complexity** — Functions with cyclomatic complexity >30
- **Orphaned Modules** — Files never imported by anything
- **Deep Nesting** — Files buried 7+ directories deep
- **Mixed Concerns** — Wrong language in wrong component directory

---

## REST API

```
GET  /api/scan            Full project scan
GET  /api/score           Architecture health score
GET  /api/dep-graph       Dependency graph (?type=imports|calls|packages|components)
GET  /api/anti-patterns   Anti-pattern detection
GET  /api/file?path=...   Read a file
PUT  /api/file            Write a file
GET  /api/search?q=...    Text search across files
GET  /api/recent          Recent file changes
GET  /api/keys            Provider/model status
POST /api/keys            Save API keys + model selection
POST /api/refresh         Force rescan + AI analysis
```

---

## AI Providers

arch-viewer supports four cloud LLM providers. API keys are stored in `.arch-viewer.keys.json` (auto-gitignored) or environment variables.

| Provider | Default Model | Env Variable |
|----------|--------------|--------------|
| Ollama Cloud | qwen3-coder:480b-cloud | `OLLAMA_API_KEY` |
| OpenAI | gpt-4.1-mini | `OPENAI_API_KEY` |
| Anthropic | claude-sonnet-4-6 | `ANTHROPIC_API_KEY` |
| Groq | llama-3.3-70b-versatile | `GROQ_API_KEY` |

The web dashboard has a settings panel to enter keys and select models on first use.

---

## Optional: Graph + Memory Stack

arch-viewer can persist its findings into a real graph database and a
semantic memory store. This is entirely opt-in — the flat-file memory at
`.arch_viewer/memory.json` keeps working if you skip this section.

### 1. Start the stores

```bash
cp .env.example .env              # tweak NEO4J_PASSWORD if you like
docker compose up -d neo4j qdrant
```

- **Neo4j** — component / dependency knowledge graph. Browser UI at
  [http://localhost:7474](http://localhost:7474) (login `neo4j` /
  `archviewer123`). Bolt port `7687`.
- **Qdrant** — vector store used by Mem0 for semantic memory. REST API on
  port `6333`.

To also run arch-viewer itself inside compose:

```bash
docker compose --profile app up -d
```

### 2. Install the Python clients

```bash
pip install -e ".[graph]"
```

This pulls in `neo4j`, `mem0ai`, and `qdrant-client`. None of them are
required for the core app — without them the graph store and Mem0 silently
fall back to no-ops.

### 3. What you get

| Store | What it adds | When it's used |
|-------|--------------|----------------|
| Neo4j | A queryable graph of every component, dependency, and data flow per project. Browse it at http://localhost:7474. | After every analysis, `sync_architecture_to_graph` upserts the current snapshot. |
| Mem0 (Qdrant + OpenAI embeddings) | Semantic recall of past patterns and user corrections, scoped per project. | Every `add_pattern` / `add_correction` is mirrored from the flat file into Mem0, and `get_context_for_analysis` pulls the top-relevant memories before each AI run. |

Project isolation: every Neo4j node carries a deterministic `project_id`
hash of the absolute project root, and every Mem0 entry uses a
project-scoped `user_id`. Multiple projects share the same stores without
collisions.

---

## Project Structure

```
arch-viewer/
  arch_viewer/          # Python package (MCP server + AI + AST)
    __main__.py         # CLI entry point
    mcp_server.py       # MCP tools (13 tools)
    scanner.py          # Static code scanner
    ast_analyzer.py     # AST parsing (Python native, JS regex)
    scoring.py          # Architecture health scoring
    dep_graph.py        # Dependency graph builder
    agent.py            # AI agent (pydantic-ai)
    memory.py           # Flat-file + Mem0 + Neo4j memory layer
    graph_store.py      # Neo4j wrapper (optional)
    mem_store.py        # Mem0 wrapper (optional)
    models.py           # Pydantic data models
    watcher.py          # File system watcher
    web_server.py       # aiohttp web dashboard
  src/                  # Node.js server (zero-dep dashboard)
    cli.js              # CLI
    server.js           # Express + WebSocket
    scanner.js          # File scanner
    watcher.js          # chokidar file watcher
  web/                  # Enhanced dashboard (HTML/CSS/JS)
  public/               # Legacy dashboard
```

---

## License

MIT
