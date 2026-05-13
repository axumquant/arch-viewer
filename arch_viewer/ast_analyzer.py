"""
AST-based code analyzer — extracts functions, classes, imports, exports,
call graphs, and complexity metrics using Python's built-in ast module.

This replaces the regex-based import extraction with real parse-tree analysis.
For JS/TS files, we use regex heuristics (Tree-sitter can be added as optional dep).
"""

from __future__ import annotations

import ast
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("arch-viewer.ast")


@dataclass
class FunctionDef:
    """A function or method definition."""
    name: str
    file: str
    line: int
    end_line: int = 0
    is_async: bool = False
    is_method: bool = False
    class_name: str = ""
    decorators: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)
    return_type: str = ""
    calls: list[str] = field(default_factory=list)  # functions this calls
    complexity: int = 1  # cyclomatic complexity


@dataclass
class ClassDef:
    """A class definition."""
    name: str
    file: str
    line: int
    end_line: int = 0
    bases: list[str] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)
    attributes: list[str] = field(default_factory=list)


@dataclass
class ImportInfo:
    """An import statement."""
    module: str
    names: list[str] = field(default_factory=list)  # specific imports
    file: str = ""
    line: int = 0
    is_relative: bool = False


@dataclass
class ExportInfo:
    """An exported symbol."""
    name: str
    kind: str = ""  # "function", "class", "variable", "default"
    file: str = ""
    line: int = 0


@dataclass
class FileAnalysis:
    """Complete AST analysis of a single file."""
    path: str
    language: str
    functions: list[FunctionDef] = field(default_factory=list)
    classes: list[ClassDef] = field(default_factory=list)
    imports: list[ImportInfo] = field(default_factory=list)
    exports: list[ExportInfo] = field(default_factory=list)
    total_lines: int = 0
    code_lines: int = 0
    comment_lines: int = 0
    blank_lines: int = 0
    complexity: int = 0  # total cyclomatic complexity
    max_function_complexity: int = 0
    errors: list[str] = field(default_factory=list)


# ──────────────────────────────────────────────
# Python AST Analysis (real AST via ast module)
# ──────────────────────────────────────────────


class _PythonVisitor(ast.NodeVisitor):
    """Walk Python AST and extract structural information."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.functions: list[FunctionDef] = []
        self.classes: list[ClassDef] = []
        self.imports: list[ImportInfo] = []
        self._current_class: str | None = None

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            self.imports.append(ImportInfo(
                module=alias.name,
                names=[alias.asname or alias.name],
                file=self.filepath,
                line=node.lineno,
            ))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        module = node.module or ""
        names = [alias.name for alias in (node.names or [])]
        self.imports.append(ImportInfo(
            module=module,
            names=names,
            file=self.filepath,
            line=node.lineno,
            is_relative=bool(node.level and node.level > 0),
        ))
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef):
        bases = []
        for base in node.bases:
            if isinstance(base, ast.Name):
                bases.append(base.id)
            elif isinstance(base, ast.Attribute):
                bases.append(ast.unparse(base))

        decorators = []
        for dec in node.decorator_list:
            if isinstance(dec, ast.Name):
                decorators.append(dec.id)
            elif isinstance(dec, ast.Attribute):
                decorators.append(ast.unparse(dec))
            elif isinstance(dec, ast.Call):
                if isinstance(dec.func, ast.Name):
                    decorators.append(dec.func.id)
                elif isinstance(dec.func, ast.Attribute):
                    decorators.append(ast.unparse(dec.func))

        methods = []
        attributes = []
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods.append(item.name)
            elif isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        attributes.append(target.id)

        cls = ClassDef(
            name=node.name,
            file=self.filepath,
            line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            bases=bases,
            decorators=decorators,
            methods=methods,
            attributes=attributes,
        )
        self.classes.append(cls)

        # Visit methods inside the class
        old_class = self._current_class
        self._current_class = node.name
        self.generic_visit(node)
        self._current_class = old_class

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._visit_func(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._visit_func(node, is_async=True)

    def _visit_func(self, node, is_async: bool):
        decorators = []
        for dec in node.decorator_list:
            if isinstance(dec, ast.Name):
                decorators.append(dec.id)
            elif isinstance(dec, ast.Attribute):
                decorators.append(ast.unparse(dec))
            elif isinstance(dec, ast.Call):
                if isinstance(dec.func, ast.Name):
                    decorators.append(dec.func.id)

        args = []
        for arg in node.args.args:
            if arg.arg != "self" and arg.arg != "cls":
                args.append(arg.arg)

        return_type = ""
        if node.returns:
            try:
                return_type = ast.unparse(node.returns)
            except Exception:
                pass

        # Extract function calls within this function
        calls = _extract_calls(node)

        # Calculate cyclomatic complexity
        complexity = _cyclomatic_complexity(node)

        func = FunctionDef(
            name=node.name,
            file=self.filepath,
            line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            is_async=is_async,
            is_method=self._current_class is not None,
            class_name=self._current_class or "",
            decorators=decorators,
            args=args,
            return_type=return_type,
            calls=calls,
            complexity=complexity,
        )
        self.functions.append(func)
        # Don't generic_visit — we already extracted what we need


def _extract_calls(node: ast.AST) -> list[str]:
    """Extract all function/method calls within an AST node."""
    calls = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            if isinstance(child.func, ast.Name):
                calls.add(child.func.id)
            elif isinstance(child.func, ast.Attribute):
                calls.add(child.func.attr)
    return sorted(calls)


def _cyclomatic_complexity(node: ast.AST) -> int:
    """Calculate McCabe cyclomatic complexity for a function."""
    complexity = 1  # base
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.While, ast.For, ast.AsyncFor)):
            complexity += 1
        elif isinstance(child, ast.ExceptHandler):
            complexity += 1
        elif isinstance(child, ast.Assert):
            complexity += 1
        elif isinstance(child, ast.BoolOp):
            # Each `and` / `or` adds a branch
            complexity += len(child.values) - 1
        elif isinstance(child, ast.comprehension):
            complexity += 1
            if child.ifs:
                complexity += len(child.ifs)
    return complexity


def analyze_python_file(filepath: str, content: str | None = None) -> FileAnalysis:
    """Full AST analysis of a Python file."""
    if content is None:
        try:
            content = Path(filepath).read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            return FileAnalysis(path=filepath, language="python", errors=[str(e)])

    # Line metrics
    lines = content.split("\n")
    total_lines = len(lines)
    blank_lines = sum(1 for line in lines if not line.strip())
    comment_lines = sum(1 for line in lines if line.strip().startswith("#"))
    code_lines = total_lines - blank_lines - comment_lines

    analysis = FileAnalysis(
        path=filepath,
        language="python",
        total_lines=total_lines,
        code_lines=code_lines,
        comment_lines=comment_lines,
        blank_lines=blank_lines,
    )

    try:
        tree = ast.parse(content, filename=filepath)
    except SyntaxError as e:
        analysis.errors.append(f"SyntaxError: {e}")
        return analysis

    visitor = _PythonVisitor(filepath)
    visitor.visit(tree)

    analysis.functions = visitor.functions
    analysis.classes = visitor.classes
    analysis.imports = visitor.imports

    # Exports: module-level names (heuristic: __all__ or top-level defs)
    exports = _extract_python_exports(tree, filepath)
    analysis.exports = exports

    # Complexity totals
    if analysis.functions:
        analysis.complexity = sum(f.complexity for f in analysis.functions)
        analysis.max_function_complexity = max(f.complexity for f in analysis.functions)

    return analysis


def _extract_python_exports(tree: ast.Module, filepath: str) -> list[ExportInfo]:
    """Extract exported symbols from a Python module."""
    exports = []

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    # Explicit exports via __all__
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                exports.append(ExportInfo(
                                    name=elt.value, kind="explicit",
                                    file=filepath, line=node.lineno,
                                ))
                    return exports  # __all__ is authoritative

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                exports.append(ExportInfo(
                    name=node.name, kind="function",
                    file=filepath, line=node.lineno,
                ))
        elif isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                exports.append(ExportInfo(
                    name=node.name, kind="class",
                    file=filepath, line=node.lineno,
                ))

    return exports


# ──────────────────────────────────────────────────
# JavaScript/TypeScript Analysis (regex heuristics)
# ──────────────────────────────────────────────────

_JS_IMPORT_RE = re.compile(
    r"""(?:import\s+(?:{([^}]+)}|(\w+))\s+from\s+['"]([^'"]+)['"]"""
    r"""|require\s*\(\s*['"]([^'"]+)['"]\s*\))""",
    re.MULTILINE,
)

_JS_EXPORT_RE = re.compile(
    r"""(?:export\s+(?:default\s+)?(?:function|class|const|let|var|async\s+function)\s+(\w+)"""
    r"""|module\.exports\s*=\s*(?:\{([^}]+)\}|(\w+)))""",
    re.MULTILINE,
)

_JS_FUNCTION_RE = re.compile(
    r"""(?:(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\("""
    r"""|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>"""
    r"""|(\w+)\s*\([^)]*\)\s*\{)""",
    re.MULTILINE,
)

_JS_CLASS_RE = re.compile(
    r"""class\s+(\w+)(?:\s+extends\s+(\w+))?\s*\{""",
    re.MULTILINE,
)


def analyze_js_file(filepath: str, content: str | None = None) -> FileAnalysis:
    """Regex-based analysis of a JavaScript/TypeScript file."""
    lang = "typescript" if filepath.endswith((".ts", ".tsx")) else "javascript"

    if content is None:
        try:
            content = Path(filepath).read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            return FileAnalysis(path=filepath, language=lang, errors=[str(e)])

    lines = content.split("\n")
    total_lines = len(lines)
    blank_lines = sum(1 for line in lines if not line.strip())
    comment_lines = sum(
        1 for line in lines
        if line.strip().startswith("//") or line.strip().startswith("/*") or line.strip().startswith("*")
    )
    code_lines = total_lines - blank_lines - comment_lines

    analysis = FileAnalysis(
        path=filepath,
        language=lang,
        total_lines=total_lines,
        code_lines=code_lines,
        comment_lines=comment_lines,
        blank_lines=blank_lines,
    )

    # Imports
    for match in _JS_IMPORT_RE.finditer(content):
        named = match.group(1)  # { named imports }
        default = match.group(2)  # default import
        from_mod = match.group(3)  # from 'module'
        require_mod = match.group(4)  # require('module')

        module = from_mod or require_mod or ""
        names = []
        if named:
            names = [n.strip().split(" as ")[0].strip() for n in named.split(",")]
        elif default:
            names = [default]

        analysis.imports.append(ImportInfo(
            module=module,
            names=names,
            file=filepath,
            is_relative=module.startswith("."),
        ))

    # Exports
    for match in _JS_EXPORT_RE.finditer(content):
        name = match.group(1) or match.group(3)
        obj_exports = match.group(2)
        if name:
            analysis.exports.append(ExportInfo(name=name, kind="default" if "default" in match.group() else "named", file=filepath))
        elif obj_exports:
            for sym in obj_exports.split(","):
                sym = sym.strip().split(":")[0].strip()
                if sym:
                    analysis.exports.append(ExportInfo(name=sym, kind="named", file=filepath))

    # Functions
    for match in _JS_FUNCTION_RE.finditer(content):
        name = match.group(1) or match.group(2) or match.group(3)
        if name and not name[0].isupper():  # skip class-like names
            line_num = content[:match.start()].count("\n") + 1
            analysis.functions.append(FunctionDef(
                name=name,
                file=filepath,
                line=line_num,
            ))

    # Classes
    for match in _JS_CLASS_RE.finditer(content):
        name = match.group(1)
        base = match.group(2) or ""
        line_num = content[:match.start()].count("\n") + 1
        analysis.classes.append(ClassDef(
            name=name,
            file=filepath,
            line=line_num,
            bases=[base] if base else [],
        ))

    return analysis


# ──────────────────────────────────────────────
# Unified Analysis Entry Point
# ──────────────────────────────────────────────


def analyze_file(filepath: str, content: str | None = None) -> FileAnalysis | None:
    """Analyze a file based on its extension. Returns None for unsupported types."""
    ext = os.path.splitext(filepath)[1].lower()

    if ext in (".py", ".pyi"):
        return analyze_python_file(filepath, content)
    elif ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
        return analyze_js_file(filepath, content)

    return None


def analyze_project(root: str | Path, max_files: int = 500) -> dict[str, FileAnalysis]:
    """Analyze all supported files in a project directory."""
    root = Path(root).resolve()
    results: dict[str, FileAnalysis] = {}

    ignore_dirs = {
        "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
        ".next", ".nuxt", "dist", "build", ".turbo", ".cache",
    }

    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignore_dirs and not (d.startswith(".") and d not in (".claude", ".github"))]

        for fname in filenames:
            if count >= max_files:
                break

            fpath = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(fpath, root).replace("\\", "/")

            try:
                stat = os.stat(fpath)
                if stat.st_size > 500_000:  # skip files > 500KB
                    continue
            except OSError:
                continue

            analysis = analyze_file(fpath)
            if analysis:
                analysis.path = rel_path
                results[rel_path] = analysis
                count += 1

    return results
