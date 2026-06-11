"""Stdlib ``ast`` extraction backend for Python sources."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING, Any

from codd.parsing._shared import (
    PrismaSchemaInfo,
    SqlSchemaInfo,
    _make_symbol,
    _normalize_ws,
    _route_from_decorator,
)

if TYPE_CHECKING:
    from codd.extractor import CallEdge, ModuleInfo, Symbol


class PythonAstExtractor:
    """Stdlib ``ast`` backend for Python source files.

    Unlike the Tree-sitter backend, this parser is available in every supported
    Python runtime. Syntax errors intentionally fall back to empty structural
    facts so callers still keep the file's raw text, line count, and module.
    """

    language = "python"

    def __init__(self, language: str = "python", category: str = "source"):
        self.language = language.lower()
        self.category = category.lower()

    def extract_symbols(self, content: str, file_path: str) -> list[Symbol]:
        if self.category != "source":
            return []
        try:
            tree = ast.parse(content)
        except (SyntaxError, ValueError, TypeError):
            return []
        return _extract_python_symbols_stdlib(tree, content, file_path)

    def extract_imports(
        self,
        content: str,
        file_path: Path,
        project_root: Path,
        src_dir: Path,
    ) -> tuple[dict[str, list[str]], set[str]]:
        if self.category != "source":
            return {}, set()
        try:
            tree = ast.parse(content)
        except (SyntaxError, ValueError, TypeError):
            return {}, set()
        return _extract_python_imports_stdlib(tree, content, file_path, project_root, src_dir)

    def detect_code_patterns(self, mod: ModuleInfo, content: str) -> None:
        if self.category != "source":
            return None
        try:
            tree = ast.parse(content)
        except (SyntaxError, ValueError, TypeError):
            return None
        _detect_python_code_patterns_stdlib(mod, tree, content)
        return None

    def extract_schema(self, content: str, file_path: str | Path) -> SqlSchemaInfo | PrismaSchemaInfo | None:
        return None

    def extract_call_graph(self, content: str, file_path: str, symbols: list[Symbol]) -> list[CallEdge]:
        if self.category != "source":
            return []
        try:
            tree = ast.parse(content)
        except (SyntaxError, ValueError, TypeError):
            return []
        return _extract_python_call_graph_stdlib(tree, file_path, symbols)

def _resolve_python_relative_key(module: str, file_path: Path, src_dir: Path) -> str | None:
    leading_dots = len(module) - len(module.lstrip("."))
    relative_module = module.lstrip(".")
    try:
        rel_file = file_path.relative_to(src_dir)
    except ValueError:
        return None

    package_parts = list(rel_file.with_suffix("").parts[:-1])
    remove_count = max(leading_dots - 1, 0)
    if remove_count > len(package_parts):
        target_parts: list[str] = []
    else:
        target_parts = package_parts[: len(package_parts) - remove_count]

    if relative_module:
        target_parts.extend(relative_module.split("."))

    return target_parts[0] if target_parts else "root"

def _record_python_import(
    module: str,
    line: str,
    internal: dict[str, list[str]],
    external: set[str],
    *,
    project_root: Path,
    src_dir: Path,
    file_path: Path,
):
    normalized = module.strip()
    if not normalized:
        return

    if normalized.startswith("."):
        internal_key = _resolve_python_relative_key(normalized, file_path, src_dir)
        if internal_key:
            internal.setdefault(internal_key, []).append(line)
        return

    parts = normalized.split(".")
    top_level = parts[0]
    if not top_level:
        return

    src_pkg_name = src_dir.name if (src_dir / "__init__.py").exists() else None
    is_internal = False
    internal_key = top_level

    if src_pkg_name and top_level == src_pkg_name and len(parts) >= 2:
        is_internal = True
        internal_key = parts[1]
    else:
        search_dirs = [src_dir]
        search_dirs.extend(project_root / candidate for candidate in ("src", "lib", "app") if (project_root / candidate).is_dir())
        for search_dir in search_dirs:
            if (search_dir / top_level).is_dir() or (search_dir / f"{top_level}.py").is_file():
                is_internal = True
                break

    if is_internal:
        internal.setdefault(internal_key, []).append(line)
    else:
        external.add(top_level)

def _python_source_segment(content: str, node: ast.AST) -> str:
    segment = ast.get_source_segment(content, node)
    if segment:
        return segment
    lines = content.splitlines()
    start = max(getattr(node, "lineno", 1) - 1, 0)
    end = getattr(node, "end_lineno", getattr(node, "lineno", 1))
    return "\n".join(lines[start:end])

def _python_expr_text(content: str, node: ast.AST) -> str:
    segment = ast.get_source_segment(content, node)
    if segment:
        return _normalize_ws(segment)
    try:
        return _normalize_ws(ast.unparse(node))
    except Exception:
        return ""

def _header_until_colon(text: str) -> str:
    depth = 0
    quote: str | None = None
    escape = False
    for index, char in enumerate(text):
        if quote is not None:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}" and depth > 0:
            depth -= 1
        elif char == ":" and depth == 0:
            return text[:index]
    return text.splitlines()[0] if text.splitlines() else text

def _matching_paren_index(text: str, open_index: int) -> int:
    depth = 0
    quote: str | None = None
    escape = False
    for index in range(open_index, len(text)):
        char = text[index]
        if quote is not None:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return -1

def _python_function_signature(content: str, node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, str]:
    header = _header_until_colon(_python_source_segment(content, node))
    open_index = header.find("(")
    close_index = _matching_paren_index(header, open_index) if open_index >= 0 else -1
    if open_index < 0 or close_index < 0:
        return "", ""

    params = _normalize_ws(header[open_index + 1:close_index])
    suffix = header[close_index + 1:].strip()
    return_type = ""
    if suffix.startswith("->"):
        return_type = _normalize_ws(suffix[2:])
    return params, return_type

def _extract_python_symbols_stdlib(tree: ast.AST, content: str, file_path: str) -> list[Symbol]:
    symbols: list[Symbol] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.ClassDef):
            symbols.append(
                _make_symbol(
                    node.name,
                    "class",
                    file_path,
                    node.lineno,
                    decorators=[_python_expr_text(content, dec) for dec in node.decorator_list],
                    bases=[text for base in node.bases if (text := _python_expr_text(content, base))],
                )
            )
            for child in node.body:
                if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(child)
            return

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                params, return_type = _python_function_signature(content, node)
                symbols.append(
                    _make_symbol(
                        node.name,
                        "function",
                        file_path,
                        node.lineno,
                        params=params,
                        return_type=return_type,
                        decorators=[_python_expr_text(content, dec) for dec in node.decorator_list],
                        is_async=isinstance(node, ast.AsyncFunctionDef),
                    )
                )
            for child in node.body:
                if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(child)
            return

        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child)

    visit(tree)
    return symbols

def _extract_python_imports_stdlib(
    tree: ast.AST,
    content: str,
    file_path: Path,
    project_root: Path,
    src_dir: Path,
) -> tuple[dict[str, list[str]], set[str]]:
    internal: dict[str, list[str]] = {}
    external: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            line = _normalize_ws(_python_source_segment(content, node))
            for alias in node.names:
                _record_python_import(
                    alias.name,
                    line,
                    internal,
                    external,
                    project_root=project_root,
                    src_dir=src_dir,
                    file_path=file_path,
                )
        elif isinstance(node, ast.ImportFrom):
            line = _normalize_ws(_python_source_segment(content, node))
            prefix = "." * node.level
            if node.module:
                modules = [f"{prefix}{node.module}"]
            else:
                modules = [
                    f"{prefix}{alias.name}"
                    for alias in node.names
                    if alias.name != "*"
                ] or [prefix]
            for module in modules:
                _record_python_import(
                    module,
                    line,
                    internal,
                    external,
                    project_root=project_root,
                    src_dir=src_dir,
                    file_path=file_path,
                )

    external -= _python_stdlib()
    return internal, external

def _detect_python_code_patterns_stdlib(mod: ModuleInfo, tree: ast.AST, content: str) -> None:
    docstring = ast.get_docstring(tree, clean=True)
    if docstring:
        first_line = next((line.strip() for line in docstring.splitlines() if line.strip()), "")
        if first_line:
            mod.patterns["module_docstring"] = first_line

    routes: list[str] = []
    orm_models: list[str] = []
    background_tasks: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                decorator_text = _python_expr_text(content, decorator)
                route = _route_from_decorator(decorator_text)
                if route:
                    routes.append(route)
                if decorator_text.endswith(".task"):
                    background_tasks.append(decorator_text)
        elif isinstance(node, ast.ClassDef):
            bases = [_python_expr_text(content, base) for base in node.bases]
            if any(base.endswith(("Base", "Model")) or base.endswith(".Model") for base in bases):
                orm_models.append(node.name)

    if routes:
        mod.patterns["api_routes"] = f"HTTP route handlers: {', '.join(sorted(set(routes)))}"
    if orm_models:
        mod.patterns["db_models"] = f"ORM models: {', '.join(sorted(set(orm_models)))}"
    if background_tasks:
        mod.patterns["background_tasks"] = "Async task handlers"

_PY_BUILTIN_CALLS = {
    "abs",
    "all",
    "any",
    "bool",
    "callable",
    "dict",
    "dir",
    "enumerate",
    "filter",
    "float",
    "format",
    "getattr",
    "hasattr",
    "hash",
    "id",
    "int",
    "isinstance",
    "issubclass",
    "iter",
    "len",
    "list",
    "map",
    "max",
    "min",
    "next",
    "open",
    "print",
    "property",
    "range",
    "repr",
    "reversed",
    "round",
    "set",
    "setattr",
    "sorted",
    "staticmethod",
    "str",
    "sum",
    "super",
    "tuple",
    "type",
    "vars",
    "zip",
}

def _python_callee_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _python_callee_name(node.value)
        return f"{owner}.{node.attr}" if owner else node.attr
    if isinstance(node, ast.Call):
        return _python_callee_name(node.func)
    return None

def _extract_python_call_graph_stdlib(tree: ast.AST, file_path: str, symbols: list[Symbol]) -> list[CallEdge]:
    from codd.extractor import CallEdge

    edges: list[CallEdge] = []
    symbol_names = {symbol.name for symbol in symbols}

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []
            self.await_depth = 0

        def visit_ClassDef(self, node: ast.ClassDef) -> Any:
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_Await(self, node: ast.Await) -> Any:
            self.await_depth += 1
            self.generic_visit(node)
            self.await_depth -= 1

        def visit_Call(self, node: ast.Call) -> Any:
            callee = _python_callee_name(node.func)
            if callee:
                bare = callee.split(".")[-1]
                if not (bare.startswith("__") and bare.endswith("__")) and bare not in _PY_BUILTIN_CALLS:
                    known = bare in symbol_names or callee in symbol_names
                    if not known and callee.startswith("self."):
                        known = callee[5:] in symbol_names
                    if known:
                        edges.append(
                            CallEdge(
                                caller=".".join(self.scope) if self.scope else "<module>",
                                callee=callee,
                                call_site=f"{file_path}:{node.lineno}",
                                is_async=self.await_depth > 0,
                            )
                        )
            self.generic_visit(node)

    Visitor().visit(tree)
    return edges

def _python_stdlib() -> set[str]:
    return {
        "__future__",
        "abc",
        "argparse",
        "asyncio",
        "collections",
        "dataclasses",
        "datetime",
        "functools",
        "importlib",
        "io",
        "json",
        "math",
        "os",
        "pathlib",
        "re",
        "subprocess",
        "sys",
        "textwrap",
        "typing",
    }
