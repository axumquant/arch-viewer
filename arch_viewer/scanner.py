"""
Static code scanner — builds architecture model WITHOUT AI.
This is the fast, deterministic baseline. The AI agent enriches it.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .models import (
    APIRoute,
    Architecture,
    Component,
    ComponentType,
    DataFlow,
    DataFlowDirection,
    DependencyInfo,
    FileInfo,
)

IGNORE_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
    ".next", ".nuxt", "dist", "build", ".turbo", ".cache",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "htmlcov",
    "coverage", ".tox", ".terraform", ".svn",
}

EXT_LANG = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".json": "json", ".yaml": "yaml",
    ".yml": "yaml", ".toml": "toml", ".md": "markdown", ".html": "html",
    ".css": "css", ".scss": "scss", ".sql": "sql", ".sh": "shell",
    ".bash": "shell", ".graphql": "graphql", ".prisma": "prisma",
    ".vue": "vue", ".svelte": "svelte", ".rs": "rust", ".go": "go",
    ".java": "java", ".rb": "ruby", ".php": "php", ".cs": "csharp",
    ".cpp": "cpp", ".c": "c", ".h": "c", ".hpp": "cpp", ".kt": "kotlin",
    ".swift": "swift", ".r": "r", ".jl": "julia",
}

EXT_CATEGORY = {
    ".py": "backend", ".pyi": "backend",
    ".tsx": "frontend", ".jsx": "frontend",
    ".ts": "frontend", ".js": "frontend",
    ".vue": "frontend", ".svelte": "frontend",
    ".json": "config", ".yaml": "config", ".yml": "config",
    ".toml": "config", ".ini": "config", ".cfg": "config",
    ".css": "style", ".scss": "style", ".sass": "style", ".less": "style",
    ".html": "template", ".ejs": "template", ".hbs": "template",
    ".sql": "data", ".graphql": "data", ".prisma": "data",
    ".md": "docs", ".rst": "docs", ".txt": "docs",
    ".sh": "shell", ".bash": "shell", ".ps1": "shell",
}


def scan_project(root: str | Path, max_files: int = 5000) -> Architecture:
    """Full static scan — produces an Architecture model ready for AI enrichment."""
    root = Path(root).resolve()
    project_name = root.name

    all_files: list[FileInfo] = []
    tree: dict = {}
    file_count = 0

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune ignored dirs
        dirnames[:] = [
            d for d in dirnames
            if d not in IGNORE_DIRS and not (d.startswith(".") and d not in (".claude", ".github"))
        ]

        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            rel_dir = ""

        for fname in sorted(filenames):
            if file_count >= max_files:
                break

            fpath = os.path.join(dirpath, fname)
            rel_path = os.path.join(rel_dir, fname).replace("\\", "/") if rel_dir else fname

            try:
                stat = os.stat(fpath)
            except OSError:
                continue

            ext = os.path.splitext(fname)[1].lower()
            lang = EXT_LANG.get(ext, "text")
            cat = EXT_CATEGORY.get(ext, "other")

            # Special cases
            if fname == "Dockerfile":
                cat = "docker"
            elif fname.startswith("docker-compose"):
                cat = "docker"
            elif ".github" in rel_path:
                cat = "ci"

            fi = FileInfo(
                path=rel_path,
                language=lang,
                category=cat,
                size=stat.st_size,
                modified=stat.st_mtime,
            )

            # Extract imports for Python files (lightweight)
            if lang == "python" and stat.st_size < 500_000:
                fi.imports = _extract_python_imports(fpath)

            all_files.append(fi)
            file_count += 1

            # Build nested tree dict
            _insert_tree(tree, rel_path.split("/"), fi)

    components = _detect_components(root, all_files)
    data_flows = _infer_data_flows(components, all_files)
    deps = _collect_dependencies(root)
    stats = _compute_stats(all_files)

    return Architecture(
        project_name=project_name,
        components=components,
        data_flows=data_flows,
        dependencies=deps,
        file_tree=tree,
        stats=stats,
    )


def _extract_python_imports(fpath: str) -> list[str]:
    """Fast regex-based import extraction."""
    imports = []
    try:
        with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line.startswith("import "):
                    imports.append(line.split()[1].split(".")[0])
                elif line.startswith("from "):
                    parts = line.split()
                    if len(parts) >= 2:
                        imports.append(parts[1].split(".")[0])
                elif line and not line.startswith("#") and not line.startswith('"""'):
                    # Stop after we pass the import block
                    if not any(line.startswith(kw) for kw in ("import", "from", "#", '"""', "'''", "@")):
                        break
    except Exception:
        pass
    return list(set(imports))


def _insert_tree(tree: dict, parts: list[str], fi: FileInfo):
    """Insert a file into the nested tree dict."""
    node = tree
    for i, part in enumerate(parts):
        if i == len(parts) - 1:
            node[part] = {"_file": fi.model_dump()}
        else:
            if part not in node:
                node[part] = {}
            node = node[part]


def _detect_components(root: Path, files: list[FileInfo]) -> list[Component]:
    """Auto-detect project components from directory structure and files."""
    components: list[Component] = []

    # Backend detection
    for dirname in ("backend", "server", "api", "src"):
        dirpath = root / dirname
        if dirpath.is_dir():
            tech = _detect_backend_tech(dirpath)
            if tech:
                comp_files = [f.path for f in files if f.path.startswith(dirname + "/")]
                routes = _extract_routes(root, comp_files)
                components.append(Component(
                    name=dirname.capitalize(),
                    type=ComponentType.BACKEND,
                    path=dirname,
                    tech_stack=tech,
                    files=comp_files,
                    api_routes=routes,
                    entry_points=_find_entry_points(root / dirname, "backend"),
                ))
                break

    # Check root-level Python project
    if not any(c.type == ComponentType.BACKEND for c in components):
        if (root / "pyproject.toml").exists() or (root / "setup.py").exists():
            app_dir = root / "app"
            if app_dir.is_dir():
                tech = _detect_backend_tech(root)
                comp_files = [f.path for f in files if f.path.startswith("app/")]
                components.append(Component(
                    name="App",
                    type=ComponentType.BACKEND,
                    path=".",
                    tech_stack=tech,
                    files=comp_files,
                    entry_points=_find_entry_points(root, "backend"),
                ))

    # Frontend / Portal detection
    for dirname in ("portal", "frontend", "web", "app", "client", "ui", "dashboard"):
        pkg_path = root / dirname / "package.json"
        if pkg_path.exists():
            tech = _detect_frontend_tech(pkg_path)
            comp_files = [f.path for f in files if f.path.startswith(dirname + "/")]
            components.append(Component(
                name=dirname.capitalize(),
                type=ComponentType.FRONTEND,
                path=dirname,
                tech_stack=tech,
                files=comp_files,
                entry_points=_find_entry_points(root / dirname, "frontend"),
            ))
            break

    # Chrome extension detection
    for dirname in ("chrome-extension", "extension", "ext", "browser-extension"):
        manifest = root / dirname / "manifest.json"
        if manifest.exists():
            tech = ["Chrome MV3"]
            try:
                m = json.loads(manifest.read_text())
                if m.get("manifest_version") == 2:
                    tech = ["Chrome MV2"]
            except Exception:
                pass
            comp_files = [f.path for f in files if f.path.startswith(dirname + "/")]
            components.append(Component(
                name="Extension",
                type=ComponentType.EXTENSION,
                path=dirname,
                tech_stack=tech,
                files=comp_files,
            ))
            break

    # Database detection
    for pattern in ("**/alembic.ini", "**/prisma/schema.prisma", "**/migrations/**"):
        if list(root.glob(pattern)):
            components.append(Component(
                name="Database",
                type=ComponentType.DATABASE,
                path="migrations",
                tech_stack=_detect_db_tech(root),
            ))
            break

    # Docker detection
    dockerfiles = list(root.glob("**/Dockerfile")) + list(root.glob("**/docker-compose*"))
    if dockerfiles:
        comp_files = [str(p.relative_to(root)).replace("\\", "/") for p in dockerfiles]
        components.append(Component(
            name="Docker",
            type=ComponentType.DOCKER,
            path=".",
            tech_stack=["Docker", "Docker Compose"] if any("compose" in str(p) for p in dockerfiles) else ["Docker"],
            files=comp_files,
        ))

    # CI/CD detection
    gh_workflows = root / ".github" / "workflows"
    if gh_workflows.is_dir():
        comp_files = [f.path for f in files if ".github/workflows" in f.path]
        components.append(Component(
            name="CI/CD",
            type=ComponentType.CI_CD,
            path=".github/workflows",
            tech_stack=["GitHub Actions"],
            files=comp_files,
        ))

    # Claude / MCP config
    claude_dir = root / ".claude"
    if claude_dir.is_dir():
        comp_files = [f.path for f in files if f.path.startswith(".claude/")]
        components.append(Component(
            name="Claude Config",
            type=ComponentType.CONFIG,
            path=".claude",
            tech_stack=["Claude Code", "MCP"],
            files=comp_files,
        ))

    return components


def _detect_backend_tech(dirpath: Path) -> list[str]:
    tech = []
    pyproject = dirpath / "pyproject.toml"
    requirements = dirpath / "requirements.txt"

    text = ""
    if pyproject.exists():
        text = pyproject.read_text(errors="ignore")
    elif requirements.exists():
        text = requirements.read_text(errors="ignore")

    if "fastapi" in text.lower():
        tech.append("FastAPI")
    if "django" in text.lower():
        tech.append("Django")
    if "flask" in text.lower():
        tech.append("Flask")
    if "sqlalchemy" in text.lower():
        tech.append("SQLAlchemy")
    if "pydantic" in text.lower():
        tech.append("Pydantic")
    if "celery" in text.lower():
        tech.append("Celery")
    if "redis" in text.lower():
        tech.append("Redis")
    if not tech:
        tech.append("Python")
    return tech


def _detect_frontend_tech(pkg_json: Path) -> list[str]:
    tech = []
    try:
        pkg = json.loads(pkg_json.read_text())
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        if "next" in deps:
            tech.append("Next.js")
        if "react" in deps:
            tech.append("React")
        if "vue" in deps:
            tech.append("Vue")
        if "svelte" in deps or "@sveltejs/kit" in deps:
            tech.append("Svelte")
        if "@angular/core" in deps:
            tech.append("Angular")
        if "tailwindcss" in deps:
            tech.append("Tailwind")
        if "typescript" in deps:
            tech.append("TypeScript")
    except Exception:
        tech.append("Node.js")
    return tech or ["Node.js"]


def _detect_db_tech(root: Path) -> list[str]:
    tech = []
    if (root / "alembic.ini").exists() or list(root.glob("**/alembic/**")):
        tech.append("Alembic")
    if list(root.glob("**/prisma/schema.prisma")):
        tech.append("Prisma")
    # Check for DB URLs in configs
    for cfg in root.glob("**/.env*"):
        try:
            text = cfg.read_text(errors="ignore")
            if "postgres" in text.lower():
                tech.append("PostgreSQL")
            if "sqlite" in text.lower():
                tech.append("SQLite")
            if "mysql" in text.lower():
                tech.append("MySQL")
        except Exception:
            pass
    return tech or ["SQL"]


def _extract_routes(root: Path, files: list[str]) -> list[APIRoute]:
    """Extract API routes from Python FastAPI/Flask files."""
    routes: list[APIRoute] = []
    route_pattern = re.compile(
        r'@(?:app|router)\.(get|post|put|delete|patch|websocket)\s*\(\s*["\']([^"\']+)["\']'
    )

    for fpath in files:
        if not fpath.endswith(".py"):
            continue
        abs_path = root / fpath
        try:
            content = abs_path.read_text(errors="ignore")
            for match in route_pattern.finditer(content):
                method = match.group(1).upper()
                path = match.group(2)
                routes.append(APIRoute(method=method, path=path, file=fpath))
        except Exception:
            continue

    return routes


def _find_entry_points(dirpath: Path, comp_type: str) -> list[str]:
    entries = []
    if comp_type == "backend":
        for name in ("main.py", "app.py", "server.py", "wsgi.py", "asgi.py"):
            for p in dirpath.rglob(name):
                entries.append(str(p.relative_to(dirpath)).replace("\\", "/"))
    elif comp_type == "frontend":
        for name in ("page.tsx", "page.jsx", "index.tsx", "index.jsx", "App.tsx", "App.jsx"):
            for p in dirpath.rglob(name):
                entries.append(str(p.relative_to(dirpath)).replace("\\", "/"))
    return entries[:10]


def _infer_data_flows(components: list[Component], files: list[FileInfo]) -> list[DataFlow]:
    """Infer data flows between components based on imports and known patterns."""
    flows: list[DataFlow] = []
    comp_names = {c.name for c in components}

    # Frontend → Backend (if both exist)
    frontend = next((c for c in components if c.type == ComponentType.FRONTEND), None)
    backend = next((c for c in components if c.type == ComponentType.BACKEND), None)
    if frontend and backend:
        flows.append(DataFlow(
            source=frontend.name,
            target=backend.name,
            protocol="HTTP/REST",
            description="API calls",
            direction=DataFlowDirection.BIDIRECTIONAL,
        ))

    # Extension → Backend (if both exist)
    ext = next((c for c in components if c.type == ComponentType.EXTENSION), None)
    if ext and backend:
        flows.append(DataFlow(
            source=ext.name,
            target=backend.name,
            protocol="WebSocket",
            description="Real-time messaging",
            direction=DataFlowDirection.BIDIRECTIONAL,
        ))

    # Backend → Database
    db = next((c for c in components if c.type == ComponentType.DATABASE), None)
    if backend and db:
        flows.append(DataFlow(
            source=backend.name,
            target=db.name,
            protocol="SQL",
            description="Data persistence",
            direction=DataFlowDirection.BIDIRECTIONAL,
        ))

    return flows


def _collect_dependencies(root: Path) -> list[DependencyInfo]:
    """Collect top-level dependencies from package files."""
    deps: list[DependencyInfo] = []

    # Python
    for pyproject in root.rglob("pyproject.toml"):
        if "node_modules" in str(pyproject):
            continue
        try:
            text = pyproject.read_text(errors="ignore")
            # Simple regex extraction — not a full TOML parser
            in_deps = False
            for line in text.split("\n"):
                if "dependencies" in line and "=" in line and "[" in line:
                    in_deps = True
                    continue
                if in_deps:
                    if line.strip().startswith("]"):
                        in_deps = False
                        continue
                    match = re.match(r'\s*"([^">=<\[]+)', line)
                    if match:
                        deps.append(DependencyInfo(name=match.group(1).strip(), category="python"))
        except Exception:
            pass

    # Node
    for pkg_json in root.rglob("package.json"):
        if "node_modules" in str(pkg_json):
            continue
        try:
            pkg = json.loads(pkg_json.read_text())
            for name in pkg.get("dependencies", {}):
                deps.append(DependencyInfo(
                    name=name,
                    version=pkg["dependencies"][name],
                    category="node-runtime",
                ))
            for name in pkg.get("devDependencies", {}):
                deps.append(DependencyInfo(
                    name=name,
                    version=pkg["devDependencies"][name],
                    category="node-dev",
                ))
        except Exception:
            pass

    return deps


def _compute_stats(files: list[FileInfo]) -> dict:
    by_cat: dict[str, int] = {}
    by_lang: dict[str, int] = {}
    total_size = 0

    for f in files:
        by_cat[f.category] = by_cat.get(f.category, 0) + 1
        by_lang[f.language] = by_lang.get(f.language, 0) + 1
        total_size += f.size

    return {
        "total_files": len(files),
        "total_size": total_size,
        "by_category": by_cat,
        "by_language": by_lang,
    }
