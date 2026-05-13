"""
AI Architecture Agent — uses pydantic-ai to analyze codebases and maintain
a living architecture document.

Supported providers (online only — no local models):
  - ollama    → Ollama Cloud API (OpenAI-compatible)
  - openai    → OpenAI API
  - anthropic → Anthropic API
  - groq      → Groq API

API keys are stored in .arch_viewer/keys.json in the project root.
After a key is accepted, the user selects a model from the provider's catalog.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from .models import Architecture, Component, DataFlow, DataFlowDirection

log = logging.getLogger("arch-viewer.agent")

CONFIG_DIR = ".arch_viewer"
KEYS_FILENAME = "keys.json"
LEGACY_KEYS_FILE = ".arch-viewer.keys.json"


# ─── Structured Output Models ───


class ComponentAnalysis(BaseModel):
    """AI-generated analysis of a single component."""
    description: str = Field(description="2-3 sentence description of what this component does")
    key_patterns: list[str] = Field(default_factory=list, description="Design patterns used")
    external_services: list[str] = Field(default_factory=list, description="External APIs or services this connects to")


class ArchitectureSummary(BaseModel):
    """AI-generated architecture overview."""
    description: str = Field(description="One paragraph project description")
    architecture_style: str = Field(description="e.g. monolith, microservices, modular monolith, serverless")
    data_flows: list[FlowDescription] = Field(default_factory=list)
    key_decisions: list[str] = Field(default_factory=list, description="Notable architectural decisions")
    tech_highlights: list[str] = Field(default_factory=list, description="Key technology choices")


class FlowDescription(BaseModel):
    source: str
    target: str
    protocol: str
    description: str


# Fix forward ref
ArchitectureSummary.model_rebuild()


# ─── Provider & Model Catalog ───

PROVIDERS: dict[str, dict] = {
    "ollama": {
        "display": "Ollama Cloud",
        "env_var": "OLLAMA_API_KEY",
        "base_url": "https://api.ollama.com/v1",
        "models": [
            {"id": "qwen3-coder:480b-cloud", "name": "Qwen3 Coder 480B", "recommended": True,
             "description": "Best for code analysis and architecture review"},
            {"id": "gpt-oss:120b-cloud", "name": "GPT-OSS 120B", "recommended": False,
             "description": "Strong general-purpose model"},
            {"id": "gpt-oss:20b-cloud", "name": "GPT-OSS 20B", "recommended": False,
             "description": "Fast and lightweight"},
            {"id": "deepseek-v3.1:671b-cloud", "name": "DeepSeek V3.1 671B", "recommended": False,
             "description": "Largest, most capable reasoning model"},
            {"id": "kimi-k2:1t-cloud", "name": "Kimi K2 1T", "recommended": False,
             "description": "1 trillion parameter model"},
        ],
        "default_model": "qwen3-coder:480b-cloud",
    },
    "openai": {
        "display": "OpenAI",
        "env_var": "OPENAI_API_KEY",
        "base_url": None,
        "models": [
            {"id": "gpt-4.1-mini", "name": "GPT-4.1 Mini", "recommended": True,
             "description": "Fast, cost-efficient — ideal for architecture scanning"},
            {"id": "gpt-4.1", "name": "GPT-4.1", "recommended": False,
             "description": "Latest flagship — best coding and instruction following"},
            {"id": "gpt-4o", "name": "GPT-4o", "recommended": False,
             "description": "Previous flagship, solid all-around"},
            {"id": "gpt-4o-mini", "name": "GPT-4o Mini", "recommended": False,
             "description": "Budget-friendly, good for simple scans"},
            {"id": "o3", "name": "o3", "recommended": False,
             "description": "Advanced reasoning model — slower but deeper analysis"},
        ],
        "default_model": "gpt-4.1-mini",
    },
    "anthropic": {
        "display": "Anthropic",
        "env_var": "ANTHROPIC_API_KEY",
        "base_url": None,
        "models": [
            {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6", "recommended": True,
             "description": "Best balance — near-Opus quality at practical cost"},
            {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5", "recommended": False,
             "description": "Fastest and cheapest — great for quick scans"},
            {"id": "claude-opus-4-6", "name": "Claude Opus 4.6", "recommended": False,
             "description": "Most capable — deep architecture analysis"},
            {"id": "claude-sonnet-4-0", "name": "Claude Sonnet 4", "recommended": False,
             "description": "Previous gen sonnet, still solid"},
        ],
        "default_model": "claude-sonnet-4-6",
    },
    "groq": {
        "display": "Groq",
        "env_var": "GROQ_API_KEY",
        "base_url": None,
        "models": [
            {"id": "llama-3.3-70b-versatile", "name": "Llama 3.3 70B", "recommended": True,
             "description": "Best general-purpose — fast inference on Groq hardware"},
            {"id": "meta-llama/llama-4-scout-17b-16e-instruct", "name": "Llama 4 Scout", "recommended": False,
             "description": "Latest Llama 4 with 10M context window"},
            {"id": "llama-3.1-8b-instant", "name": "Llama 3.1 8B Instant", "recommended": False,
             "description": "Ultra-fast, lowest latency"},
            {"id": "gemma2-9b-it", "name": "Gemma 2 9B", "recommended": False,
             "description": "Google's efficient open model"},
            {"id": "mixtral-8x7b-32768", "name": "Mixtral 8x7B", "recommended": False,
             "description": "Mixture-of-experts, good for diverse tasks"},
        ],
        "default_model": "llama-3.3-70b-versatile",
    },
}


# ─── API Key Management ───


def _keys_path(project_root: str | Path) -> Path:
    """Return the canonical keys path: <project>/.arch_viewer/keys.json"""
    return Path(project_root) / CONFIG_DIR / KEYS_FILENAME


def _migrate_legacy_keys(project_root: str | Path) -> None:
    """Auto-migrate from old .arch-viewer.keys.json to .arch_viewer/keys.json."""
    root = Path(project_root)
    legacy = root / LEGACY_KEYS_FILE
    new_dir = root / CONFIG_DIR
    new_path = new_dir / KEYS_FILENAME

    if legacy.exists() and not new_path.exists():
        log.info("Migrating keys from %s to %s", legacy, new_path)
        new_dir.mkdir(parents=True, exist_ok=True)
        data = legacy.read_text(encoding="utf-8")
        new_path.write_text(data, encoding="utf-8")
        legacy.unlink()  # remove old file after migration


def load_keys(project_root: str | Path) -> dict:
    """
    Load API keys and model selections from .arch_viewer/keys.json.
    Auto-migrates from legacy .arch-viewer.keys.json if found.
    Returns dict like:
      {"openai": "sk-...", "selected_models": {"openai": "gpt-4.1-mini"}}
    """
    _migrate_legacy_keys(project_root)
    keys_path = _keys_path(project_root)
    if keys_path.exists():
        try:
            return json.loads(keys_path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("Failed to read %s: %s", keys_path, e)
    return {}


def save_keys(project_root: str | Path, keys: dict[str, str], selected_models: dict[str, str] | None = None) -> None:
    """
    Save API keys and model selections to .arch_viewer/keys.json.
    Creates the .arch_viewer/ directory if it doesn't exist. Merges with existing data.
    """
    keys_path = _keys_path(project_root)
    keys_path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_keys(project_root)

    # Merge keys (provider name → api key string)
    for k, v in keys.items():
        if isinstance(v, str) and v.strip() and k in PROVIDERS:
            existing[k] = v.strip()

    # Merge model selections
    if selected_models:
        if "selected_models" not in existing:
            existing["selected_models"] = {}
        existing["selected_models"].update(selected_models)

    keys_path.write_text(
        json.dumps(existing, indent=2) + "\n",
        encoding="utf-8",
    )
    log.info("API keys saved to %s", keys_path)


def get_selected_model(project_root: str | Path, provider: str) -> str:
    """Get the user-selected model for a provider, or fall back to default."""
    data = load_keys(project_root)
    selected = data.get("selected_models", {})
    if provider in selected:
        return selected[provider]
    return PROVIDERS.get(provider, {}).get("default_model", "")


def inject_keys_to_env(project_root: str | Path) -> dict[str, str]:
    """
    Load keys from project file and inject into environment variables.
    Returns the keys that were loaded (provider → key).
    """
    data = load_keys(project_root)
    injected = {}

    for provider, info in PROVIDERS.items():
        env_var = info["env_var"]
        key = data.get(provider, "")
        if key:
            os.environ[env_var] = key
            injected[provider] = key
        elif os.environ.get(env_var):
            injected[provider] = os.environ[env_var]

    return injected


def detect_available_provider(project_root: str | Path) -> str | None:
    """
    Auto-detect which provider has a key available.
    Priority: ollama > openai > anthropic > groq
    """
    available = inject_keys_to_env(project_root)
    for provider in ("ollama", "openai", "anthropic", "groq"):
        if provider in available:
            return provider
    return None


# ─── Model Resolution ───


def resolve_model(provider: str, model_name: str | None = None, project_root: str | Path | None = None):
    """
    Resolve a pydantic-ai model from provider + optional model name.

    For standard providers (openai, anthropic, groq): returns a model string.
    For Ollama Cloud: returns an OpenAI-compatible model with custom base_url.
    """
    if provider not in PROVIDERS:
        raise ValueError(
            f"Unknown provider: {provider}. "
            f"Available: {list(PROVIDERS.keys())}."
        )

    # Determine model ID
    if not model_name and project_root:
        model_name = get_selected_model(project_root, provider)
    if not model_name:
        model_name = PROVIDERS[provider]["default_model"]

    # Ollama Cloud uses OpenAI-compatible API with custom base_url
    if provider == "ollama":
        try:
            import openai as openai_lib
            from pydantic_ai.models.openai import OpenAIModel

            api_key = os.environ.get("OLLAMA_API_KEY", "")
            client = openai_lib.AsyncOpenAI(
                base_url=PROVIDERS["ollama"]["base_url"],
                api_key=api_key,
            )
            return OpenAIModel(model_name, openai_client=client)
        except ImportError:
            raise ImportError(
                "Ollama Cloud requires the 'openai' package. "
                "Install with: pip install openai"
            )

    # Standard providers: return pydantic-ai model string
    return f"{provider}:{model_name}"


# ─── Architecture Agent ───


class ArchitectureAgent:
    """
    Pydantic-AI powered agent that analyzes code and enriches Architecture models.
    Online-only: works with Ollama Cloud, OpenAI, Anthropic, or Groq.
    """

    def __init__(
        self,
        provider: str = "openai",
        model_name: str | None = None,
        project_root: str | Path | None = None,
    ):
        # Inject API keys from project config
        if project_root:
            inject_keys_to_env(project_root)

        self._model = resolve_model(provider, model_name, project_root)
        self._provider = provider

        # Summary agent — generates project-level overview
        self._summary_agent = Agent(
            model=self._model,
            result_type=ArchitectureSummary,
            system_prompt=(
                "You are a software architecture analyst. Given a project's file structure, "
                "components, tech stack, and code samples, produce a concise architecture summary. "
                "Be specific about the actual code — don't guess or hallucinate features that aren't there. "
                "Focus on: what the project does, how components connect, key design patterns, and tech choices."
            ),
        )

        # Component agent — analyzes individual components
        self._component_agent = Agent(
            model=self._model,
            result_type=ComponentAnalysis,
            system_prompt=(
                "You are a software architecture analyst. Given a component's file listing, "
                "tech stack, and code samples, describe what this component does, what patterns it uses, "
                "and what external services it connects to. Be concise and accurate."
            ),
        )

    async def analyze_architecture(
        self, arch: Architecture, root: Path, sample_limit: int = 20
    ) -> Architecture:
        """
        Full architecture analysis — enriches components and generates summary.
        Reads key files to give the AI actual code context.
        """
        model_display = self._model if isinstance(self._model, str) else f"ollama-cloud"
        log.info("Starting AI architecture analysis with %s", model_display)

        # 1. Analyze each component
        tasks = []
        for comp in arch.components:
            tasks.append(self._analyze_component(comp, root, sample_limit))
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for comp, result in zip(arch.components, results):
            if isinstance(result, ComponentAnalysis):
                comp.description = result.description
            elif isinstance(result, Exception):
                log.warning("Failed to analyze %s: %s", comp.name, result)

        # 2. Generate overall summary
        try:
            summary = await self._generate_summary(arch, root)
            arch.description = summary.description
            arch.ai_summary = (
                f"**Architecture:** {summary.architecture_style}\n\n"
                f"{summary.description}\n\n"
                f"**Key Decisions:**\n"
                + "\n".join(f"- {d}" for d in summary.key_decisions)
                + "\n\n**Tech Highlights:**\n"
                + "\n".join(f"- {t}" for t in summary.tech_highlights)
            )

            # Merge AI-discovered data flows
            existing_pairs = {(f.source, f.target) for f in arch.data_flows}
            for flow_desc in summary.data_flows:
                pair = (flow_desc.source, flow_desc.target)
                if pair not in existing_pairs:
                    arch.data_flows.append(DataFlow(
                        source=flow_desc.source,
                        target=flow_desc.target,
                        protocol=flow_desc.protocol,
                        description=flow_desc.description,
                        direction=DataFlowDirection.UNIDIRECTIONAL,
                    ))
                    existing_pairs.add(pair)

        except Exception as e:
            log.warning("Failed to generate summary: %s", e)
            arch.ai_summary = "(AI analysis unavailable — check API key in .arch-viewer.keys.json)"

        arch.analysis_version += 1
        return arch

    async def _analyze_component(
        self, comp: Component, root: Path, sample_limit: int
    ) -> ComponentAnalysis:
        """Analyze a single component by reading its key files."""
        context = self._build_component_context(comp, root, sample_limit)
        result = await self._component_agent.run(context)
        return result.data

    async def _generate_summary(self, arch: Architecture, root: Path) -> ArchitectureSummary:
        """Generate project-level architecture summary."""
        context = self._build_summary_context(arch, root)
        result = await self._summary_agent.run(context)
        return result.data

    def _build_component_context(self, comp: Component, root: Path, sample_limit: int) -> str:
        """Build a text context for component analysis."""
        lines = [
            f"Component: {comp.name}",
            f"Type: {comp.type.value}",
            f"Path: {comp.path}/",
            f"Tech: {', '.join(comp.tech_stack)}",
            f"Files ({len(comp.files)} total):",
        ]

        for f in comp.files[:100]:
            lines.append(f"  - {f}")

        lines.append("\n--- Key File Contents ---\n")
        sampled = 0
        priority_files = [f for f in comp.files if _is_key_file(f)]
        other_files = [f for f in comp.files if not _is_key_file(f)]

        for fpath in (priority_files + other_files)[:sample_limit]:
            abs_path = root / fpath
            if not abs_path.exists() or abs_path.stat().st_size > 50_000:
                continue
            try:
                content = abs_path.read_text(errors="ignore")
                lines.append(f"\n=== {fpath} ===")
                if len(content) > 3000:
                    content = content[:3000] + "\n... (truncated)"
                lines.append(content)
                sampled += 1
                if sampled >= sample_limit:
                    break
            except Exception:
                continue

        if comp.api_routes:
            lines.append("\n--- API Routes ---")
            for r in comp.api_routes:
                lines.append(f"  {r.method} {r.path} ({r.file})")

        return "\n".join(lines)

    def _build_summary_context(self, arch: Architecture, root: Path) -> str:
        """Build a text context for architecture summary."""
        lines = [
            f"Project: {arch.project_name}",
            f"Total files: {arch.stats.get('total_files', 0)}",
            f"\nComponents ({len(arch.components)}):",
        ]

        for comp in arch.components:
            lines.append(f"\n  [{comp.type.value}] {comp.name}")
            lines.append(f"    Path: {comp.path}/")
            lines.append(f"    Tech: {', '.join(comp.tech_stack)}")
            if comp.description:
                lines.append(f"    Description: {comp.description}")
            lines.append(f"    Files: {len(comp.files)}")
            if comp.api_routes:
                lines.append(f"    API Routes: {len(comp.api_routes)}")
                for r in comp.api_routes[:10]:
                    lines.append(f"      {r.method} {r.path}")

        if arch.data_flows:
            lines.append(f"\nData Flows ({len(arch.data_flows)}):")
            for f in arch.data_flows:
                lines.append(f"  {f.source} → {f.target} ({f.protocol}): {f.description}")

        if arch.dependencies:
            lines.append(f"\nDependencies ({len(arch.dependencies)}):")
            by_cat: dict[str, list[str]] = {}
            for d in arch.dependencies:
                by_cat.setdefault(d.category, []).append(d.name)
            for cat, names in by_cat.items():
                lines.append(f"  [{cat}]: {', '.join(names[:20])}")

        for fname in ("CLAUDE.md", "README.md", "AGENTS.md"):
            fpath = root / fname
            if fpath.exists():
                try:
                    content = fpath.read_text(errors="ignore")
                    if len(content) > 2000:
                        content = content[:2000] + "..."
                    lines.append(f"\n=== {fname} ===")
                    lines.append(content)
                except Exception:
                    pass

        # Inject AI memory context (learned patterns, corrections, history)
        try:
            from .memory import get_context_for_analysis
            memory_ctx = get_context_for_analysis(root)
            if memory_ctx:
                lines.append(memory_ctx)
        except Exception as exc:
            log.debug("Could not load memory context: %s", exc)

        return "\n".join(lines)

    async def analyze_single_file(self, file_path: str, content: str) -> str:
        """Quick single-file analysis — returns a one-line summary."""
        try:
            result = await self._component_agent.run(
                f"Summarize this file in one sentence:\n\nFile: {file_path}\n\n{content[:3000]}"
            )
            return result.data.description
        except Exception as e:
            return f"(analysis failed: {e})"


def _is_key_file(fpath: str) -> bool:
    """Heuristic: is this file architecturally important?"""
    key_names = {
        "main.py", "app.py", "server.py", "config.py", "settings.py",
        "models.py", "schema.py", "routes.py", "api.py", "ws.py",
        "background.js", "manifest.json", "content.js", "sidepanel.js",
        "page.tsx", "layout.tsx", "index.tsx", "App.tsx",
        "Dockerfile", "docker-compose.yml", "pyproject.toml", "package.json",
        "CLAUDE.md", "README.md",
    }
    name = fpath.split("/")[-1]
    return name in key_names
