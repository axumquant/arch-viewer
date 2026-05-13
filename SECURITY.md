# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 2.x     | :white_check_mark: |
| < 2.0   | :x:                |

## Reporting a Vulnerability

**Please do NOT open a public GitHub issue for security vulnerabilities.**

Instead, email **hello@axumlabs.com** with:

1. A description of the vulnerability
2. Steps to reproduce (or a proof-of-concept)
3. The potential impact

You will receive an acknowledgement within **48 hours** and a detailed response
within **5 business days** indicating next steps.

## Disclosure Policy

- We follow [coordinated disclosure](https://en.wikipedia.org/wiki/Coordinated_vulnerability_disclosure).
- A fix will be developed privately, and a patched release will be published
  before any public disclosure.
- Credit will be given to the reporter (unless they prefer anonymity).

## Security Best Practices for Users

- **Never commit API keys.** Store them in `.arch_viewer/keys.json` (git-ignored)
  or use environment variables.
- **Run Docker services on localhost only.** The default `docker-compose.yml`
  binds Neo4j and Qdrant to `127.0.0.1`.
- **Keep dependencies updated.** Run `pip install --upgrade arch-viewer` and
  watch Dependabot alerts on this repo.
- **Review MCP tool permissions.** arch-viewer's MCP tools are read-only by
  default; diagram generation writes only to `docs/`.
