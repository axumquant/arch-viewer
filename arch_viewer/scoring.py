"""
Architecture Scoring Engine — produces a 0-100 health score with
category breakdowns and actionable recommendations.

Scoring categories:
  1. Modularity      (0-25) — component separation, cohesion, coupling
  2. Code Quality    (0-25) — complexity, documentation, test coverage
  3. Maintainability (0-25) — file sizes, dependency hygiene, naming
  4. Structure       (0-25) — file organization, entry points, patterns

Anti-pattern detection runs as part of scoring and flags specific issues.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from .ast_analyzer import FileAnalysis
from .models import Architecture, Component

log = logging.getLogger("arch-viewer.scoring")


@dataclass
class AntiPattern:
    """A detected anti-pattern in the codebase."""
    name: str
    severity: str  # "critical", "warning", "info"
    description: str
    file: str = ""
    line: int = 0
    suggestion: str = ""


@dataclass
class CategoryScore:
    """Score for one category with reasoning."""
    name: str
    score: int  # 0-25
    max_score: int = 25
    details: list[str] = field(default_factory=list)
    penalties: list[str] = field(default_factory=list)


@dataclass
class ArchitectureScore:
    """Complete architecture health score."""
    total: int = 0  # 0-100
    grade: str = "F"
    categories: list[CategoryScore] = field(default_factory=list)
    anti_patterns: list[AntiPattern] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    file_count: int = 0
    analyzed_count: int = 0

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "grade": self.grade,
            "categories": [
                {
                    "name": c.name,
                    "score": c.score,
                    "max_score": c.max_score,
                    "details": c.details,
                    "penalties": c.penalties,
                }
                for c in self.categories
            ],
            "anti_patterns": [
                {
                    "name": ap.name,
                    "severity": ap.severity,
                    "description": ap.description,
                    "file": ap.file,
                    "line": ap.line,
                    "suggestion": ap.suggestion,
                }
                for ap in self.anti_patterns
            ],
            "recommendations": self.recommendations,
            "file_count": self.file_count,
            "analyzed_count": self.analyzed_count,
        }


def score_architecture(
    arch: Architecture,
    analyses: dict[str, FileAnalysis],
    root: str | Path,
) -> ArchitectureScore:
    """
    Score a project's architecture health on a 0-100 scale.
    Requires both the Architecture model and AST analyses.
    """
    root = Path(root).resolve()
    result = ArchitectureScore(
        file_count=arch.stats.get("total_files", 0),
        analyzed_count=len(analyses),
    )

    # Score each category
    modularity = _score_modularity(arch, analyses)
    quality = _score_code_quality(analyses)
    maintainability = _score_maintainability(arch, analyses, root)
    structure = _score_structure(arch, analyses, root)

    result.categories = [modularity, quality, maintainability, structure]
    result.total = sum(c.score for c in result.categories)
    result.grade = _grade_from_score(result.total)

    # Anti-pattern detection
    result.anti_patterns = _detect_anti_patterns(arch, analyses, root)

    # Penalties from anti-patterns
    critical_count = sum(1 for ap in result.anti_patterns if ap.severity == "critical")
    warning_count = sum(1 for ap in result.anti_patterns if ap.severity == "warning")
    penalty = min(15, critical_count * 5 + warning_count * 2)
    if penalty > 0:
        result.total = max(0, result.total - penalty)
        result.grade = _grade_from_score(result.total)

    # Generate recommendations
    result.recommendations = _generate_recommendations(result)

    return result


# ──────────────────────────────────────────
# Scoring Categories
# ──────────────────────────────────────────


def _score_modularity(arch: Architecture, analyses: dict[str, FileAnalysis]) -> CategoryScore:
    """Score component separation and cohesion."""
    cat = CategoryScore(name="Modularity", score=25)

    # Fewer than 2 components → probably a monolith blob
    comp_count = len(arch.components)
    if comp_count == 0:
        cat.score -= 10
        cat.penalties.append("No components detected — project may lack clear structure")
    elif comp_count == 1:
        cat.score -= 5
        cat.penalties.append("Only 1 component — consider separating concerns")
    else:
        cat.details.append(f"{comp_count} components detected")

    # Check for data flows (components that talk to each other)
    if arch.data_flows:
        cat.details.append(f"{len(arch.data_flows)} data flows mapped")
    elif comp_count > 1:
        cat.score -= 3
        cat.penalties.append("Multiple components but no data flows detected")

    # Check import coupling — files importing from too many different dirs
    high_coupling_files = 0
    for path, analysis in analyses.items():
        external_modules = set()
        for imp in analysis.imports:
            if not imp.is_relative and imp.module:
                top_module = imp.module.split(".")[0]
                external_modules.add(top_module)
        if len(external_modules) > 15:
            high_coupling_files += 1

    if high_coupling_files > 5:
        cat.score -= 5
        cat.penalties.append(f"{high_coupling_files} files have >15 external imports (high coupling)")
    elif high_coupling_files > 0:
        cat.score -= 2
        cat.penalties.append(f"{high_coupling_files} files have high import coupling")

    cat.score = max(0, cat.score)
    return cat


def _score_code_quality(analyses: dict[str, FileAnalysis]) -> CategoryScore:
    """Score complexity, documentation, and code health."""
    cat = CategoryScore(name="Code Quality", score=25)

    if not analyses:
        cat.score = 5
        cat.penalties.append("No analyzable files found")
        return cat

    # Complexity analysis
    total_complexity = 0
    complex_functions = 0
    very_complex_functions = 0
    total_functions = 0

    for analysis in analyses.values():
        for func in analysis.functions:
            total_functions += 1
            total_complexity += func.complexity
            if func.complexity > 10:
                complex_functions += 1
            if func.complexity > 20:
                very_complex_functions += 1

    if very_complex_functions > 0:
        cat.score -= min(8, very_complex_functions * 2)
        cat.penalties.append(
            f"{very_complex_functions} functions with complexity >20 (very complex)"
        )
    if complex_functions > 5:
        cat.score -= min(5, (complex_functions - 5))
        cat.penalties.append(
            f"{complex_functions} functions with complexity >10"
        )

    if total_functions > 0:
        avg_complexity = total_complexity / total_functions
        cat.details.append(f"Average function complexity: {avg_complexity:.1f}")
        if avg_complexity < 5:
            cat.details.append("Low average complexity — good")
        elif avg_complexity > 10:
            cat.score -= 3
            cat.penalties.append(f"High average complexity: {avg_complexity:.1f}")

    # Comment ratio
    total_code = sum(a.code_lines for a in analyses.values())
    total_comments = sum(a.comment_lines for a in analyses.values())
    if total_code > 0:
        comment_ratio = total_comments / total_code
        if comment_ratio < 0.02:
            cat.score -= 3
            cat.penalties.append("Very low comment ratio (<2%) — consider adding documentation")
        elif comment_ratio > 0.05:
            cat.details.append(f"Comment ratio: {comment_ratio:.0%} — well documented")

    cat.score = max(0, cat.score)
    return cat


def _score_maintainability(
    arch: Architecture, analyses: dict[str, FileAnalysis], root: Path
) -> CategoryScore:
    """Score file sizes, dependency hygiene, naming consistency."""
    cat = CategoryScore(name="Maintainability", score=25)

    # Large file detection
    large_files = 0
    very_large_files = 0
    for analysis in analyses.values():
        if analysis.total_lines > 500:
            large_files += 1
        if analysis.total_lines > 1000:
            very_large_files += 1

    if very_large_files > 3:
        cat.score -= min(8, very_large_files * 2)
        cat.penalties.append(
            f"{very_large_files} files >1000 lines — consider splitting"
        )
    elif large_files > 10:
        cat.score -= 3
        cat.penalties.append(f"{large_files} files >500 lines")

    # Dependency count
    dep_count = len(arch.dependencies)
    if dep_count > 100:
        cat.score -= 5
        cat.penalties.append(f"{dep_count} dependencies — dependency bloat risk")
    elif dep_count > 50:
        cat.score -= 2
        cat.penalties.append(f"{dep_count} dependencies")
    else:
        cat.details.append(f"{dep_count} dependencies — manageable")

    # Check for README
    has_readme = (root / "README.md").exists() or (root / "readme.md").exists()
    if has_readme:
        cat.details.append("README.md present")
    else:
        cat.score -= 3
        cat.penalties.append("No README.md — documentation gap")

    # Check for tests
    has_tests = False
    for d in ("tests", "test", "__tests__", "spec"):
        if (root / d).is_dir():
            has_tests = True
            break
    if not has_tests:
        # Check for test files scattered in source
        for path in analyses:
            if "test" in path.lower():
                has_tests = True
                break

    if has_tests:
        cat.details.append("Test directory/files found")
    else:
        cat.score -= 5
        cat.penalties.append("No test directory found — testing gap")

    cat.score = max(0, cat.score)
    return cat


def _score_structure(
    arch: Architecture, analyses: dict[str, FileAnalysis], root: Path
) -> CategoryScore:
    """Score file organization, entry points, patterns."""
    cat = CategoryScore(name="Structure", score=25)

    # Entry points
    entry_points = 0
    for comp in arch.components:
        entry_points += len(comp.entry_points)

    if entry_points > 0:
        cat.details.append(f"{entry_points} entry points detected")
    else:
        cat.score -= 3
        cat.penalties.append("No clear entry points detected")

    # API routes
    total_routes = sum(len(c.api_routes) for c in arch.components)
    if total_routes > 0:
        cat.details.append(f"{total_routes} API routes detected")

    # File organization — check for flat vs nested structure
    depths = []
    for path in analyses:
        depth = path.count("/")
        depths.append(depth)

    if depths:
        avg_depth = sum(depths) / len(depths)
        max_depth = max(depths)

        if avg_depth < 0.5 and len(analyses) > 20:
            cat.score -= 5
            cat.penalties.append("Very flat structure — consider organizing into subdirectories")
        elif max_depth > 8:
            cat.score -= 3
            cat.penalties.append(f"Max nesting depth {max_depth} — deeply nested files")
        else:
            cat.details.append(f"Average directory depth: {avg_depth:.1f}")

    # Check for config files at root
    config_files = []
    for fname in ("pyproject.toml", "package.json", "tsconfig.json", "Dockerfile"):
        if (root / fname).exists():
            config_files.append(fname)
    if config_files:
        cat.details.append(f"Config files: {', '.join(config_files)}")

    # Check for .gitignore
    if (root / ".gitignore").exists():
        cat.details.append(".gitignore present")
    else:
        cat.score -= 2
        cat.penalties.append("No .gitignore — risk of committing unwanted files")

    cat.score = max(0, cat.score)
    return cat


# ──────────────────────────────────────────
# Anti-Pattern Detection
# ──────────────────────────────────────────


def _detect_anti_patterns(
    arch: Architecture,
    analyses: dict[str, FileAnalysis],
    root: Path,
) -> list[AntiPattern]:
    """Detect architectural anti-patterns."""
    patterns: list[AntiPattern] = []

    # 1. Circular imports
    patterns.extend(_detect_circular_imports(analyses))

    # 2. God files (files with too many classes/functions)
    patterns.extend(_detect_god_files(analyses))

    # 3. Orphaned modules (files that nothing imports)
    patterns.extend(_detect_orphaned_modules(analyses))

    # 4. Deep nesting
    patterns.extend(_detect_deep_nesting(analyses))

    # 5. Mixed concerns (e.g., frontend code in backend dirs)
    patterns.extend(_detect_mixed_concerns(arch, analyses))

    return patterns


def _detect_circular_imports(analyses: dict[str, FileAnalysis]) -> list[AntiPattern]:
    """Detect circular import chains between project files."""
    patterns = []

    # Build import graph (file → set of files it imports)
    import_graph: dict[str, set[str]] = {}
    file_modules: dict[str, str] = {}  # module name → file path

    for path, analysis in analyses.items():
        # Convert file path to module name
        module = path.replace("/", ".").replace("\\", ".")
        if module.endswith(".py"):
            module = module[:-3]
        if module.endswith(".__init__"):
            module = module[:-9]
        file_modules[module] = path

        import_graph[path] = set()
        for imp in analysis.imports:
            if imp.is_relative:
                # Try to resolve relative import
                parent = "/".join(path.split("/")[:-1])
                candidate = f"{parent}/{imp.module.replace('.', '/')}.py"
                if candidate in analyses:
                    import_graph[path].add(candidate)

    # Find cycles (simple 2-node cycles — A imports B and B imports A)
    checked = set()
    for file_a, imports_a in import_graph.items():
        for file_b in imports_a:
            if file_b in import_graph and file_a in import_graph.get(file_b, set()):
                pair = tuple(sorted([file_a, file_b]))
                if pair not in checked:
                    checked.add(pair)
                    patterns.append(AntiPattern(
                        name="Circular Import",
                        severity="warning",
                        description=f"Circular import between {file_a} and {file_b}",
                        file=file_a,
                        suggestion="Extract shared code into a separate module to break the cycle",
                    ))

    return patterns


def _detect_god_files(analyses: dict[str, FileAnalysis]) -> list[AntiPattern]:
    """Detect files with too many classes or functions (god objects)."""
    patterns = []

    for path, analysis in analyses.items():
        func_count = len(analysis.functions)
        class_count = len(analysis.classes)

        if func_count > 30:
            patterns.append(AntiPattern(
                name="God File",
                severity="warning",
                description=f"{path} has {func_count} functions — too many responsibilities",
                file=path,
                suggestion="Split into smaller, focused modules",
            ))
        elif class_count > 5:
            patterns.append(AntiPattern(
                name="God File",
                severity="warning",
                description=f"{path} has {class_count} classes — consider splitting",
                file=path,
                suggestion="Move each class to its own module",
            ))

        # Check for extremely high complexity in a single file
        if analysis.max_function_complexity > 30:
            patterns.append(AntiPattern(
                name="Excessive Complexity",
                severity="critical",
                description=f"{path} has a function with complexity {analysis.max_function_complexity}",
                file=path,
                suggestion="Refactor into smaller functions with clear single responsibilities",
            ))

    return patterns


def _detect_orphaned_modules(analyses: dict[str, FileAnalysis]) -> list[AntiPattern]:
    """Detect files that are never imported by other project files."""
    patterns = []

    # Build set of all imported modules
    imported_files: set[str] = set()
    for path, analysis in analyses.items():
        for imp in analysis.imports:
            if imp.is_relative:
                parent = "/".join(path.split("/")[:-1])
                candidate = f"{parent}/{imp.module.replace('.', '/')}.py"
                imported_files.add(candidate)
            # Also check direct module references
            mod_path = imp.module.replace(".", "/") + ".py"
            imported_files.add(mod_path)

    # Check which files are never imported
    entry_patterns = {"main.py", "__main__.py", "cli.py", "app.py", "server.py",
                      "conftest.py", "setup.py", "__init__.py"}
    test_patterns = {"test_", "tests/", "spec/", "__tests__/"}

    for path in analyses:
        if not path.endswith(".py"):
            continue
        basename = os.path.basename(path)
        if basename in entry_patterns:
            continue
        if any(p in path for p in test_patterns):
            continue
        if path not in imported_files and not any(path.endswith(imp) for imp in imported_files):
            # Double check — is it imported by module name?
            mod_name = path.replace("/", ".").replace(".py", "")
            is_imported = any(
                mod_name in imp.module or imp.module.endswith(basename[:-3])
                for analysis in analyses.values()
                for imp in analysis.imports
            )
            if not is_imported:
                patterns.append(AntiPattern(
                    name="Orphaned Module",
                    severity="info",
                    description=f"{path} is not imported by any other project file",
                    file=path,
                    suggestion="Verify this file is still needed; remove if dead code",
                ))

    return patterns


def _detect_deep_nesting(analyses: dict[str, FileAnalysis]) -> list[AntiPattern]:
    """Detect files buried too deep in the directory structure."""
    patterns = []
    for path in analyses:
        depth = path.count("/")
        if depth > 7:
            patterns.append(AntiPattern(
                name="Deep Nesting",
                severity="info",
                description=f"{path} is {depth} directories deep",
                file=path,
                suggestion="Consider flattening the directory structure",
            ))
    return patterns


def _detect_mixed_concerns(
    arch: Architecture, analyses: dict[str, FileAnalysis]
) -> list[AntiPattern]:
    """Detect files that appear in the wrong component directory."""
    patterns = []

    # Simple heuristic: Python files in frontend dirs, JS files in backend dirs
    for path, analysis in analyses.items():
        parts = path.split("/")
        if len(parts) < 2:
            continue

        top_dir = parts[0].lower()
        lang = analysis.language

        if top_dir in ("frontend", "portal", "web", "client", "ui") and lang == "python":
            patterns.append(AntiPattern(
                name="Mixed Concerns",
                severity="info",
                description=f"Python file {path} in frontend directory",
                file=path,
                suggestion="Move backend logic to the backend directory",
            ))
        elif top_dir in ("backend", "server", "api") and lang in ("javascript", "typescript"):
            # Node.js backend is valid, so only flag if there's also a Python backend
            py_backend = any(c.type.value == "backend" and "Python" in str(c.tech_stack) for c in arch.components)
            if py_backend:
                patterns.append(AntiPattern(
                    name="Mixed Concerns",
                    severity="info",
                    description=f"JS/TS file {path} in Python backend directory",
                    file=path,
                    suggestion="Organize JS files separately from Python backend",
                ))

    return patterns


# ──────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────


def _grade_from_score(score: int) -> str:
    """Convert numeric score to letter grade."""
    if score >= 90:
        return "A"
    elif score >= 80:
        return "B"
    elif score >= 70:
        return "C"
    elif score >= 60:
        return "D"
    return "F"


def _generate_recommendations(result: ArchitectureScore) -> list[str]:
    """Generate actionable recommendations from the score."""
    recs = []

    # From category penalties
    for cat in result.categories:
        if cat.score < 15:  # below 60% in any category
            recs.append(f"Focus on {cat.name}: {cat.penalties[0]}" if cat.penalties else f"Improve {cat.name} score")

    # From anti-patterns
    critical = [ap for ap in result.anti_patterns if ap.severity == "critical"]
    if critical:
        recs.insert(0, f"Fix {len(critical)} critical anti-pattern(s): " +
                    ", ".join(ap.name for ap in critical[:3]))

    warnings = [ap for ap in result.anti_patterns if ap.severity == "warning"]
    if warnings:
        recs.append(f"Address {len(warnings)} warning-level anti-pattern(s)")

    if not recs:
        recs.append("Architecture looks healthy — keep it up!")

    return recs[:8]  # cap at 8 recommendations
