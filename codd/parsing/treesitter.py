"""Tree-sitter extraction backend for Python and TypeScript/JavaScript."""

from __future__ import annotations

import re
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING, Any

from codd.parsing._shared import (
    PrismaSchemaInfo,
    RegexExtractor,
    SqlSchemaInfo,
    _make_symbol,
    _normalize_ws,
    _route_from_decorator,
    _split_csv,
)
from codd.parsing.python_ast import _python_stdlib, _record_python_import

if TYPE_CHECKING:
    from codd.extractor import CallEdge, ModuleInfo, Symbol


_TREE_SITTER_LANGUAGE_PACKAGES = {
    "sql": "tree_sitter_sql",
    "python": "tree_sitter_python",
    "typescript": "tree_sitter_typescript",
    "javascript": "tree_sitter_typescript",
}

_JS_IMPORT_SUFFIXES = {
    "javascript": ("", ".js", ".jsx", ".mjs", ".cjs", "/index.js", "/index.jsx"),
    "typescript": ("", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", "/index.ts", "/index.tsx"),
}


def _strip_js_comments(src: str) -> str:
    """Remove ``/* block */`` (including JSDoc) and ``// line`` comments.

    Used to keep route-path extraction from reading example routes embedded in
    documentation comments. String-literal edge cases are tolerated: route
    specifiers of interest are app-relative paths (``/users``), never URLs that
    would carry a ``//`` sequence inside a string literal.
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"//[^\n]*", "", src)
    return src


class TreeSitterExtractor:
    """Tree-sitter backend for Python and TypeScript/JavaScript source files."""

    def __init__(self, language: str, category: str = "source"):
        self.language = language.lower()
        self.category = category.lower()
        self._fallback = RegexExtractor(self.language, self.category)
        self._parser = _build_parser(self.language)

    @classmethod
    def is_available(cls, language: str | None = None) -> bool:
        """Return True when Tree-sitter core and the language binding are importable."""
        if find_spec("tree_sitter") is None:
            return False
        if language is None:
            return True
        package_name = _TREE_SITTER_LANGUAGE_PACKAGES.get(language.lower())
        if package_name is None:
            return False
        return find_spec(package_name) is not None

    def extract_symbols(self, content: str, file_path: str) -> list[Symbol]:
        if self.category != "source":
            return []
        try:
            root = self._parse(content)
            if self.language == "python":
                return _extract_python_symbols_ast(root, content, file_path)
            if self.language in {"typescript", "javascript"}:
                return _extract_typescript_symbols(root, content, file_path)
        except Exception:
            return self._fallback.extract_symbols(content, file_path)
        return self._fallback.extract_symbols(content, file_path)

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
            root = self._parse(content)
            if self.language == "python":
                return _extract_python_imports_ast(root, content, file_path, project_root, src_dir)
            if self.language in {"typescript", "javascript"}:
                return _extract_typescript_imports_ast(root, content, file_path, src_dir, self.language)
        except Exception:
            return self._fallback.extract_imports(content, file_path, project_root, src_dir)
        return self._fallback.extract_imports(content, file_path, project_root, src_dir)

    def detect_code_patterns(self, mod: ModuleInfo, content: str) -> None:
        if self.category != "source":
            return None
        try:
            root = self._parse(content)
            if self.language == "python":
                _detect_python_code_patterns(mod, root, content)
                return None
            if self.language in {"typescript", "javascript"}:
                _detect_typescript_code_patterns(mod, root, content)
                return None
        except Exception:
            self._fallback.detect_code_patterns(mod, content)
            return None
        self._fallback.detect_code_patterns(mod, content)
        return None

    def extract_schema(self, content: str, file_path: str | Path) -> SqlSchemaInfo | PrismaSchemaInfo | None:
        return self._fallback.extract_schema(content, file_path)

    def extract_call_graph(self, content: str, file_path: str, symbols: list[Symbol]) -> list[CallEdge]:
        if self.category != "source":
            return []
        try:
            root = self._parse(content)
            if self.language == "python":
                return _extract_python_call_graph(root, content, file_path, symbols)
            if self.language in {"typescript", "javascript"}:
                return _extract_ts_call_graph(root, content, file_path, symbols)
        except Exception:
            return []
        return []

    def _parse(self, content: str):
        return self._parser.parse(content.encode("utf-8", errors="ignore")).root_node

def _build_parser(language: str):
    from tree_sitter import Parser

    parser = Parser()
    parser.language = _load_tree_sitter_language(language)
    return parser

def _load_tree_sitter_language(language: str):
    from tree_sitter import Language

    normalized = language.lower()
    if normalized == "python":
        import tree_sitter_python

        return Language(tree_sitter_python.language())
    if normalized in {"typescript", "javascript"}:
        import tree_sitter_typescript

        return Language(tree_sitter_typescript.language_typescript())
    if normalized == "sql":
        import tree_sitter_sql

        return Language(tree_sitter_sql.language())
    raise ValueError(f"Unsupported Tree-sitter language: {language}")

def _node_text(content_bytes: bytes, node: Any) -> str:
    if node is None:
        return ""
    return content_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="ignore")

def _field_text(content_bytes: bytes, node: Any, field_name: str) -> str:
    return _node_text(content_bytes, node.child_by_field_name(field_name))

def _iter_named_nodes(node: Any):
    yield node
    for child in getattr(node, "named_children", []):
        yield from _iter_named_nodes(child)

def _strip_wrapping(text: str, opening: str, closing: str) -> str:
    stripped = text.strip()
    if stripped.startswith(opening) and stripped.endswith(closing):
        return stripped[len(opening):-len(closing)].strip()
    return stripped

def _strip_type_annotation(text: str) -> str:
    stripped = _normalize_ws(text)
    if stripped.startswith(":"):
        return stripped[1:].strip()
    return stripped

def _extract_string_literal(text: str) -> str | None:
    stripped = text.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {'"', "'"}:
        return stripped[1:-1]
    return None

def _is_async_node(node: Any) -> bool:
    return any(child.type == "async" for child in node.children)

def _extract_object_shape(content_bytes: bytes, node: Any) -> str:
    """Extract top-level keys from an object literal node.

    Returns a compact representation like ``keys: foo, bar, baz`` or
    ``keys(4): foo, bar, baz, ...`` when truncated.
    """
    keys: list[str] = []
    for child in getattr(node, "named_children", []):
        if child.type == "pair":
            key_node = child.child_by_field_name("key")
            if key_node is not None:
                keys.append(_node_text(content_bytes, key_node))
        elif child.type == "shorthand_property_identifier":
            keys.append(_node_text(content_bytes, child))
        elif child.type == "spread_element":
            keys.append("..." + _node_text(content_bytes, child)[3:].strip())
    if not keys:
        return "{}"
    total = len(keys)
    shown = keys[:8]
    suffix = ", ..." if total > 8 else ""
    return f"keys({total}): {', '.join(shown)}{suffix}"

def _unwrap_value_node(node: Any) -> Any:
    """Unwrap ``as const`` / ``satisfies Type`` wrappers to get the real value node."""
    if node is None:
        return None
    if node.type in ("as_expression", "satisfies_expression"):
        for child in getattr(node, "named_children", []):
            if child.type in ("object", "array"):
                return child
        # Fallback: first named child
        children = getattr(node, "named_children", [])
        return children[0] if children else node
    return node

def _resolve_js_import(import_path: str, file_path: Path, src_dir: Path, language: str) -> Path | None:
    candidate_base = (file_path.parent / import_path).resolve()
    for suffix in _JS_IMPORT_SUFFIXES.get(language, _JS_IMPORT_SUFFIXES["typescript"]):
        candidate = candidate_base if suffix == "" else Path(f"{candidate_base}{suffix}")
        if candidate.exists():
            return candidate
    return None

def _record_js_import(
    import_path: str,
    line: str,
    internal: dict[str, list[str]],
    external: set[str],
    *,
    file_path: Path,
    src_dir: Path,
    language: str,
):
    if import_path.startswith("."):
        resolved = _resolve_js_import(import_path, file_path, src_dir, language)
        if resolved is None:
            external.add(import_path)
            return
        try:
            rel = resolved.relative_to(src_dir)
        except ValueError:
            external.add(import_path)
            return
        internal_key = rel.parts[0] if rel.parts else "root"
        internal.setdefault(internal_key, []).append(line)
        return

    if import_path.startswith("@"):
        parts = import_path.split("/")
        external.add("/".join(parts[:2]) if len(parts) >= 2 else import_path)
        return

    external.add(import_path.split("/")[0])

def _extract_python_decorators(content_bytes: bytes, node: Any) -> list[str]:
    decorators: list[str] = []
    for child in node.children:
        if child.type != "decorator":
            continue
        decorator_text = _node_text(content_bytes, child).strip()
        decorators.append(decorator_text[1:] if decorator_text.startswith("@") else decorator_text)
    return decorators

def _extract_python_symbols_ast(root: Any, content: str, file_path: str) -> list[Symbol]:
    content_bytes = content.encode("utf-8", errors="ignore")
    symbols: list[Symbol] = []

    def visit(node: Any, decorators: list[str] | None = None):
        if node.type == "decorated_definition":
            definition = node.child_by_field_name("definition")
            if definition is not None:
                visit(definition, _extract_python_decorators(content_bytes, node))
            return

        if node.type == "class_definition":
            name = _field_text(content_bytes, node, "name")
            bases = _split_csv(_strip_wrapping(_field_text(content_bytes, node, "superclasses"), "(", ")"))
            if name:
                symbols.append(
                    _make_symbol(
                        name,
                        "class",
                        file_path,
                        node.start_point.row + 1,
                        decorators=decorators,
                        bases=bases,
                    )
                )
            body = node.child_by_field_name("body")
            for child in getattr(body, "named_children", []):
                visit(child)
            return

        if node.type == "function_definition":
            name = _field_text(content_bytes, node, "name")
            if name and not name.startswith("_"):
                symbols.append(
                    _make_symbol(
                        name,
                        "function",
                        file_path,
                        node.start_point.row + 1,
                        params=_strip_wrapping(_normalize_ws(_field_text(content_bytes, node, "parameters")), "(", ")"),
                        return_type=_normalize_ws(_field_text(content_bytes, node, "return_type")),
                        decorators=decorators,
                        is_async=_is_async_node(node),
                    )
                )
            body = node.child_by_field_name("body")
            for child in getattr(body, "named_children", []):
                if child.type in {"class_definition", "decorated_definition", "function_definition"}:
                    visit(child)
            return

        for child in getattr(node, "named_children", []):
            visit(child)

    visit(root)
    return symbols

def _extract_python_imports_ast(
    root: Any,
    content: str,
    file_path: Path,
    project_root: Path,
    src_dir: Path,
) -> tuple[dict[str, list[str]], set[str]]:
    content_bytes = content.encode("utf-8", errors="ignore")
    internal: dict[str, list[str]] = {}
    external: set[str] = set()

    for node in _iter_named_nodes(root):
        if node.type == "import_statement":
            line = _normalize_ws(_node_text(content_bytes, node))
            for index, child in enumerate(node.children):
                if node.field_name_for_child(index) != "name":
                    continue
                _record_python_import(
                    _normalize_ws(_node_text(content_bytes, child)),
                    line,
                    internal,
                    external,
                    project_root=project_root,
                    src_dir=src_dir,
                    file_path=file_path,
                )
        elif node.type == "import_from_statement":
            line = _normalize_ws(_node_text(content_bytes, node))
            module = _field_text(content_bytes, node, "module_name")
            if not module:
                match = re.search(r"from\s+([.\w]+)\s+import", line)
                module = match.group(1) if match else ""
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

def _extract_typescript_heritage(content_bytes: bytes, node: Any) -> tuple[list[str], list[str]]:
    bases: list[str] = []
    implements: list[str] = []
    for child in node.children:
        if child.type == "class_heritage":
            for heritage_child in getattr(child, "named_children", []):
                text = _normalize_ws(_node_text(content_bytes, heritage_child))
                if heritage_child.type == "extends_clause":
                    bases.extend(_split_csv(text.removeprefix("extends ").strip()))
                elif heritage_child.type == "implements_clause":
                    implements.extend(_split_csv(text.removeprefix("implements ").strip()))
        elif child.type == "extends_type_clause":
            text = _normalize_ws(_node_text(content_bytes, child))
            bases.extend(_split_csv(text.removeprefix("extends ").strip()))
    return bases, implements

def _extract_typescript_symbols(root: Any, content: str, file_path: str) -> list[Symbol]:
    content_bytes = content.encode("utf-8", errors="ignore")
    symbols: list[Symbol] = []

    def visit(node: Any):
        if node.type == "export_statement":
            declaration = node.child_by_field_name("declaration")
            if declaration is not None:
                visit(declaration)
            return

        if node.type == "class_declaration":
            name = _field_text(content_bytes, node, "name")
            bases, implements = _extract_typescript_heritage(content_bytes, node)
            if name:
                symbols.append(
                    _make_symbol(
                        name,
                        "class",
                        file_path,
                        node.start_point.row + 1,
                        bases=bases,
                        implements=implements,
                    )
                )
            return

        if node.type == "interface_declaration":
            name = _field_text(content_bytes, node, "name")
            bases, _ = _extract_typescript_heritage(content_bytes, node)
            if name:
                symbols.append(_make_symbol(name, "interface", file_path, node.start_point.row + 1, bases=bases))
            return

        if node.type == "type_alias_declaration":
            name = _field_text(content_bytes, node, "name")
            if name:
                symbols.append(
                    _make_symbol(
                        name,
                        "type_alias",
                        file_path,
                        node.start_point.row + 1,
                        return_type=_normalize_ws(_field_text(content_bytes, node, "value")),
                    )
                )
            return

        if node.type == "enum_declaration":
            name = _field_text(content_bytes, node, "name")
            if name:
                symbols.append(_make_symbol(name, "enum", file_path, node.start_point.row + 1))
            return

        if node.type == "function_declaration":
            name = _field_text(content_bytes, node, "name")
            if name:
                symbols.append(
                    _make_symbol(
                        name,
                        "function",
                        file_path,
                        node.start_point.row + 1,
                        params=_strip_wrapping(_normalize_ws(_field_text(content_bytes, node, "parameters")), "(", ")"),
                        return_type=_strip_type_annotation(_field_text(content_bytes, node, "return_type")),
                        is_async=_is_async_node(node),
                    )
                )
            return

        if node.type == "lexical_declaration":
            for declarator in getattr(node, "named_children", []):
                if declarator.type != "variable_declarator":
                    continue
                raw_value = declarator.child_by_field_name("value")
                name = _field_text(content_bytes, declarator, "name")
                if not name:
                    continue

                # Arrow function → function symbol (existing)
                if raw_value is not None and raw_value.type == "arrow_function":
                    symbols.append(
                        _make_symbol(
                            name,
                            "function",
                            file_path,
                            declarator.start_point.row + 1,
                            params=_strip_wrapping(_normalize_ws(_field_text(content_bytes, raw_value, "parameters")), "(", ")"),
                            return_type=_strip_type_annotation(_field_text(content_bytes, raw_value, "return_type")),
                            is_async=_is_async_node(raw_value),
                        )
                    )
                    continue

                # Const object / array → const_object symbol
                # Skip test files — their fixtures/mocks add noise, not domain knowledge
                _fp = file_path.lower()
                if ".test." in _fp or ".spec." in _fp or ".e2e." in _fp or "test-harness" in _fp:
                    continue
                value = _unwrap_value_node(raw_value)
                if value is not None and value.type == "object":
                    type_ann = _strip_type_annotation(_field_text(content_bytes, declarator, "type"))
                    shape = _extract_object_shape(content_bytes, value)
                    symbols.append(
                        _make_symbol(
                            name,
                            "const_object",
                            file_path,
                            declarator.start_point.row + 1,
                            return_type=type_ann,
                            params=shape,
                        )
                    )
                elif value is not None and value.type == "array":
                    type_ann = _strip_type_annotation(_field_text(content_bytes, declarator, "type"))
                    elements = [
                        c for c in getattr(value, "named_children", [])
                        if c.type != "comment"
                    ]
                    count = len(elements)
                    preview_items = [
                        _normalize_ws(_node_text(content_bytes, e))[:40]
                        for e in elements[:5]
                    ]
                    suffix = ", ..." if count > 5 else ""
                    shape = f"[{count}]: {', '.join(preview_items)}{suffix}" if preview_items else "[]"
                    symbols.append(
                        _make_symbol(
                            name,
                            "const_object",
                            file_path,
                            declarator.start_point.row + 1,
                            return_type=type_ann,
                            params=shape,
                        )
                    )
            return

        for child in getattr(node, "named_children", []):
            visit(child)

    visit(root)
    return symbols

def _extract_typescript_imports_ast(
    root: Any,
    content: str,
    file_path: Path,
    src_dir: Path,
    language: str,
) -> tuple[dict[str, list[str]], set[str]]:
    content_bytes = content.encode("utf-8", errors="ignore")
    internal: dict[str, list[str]] = {}
    external: set[str] = set()

    for node in _iter_named_nodes(root):
        if node.type in {"export_statement", "import_statement"}:
            source_node = node.child_by_field_name("source")
            if source_node is None:
                continue
            import_path = _extract_string_literal(_node_text(content_bytes, source_node))
            if not import_path:
                continue
            _record_js_import(
                import_path,
                _normalize_ws(_node_text(content_bytes, node)),
                internal,
                external,
                file_path=file_path,
                src_dir=src_dir,
                language=language,
            )
            continue

        # CommonJS ``require('..')`` and dynamic ``import('..')`` are
        # call_expressions, not import_statements. A CommonJS codebase (e.g.
        # Express) carries its entire internal dependency graph here, so the AST
        # walk must capture it too — otherwise the scan reports no inter-module
        # dependencies. Mirrors codd.dag.extractor._IMPORT_SPECIFIER_RE.
        if node.type == "call_expression":
            import_path = _call_expression_module_specifier(content_bytes, node)
            if not import_path:
                continue
            _record_js_import(
                import_path,
                _normalize_ws(_node_text(content_bytes, node)),
                internal,
                external,
                file_path=file_path,
                src_dir=src_dir,
                language=language,
            )

    return internal, external


def _call_expression_module_specifier(content_bytes: bytes, node: Any) -> str | None:
    """Return the module specifier of a ``require('..')`` / ``import('..')`` call.

    Returns ``None`` for any other call expression (including ``require(var)``
    with a non-literal argument — only statically resolvable specifiers count).
    """
    func_node = node.child_by_field_name("function")
    if func_node is None:
        return None
    callee = _node_text(content_bytes, func_node).strip()
    # ``import(...)`` parses with an ``import`` callee; ``require(...)`` with an
    # identifier callee. Accept both; reject everything else.
    if callee != "require" and func_node.type != "import":
        return None
    args_node = node.child_by_field_name("arguments")
    if args_node is None:
        return None
    for child in getattr(args_node, "named_children", []):
        if child.type in {"string", "template_string"}:
            return _extract_string_literal(_node_text(content_bytes, child))
        # A non-literal first argument (e.g. require(modName)) is not statically
        # resolvable — stop at the first argument either way.
        break
    return None

def _detect_python_code_patterns(mod: ModuleInfo, root: Any, content: str) -> None:
    content_bytes = content.encode("utf-8", errors="ignore")
    routes: list[str] = []
    orm_models: list[str] = []
    background_tasks: list[str] = []

    def visit(node: Any):
        if node.type == "decorated_definition":
            decorators = _extract_python_decorators(content_bytes, node)
            definition = node.child_by_field_name("definition")
            for decorator in decorators:
                route = _route_from_decorator(decorator)
                if route:
                    routes.append(route)
                if decorator.endswith(".task"):
                    background_tasks.append(decorator)
            if definition is not None:
                visit(definition)
            return

        if node.type == "class_definition":
            name = _field_text(content_bytes, node, "name")
            bases = _split_csv(_strip_wrapping(_field_text(content_bytes, node, "superclasses"), "(", ")"))
            if name and any(base.endswith(("Base", "Model")) or base.endswith(".Model") for base in bases):
                orm_models.append(name)
            body = node.child_by_field_name("body")
            for child in getattr(body, "named_children", []):
                visit(child)
            return

        for child in getattr(node, "named_children", []):
            visit(child)

    visit(root)
    if routes:
        mod.patterns["api_routes"] = f"HTTP route handlers: {', '.join(sorted(set(routes)))}"
    if orm_models:
        mod.patterns["db_models"] = f"ORM models: {', '.join(sorted(set(orm_models)))}"
    if background_tasks:
        mod.patterns["background_tasks"] = "Async task handlers"
    if "api_routes" not in mod.patterns and re.search(r"@(?:app|router)\.(get|post|put|delete|patch)\s*\(", content):
        mod.patterns["api_routes"] = "HTTP route handlers"

def _detect_typescript_code_patterns(mod: ModuleInfo, root: Any, content: str) -> None:
    content_bytes = content.encode("utf-8", errors="ignore")
    orm_models: list[str] = []

    for node in _iter_named_nodes(root):
        candidate = node
        if node.type == "export_statement":
            declaration = node.child_by_field_name("declaration")
            if declaration is not None:
                candidate = declaration
        if candidate.type != "class_declaration":
            continue
        name = _field_text(content_bytes, candidate, "name")
        bases, implements = _extract_typescript_heritage(content_bytes, candidate)
        heritage = bases + implements
        if name and any(item in {"BaseEntity", "Model"} or item.endswith(("Base", "Entity", "Model")) for item in heritage):
            orm_models.append(name)

    if orm_models:
        mod.patterns["db_models"] = f"ORM models: {', '.join(sorted(set(orm_models)))}"

    # Strip /* block */ (incl. JSDoc) and // line comments BEFORE pulling route
    # path strings: framework docs embed example routes such as
    # ``app.get('/user/:uid/photos/:file', ...)`` inside @example JSDoc blocks
    # (e.g. express/lib/response.js), which would otherwise register as real
    # endpoints (false positives).
    route_matches = re.findall(
        r'(?:app|router)\.(?:get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']',
        _strip_js_comments(content),
    )
    if route_matches:
        mod.patterns["api_routes"] = f"HTTP route handlers: {', '.join(sorted(set(route_matches)))}"
    elif re.search(r"@(?:Controller|Get|Post|Put|Delete|Patch)\s*\(", content):
        mod.patterns["api_routes"] = "NestJS controller"

def _extract_python_call_graph(root: Any, content: str, file_path: str, symbols: list[Symbol]) -> list[CallEdge]:
    """Extract function call edges from Python AST using tree-sitter."""
    from codd.extractor import CallEdge

    content_bytes = content.encode("utf-8", errors="ignore")
    edges: list[CallEdge] = []
    symbol_names = {s.name for s in symbols}

    def _current_scope(node: Any) -> str:
        """Walk parents to find enclosing function/class scope."""
        parts: list[str] = []
        current = node.parent
        while current is not None:
            if current.type in ("function_definition", "class_definition"):
                name = _field_text(content_bytes, current, "name")
                if name:
                    parts.append(name)
            current = current.parent
        parts.reverse()
        return ".".join(parts) if parts else "<module>"

    for node in _iter_named_nodes(root):
        if node.type != "call":
            continue

        func_node = node.child_by_field_name("function")
        if func_node is None:
            continue

        callee_text = _node_text(content_bytes, func_node).strip()

        # Skip builtins and dunder calls
        bare_name = callee_text.split(".")[-1] if "." in callee_text else callee_text
        if bare_name.startswith("__") and bare_name.endswith("__"):
            continue
        if bare_name in ("print", "len", "range", "enumerate", "zip", "map", "filter",
                         "sorted", "reversed", "list", "dict", "set", "tuple", "str",
                         "int", "float", "bool", "type", "isinstance", "issubclass",
                         "getattr", "setattr", "hasattr", "super", "property",
                         "staticmethod", "classmethod", "open", "repr", "id", "vars",
                         "dir", "any", "all", "min", "max", "sum", "abs", "round",
                         "format", "iter", "next", "hash", "callable"):
            continue

        # Only include calls to known symbols (intra-project)
        if bare_name not in symbol_names and callee_text not in symbol_names:
            # Check if it's a method call on self (self.method)
            if callee_text.startswith("self."):
                method_name = callee_text[5:]  # strip "self."
                if method_name not in symbol_names:
                    continue
            else:
                continue

        caller = _current_scope(node)
        line_no = node.start_point.row + 1
        is_async = node.parent is not None and node.parent.type == "await"

        edges.append(CallEdge(
            caller=caller,
            callee=callee_text,
            call_site=f"{file_path}:{line_no}",
            is_async=is_async,
        ))

    return edges

_TS_BUILTIN_NAMES = {
    "console", "Math", "JSON", "Object", "Array", "Promise",
    "setTimeout", "setInterval", "clearTimeout", "clearInterval",
    "require", "parseInt", "parseFloat", "isNaN",
    "encodeURIComponent", "decodeURIComponent",
}

def _extract_ts_call_graph(root: Any, content: str, file_path: str, symbols: list[Symbol]) -> list[CallEdge]:
    """Extract function call edges from TypeScript/JavaScript AST using tree-sitter."""
    from codd.extractor import CallEdge

    content_bytes = content.encode("utf-8", errors="ignore")
    edges: list[CallEdge] = []
    symbol_names = {s.name for s in symbols}

    def _current_scope(node: Any) -> str:
        """Walk parents to find enclosing function/method/class scope."""
        parts: list[str] = []
        current = node.parent
        while current is not None:
            if current.type in (
                "function_declaration",
                "function",
                "method_definition",
                "arrow_function",
                "class_declaration",
                "class",
            ):
                name = _field_text(content_bytes, current, "name")
                if name:
                    parts.append(name)
            current = current.parent
        parts.reverse()
        return ".".join(parts) if parts else "<module>"

    def _callee_name(func_node: Any) -> str | None:
        """Extract callee name from the function child of a call/new expression."""
        if func_node is None:
            return None
        node_type = func_node.type
        if node_type == "identifier":
            return _node_text(content_bytes, func_node).strip()
        if node_type in ("member_expression", "optional_chain"):
            obj = func_node.child_by_field_name("object")
            prop = func_node.child_by_field_name("property")
            if obj is not None and prop is not None:
                obj_text = _node_text(content_bytes, obj).strip()
                prop_text = _node_text(content_bytes, prop).strip()
                return f"{obj_text}.{prop_text}"
            # Fallback: return full text
            return _node_text(content_bytes, func_node).strip()
        # Other node types (parenthesized_expression, etc.) — use full text
        return _node_text(content_bytes, func_node).strip()

    for node in _iter_named_nodes(root):
        if node.type == "call_expression":
            func_node = node.child_by_field_name("function")
            is_async = node.parent is not None and node.parent.type == "await_expression"
        elif node.type == "new_expression":
            func_node = node.child_by_field_name("constructor")
            is_async = False
        else:
            continue

        callee_text = _callee_name(func_node)
        if not callee_text:
            continue

        bare_name = callee_text.split(".")[-1] if "." in callee_text else callee_text

        # Skip known JS/TS builtins
        root_name = callee_text.split(".")[0]
        if root_name in _TS_BUILTIN_NAMES or bare_name in _TS_BUILTIN_NAMES:
            continue

        # Only include calls to known project symbols (intra-project filter)
        if bare_name not in symbol_names and callee_text not in symbol_names:
            # Allow method calls on this/self (this.method)
            if callee_text.startswith("this."):
                method_name = callee_text[5:]
                if method_name not in symbol_names:
                    continue
            else:
                continue

        caller = _current_scope(node)
        line_no = node.start_point.row + 1

        edges.append(CallEdge(
            caller=caller,
            callee=callee_text,
            call_site=f"{file_path}:{line_no}",
            is_async=is_async,
        ))

    return edges
