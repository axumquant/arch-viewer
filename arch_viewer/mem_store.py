"""
Mem0-backed semantic memory for arch-viewer.

Stores learned architecture patterns and user corrections as embeddings so
the agent can recall the most relevant prior knowledge for a given component
or analysis prompt, rather than dumping every flat-file note into context.

Fails soft: if `mem0ai` is missing or Qdrant is unreachable, every method
becomes a no-op. The flat-file `memory.json` remains the source of truth.

Defaults:
  - Vector store: local Qdrant at http://localhost:6333
  - Embedder:     OpenAI text-embedding-3-small (uses OPENAI_API_KEY from
                  the project's .arch_viewer/keys.json via agent.load_keys)
"""

from __future__ import annotations

import logging
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger("arch-viewer.mem_store")


class MemStore:
    """
    Lazy wrapper around the mem0 SDK.

    Usage:
        ms = MemStore(project_root="/path/to/proj")
        if ms.connect():
            ms.add_pattern("uses async event loop", {"category": "architecture"})
            results = ms.search("event loop", limit=3)
    """

    def __init__(self, project_root: str | Path):
        self.project_root = Path(project_root).resolve()
        self.qdrant_url = os.environ.get("QDRANT_URL", "http://localhost:6333")
        self.collection = "arch_viewer_memory"
        self._memory: Any = None
        self._available: bool = False
        # Stable user id so mem0 scopes recall to this project
        self._user_id = f"arch-viewer:{self.project_root.name}"

    # ─── Lifecycle ───

    def connect(self) -> bool:
        """
        Initialize the mem0 Memory client.
        Returns True on success, False otherwise.
        """
        if os.environ.get("MEM0_ENABLED", "1") == "0":
            log.info("MEM0_ENABLED=0 — semantic memory disabled")
            return False

        if self._memory is not None:
            return self._available

        # Load OpenAI key from project config if present
        self._ensure_openai_key()
        if not os.environ.get("OPENAI_API_KEY"):
            log.info("OPENAI_API_KEY not set — MemStore disabled "
                     "(mem0 needs an embedder)")
            self._available = False
            return False

        # Check Qdrant reachability before importing mem0
        if not self._qdrant_reachable():
            log.info("Qdrant not reachable at %s — MemStore disabled. "
                     "Start it with: docker compose up -d qdrant", self.qdrant_url)
            self._available = False
            return False

        try:
            from mem0 import Memory  # type: ignore
        except ImportError:
            log.info("mem0ai package not installed — MemStore disabled. "
                     "Install with: pip install -e '.[graph]'")
            self._available = False
            return False

        try:
            parsed = urllib.parse.urlparse(self.qdrant_url)
            qhost = parsed.hostname or "localhost"
            qport = parsed.port or 6333
            config = {
                "vector_store": {
                    "provider": "qdrant",
                    "config": {
                        "collection_name": self.collection,
                        "host": qhost,
                        "port": qport,
                    },
                },
                "embedder": {
                    "provider": "openai",
                    "config": {"model": "text-embedding-3-small"},
                },
                "llm": {
                    "provider": "openai",
                    "config": {"model": "gpt-4o-mini"},
                },
            }
            self._memory = Memory.from_config(config)
            self._available = True
            log.info("MemStore connected (qdrant=%s, user_id=%s)",
                     self.qdrant_url, self._user_id)
            return True
        except Exception as exc:
            log.warning("Could not initialize mem0 Memory: %s — MemStore disabled", exc)
            self._memory = None
            self._available = False
            return False

    def close(self) -> None:
        self._memory = None
        self._available = False

    @property
    def available(self) -> bool:
        return self._available

    # ─── Public API ───

    def add_pattern(self, text: str, metadata: dict[str, Any] | None = None) -> None:
        """Store a learned pattern as semantic memory."""
        if not self._available or not text:
            return
        meta = {
            "type": "pattern",
            "project": self.project_root.name,
            "timestamp": time.time(),
        }
        if metadata:
            meta.update(metadata)
        try:
            self._memory.add(text, user_id=self._user_id, metadata=meta)
        except Exception as exc:
            log.debug("mem0 add_pattern failed: %s", exc)

    def add_correction(
        self, original: str, corrected: str, context: str | None = None
    ) -> None:
        """Store a user correction so the agent can recall the right answer later."""
        if not self._available:
            return
        text = (
            f"CORRECTION — when context suggests '{original}', the right "
            f"interpretation is '{corrected}'."
        )
        if context:
            text += f" Context: {context}"
        meta = {
            "type": "correction",
            "project": self.project_root.name,
            "timestamp": time.time(),
            "original": original,
            "corrected": corrected,
        }
        try:
            self._memory.add(text, user_id=self._user_id, metadata=meta)
        except Exception as exc:
            log.debug("mem0 add_correction failed: %s", exc)

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Semantic search across all stored memories."""
        if not self._available or not query:
            return []
        try:
            result = self._memory.search(
                query=query, user_id=self._user_id, limit=limit
            )
            # mem0 returns either a list or a dict {"results": [...]}.
            if isinstance(result, dict):
                return result.get("results", []) or []
            return list(result or [])
        except Exception as exc:
            log.debug("mem0 search failed: %s", exc)
            return []

    def get_relevant_for_analysis(self, component_name: str) -> list[dict[str, Any]]:
        """Pull the most relevant memories for an upcoming analysis of a component."""
        if not self._available or not component_name:
            return []
        query = f"{component_name} component architecture patterns and corrections"
        return self.search(query, limit=8)

    # ─── helpers ───

    def _ensure_openai_key(self) -> None:
        """Pull OPENAI_API_KEY from the project's keys.json if not in env."""
        if os.environ.get("OPENAI_API_KEY"):
            return
        try:
            from .agent import load_keys
            keys = load_keys(self.project_root)
            if isinstance(keys, dict):
                k = keys.get("openai")
                if isinstance(k, str) and k.strip():
                    os.environ["OPENAI_API_KEY"] = k.strip()
        except Exception as exc:
            log.debug("load_keys failed: %s", exc)

    def _qdrant_reachable(self) -> bool:
        try:
            with urllib.request.urlopen(
                f"{self.qdrant_url.rstrip('/')}/readyz", timeout=2
            ) as resp:
                return 200 <= resp.status < 500
        except Exception:
            # Some Qdrant versions don't expose /readyz — fall back to root.
            try:
                with urllib.request.urlopen(self.qdrant_url, timeout=2) as resp:
                    return 200 <= resp.status < 500
            except Exception:
                return False
