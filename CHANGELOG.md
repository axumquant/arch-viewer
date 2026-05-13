# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.0.0] - 2025-05-13

### Added
- **Neo4j knowledge graph** — components, dependencies, and data flows stored
  in Neo4j 5 with full project isolation via `project_id`.
- **Mem0 semantic memory** — learned patterns and user corrections stored as
  vector embeddings in Qdrant via the mem0 SDK.
- **Auto-bootstrap stack** — `bootstrap_stack()` ensures Neo4j + Qdrant
  containers are running before the web server starts; auto-launches Docker
  Desktop on Windows if needed.
- **Interactive architecture diagram** — HTML/SVG renderer with 40+ sub-nodes,
  tier-colored glows, animated edge dashes, pan/zoom, and slide-in detail panel.
- **MCP diagram generator tool** — `generate_interactive_diagram` produces a
  self-contained HTML file matching the dashboard visual style.
- **Dashboard graph & memory tabs** — embedded Neo4j browser iframe, Mem0 stat
  cards, and live status pills.
- **Auto-credential detection** — inspects running Neo4j containers to share
  infrastructure with other stacks (e.g., sales-coach).
- **Ollama Cloud fallback** — when no OpenAI key is present, uses Ollama as an
  OpenAI-compatible embedder backend for Mem0.
- **17+ MCP tools** for architecture analysis, diagram generation, and memory
  management.
- **Docker Compose** config for Neo4j 5 + Qdrant with health checks and named
  volumes.

### Changed
- Diagram engine replaced: vis-network → Cytoscape.js → pure HTML/SVG for
  maximum compatibility and visual fidelity.
- pydantic-ai upgraded to v1.x API (`output_type`, `OpenAIChatModel`).
- Neo4j, mem0ai, and qdrant-client moved from optional extras to required
  dependencies — no flat-file fallback.

### Fixed
- Settings modal no longer auto-opens on every page load.
- Neo4j dashboard counts now use correct property names and relationship types.

## [1.0.0] - 2025-03-01

### Added
- Initial release with MCP-native architecture analysis.
- AST-based code parsing for Python, TypeScript, and JavaScript.
- Architecture scoring with complexity, cohesion, and coupling metrics.
- Web dashboard with real-time scan results.
- Flat-file memory system (`memory.json`).

[Unreleased]: https://github.com/axumquant/arch-viewer/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/axumquant/arch-viewer/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/axumquant/arch-viewer/releases/tag/v1.0.0
