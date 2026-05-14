"""
Neo4j-backed graph store for arch-viewer.

Persists component, dependency, and data-flow nodes/edges into a local Neo4j
instance so projects can be queried and visualized at http://localhost:7474.

Designed to fail soft: if the `neo4j` package is missing or the database is
unreachable, every method becomes a no-op and a warning is logged. The flat
file memory at `.arch_viewer/memory.json` keeps working regardless.

Project isolation: every node is tagged with a deterministic `project_id`
(hash of the absolute project root) so multiple scanned projects can share
one Neo4j without colliding.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("arch-viewer.graph_store")


def _project_id(project_root: str | Path) -> str:
    """Deterministic short hash for a project root path."""
    abs_path = str(Path(project_root).resolve()).lower()
    return hashlib.sha1(abs_path.encode("utf-8")).hexdigest()[:12]


# Passwords tried in order for localhost connections — no env var required.
_DEFAULT_PASSWORDS = ["archviewer123", "neo4j", "password", "neo4j123", ""]


class GraphStore:
    """
    Lazy wrapper around the official `neo4j` Python driver.

    Auto-tries common local Neo4j passwords so you don't need to set
    NEO4J_PASSWORD for a localhost instance. Pass an explicit password
    or set NEO4J_PASSWORD to override.

    Usage:
        gs = GraphStore(project_root="/path/to/proj")
        if gs.connect():
            gs.upsert_component({...})
        gs.close()
    """

    def __init__(
        self,
        project_root: str | Path,
        uri: str | None = None,
        user: str | None = None,
        password: str | None = None,
    ):
        self.project_root = Path(project_root).resolve()
        self.project_id = _project_id(self.project_root)
        self.uri = uri or os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        self.user = user or os.environ.get("NEO4J_USER", "neo4j")
        # password=None means "try all defaults"; explicit value skips the loop.
        self._explicit_password: str | None = password or os.environ.get("NEO4J_PASSWORD")
        self.password: str = self._explicit_password or _DEFAULT_PASSWORDS[0]
        self._driver: Any = None
        self._available: bool = False

    # ─── Lifecycle ───

    def connect(self) -> bool:
        """
        Open a Neo4j driver and verify connectivity.

        If no explicit password is configured, cycles through _DEFAULT_PASSWORDS
        until one works — covers fresh Docker installs, custom setups, and
        passwordless Neo4j Community without requiring any env var.

        Returns True on success, False otherwise (silent no-op mode).
        """
        if self._driver is not None:
            return self._available

        try:
            from neo4j import GraphDatabase  # type: ignore
        except ImportError:
            log.info("neo4j package not installed — GraphStore disabled. "
                     "Install with: pip install -e '.[graph]'")
            self._available = False
            return False

        # Build ordered list of passwords to attempt
        if self._explicit_password is not None:
            attempts = [self._explicit_password]
        else:
            attempts = list(_DEFAULT_PASSWORDS)

        last_exc: Exception | None = None
        for pwd in attempts:
            auth = (self.user, pwd) if pwd else None
            driver = None
            try:
                driver = GraphDatabase.driver(self.uri, auth=auth)
                driver.verify_connectivity()
                self._driver = driver
                self._available = True
                self.password = pwd
                log.info("Connected to Neo4j at %s (project_id=%s)", self.uri, self.project_id)
                self._ensure_constraints()
                return True
            except Exception as exc:
                last_exc = exc
                if driver is not None:
                    try:
                        driver.close()
                    except Exception:
                        pass

        log.warning(
            "Could not connect to Neo4j at %s (tried %d passwords): %s — graph store disabled",
            self.uri, len(attempts), last_exc,
        )
        self._driver = None
        self._available = False
        return False

    def close(self) -> None:
        if self._driver is not None:
            try:
                self._driver.close()
            except Exception:
                pass
            self._driver = None
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    # ─── Schema ───

    def _ensure_constraints(self) -> None:
        """Create idempotent constraints / indexes once per session."""
        if not self._available:
            return
        try:
            with self._driver.session() as session:
                session.run(
                    "CREATE CONSTRAINT component_key IF NOT EXISTS "
                    "FOR (c:Component) REQUIRE (c.project_id, c.name) IS UNIQUE"
                )
                session.run(
                    "CREATE CONSTRAINT dependency_key IF NOT EXISTS "
                    "FOR (d:Dependency) REQUIRE (d.project_id, d.name) IS UNIQUE"
                )
                session.run(
                    "CREATE INDEX component_type IF NOT EXISTS "
                    "FOR (c:Component) ON (c.type)"
                )
        except Exception as exc:
            log.debug("Constraint setup skipped: %s", exc)

    # ─── Upserts ───

    def upsert_component(self, comp: dict[str, Any] | Any) -> None:
        """
        Create or update a Component node.

        Accepts either a dict or a pydantic model with attributes
        `name`, `type`, `path`, `tech_stack`, `description`.
        """
        if not self._available:
            return
        c = _to_dict(comp)
        name = c.get("name")
        if not name:
            return
        ctype = _str_enum(c.get("type", "other"))
        try:
            with self._driver.session() as session:
                session.run(
                    """
                    MERGE (c:Component {project_id: $pid, name: $name})
                    SET c.type = $type,
                        c.path = $path,
                        c.tech_stack = $tech,
                        c.description = $desc,
                        c.file_count = $files,
                        c.updated_at = timestamp()
                    """,
                    pid=self.project_id,
                    name=name,
                    type=ctype,
                    path=c.get("path", ""),
                    tech=list(c.get("tech_stack", []) or []),
                    desc=c.get("description", "") or "",
                    files=len(c.get("files", []) or []),
                )
        except Exception as exc:
            log.debug("upsert_component(%s) failed: %s", name, exc)

    def upsert_dependency(self, dep: dict[str, Any] | Any) -> None:
        """
        Create a Dependency node and link it to the project.

        Accepts dict / pydantic with `name`, `version`, `category`.
        """
        if not self._available:
            return
        d = _to_dict(dep)
        name = d.get("name")
        if not name:
            return
        try:
            with self._driver.session() as session:
                session.run(
                    """
                    MERGE (d:Dependency {project_id: $pid, name: $name})
                    SET d.version = $ver,
                        d.category = $cat,
                        d.updated_at = timestamp()
                    """,
                    pid=self.project_id,
                    name=name,
                    ver=d.get("version", "") or "",
                    cat=d.get("category", "") or "",
                )
        except Exception as exc:
            log.debug("upsert_dependency(%s) failed: %s", name, exc)

    def upsert_data_flow(self, flow: dict[str, Any] | Any) -> None:
        """
        Create a FLOWS_TO relationship between two existing Component nodes.

        Accepts dict / pydantic with `source`, `target`, `protocol`,
        `description`, `direction`.
        """
        if not self._available:
            return
        f = _to_dict(flow)
        source = f.get("source")
        target = f.get("target")
        if not source or not target:
            return
        direction = _str_enum(f.get("direction", "unidirectional"))
        try:
            with self._driver.session() as session:
                # Ensure endpoints exist (lightweight stubs if missing)
                session.run(
                    "MERGE (a:Component {project_id: $pid, name: $src}) "
                    "MERGE (b:Component {project_id: $pid, name: $tgt}) "
                    "MERGE (a)-[r:FLOWS_TO {project_id: $pid}]->(b) "
                    "SET r.protocol = $proto, r.description = $desc, "
                    "    r.direction = $dir, r.updated_at = timestamp()",
                    pid=self.project_id,
                    src=source,
                    tgt=target,
                    proto=f.get("protocol", "") or "",
                    desc=f.get("description", "") or "",
                    dir=direction,
                )
        except Exception as exc:
            log.debug("upsert_data_flow(%s→%s) failed: %s", source, target, exc)

    # ─── Queries ───

    def query_component(self, name: str) -> dict[str, Any] | None:
        if not self._available:
            return None
        try:
            with self._driver.session() as session:
                rec = session.run(
                    "MATCH (c:Component {project_id: $pid, name: $name}) "
                    "RETURN c LIMIT 1",
                    pid=self.project_id, name=name,
                ).single()
                if rec is None:
                    return None
                return dict(rec["c"])
        except Exception as exc:
            log.debug("query_component(%s) failed: %s", name, exc)
            return None

    def find_similar_components(self, type_: str, limit: int = 5) -> list[dict[str, Any]]:
        """Return components of the same type within this project."""
        if not self._available:
            return []
        try:
            with self._driver.session() as session:
                result = session.run(
                    "MATCH (c:Component {project_id: $pid, type: $type}) "
                    "RETURN c LIMIT $limit",
                    pid=self.project_id, type=type_, limit=int(limit),
                )
                return [dict(r["c"]) for r in result]
        except Exception as exc:
            log.debug("find_similar_components(%s) failed: %s", type_, exc)
            return []

    def clear_project(self, project_path: str | Path | None = None) -> None:
        """Delete every node / edge tagged with this project's id."""
        if not self._available:
            return
        pid = (
            _project_id(project_path) if project_path is not None
            else self.project_id
        )
        try:
            with self._driver.session() as session:
                session.run(
                    "MATCH (n {project_id: $pid}) DETACH DELETE n",
                    pid=pid,
                )
            log.info("Cleared Neo4j graph for project_id=%s", pid)
        except Exception as exc:
            log.warning("clear_project(%s) failed: %s", pid, exc)


# ─── helpers ───


def _to_dict(obj: Any) -> dict[str, Any]:
    """Coerce a pydantic model or arbitrary object into a plain dict."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass
    if hasattr(obj, "dict"):
        try:
            return obj.dict()
        except Exception:
            pass
    # Fallback: scrape public attributes
    return {k: getattr(obj, k) for k in dir(obj)
            if not k.startswith("_") and not callable(getattr(obj, k, None))}


def _str_enum(value: Any) -> str:
    """Convert an enum or string to its plain string value."""
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)
