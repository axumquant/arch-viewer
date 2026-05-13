"""
Stack bootstrap — ensures Neo4j + Qdrant are running before the web server starts.

No flat-file fallback. If the docker services can't be brought up, arch-viewer
fails fast with a clear error message.

Public API:
    bootstrap_stack(compose_dir: Path) -> None    # raises StackError on failure
"""

from __future__ import annotations

import logging
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("arch-viewer.stack")


class StackError(RuntimeError):
    """Raised when a required service cannot be started."""


NEO4J_HEALTH_PORT = 7474
QDRANT_HEALTH_URL = "http://localhost:6333/readyz"
NEO4J_BOLT_PORT = 7687
DOCKER_START_TIMEOUT_S = 90
SERVICE_START_TIMEOUT_S = 120


def _which(cmd: str) -> str | None:
    return shutil.which(cmd)


def _docker_daemon_up() -> bool:
    """Return True if `docker ps` succeeds."""
    docker = _which("docker")
    if not docker:
        return False
    try:
        r = subprocess.run(
            [docker, "ps", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _wait_for_docker_daemon(timeout_s: int = DOCKER_START_TIMEOUT_S) -> bool:
    """Poll until docker daemon is up. Returns True on success."""
    log.info("Waiting for Docker daemon...")
    start = time.monotonic()
    while time.monotonic() - start < timeout_s:
        if _docker_daemon_up():
            log.info("Docker daemon is up.")
            return True
        time.sleep(2)
    return False


def _try_start_docker_desktop_windows() -> None:
    """On Windows, try to launch Docker Desktop if the daemon is down."""
    import os, sys
    if sys.platform != "win32":
        return
    candidates = [
        os.environ.get("ProgramFiles", r"C:\Program Files") + r"\Docker\Docker\Docker Desktop.exe",
        os.environ.get("LOCALAPPDATA", "") + r"\Docker\Docker Desktop.exe",
    ]
    for c in candidates:
        if c and Path(c).exists():
            log.info("Starting Docker Desktop: %s", c)
            try:
                subprocess.Popen([c], close_fds=True, creationflags=0x00000008)  # DETACHED_PROCESS
                return
            except Exception as e:
                log.warning("Failed to launch Docker Desktop: %s", e)


def _port_open(host: str, port: int, timeout_s: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except Exception:
        return False


def _http_ok(url: str, timeout_s: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def _wait_for(check, name: str, timeout_s: int = SERVICE_START_TIMEOUT_S) -> bool:
    """Poll a check function until it returns True or timeout."""
    log.info("Waiting for %s to become ready...", name)
    start = time.monotonic()
    while time.monotonic() - start < timeout_s:
        if check():
            log.info("%s is ready.", name)
            return True
        time.sleep(2)
    log.error("Timed out waiting for %s", name)
    return False


def _compose_up(compose_dir: Path, services: list[str]) -> None:
    """Run `docker compose up -d <services>`."""
    docker = _which("docker")
    if not docker:
        raise StackError("docker CLI not found on PATH")
    compose_file = compose_dir / "docker-compose.yml"
    if not compose_file.exists():
        raise StackError(f"docker-compose.yml not found at {compose_file}")

    cmd = [docker, "compose", "-f", str(compose_file), "up", "-d"] + services
    log.info("Running: %s", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise StackError(
            f"docker compose up failed (exit {r.returncode}):\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )


def bootstrap_stack(compose_dir: Path | None = None) -> None:
    """
    Ensure Neo4j and Qdrant are running. Auto-starts Docker Desktop on Windows
    if needed. Raises StackError on failure — no flat-file fallback.

    compose_dir: directory containing docker-compose.yml. If None, uses the
                 arch-viewer install dir.
    """
    if compose_dir is None:
        compose_dir = Path(__file__).resolve().parent.parent

    # 1. Docker daemon
    if not _docker_daemon_up():
        log.warning("Docker daemon is not running — attempting to start it.")
        _try_start_docker_desktop_windows()
        if not _wait_for_docker_daemon(DOCKER_START_TIMEOUT_S):
            raise StackError(
                "Docker daemon failed to start within "
                f"{DOCKER_START_TIMEOUT_S}s. Install Docker Desktop and start it manually, "
                "then re-launch arch-viewer."
            )

    # 2. Quick check — if Neo4j and Qdrant are already up, skip compose
    neo4j_up = _port_open("localhost", NEO4J_BOLT_PORT) and _http_ok(f"http://localhost:{NEO4J_HEALTH_PORT}")
    qdrant_up = _http_ok(QDRANT_HEALTH_URL)
    if neo4j_up and qdrant_up:
        log.info("Neo4j and Qdrant already running — skipping compose up.")
        return

    # 3. Compose up missing services
    services = []
    if not neo4j_up:
        services.append("neo4j")
    if not qdrant_up:
        services.append("qdrant")

    log.info("Starting services via docker compose: %s", services)
    _compose_up(compose_dir, services)

    # 4. Wait for health
    ok_neo4j = _wait_for(
        lambda: _port_open("localhost", NEO4J_BOLT_PORT) and _http_ok(f"http://localhost:{NEO4J_HEALTH_PORT}"),
        "Neo4j",
    )
    ok_qdrant = _wait_for(lambda: _http_ok(QDRANT_HEALTH_URL), "Qdrant")

    if not ok_neo4j:
        raise StackError(
            f"Neo4j did not become ready on bolt://localhost:{NEO4J_BOLT_PORT} "
            f"and http://localhost:{NEO4J_HEALTH_PORT} within {SERVICE_START_TIMEOUT_S}s. "
            "Check `docker compose logs neo4j`."
        )
    if not ok_qdrant:
        raise StackError(
            f"Qdrant did not become ready on {QDRANT_HEALTH_URL} within "
            f"{SERVICE_START_TIMEOUT_S}s. Check `docker compose logs qdrant`."
        )

    log.info("Stack ready: Neo4j on bolt://localhost:%d, Qdrant on http://localhost:6333",
             NEO4J_BOLT_PORT)
