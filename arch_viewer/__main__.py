"""
CLI entry point for arch-viewer.

Usage:
  # Default: MCP server + web dashboard (recommended)
  python -m arch_viewer

  # Web dashboard only (no MCP stdio)
  python -m arch_viewer --web

  # Scan only (print architecture JSON and exit)
  python -m arch_viewer --scan

  # Specify provider (online only: openai, anthropic, groq)
  python -m arch_viewer --provider openai

Environment variables:
  ARCH_VIEWER_ROOT      Project root to scan (default: cwd)
  ARCH_VIEWER_PROVIDER  LLM provider: openai, anthropic, groq (default: auto-detect)
  ARCH_VIEWER_MODEL     Model name override (default: provider-specific)
  ARCH_VIEWER_PORT      Web dashboard port (default: 3777)
  ARCH_VIEWER_NO_AI     Set to "1" to disable AI analysis

API keys are stored in .arch-viewer.keys.json in the project root.
On first run without keys, the web dashboard prompts for key entry.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys


def main():
    parser = argparse.ArgumentParser(
        description="arch-viewer — MCP-native architecture viewer with AI analysis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  arch-viewer                               # MCP + web dashboard (default)
  arch-viewer --web                         # Web dashboard only at :3777
  arch-viewer --scan                        # Print architecture JSON
  arch-viewer --provider openai             # Use OpenAI for analysis
  arch-viewer --provider anthropic          # Use Anthropic for analysis
  arch-viewer --web --port 4000 --no-ai     # Dashboard without AI

Providers (online only — no local models):
  openai     GPT-4o-mini (default)    Needs OPENAI_API_KEY
  anthropic  Claude Sonnet            Needs ANTHROPIC_API_KEY
  groq       Llama 3.1 70B           Needs GROQ_API_KEY

API keys are loaded from .arch-viewer.keys.json in the project root,
or from environment variables. The web dashboard has a settings panel
to enter keys on first use.
        """,
    )

    parser.add_argument(
        "root", nargs="?", default=None,
        help="Project root directory (default: cwd or ARCH_VIEWER_ROOT)",
    )
    parser.add_argument(
        "--web", action="store_true",
        help="Run web dashboard only (no MCP stdio)",
    )
    parser.add_argument(
        "--scan", action="store_true",
        help="Scan project and print architecture JSON, then exit",
    )
    parser.add_argument(
        "--provider", default=None,
        help="LLM provider: openai, anthropic, groq (default: auto-detect from keys)",
    )
    parser.add_argument(
        "--model", default=None,
        help="Model name override",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="Web dashboard port (default: 3777)",
    )
    parser.add_argument(
        "--no-ai", action="store_true",
        help="Disable AI analysis (static scan only)",
    )
    parser.add_argument(
        "--no-mcp", action="store_true",
        help="Disable MCP server (web dashboard only — same as --web)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Verbose logging",
    )

    args = parser.parse_args()

    # Resolve config from args + env
    root = args.root or os.environ.get("ARCH_VIEWER_ROOT", os.getcwd())
    port = args.port or int(os.environ.get("ARCH_VIEWER_PORT", "3777"))
    no_ai = args.no_ai or os.environ.get("ARCH_VIEWER_NO_AI", "").strip() == "1"

    # Provider: explicit > env > auto-detect from keys
    provider = args.provider or os.environ.get("ARCH_VIEWER_PROVIDER")
    if not provider and not no_ai:
        from .agent import detect_available_provider
        provider = detect_available_provider(root)
        if provider:
            logging.getLogger("arch-viewer").info("Auto-detected provider: %s", provider)
        else:
            logging.getLogger("arch-viewer").info(
                "No API key found — AI analysis disabled. "
                "Add a key via the web dashboard settings or .arch-viewer.keys.json"
            )
            no_ai = True

    if not provider:
        provider = "openai"  # default for display purposes

    model = args.model or os.environ.get("ARCH_VIEWER_MODEL")

    # Logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,  # MCP uses stdout for protocol
    )

    if args.scan:
        # Scan-only mode
        from .scanner import scan_project
        arch = scan_project(root)

        if not no_ai:
            from .agent import ArchitectureAgent
            agent = ArchitectureAgent(provider=provider, model_name=model, project_root=root)
            arch = asyncio.run(agent.analyze_architecture(arch, root))

        print(arch.model_dump_json(indent=2))
        return

    if args.web or args.no_mcp:
        # Web-only mode (no MCP stdio)
        asyncio.run(_run_web_only(root, provider, model, port, no_ai))
        return

    # Default: MCP server + web dashboard
    from .mcp_server import ArchViewerMCP
    server = ArchViewerMCP(
        root=root,
        provider=provider,
        model_name=model,
        auto_analyze=not no_ai,
        web_port=port,
    )
    asyncio.run(server.run())


async def _run_web_only(root, provider, model, port, no_ai):
    """Run just the web dashboard without MCP stdio."""
    from .mcp_server import ArchViewerMCP
    from .web_server import start_web_server

    mcp = ArchViewerMCP(
        root=root,
        provider=provider,
        model_name=model,
        auto_analyze=not no_ai,
        web_port=None,  # We'll start web ourselves
    )

    # Initial scan
    await mcp._scan(run_ai=not no_ai)

    # Start watcher
    from .watcher import ProjectWatcher
    mcp._watcher = ProjectWatcher(
        root=mcp.root,
        on_change=mcp._on_file_change,
        on_rescan_needed=mcp._on_rescan_needed,
    )
    mcp._watcher.start()

    # Start web server
    await start_web_server(arch_mcp=mcp, port=port)

    print(f"\n  ⚡ Arch Viewer running at http://localhost:{port}")
    print(f"  📂 Watching: {root}")
    print(f"  🤖 Provider: {provider}" + (" (AI disabled)" if no_ai else ""))
    print()

    # Keep running
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        mcp._watcher.stop()


if __name__ == "__main__":
    main()
