"""
AI Memory Layer — stores learned patterns about a project's architecture.

The agent uses this to improve over time by remembering:
  1. Architecture patterns it discovered
  2. Component relationships not obvious from code alone
  3. Anti-patterns flagged and whether they were fixed
  4. User corrections when the AI got something wrong
  5. Analysis history with timestamps and score deltas

Storage: .arch_viewer/memory.json in the project root.
Uses the same CONFIG_DIR constant as agent.py.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from .agent import CONFIG_DIR

log = logging.getLogger("arch-viewer.memory")

MEMORY_FILENAME = "memory.json"
MEMORY_VERSION = 1

# ─── Default memory structure ───


def _empty_memory(project_name: str = "") -> dict[str, Any]:
    """Return a blank memory dict with all required keys."""
    return {
        "project_name": project_name,
        "patterns": [],
        "corrections": [],
        "analysis_history": [],
        "component_notes": {},
        "version": MEMORY_VERSION,
    }


# ─── Persistence ───


def _memory_path(project_root: str | Path) -> Path:
    """Return the canonical path: <project>/.arch_viewer/memory.json"""
    return Path(project_root) / CONFIG_DIR / MEMORY_FILENAME


def load_memory(project_root: str | Path) -> dict[str, Any]:
    """
    Load the memory store from .arch_viewer/memory.json.

    Returns a valid memory dict even if the file is missing or corrupt.
    """
    path = _memory_path(project_root)
    if not path.exists():
        return _empty_memory(project_name=Path(project_root).name)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Failed to read memory at %s: %s — starting fresh", path, exc)
        return _empty_memory(project_name=Path(project_root).name)

    # Ensure all required keys exist (forward-compat)
    template = _empty_memory()
    for key, default in template.items():
        data.setdefault(key, default)

    return data


def save_memory(project_root: str | Path, memory: dict[str, Any]) -> None:
    """
    Persist the memory dict to .arch_viewer/memory.json.

    Creates the .arch_viewer/ directory if it does not exist.
    """
    path = _memory_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(memory, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    log.debug("Memory saved to %s (%d patterns, %d corrections)",
              path, len(memory.get("patterns", [])), len(memory.get("corrections", [])))


# ─── Pattern Management ───


def add_pattern(
    project_root: str | Path,
    category: str,
    description: str,
    confidence: float,
    source: str = "ai",
) -> dict[str, Any]:
    """
    Add a learned architecture pattern.

    Args:
        project_root: Path to the project.
        category: Pattern category — e.g. "architecture", "design-pattern",
                  "anti-pattern", "relationship", "technology".
        description: Human-readable description of the pattern.
        confidence: 0.0-1.0 confidence score.
        source: "ai" (discovered automatically) or "user" (provided by human).

    Returns:
        The newly created pattern entry.
    """
    confidence = max(0.0, min(1.0, float(confidence)))
    memory = load_memory(project_root)

    # Deduplicate — skip if an identical description already exists in the same category
    for existing in memory["patterns"]:
        if existing["category"] == category and existing["description"] == description:
            log.debug("Pattern already exists, skipping: %s", description[:80])
            return existing

    entry = {
        "category": category,
        "description": description,
        "confidence": round(confidence, 3),
        "timestamp": time.time(),
        "source": source,
    }
    memory["patterns"].append(entry)
    save_memory(project_root, memory)
    log.info("Pattern added [%s] (%.0f%%): %s", category, confidence * 100, description[:80])
    return entry


def add_correction(
    project_root: str | Path,
    original: str,
    correction: str,
) -> dict[str, Any]:
    """
    Record a user correction — the AI got something wrong and the user fixed it.

    Args:
        project_root: Path to the project.
        original: What the AI originally said/concluded.
        correction: What the correct interpretation is.

    Returns:
        The newly created correction entry.
    """
    memory = load_memory(project_root)
    entry = {
        "original": original,
        "correction": correction,
        "timestamp": time.time(),
    }
    memory["corrections"].append(entry)
    save_memory(project_root, memory)
    log.info("Correction recorded: '%s' -> '%s'", original[:60], correction[:60])
    return entry


def update_component_note(
    project_root: str | Path,
    component_name: str,
    note: str,
) -> None:
    """Store or update a free-text note about a specific component."""
    memory = load_memory(project_root)
    memory["component_notes"][component_name] = note
    save_memory(project_root, memory)


# ─── Analysis History ───


def record_analysis(
    project_root: str | Path,
    score: int,
    component_count: int,
    summary: str,
) -> dict[str, Any]:
    """
    Log an analysis run so we can track how the project evolves.

    Args:
        project_root: Path to the project.
        score: Architecture health score (0-100).
        component_count: Number of components detected.
        summary: Short summary of findings.

    Returns:
        The newly created history entry.
    """
    memory = load_memory(project_root)
    entry = {
        "timestamp": time.time(),
        "score": int(score),
        "components": int(component_count),
        "summary": summary,
    }
    memory["analysis_history"].append(entry)

    # Keep history bounded — retain the most recent 100 entries
    if len(memory["analysis_history"]) > 100:
        memory["analysis_history"] = memory["analysis_history"][-100:]

    save_memory(project_root, memory)
    log.info("Analysis recorded: score=%d, components=%d", score, component_count)
    return entry


def get_analysis_history(
    project_root: str | Path,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return the most recent analysis history entries, newest first."""
    memory = load_memory(project_root)
    history = memory.get("analysis_history", [])
    # Return newest first, capped at limit
    return list(reversed(history[-limit:]))


# ─── Context Builder ───


def get_context_for_analysis(project_root: str | Path) -> str:
    """
    Build a text summary of all remembered patterns, corrections, and history
    that the AI agent can inject into its next analysis prompt.

    Returns an empty string if no memory exists yet.
    """
    memory = load_memory(project_root)
    sections: list[str] = []

    # Patterns grouped by category
    patterns = memory.get("patterns", [])
    if patterns:
        sections.append("=== Learned Architecture Patterns ===")
        by_category: dict[str, list[dict]] = {}
        for p in patterns:
            by_category.setdefault(p["category"], []).append(p)
        for category, items in sorted(by_category.items()):
            sections.append(f"\n[{category}]")
            for item in items:
                conf = item.get("confidence", 0)
                src = item.get("source", "ai")
                sections.append(f"  - ({src}, {conf:.0%}) {item['description']}")

    # Corrections
    corrections = memory.get("corrections", [])
    if corrections:
        sections.append("\n=== User Corrections (apply these!) ===")
        for c in corrections:
            sections.append(f"  - WRONG: {c['original']}")
            sections.append(f"    RIGHT: {c['correction']}")

    # Component notes
    notes = memory.get("component_notes", {})
    if notes:
        sections.append("\n=== Component Notes ===")
        for comp, note in sorted(notes.items()):
            sections.append(f"  [{comp}]: {note}")

    # Recent analysis trend
    history = memory.get("analysis_history", [])
    if len(history) >= 2:
        recent = history[-1]
        previous = history[-2]
        delta = recent["score"] - previous["score"]
        direction = "improved" if delta > 0 else "declined" if delta < 0 else "unchanged"
        sections.append(f"\n=== Analysis Trend ===")
        sections.append(
            f"  Last score: {recent['score']}/100 ({direction} by {abs(delta)} points)"
        )
        sections.append(f"  Components: {recent['components']}")
    elif len(history) == 1:
        recent = history[-1]
        sections.append(f"\n=== Previous Analysis ===")
        sections.append(f"  Score: {recent['score']}/100, Components: {recent['components']}")

    if not sections:
        return ""

    return (
        "\n--- AI Memory (learned from previous analyses) ---\n"
        + "\n".join(sections)
        + "\n--- End AI Memory ---\n"
    )
