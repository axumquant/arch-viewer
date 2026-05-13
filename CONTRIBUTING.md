# Contributing to arch-viewer

Thanks for your interest in arch-viewer! This is a small project run by a tiny team and every PR, issue, and discussion thread matters.

This guide covers everything you need: dev setup, code style, branch and commit conventions, how to propose new MCP tools or LLM providers, and the PR review process.

---

## Code of Conduct

This project follows the [Contributor Covenant v2.1](CODE_OF_CONDUCT.md). By participating you agree to abide by its terms. Report unacceptable behaviour to `axumquant+conduct@gmail.com`.

---

## Dev setup

You need Python 3.11+ and Docker Desktop running.

```bash
git clone https://github.com/axumquant/arch-viewer
cd arch-viewer
pip install -e ".[dev]"
docker compose up -d neo4j qdrant
pytest
```

The `[dev]` extra pulls `pytest`, `pytest-asyncio`, and `ruff`.

If you want to work on the Anthropic provider path, also install the `anthropic` extra:

```bash
pip install -e ".[dev,anthropic]"
```

To run arch-viewer locally against itself:

```bash
python -m arch_viewer --web .
```

---

## Code style

We use **ruff** for both formatting and linting. The config lives in `pyproject.toml` under `[tool.ruff]`.

Before opening a PR:

```bash
ruff check arch_viewer
ruff format arch_viewer
```

Optional but encouraged: run **pyright** for static typing. We aim for clean type annotations on all new code.

```bash
pip install pyright
pyright arch_viewer
```

General principles:

- Prefer small, well-named functions over clever one-liners.
- Use `from __future__ import annotations` in new modules.
- All public functions and classes get a one-line docstring at minimum.
- Catch broad `Exception` only when interacting with external services (LLM, Neo4j, Qdrant) and log the cause.

---

## Branch naming

Use a topic prefix:

- `feat/` — new feature
- `fix/` — bug fix
- `docs/` — documentation only
- `refactor/` — code restructure with no behaviour change
- `chore/` — tooling, config, deps
- `test/` — tests only

Example: `feat/cytoscape-renderer`, `fix/windows-stdout-unicode`.

---

## Commit messages

We follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(optional scope): short imperative summary

Optional body explaining the why, not the what.
```

Types: `feat`, `fix`, `docs`, `refactor`, `chore`, `test`, `perf`, `style`, `build`, `ci`.

Examples:

```
feat(mcp): add generate_interactive_diagram tool
fix(stack): handle Docker Desktop pipe path on Windows 11
docs(readme): add Cursor MCP integration snippet
```

Breaking changes go in the footer with `BREAKING CHANGE:` prefix.

---

## Pull request process

1. **Fork** the repo and create a topic branch off `main`.
2. **Make your changes** with focused commits.
3. **Add tests** if you touched logic. Pure docs / refactor PRs are fine without.
4. **Run** `ruff check`, `ruff format`, and `pytest`.
5. **Open the PR** against `main` with a clear description, the relevant issue number, and screenshots if you touched UI.
6. The CI workflow runs lint + tests on Python 3.11/3.12/3.13. Green CI is required.
7. A maintainer will review. Expect feedback; expect to iterate.

Keep PRs small. A 200-line PR gets merged in a day; a 2,000-line PR gets stuck for weeks.

---

## Proposing a new MCP tool

Tools are registered in `arch_viewer/mcp_server.py` inside `ArchViewerMCP._register_tools()`.

To add a tool:

1. Append a new `Tool(name=..., description=..., inputSchema=...)` to the list returned by `list_tools()`.
2. Add an `elif name == "your_tool":` branch in `_handle_tool()` with the implementation.
3. If the tool needs new scanner output, extend the relevant model in `arch_viewer/models.py`.
4. Update the **MCP tools** table in `README.md`.
5. Add a test in `tests/` (or open the PR explaining why the tool is hard to unit test — e.g. heavy LLM dependency — and we'll figure it out together).

Keep tool descriptions specific and example-rich. The description is what the LLM sees when deciding whether to call your tool, so it has to be self-explanatory.

---

## Adding an LLM provider

Providers are declared in the `PROVIDERS` dict at the top of `arch_viewer/agent.py`.

A new provider entry needs:

- `display` — human-readable name (shown in the dashboard)
- `env_var` — API key env variable (e.g. `MISTRAL_API_KEY`)
- `base_url` — OpenAI-compatible endpoint, if applicable
- `models` — list of `{id, name, recommended, description}` dicts

If the provider isn't OpenAI-compatible, also extend `_build_agent()` to construct the right pydantic-ai model class.

Open a draft PR early — provider integration usually involves a few back-and-forths about defaults and rate limits.

---

## Questions, ideas, kudos

- For **bugs**, open an issue with the bug report template.
- For **feature ideas**, open an issue with the feature request template.
- For **open-ended questions** or "is anyone using this for X?", start a [Discussion](https://github.com/axumquant/arch-viewer/discussions).

Be kind. Assume good faith. We're all here building something useful together.
