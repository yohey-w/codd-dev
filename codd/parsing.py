"""Extractor abstractions and non-source parsers shared by CoDD backends."""

from __future__ import annotations

import ast
import fnmatch
import io
import json
import os
import re
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import yaml

try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

try:
    import hcl2
except ModuleNotFoundError:
    hcl2 = None

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

_PRISMA_SCALARS = {
    "BigInt",
    "Boolean",
    "Bytes",
    "DateTime",
    "Decimal",
    "Float",
    "Int",
    "Json",
    "String",
    "Unsupported",
}

_IGNORED_DIR_NAMES = {
    ".git",
    ".terraform",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "site-packages",
    "vendor",
    "venv",
}

_OPENAPI_METHODS = {
    "delete",
    "get",
    "head",
    "options",
    "patch",
    "post",
    "put",
    "trace",
}


@dataclass
class ApiSpecInfo:
    """Normalized representation of a discovered API definition file."""

    format: str
    file_path: str
    endpoints: list[dict[str, Any]] = field(default_factory=list)
    schemas: list[dict[str, Any]] = field(default_factory=list)
    services: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ConfigInfo:
    """Normalized representation of infrastructure/configuration files."""

    format: str
    file_path: str
    services: list[dict[str, Any]] = field(default_factory=list)
    resources: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class BuildDepsInfo:
    """Build and dependency metadata extracted from project manifests."""

    file_path: str
    runtime: list[str] = field(default_factory=list)
    dev: list[str] = field(default_factory=list)
    scripts: dict[str, str] = field(default_factory=dict)


@dataclass
class TestInfo:
    """Per-test-file metadata used for test-to-source mapping."""

    file_path: str
    test_functions: list[str] = field(default_factory=list)
    fixtures: list[str] = field(default_factory=list)
    source_module: str | None = None


@dataclass
class SqlSchemaInfo:
    """Extracted SQL DDL metadata."""

    file_path: str
    tables: list[dict[str, Any]] = field(default_factory=list)
    foreign_keys: list[dict[str, Any]] = field(default_factory=list)
    indexes: list[dict[str, Any]] = field(default_factory=list)
    views: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PrismaSchemaInfo:
    """Extracted Prisma schema metadata."""

    file_path: str
    models: list[dict[str, Any]] = field(default_factory=list)
    enums: list[dict[str, Any]] = field(default_factory=list)


class LanguageExtractor(Protocol):
    """Common interface for language-aware symbol/import extraction."""

    language: str
    category: str

    def extract_symbols(self, content: str, file_path: str) -> list[Symbol]:
        """Return symbols found in the given source content."""

    def extract_imports(
        self,
        content: str,
        file_path: Path,
        project_root: Path,
        src_dir: Path,
    ) -> tuple[dict[str, list[str]], set[str]]:
        """Return internal and external imports for the given source content."""

    def detect_code_patterns(self, mod: ModuleInfo, content: str) -> None:
        """Mutate ModuleInfo with any detected structural patterns."""

    def extract_schema(self, content: str, file_path: str | Path) -> SqlSchemaInfo | PrismaSchemaInfo | None:
        """Return schema information when supported by the extractor."""

    def extract_call_graph(self, content: str, file_path: str, symbols: list[Symbol]) -> list[CallEdge]:
        """Return call edges found in the given source content."""


class RegexExtractor:
    """Adapter for regex-based extraction and schema parsing."""

    def __init__(self, language: str, category: str = "source"):
        self.language = language.lower()
        self.category = category.lower()

    def extract_symbols(self, content: str, file_path: str) -> list[Symbol]:
        from codd import extractor as extractor_module

        return extractor_module._extract_symbols(content, file_path, self.language)

    def extract_imports(
        self,
        content: str,
        file_path: Path,
        project_root: Path,
        src_dir: Path,
    ) -> tuple[dict[str, list[str]], set[str]]:
        from codd import extractor as extractor_module

        return extractor_module._extract_imports(
            content,
            self.language,
            project_root,
            src_dir,
            file_path,
        )

    def detect_code_patterns(self, mod: ModuleInfo, content: str) -> None:
        from codd import extractor as extractor_module

        extractor_module._detect_code_patterns(mod, content, self.language)
        return None

    def extract_schema(self, content: str, file_path: str | Path) -> SqlSchemaInfo | PrismaSchemaInfo | None:
        if self.category != "schema":
            return None

        normalized_path = Path(file_path).as_posix()
        if self.language == "sql":
            return _extract_sql_schema(content, normalized_path)
        if self.language == "prisma":
            return _extract_prisma_schema(content, normalized_path)
        return None

    def extract_call_graph(self, content: str, file_path: str, symbols: list[Symbol]) -> list[CallEdge]:
        return []  # Regex fallback doesn't support call graph


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


class SqlDdlExtractor:
    """Tree-sitter backed extractor for SQL DDL artifacts."""

    language = "sql"
    category = "schema"

    def __init__(self):
        self._fallback = RegexExtractor(self.language, self.category)
        self._parser = _build_parser(self.language)

    @classmethod
    def is_available(cls) -> bool:
        return TreeSitterExtractor.is_available("sql")

    def extract_symbols(self, content: str, file_path: str) -> list[Symbol]:
        return []

    def extract_imports(
        self,
        content: str,
        file_path: Path,
        project_root: Path,
        src_dir: Path,
    ) -> tuple[dict[str, list[str]], set[str]]:
        return {}, set()

    def detect_code_patterns(self, mod: ModuleInfo, content: str) -> None:
        return None

    def extract_schema(self, content: str, file_path: str | Path) -> SqlSchemaInfo | None:
        path = Path(file_path).as_posix()
        try:
            root = self._parser.parse(content.encode("utf-8", errors="ignore")).root_node
            return _extract_sql_schema_from_tree(root, content, path)
        except Exception:
            fallback = self._fallback.extract_schema(content, path)
            return fallback if isinstance(fallback, SqlSchemaInfo) else None

    def extract_call_graph(self, content: str, file_path: str, symbols: list[Symbol]) -> list[CallEdge]:
        return []


class PrismaSchemaExtractor:
    """Regex extractor for Prisma schema files."""

    language = "prisma"
    category = "schema"

    def extract_symbols(self, content: str, file_path: str) -> list[Symbol]:
        return []

    def extract_imports(
        self,
        content: str,
        file_path: Path,
        project_root: Path,
        src_dir: Path,
    ) -> tuple[dict[str, list[str]], set[str]]:
        return {}, set()

    def detect_code_patterns(self, mod: ModuleInfo, content: str) -> None:
        return None

    def extract_schema(self, content: str, file_path: str | Path) -> PrismaSchemaInfo | None:
        return _extract_prisma_schema(content, Path(file_path).as_posix())

    def extract_call_graph(self, content: str, file_path: str, symbols: list[Symbol]) -> list[CallEdge]:
        return []


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


def _make_symbol(
    name: str,
    kind: str,
    file_path: str,
    line: int,
    *,
    params: str = "",
    return_type: str = "",
    decorators: list[str] | None = None,
    is_async: bool = False,
    bases: list[str] | None = None,
    implements: list[str] | None = None,
):
    from codd.extractor import Symbol

    return Symbol(
        name=name,
        kind=kind,
        file=file_path,
        line=line,
        params=params,
        return_type=return_type,
        decorators=list(decorators or []),
        is_async=is_async,
        bases=list(bases or []),
        implements=list(implements or []),
    )


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


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _strip_wrapping(text: str, opening: str, closing: str) -> str:
    stripped = text.strip()
    if stripped.startswith(opening) and stripped.endswith(closing):
        return stripped[len(opening):-len(closing)].strip()
    return stripped


def _split_csv(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


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
                if ".test." in _fp or ".spec." in _fp or "test-harness" in _fp:
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
        if node.type not in {"export_statement", "import_statement"}:
            continue
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

    return internal, external


def _route_from_decorator(decorator: str) -> str | None:
    match = re.search(
        r'(?:^|\.)\s*(?:route|get|post|put|delete|patch)\s*\(\s*(?:path\s*=\s*)?["\']([^"\']+)["\']',
        decorator.replace("\n", " "),
    )
    if match:
        return match.group(1)
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

    route_matches = re.findall(
        r'(?:app|router)\.(?:get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']',
        content,
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


def _sql_first_object_name(content_bytes: bytes, node: Any) -> str:
    for child in getattr(node, "named_children", []):
        if child.type == "object_reference":
            return _normalize_ws(_node_text(content_bytes, child))
    return ""


def _regex_foreign_keys(statement_text: str, table_name: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    pattern = re.compile(
        r"(?:CONSTRAINT\s+(?P<name>\w+)\s+)?FOREIGN\s+KEY\s*\((?P<columns>[^)]+)\)\s+REFERENCES\s+(?P<ref_table>[^\s(]+)\s*\((?P<ref_columns>[^)]+)\)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(statement_text):
        matches.append(
            {
                "name": match.group("name") or "",
                "table": table_name,
                "columns": _split_csv(match.group("columns")),
                "references_table": match.group("ref_table"),
                "references_columns": _split_csv(match.group("ref_columns")),
            }
        )
    return matches


def _regex_create_index(statement_text: str) -> dict[str, Any] | None:
    match = re.search(
        r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?P<name>[^\s]+)\s+ON\s+(?P<table>[^\s(]+)\s*\((?P<columns>[^)]+)\)",
        statement_text,
        re.IGNORECASE,
    )
    if not match:
        return None
    return {
        "name": match.group("name"),
        "table": match.group("table"),
        "columns": _split_csv(match.group("columns")),
    }


def _append_foreign_key(
    schema: SqlSchemaInfo,
    foreign_key: dict[str, Any],
    seen_foreign_keys: set[tuple[str, tuple[str, ...], str, tuple[str, ...]]],
):
    key = (
        str(foreign_key.get("table", "")),
        tuple(foreign_key.get("columns", [])),
        str(foreign_key.get("references_table", "")),
        tuple(foreign_key.get("references_columns", [])),
    )
    if key in seen_foreign_keys:
        return
    seen_foreign_keys.add(key)
    schema.foreign_keys.append(foreign_key)


def _extract_sql_schema_from_tree(root: Any, content: str, file_path: str) -> SqlSchemaInfo:
    content_bytes = content.encode("utf-8", errors="ignore")
    schema = SqlSchemaInfo(file_path=file_path)
    seen_foreign_keys: set[tuple[str, tuple[str, ...], str, tuple[str, ...]]] = set()

    for node in _iter_named_nodes(root):
        statement_text = _normalize_ws(_node_text(content_bytes, node))
        if node.type == "create_table":
            table_name = _sql_first_object_name(content_bytes, node)
            if not table_name:
                continue
            columns: list[dict[str, Any]] = []
            constraints: list[str] = []
            column_defs = next((child for child in getattr(node, "named_children", []) if child.type == "column_definitions"), None)
            if column_defs is not None:
                for child in getattr(column_defs, "named_children", []):
                    if child.type == "column_definition":
                        name = _field_text(content_bytes, child, "name")
                        column_type = _normalize_ws(_field_text(content_bytes, child, "type"))
                        column_text = _normalize_ws(_node_text(content_bytes, child))
                        columns.append(
                            {
                                "name": name,
                                "type": column_type,
                                "constraints": column_text.replace(name, "", 1).strip(),
                            }
                        )
                    elif child.type in {"constraint", "constraints", "ERROR"}:
                        constraint_text = _normalize_ws(_node_text(content_bytes, child))
                        if constraint_text:
                            constraints.append(constraint_text)
            schema.tables.append({"name": table_name, "columns": columns, "constraints": constraints})
            for foreign_key in _regex_foreign_keys(statement_text, table_name):
                _append_foreign_key(schema, foreign_key, seen_foreign_keys)
        elif node.type == "alter_table":
            table_name = _sql_first_object_name(content_bytes, node)
            if table_name:
                for foreign_key in _regex_foreign_keys(statement_text, table_name):
                    _append_foreign_key(schema, foreign_key, seen_foreign_keys)
        elif node.type == "create_index":
            index = _regex_create_index(statement_text)
            if index:
                schema.indexes.append(index)
        elif node.type == "create_view":
            view_name = _sql_first_object_name(content_bytes, node)
            if view_name:
                schema.views.append({"name": view_name, "definition": statement_text})

    return schema


def _extract_sql_schema(content: str, file_path: str) -> SqlSchemaInfo:
    schema = SqlSchemaInfo(file_path=file_path)
    if TreeSitterExtractor.is_available("sql"):
        try:
            parser = _build_parser("sql")
            root = parser.parse(content.encode("utf-8", errors="ignore")).root_node
            return _extract_sql_schema_from_tree(root, content, file_path)
        except Exception:
            pass

    for table_match in re.finditer(
        r"CREATE\s+TABLE\s+(?P<name>[^\s(]+)\s*\((?P<body>.*?)\)\s*;",
        content,
        re.IGNORECASE | re.DOTALL,
    ):
        table_name = table_match.group("name")
        body = table_match.group("body")
        columns: list[dict[str, Any]] = []
        constraints: list[str] = []
        for raw_line in body.splitlines():
            line = raw_line.strip().rstrip(",")
            if not line:
                continue
            if "FOREIGN KEY" in line.upper() or line.upper().startswith("CONSTRAINT "):
                constraints.append(line)
                continue
            match = re.match(r"(?P<name>[^\s]+)\s+(?P<type>[^\s,]+)(?P<rest>.*)", line)
            if not match:
                continue
            columns.append(
                {
                    "name": match.group("name"),
                    "type": match.group("type"),
                    "constraints": match.group("rest").strip(),
                }
            )
        schema.tables.append({"name": table_name, "columns": columns, "constraints": constraints})
        schema.foreign_keys.extend(_regex_foreign_keys(_normalize_ws(table_match.group(0)), table_name))

    for statement in re.findall(r"ALTER\s+TABLE\s+.*?;", content, re.IGNORECASE | re.DOTALL):
        match = re.search(r"ALTER\s+TABLE\s+([^\s;]+)", statement, re.IGNORECASE)
        if match:
            schema.foreign_keys.extend(_regex_foreign_keys(_normalize_ws(statement), match.group(1)))

    for index_match in re.finditer(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+.*?;", content, re.IGNORECASE | re.DOTALL):
        index = _regex_create_index(_normalize_ws(index_match.group(0)))
        if index:
            schema.indexes.append(index)

    for view_match in re.finditer(
        r"CREATE\s+VIEW\s+(?P<name>[^\s]+)\s+AS\s+(?P<query>.*?);",
        content,
        re.IGNORECASE | re.DOTALL,
    ):
        schema.views.append({"name": view_match.group("name"), "definition": _normalize_ws(view_match.group("query"))})

    return schema


def _extract_prisma_schema(content: str, file_path: str) -> PrismaSchemaInfo:
    schema = PrismaSchemaInfo(file_path=file_path)
    for name, block in _find_named_blocks(content, "model"):
        fields: list[dict[str, Any]] = []
        relations: list[dict[str, Any]] = []
        for raw_line in block.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("//") or line.startswith("@@"):
                continue
            match = re.match(r"(\w+)\s+([^\s]+)\s*(.*)", line)
            if not match:
                continue
            field_name = match.group(1)
            field_type = match.group(2)
            attributes = match.group(3).strip()
            base_type = field_type.rstrip("?").rstrip("[]")
            is_relation = "@relation" in attributes or base_type not in _PRISMA_SCALARS
            field_info = {
                "name": field_name,
                "type": field_type,
                "attributes": attributes,
                "is_relation": is_relation,
            }
            fields.append(field_info)
            if is_relation:
                relations.append(field_info)
        schema.models.append({"name": name, "fields": fields, "relations": relations})

    for name, block in _find_named_blocks(content, "enum"):
        values = [line.strip().split()[0] for line in block.splitlines() if line.strip() and not line.strip().startswith("//")]
        schema.enums.append({"name": name, "values": values})

    return schema


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


class OpenApiExtractor:
    """Extract endpoints and schema metadata from OpenAPI/Swagger documents."""

    format = "openapi"

    def detect_openapi_files(self, project_root: Path) -> list[Path]:
        matches: list[Path] = []
        for file_path in _iter_project_files(project_root, {".json", ".yaml", ".yml"}):
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            data = _load_structured_document(content, file_path)
            if isinstance(data, dict) and any(key in data for key in ("openapi", "swagger")):
                matches.append(file_path)
        return matches

    def extract_endpoints(self, content: str, file_path: str) -> ApiSpecInfo:
        data = _load_structured_document(content, file_path)
        spec = ApiSpecInfo(format=self.format, file_path=file_path)
        if not isinstance(data, dict):
            return spec

        for path_name, methods in (data.get("paths") or {}).items():
            if not isinstance(methods, dict):
                continue
            for method, operation in methods.items():
                if method.lower() not in _OPENAPI_METHODS or not isinstance(operation, dict):
                    continue
                spec.endpoints.append(
                    {
                        "path": path_name,
                        "method": method.upper(),
                        "operation_id": operation.get("operationId", ""),
                        "summary": operation.get("summary", "") or operation.get("description", ""),
                        "tags": operation.get("tags", []) or [],
                        "parameters": [
                            {
                                "name": parameter.get("name", ""),
                                "in": parameter.get("in", ""),
                                "required": bool(parameter.get("required", False)),
                            }
                            for parameter in operation.get("parameters", []) or []
                            if isinstance(parameter, dict)
                        ],
                        "request_body": sorted(
                            (operation.get("requestBody") or {}).get("content", {}).keys()
                        )
                        if isinstance(operation.get("requestBody"), dict)
                        else [],
                        "responses": sorted((operation.get("responses") or {}).keys()),
                    }
                )

        components = data.get("components") or {}
        for schema_name, schema in (components.get("schemas") or {}).items():
            if not isinstance(schema, dict):
                continue
            spec.schemas.append(
                {
                    "name": schema_name,
                    "kind": "schema",
                    "type": schema.get("type", "") or schema.get("$ref", ""),
                    "required": schema.get("required", []) or [],
                    "properties": _extract_openapi_properties(schema.get("properties") or {}),
                }
            )

        for server in data.get("servers") or []:
            if isinstance(server, dict):
                spec.services.append(
                    {
                        "kind": "server",
                        "url": server.get("url", ""),
                        "description": server.get("description", ""),
                    }
                )

        if not spec.services and data.get("host"):
            base_path = data.get("basePath", "")
            schemes = data.get("schemes") or ["https"]
            spec.services.append(
                {
                    "kind": "server",
                    "url": f"{schemes[0]}://{data['host']}{base_path}",
                    "description": "swagger-host",
                }
            )

        return spec


class GraphQlExtractor:
    """Extract GraphQL types and root operations from SDL files."""

    format = "graphql"

    def detect_graphql_files(self, project_root: Path) -> list[Path]:
        return list(_iter_project_files(project_root, {".gql", ".graphql", ".graphqls"}))

    def extract_schema(self, content: str, file_path: str) -> ApiSpecInfo:
        graphql_objects = _load_graphql_symbols()
        if graphql_objects is not None:
            return self._extract_with_graphql_core(content, file_path, graphql_objects)
        return self._extract_with_regex_fallback(content, file_path)

    def _extract_with_graphql_core(
        self,
        content: str,
        file_path: str,
        graphql_objects: dict[str, Any],
    ) -> ApiSpecInfo:
        spec = ApiSpecInfo(format=self.format, file_path=file_path)
        parse = graphql_objects["parse"]
        gql_ast = graphql_objects["ast"]

        try:
            document = parse(content)
        except Exception:
            return self._extract_with_regex_fallback(content, file_path)

        for definition in document.definitions:
            if isinstance(definition, gql_ast.ObjectTypeDefinitionNode):
                type_name = definition.name.value
                fields = [_graphql_field_to_dict(field) for field in definition.fields or []]
                if type_name in {"Query", "Mutation", "Subscription"}:
                    for field in fields:
                        spec.endpoints.append(
                            {
                                "name": field["name"],
                                "operation_type": type_name.lower(),
                                "return_type": field["type"],
                                "arguments": field["arguments"],
                            }
                        )
                else:
                    spec.schemas.append(
                        {
                            "name": type_name,
                            "kind": "type",
                            "fields": fields,
                        }
                    )
            elif isinstance(definition, gql_ast.InputObjectTypeDefinitionNode):
                spec.schemas.append(
                    {
                        "name": definition.name.value,
                        "kind": "input",
                        "fields": [_graphql_input_value_to_dict(field) for field in definition.fields or []],
                    }
                )
            elif isinstance(definition, gql_ast.EnumTypeDefinitionNode):
                spec.schemas.append(
                    {
                        "name": definition.name.value,
                        "kind": "enum",
                        "values": [value.name.value for value in definition.values or []],
                    }
                )
            elif isinstance(definition, gql_ast.InterfaceTypeDefinitionNode):
                spec.schemas.append(
                    {
                        "name": definition.name.value,
                        "kind": "interface",
                        "fields": [_graphql_field_to_dict(field) for field in definition.fields or []],
                    }
                )
            elif isinstance(definition, gql_ast.ScalarTypeDefinitionNode):
                spec.schemas.append(
                    {
                        "name": definition.name.value,
                        "kind": "scalar",
                    }
                )

        return spec

    def _extract_with_regex_fallback(self, content: str, file_path: str) -> ApiSpecInfo:
        spec = ApiSpecInfo(format=self.format, file_path=file_path)
        for kind in ("type", "input", "interface"):
            for name, block in _find_named_blocks(content, kind):
                fields = _parse_graphql_fields(block)
                if kind == "type" and name in {"Query", "Mutation", "Subscription"}:
                    for field in fields:
                        spec.endpoints.append(
                            {
                                "name": field["name"],
                                "operation_type": name.lower(),
                                "return_type": field["type"],
                                "arguments": field["arguments"],
                            }
                        )
                else:
                    spec.schemas.append(
                        {
                            "name": name,
                            "kind": kind,
                            "fields": fields,
                        }
                    )

        for name, block in _find_named_blocks(content, "enum"):
            values = [line.strip().split()[0] for line in block.splitlines() if line.strip()]
            spec.schemas.append({"name": name, "kind": "enum", "values": values})

        return spec


class ProtobufExtractor:
    """Extract services, RPCs, messages, and enums from protobuf files."""

    format = "protobuf"

    def detect_proto_files(self, project_root: Path) -> list[Path]:
        return list(_iter_project_files(project_root, {".proto"}))

    def extract_services(self, content: str, file_path: str) -> ApiSpecInfo:
        spec = ApiSpecInfo(format=self.format, file_path=file_path)

        for name, block in _find_named_blocks(content, "message"):
            spec.schemas.append(
                {
                    "name": name,
                    "kind": "message",
                    "fields": _parse_proto_fields(block),
                }
            )

        for name, block in _find_named_blocks(content, "enum"):
            spec.schemas.append(
                {
                    "name": name,
                    "kind": "enum",
                    "values": _parse_proto_enum_values(block),
                }
            )

        for service_name, block in _find_named_blocks(content, "service"):
            rpcs = _parse_proto_rpcs(block)
            spec.services.append(
                {
                    "name": service_name,
                    "kind": "service",
                    "rpcs": rpcs,
                }
            )
            for rpc in rpcs:
                spec.endpoints.append(
                    {
                        "name": rpc["name"],
                        "operation_type": "rpc",
                        "request_type": rpc["request_type"],
                        "response_type": rpc["response_type"],
                        "service": service_name,
                    }
                )

        return spec


class DockerComposeExtractor:
    """Extract docker-compose style service definitions."""

    format = "docker-compose"
    file_names = {
        "compose.yaml",
        "compose.yml",
        "docker-compose.override.yaml",
        "docker-compose.override.yml",
        "docker-compose.yaml",
        "docker-compose.yml",
    }

    def detect_docker_compose(self, project_root: Path) -> list[Path]:
        return [
            file_path
            for file_path in _iter_project_files(project_root, {".yaml", ".yml"})
            if file_path.name in self.file_names
        ]

    def extract_services(self, content: str, file_path: str) -> ConfigInfo:
        payload = _load_structured_document(content, file_path)
        info = ConfigInfo(format=self.format, file_path=file_path)
        if not isinstance(payload, dict):
            return info

        services = payload.get("services") or {}
        if not isinstance(services, dict):
            return info

        for name, config in services.items():
            if not isinstance(config, dict):
                continue
            depends_on = config.get("depends_on", [])
            if isinstance(depends_on, dict):
                depends_on = list(depends_on.keys())
            info.services.append(
                {
                    "name": str(name),
                    "image": str(config.get("image", "")),
                    "ports": _normalize_list(config.get("ports")),
                    "depends_on": _normalize_list(depends_on),
                    "volumes": _normalize_list(config.get("volumes")),
                    "environment": _normalize_environment(config.get("environment")),
                }
            )

        return info


class KubernetesExtractor:
    """Extract Kubernetes resources from YAML manifests."""

    format = "kubernetes"
    supported_kinds = {"ConfigMap", "Deployment", "Ingress", "Service"}

    def detect_k8s_manifests(self, project_root: Path) -> list[Path]:
        matches: list[Path] = []
        for file_path in _iter_project_files(project_root, {".yaml", ".yml"}):
            docs = _load_yaml_documents(file_path)
            if any(
                isinstance(doc, dict) and doc.get("kind") in self.supported_kinds
                for doc in docs
            ):
                matches.append(file_path)
        return matches

    def extract_manifests(self, content: str, file_path: str) -> ConfigInfo:
        info = ConfigInfo(format=self.format, file_path=file_path)
        try:
            docs = list(yaml.safe_load_all(content))
        except yaml.YAMLError:
            return info

        for doc in docs:
            if not isinstance(doc, dict):
                continue
            kind = doc.get("kind")
            if kind not in self.supported_kinds:
                continue

            metadata = doc.get("metadata") or {}
            resource: dict[str, Any] = {
                "kind": kind,
                "name": metadata.get("name", ""),
            }

            if kind == "Deployment":
                spec = doc.get("spec") or {}
                pod_spec = ((spec.get("template") or {}).get("spec") or {})
                resource["replicas"] = spec.get("replicas", 1)
                resource["containers"] = [
                    {
                        "name": container.get("name", ""),
                        "image": container.get("image", ""),
                        "ports": [
                            port.get("containerPort")
                            for port in container.get("ports", []) or []
                            if isinstance(port, dict) and "containerPort" in port
                        ],
                    }
                    for container in pod_spec.get("containers", []) or []
                    if isinstance(container, dict)
                ]
            elif kind == "Service":
                spec = doc.get("spec") or {}
                resource["service_type"] = spec.get("type", "ClusterIP")
                resource["selector"] = spec.get("selector") or {}
                resource["ports"] = [
                    {
                        "port": port.get("port"),
                        "targetPort": port.get("targetPort"),
                    }
                    for port in spec.get("ports", []) or []
                    if isinstance(port, dict)
                ]
            elif kind == "Ingress":
                spec = doc.get("spec") or {}
                resource["rules"] = [
                    {
                        "host": rule.get("host"),
                        "paths": [
                            {
                                "path": path_cfg.get("path"),
                                "service": ((path_cfg.get("backend") or {}).get("service") or {}).get("name"),
                            }
                            for path_cfg in ((rule.get("http") or {}).get("paths") or [])
                            if isinstance(path_cfg, dict)
                        ],
                    }
                    for rule in spec.get("rules", []) or []
                    if isinstance(rule, dict)
                ]
            elif kind == "ConfigMap":
                data = doc.get("data") or {}
                resource["data_keys"] = sorted(str(key) for key in data.keys())

            info.resources.append(resource)

        return info


class TerraformExtractor:
    """Extract Terraform resources via python-hcl2 or a regex fallback."""

    format = "terraform"
    _RESOURCE_BLOCK_RE = re.compile(
        r'^\s*(resource|data)\s+"([^"]+)"\s+"([^"]+)"\s*\{',
        re.MULTILINE,
    )
    _NAMED_BLOCK_RE = re.compile(
        r'^\s*(module|variable)\s+"([^"]+)"\s*\{',
        re.MULTILINE,
    )

    @classmethod
    def is_available(cls) -> bool:
        return hcl2 is not None or find_spec("hcl2") is not None

    def detect_tf_files(self, project_root: Path) -> list[Path]:
        return list(_iter_project_files(project_root, {".tf"}))

    def extract_resources(self, content: str, file_path: str) -> ConfigInfo:
        info = ConfigInfo(format=self.format, file_path=file_path)
        if hcl2 is None:
            return self._extract_resources_regex(content, file_path)

        try:
            parsed = hcl2.load(io.StringIO(content))
        except Exception:
            return self._extract_resources_regex(content, file_path)

        for block in parsed.get("resource", []) or []:
            if not isinstance(block, dict):
                continue
            for resource_type, instances in block.items():
                if not isinstance(instances, dict):
                    continue
                for name, attributes in instances.items():
                    info.resources.append(
                        {
                            "kind": "resource",
                            "type": resource_type.strip('"'),
                            "name": name.strip('"'),
                            "attributes": attributes or {},
                        }
                    )

        for block in parsed.get("data", []) or []:
            if not isinstance(block, dict):
                continue
            for data_type, instances in block.items():
                if not isinstance(instances, dict):
                    continue
                for name, attributes in instances.items():
                    info.resources.append(
                        {
                            "kind": "data",
                            "type": data_type.strip('"'),
                            "name": name.strip('"'),
                            "attributes": attributes or {},
                        }
                    )

        for block in parsed.get("module", []) or []:
            if not isinstance(block, dict):
                continue
            for name, attributes in block.items():
                info.resources.append(
                    {
                        "kind": "module",
                        "name": name.strip('"'),
                        "attributes": attributes or {},
                    }
                )

        for block in parsed.get("variable", []) or []:
            if not isinstance(block, dict):
                continue
            for name, attributes in block.items():
                info.resources.append(
                    {
                        "kind": "variable",
                        "name": name.strip('"'),
                        "attributes": attributes or {},
                    }
                )

        return info

    def _extract_resources_regex(self, content: str, file_path: str) -> ConfigInfo:
        """Fallback parser for simple Terraform blocks when python-hcl2 is unavailable."""
        info = ConfigInfo(format=self.format, file_path=file_path)

        for kind, block_type, name in self._RESOURCE_BLOCK_RE.findall(content):
            info.resources.append(
                {
                    "kind": kind,
                    "type": block_type,
                    "name": name,
                    "attributes": {},
                }
            )

        for kind, name in self._NAMED_BLOCK_RE.findall(content):
            info.resources.append(
                {
                    "kind": kind,
                    "name": name,
                    "attributes": {},
                }
            )

        return info


class BuildDepsExtractor:
    """Extract build/runtime dependencies from common project manifests."""

    file_names = ("pyproject.toml", "package.json", "go.mod")

    def detect_build_files(self, project_root: Path) -> list[Path]:
        return [project_root / name for name in self.file_names if (project_root / name).exists()]

    def extract_deps(self, content: str, file_type: str, file_path: str = "") -> BuildDepsInfo:
        normalized = file_type.lower()
        if normalized == "pyproject.toml":
            return self._extract_pyproject(content, file_path)
        if normalized == "package.json":
            return self._extract_package_json(content, file_path)
        if normalized == "go.mod":
            return self._extract_go_mod(content, file_path)
        return BuildDepsInfo(file_path=file_path)

    def merge(self, infos: list[BuildDepsInfo]) -> BuildDepsInfo | None:
        if not infos:
            return None
        if len(infos) == 1:
            return infos[0]

        merged = BuildDepsInfo(
            file_path=", ".join(info.file_path for info in infos if info.file_path),
            runtime=[],
            dev=[],
            scripts={},
        )
        for info in infos:
            merged.runtime.extend(info.runtime)
            merged.dev.extend(info.dev)
            merged.scripts.update(info.scripts)
        merged.runtime = _dedupe(merged.runtime)
        merged.dev = _dedupe(merged.dev)
        return merged

    def _extract_pyproject(self, content: str, file_path: str) -> BuildDepsInfo:
        if tomllib is None:
            return BuildDepsInfo(file_path=file_path)

        try:
            payload = tomllib.loads(content)
        except Exception:
            return BuildDepsInfo(file_path=file_path)

        project = payload.get("project") or {}
        runtime = [str(dep) for dep in project.get("dependencies", []) or []]
        dev: list[str] = []
        for deps in (project.get("optional-dependencies") or {}).values():
            if isinstance(deps, list):
                dev.extend(str(dep) for dep in deps)

        scripts = {
            str(name): str(target)
            for name, target in (project.get("scripts") or {}).items()
        }
        return BuildDepsInfo(
            file_path=file_path,
            runtime=_dedupe(runtime),
            dev=_dedupe(dev),
            scripts=scripts,
        )

    def _extract_package_json(self, content: str, file_path: str) -> BuildDepsInfo:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return BuildDepsInfo(file_path=file_path)

        return BuildDepsInfo(
            file_path=file_path,
            runtime=sorted((payload.get("dependencies") or {}).keys()),
            dev=sorted((payload.get("devDependencies") or {}).keys()),
            scripts={
                str(name): str(command)
                for name, command in (payload.get("scripts") or {}).items()
            },
        )

    def _extract_go_mod(self, content: str, file_path: str) -> BuildDepsInfo:
        runtime: list[str] = []
        scripts: dict[str, str] = {}
        in_require_block = False

        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("//"):
                continue
            if line == "require (":
                in_require_block = True
                continue
            if line == ")" and in_require_block:
                in_require_block = False
                continue
            if line.startswith("require "):
                runtime.append(line.removeprefix("require ").split()[0])
                continue
            if in_require_block:
                runtime.append(line.split()[0])
                continue
            if line.startswith("replace "):
                left, _, right = line.removeprefix("replace ").partition("=>")
                scripts[f"replace:{left.strip()}"] = right.strip()

        return BuildDepsInfo(
            file_path=file_path,
            runtime=_dedupe(runtime),
            dev=[],
            scripts=scripts,
        )

    def extract_call_graph(self, content: str, file_path: str, symbols: list[Symbol]) -> list[CallEdge]:
        return []


class TestExtractor:
    """Extract test metadata from test files."""

    def __init__(self, language: str):
        self.language = language.lower()

    def detect_test_files(self, project_root: Path) -> list[Path]:
        suffixes = {
            "python": {".py"},
            "typescript": {".ts", ".tsx"},
            "javascript": {".js", ".jsx"},
            "go": {".go"},
        }.get(self.language, set())
        if not suffixes:
            return []

        matches: list[Path] = []
        for file_path in _iter_project_files(project_root, suffixes):
            if self._is_test_file(file_path.name):
                matches.append(file_path)
        return matches

    def extract_test_info(self, content: str, file_path: str) -> TestInfo:
        if self.language == "python":
            return self._extract_python(content, file_path)
        if self.language in {"typescript", "javascript"}:
            return self._extract_javascript(content, file_path)
        if self.language == "go":
            return self._extract_go(content, file_path)
        return TestInfo(file_path=file_path)

    def _is_test_file(self, filename: str) -> bool:
        if self.language == "python":
            return filename.startswith("test_") or filename.endswith("_test.py")
        if self.language in {"typescript", "javascript"}:
            return any(
                filename.endswith(suffix)
                for suffix in (
                    ".test.ts",
                    ".spec.ts",
                    ".test.tsx",
                    ".spec.tsx",
                    ".test.js",
                    ".spec.js",
                )
            )
        if self.language == "go":
            return filename.endswith("_test.go")
        return False

    def _extract_python(self, content: str, file_path: str) -> TestInfo:
        tests: list[str] = []
        fixtures: list[str] = []
        pending_fixture = False

        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("@pytest.fixture"):
                pending_fixture = True
                continue

            match = re.match(r"^\s*def\s+(\w+)\s*\(", line)
            if not match:
                continue

            name = match.group(1)
            if pending_fixture:
                fixtures.append(name)
                pending_fixture = False
                continue
            if name.startswith("test_"):
                tests.append(name)
            elif name in {"setUp", "tearDown", "setup_method", "teardown_method"}:
                fixtures.append(name)

        return TestInfo(file_path=file_path, test_functions=tests, fixtures=fixtures)

    def _extract_javascript(self, content: str, file_path: str) -> TestInfo:
        tests = re.findall(r"\b(?:it|test|describe)\s*\(\s*['\"]([^'\"]+)['\"]", content)
        fixtures = re.findall(r"\b(beforeEach|afterEach|beforeAll|afterAll)\s*\(", content)
        return TestInfo(file_path=file_path, test_functions=tests, fixtures=fixtures)

    def _extract_go(self, content: str, file_path: str) -> TestInfo:
        tests = re.findall(r"^\s*func\s+(Test\w+)\s*\(", content, re.MULTILINE)
        fixtures = re.findall(r"^\s*func\s+(TestMain)\s*\(", content, re.MULTILINE)
        return TestInfo(file_path=file_path, test_functions=tests, fixtures=fixtures)

    def extract_call_graph(self, content: str, file_path: str, symbols: list[Symbol]) -> list[CallEdge]:
        return []


def get_extractor(language: str, category: str = "source") -> LanguageExtractor:
    """Select the best available extractor for a language/category pair."""
    normalized_language = language.lower()
    normalized_category = category.lower()

    if normalized_category == "schema":
        if normalized_language == "sql":
            if SqlDdlExtractor.is_available():
                return SqlDdlExtractor()
            return RegexExtractor(normalized_language, normalized_category)
        if normalized_language == "prisma":
            return PrismaSchemaExtractor()
        return RegexExtractor(normalized_language, normalized_category)

    if (
        normalized_category == "source"
        and normalized_language in _TREE_SITTER_LANGUAGE_PACKAGES
        and TreeSitterExtractor.is_available(normalized_language)
    ):
        return TreeSitterExtractor(normalized_language, normalized_category)

    return RegexExtractor(normalized_language, normalized_category)


def _extract_openapi_properties(properties: dict[str, Any]) -> list[dict[str, Any]]:
    extracted: list[dict[str, Any]] = []
    for prop_name, prop in properties.items():
        if not isinstance(prop, dict):
            continue
        item_type = ""
        items = prop.get("items")
        if isinstance(items, dict):
            item_type = items.get("type", "") or items.get("$ref", "")
        extracted.append(
            {
                "name": prop_name,
                "type": prop.get("type", "") or prop.get("$ref", ""),
                "required": bool(prop.get("required", False)),
                "items": item_type,
            }
        )
    return extracted


def _load_structured_document(content: str, file_path: str | Path) -> Any:
    suffix = Path(file_path).suffix.lower()
    try:
        if suffix == ".json":
            return json.loads(content)
        return yaml.safe_load(content)
    except Exception:
        return None


def _load_yaml_documents(file_path: Path) -> list[dict[str, Any]]:
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
        docs = list(yaml.safe_load_all(content))
    except Exception:
        return []
    return [doc for doc in docs if isinstance(doc, dict)]


def _iter_project_files(project_root: Path, suffixes: set[str]):
    normalized = {suffix.lower() for suffix in suffixes}
    for root, dirs, files in os.walk(project_root):
        dirs[:] = [
            directory
            for directory in dirs
            if directory not in _IGNORED_DIR_NAMES and not directory.startswith(".pytest_cache")
        ]
        for filename in files:
            file_path = Path(root) / filename
            if file_path.suffix.lower() in normalized:
                yield file_path


def _normalize_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if value is None or value == "":
        return []
    return [str(value)]


def _normalize_environment(value: Any) -> dict[str, Any] | list[str]:
    if isinstance(value, dict):
        return {str(key): val for key, val in value.items()}
    if isinstance(value, list):
        return [str(item) for item in value]
    return {}


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _load_graphql_symbols() -> dict[str, Any] | None:
    try:
        from graphql import parse
        from graphql.language import ast as gql_ast
    except ImportError:
        return None
    return {"parse": parse, "ast": gql_ast}


def _graphql_field_to_dict(field: Any) -> dict[str, Any]:
    return {
        "name": field.name.value,
        "type": _graphql_type_to_string(field.type),
        "arguments": [_graphql_input_value_to_dict(argument) for argument in field.arguments or []],
    }


def _graphql_input_value_to_dict(field: Any) -> dict[str, Any]:
    return {
        "name": field.name.value,
        "type": _graphql_type_to_string(field.type),
        "default": _graphql_default_value(field.default_value),
    }


def _graphql_type_to_string(type_node: Any) -> str:
    kind_name = type(type_node).__name__
    if kind_name == "NonNullTypeNode":
        return f"{_graphql_type_to_string(type_node.type)}!"
    if kind_name == "ListTypeNode":
        return f"[{_graphql_type_to_string(type_node.type)}]"
    return type_node.name.value


def _graphql_default_value(value_node: Any) -> Any:
    if value_node is None:
        return None
    if hasattr(value_node, "value"):
        return value_node.value
    return str(value_node)


def _parse_graphql_fields(block: str) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    for raw_line in block.splitlines():
        line = raw_line.split("#", 1)[0].strip().rstrip(",")
        if not line:
            continue
        match = re.match(r"^(\w+)(?:\((.*?)\))?\s*:\s*([!\[\]\w]+)", line)
        if not match:
            continue
        arguments = []
        if match.group(2):
            for arg in match.group(2).split(","):
                arg_match = re.match(r"\s*(\w+)\s*:\s*([!\[\]\w]+)", arg.strip())
                if arg_match:
                    arguments.append(
                        {
                            "name": arg_match.group(1),
                            "type": arg_match.group(2),
                            "default": None,
                        }
                    )
        fields.append(
            {
                "name": match.group(1),
                "type": match.group(3),
                "arguments": arguments,
            }
        )
    return fields


def _find_named_blocks(content: str, keyword: str) -> list[tuple[str, str]]:
    matches: list[tuple[str, str]] = []
    pattern = re.compile(rf"\b{keyword}\s+(\w+)\b[^\{{]*\{{", re.MULTILINE)

    for match in pattern.finditer(content):
        brace_index = content.find("{", match.start())
        body = _extract_braced_body(content, brace_index)
        if body is not None:
            matches.append((match.group(1), body))

    return matches


def _extract_braced_body(content: str, brace_index: int) -> str | None:
    if brace_index < 0:
        return None

    depth = 0
    for index in range(brace_index, len(content)):
        char = content[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return content[brace_index + 1 : index]
    return None


def _parse_proto_fields(block: str) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    pattern = re.compile(
        r"^\s*(optional|required|repeated)?\s*([\w.]+)\s+(\w+)\s*=\s*(\d+)",
        re.MULTILINE,
    )
    for match in pattern.finditer(block):
        label = match.group(1) or ""
        fields.append(
            {
                "name": match.group(3),
                "type": match.group(2),
                "field_number": int(match.group(4)),
                "label": label,
            }
        )
    return fields


def _parse_proto_enum_values(block: str) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    pattern = re.compile(r"^\s*(\w+)\s*=\s*(-?\d+)", re.MULTILINE)
    for match in pattern.finditer(block):
        values.append({"name": match.group(1), "number": int(match.group(2))})
    return values


def _parse_proto_rpcs(block: str) -> list[dict[str, Any]]:
    rpcs: list[dict[str, Any]] = []
    pattern = re.compile(
        r"rpc\s+(\w+)\s*\(\s*(?:stream\s+)?([.\w]+)\s*\)\s*returns\s*\(\s*(?:stream\s+)?([.\w]+)\s*\)",
        re.MULTILINE,
    )
    for match in pattern.finditer(block):
        rpcs.append(
            {
                "name": match.group(1),
                "request_type": match.group(2),
                "response_type": match.group(3),
            }
        )
    return rpcs


@dataclass
class FilesystemRouteInfo:
    """Extracted filesystem routing data."""

    routes: list[dict[str, str]] = field(default_factory=list)


class FileSystemRouteExtractor:
    """Extract URL endpoints from filesystem-based routing conventions.

    Framework-agnostic: driven by codd.yaml filesystem_routes config.
    Supports Next.js App/Pages, SvelteKit, Nuxt 3, Astro, Remix.
    """

    def extract_routes(self, project_root: Path, route_configs: list[dict]) -> FilesystemRouteInfo:
        """Extract routes from all configured base_dirs.

        Args:
            project_root: Project root path
            route_configs: List of filesystem_routes config blocks from codd.yaml
        Returns:
            FilesystemRouteInfo with all discovered routes
        """
        info = FilesystemRouteInfo()
        root = Path(project_root)

        for config in route_configs or []:
            if not isinstance(config, dict):
                continue

            base_dir_value = config.get("base_dir")
            if not base_dir_value:
                continue

            base_dir = _resolve_route_base_dir(root, str(base_dir_value))
            if not base_dir.is_dir():
                continue

            page_patterns = _expand_route_patterns(config.get("page_pattern"))
            api_patterns = _expand_route_patterns(config.get("api_pattern"))

            for file_path in _iter_filesystem_route_files(base_dir):
                relative_path = file_path.relative_to(base_dir)
                kind, matched_pattern = _match_filesystem_route_kind(
                    relative_path,
                    api_patterns=api_patterns,
                    page_patterns=page_patterns,
                )
                if kind is None or matched_pattern is None:
                    continue

                route_path = _filesystem_route_path(relative_path, matched_pattern, config)
                info.routes.append(
                    {
                        "url": _format_filesystem_route_url(route_path, config),
                        "file": str(file_path),
                        "kind": kind,
                    }
                )

        info.routes.sort(key=lambda route: (route["url"], route["kind"], route["file"]))
        return info


def _resolve_route_base_dir(project_root: Path, base_dir: str) -> Path:
    path = Path(base_dir)
    if path.is_absolute():
        return path
    return project_root / path


def _expand_route_patterns(value: Any) -> list[str]:
    patterns = _normalize_list(value)
    expanded: list[str] = []
    for pattern in patterns:
        expanded.extend(_expand_braced_route_pattern(pattern))
    return [pattern for pattern in expanded if pattern]


def _expand_braced_route_pattern(pattern: str) -> list[str]:
    match = re.search(r"\{([^{}]+)\}", pattern)
    if match is None:
        return [pattern]

    prefix = pattern[: match.start()]
    suffix = pattern[match.end() :]
    expanded: list[str] = []
    for option in match.group(1).split(","):
        expanded.extend(_expand_braced_route_pattern(f"{prefix}{option}{suffix}"))
    return expanded


def _iter_filesystem_route_files(base_dir: Path):
    for root, dirs, files in os.walk(base_dir):
        dirs[:] = [
            directory
            for directory in dirs
            if directory not in _IGNORED_DIR_NAMES and not directory.startswith(".pytest_cache")
        ]
        for filename in sorted(files):
            yield Path(root) / filename


def _match_filesystem_route_kind(
    relative_path: Path,
    *,
    api_patterns: list[str],
    page_patterns: list[str],
) -> tuple[str | None, str | None]:
    api_pattern = _matching_route_pattern(relative_path, api_patterns)
    if api_pattern is not None:
        return "api", api_pattern

    page_pattern = _matching_route_pattern(relative_path, page_patterns)
    if page_pattern is not None:
        return "page", page_pattern

    return None, None


def _matching_route_pattern(relative_path: Path, patterns: list[str]) -> str | None:
    candidates = (relative_path.as_posix(), relative_path.name)
    for pattern in patterns:
        if any(fnmatch.fnmatchcase(candidate, pattern) for candidate in candidates):
            return pattern
    return None


def _filesystem_route_path(relative_path: Path, matched_pattern: str, config: dict) -> str:
    route_segments: list[tuple[str, str]] = [(segment, segment) for segment in relative_path.parent.parts]
    file_segment = _route_file_segment(relative_path.name, matched_pattern)
    if file_segment:
        route_segments.extend(
            (segment, relative_path.name) for segment in _split_filesystem_route_segment(file_segment, config)
        )

    normalized_segments = _normalize_filesystem_route_segments(route_segments, config)
    return "/".join(normalized_segments)


def _route_file_segment(filename: str, matched_pattern: str) -> str:
    stem = Path(filename).stem
    if stem == "index":
        return ""
    if _pattern_identifies_route_marker(matched_pattern, stem):
        return ""
    return stem


def _pattern_identifies_route_marker(pattern: str, stem: str) -> bool:
    pattern_name = Path(pattern).name
    if any(char in pattern_name for char in "*?["):
        return False
    return Path(pattern_name).stem == stem


def _split_filesystem_route_segment(segment: str, config: dict) -> list[str]:
    split_pattern = config.get("split_segment")
    if split_pattern:
        return [part for part in re.split(str(split_pattern), segment) if part]
    if "." in segment and "[" not in segment and "]" not in segment:
        return [part for part in segment.split(".") if part]
    return [segment]


def _normalize_filesystem_route_segments(route_segments: list[tuple[str, str]], config: dict) -> list[str]:
    ignored_patterns = _normalize_list(config.get("ignore_segment"))
    dynamic_rules = _normalize_dynamic_route_rules(config.get("dynamic_segment"))
    normalized: list[str] = []

    for segment, original in route_segments:
        if _is_ignored_route_segment(segment, ignored_patterns):
            continue

        rewritten = _rewrite_dynamic_route_segment(segment, original, dynamic_rules)
        if rewritten in {"", ".", "/"}:
            continue
        normalized.append(rewritten.strip("/"))

    return [segment for segment in normalized if segment]


def _is_ignored_route_segment(segment: str, ignored_patterns: list[str]) -> bool:
    return any(re.fullmatch(pattern, segment) for pattern in ignored_patterns)


def _normalize_dynamic_route_rules(value: Any) -> list[dict[str, str]]:
    if isinstance(value, dict):
        raw_rules = [value]
    elif isinstance(value, list):
        raw_rules = [rule for rule in value if isinstance(rule, dict)]
    else:
        raw_rules = []

    rules: list[dict[str, str]] = []
    for rule in raw_rules:
        from_pattern = rule.get("from")
        to_pattern = rule.get("to")
        if from_pattern is None or to_pattern is None:
            continue
        rules.append({"from": str(from_pattern), "to": str(to_pattern)})
    return rules


def _rewrite_dynamic_route_segment(segment: str, original: str, dynamic_rules: list[dict[str, str]]) -> str:
    rewritten = segment
    for rule in dynamic_rules:
        updated = _apply_dynamic_route_rule(rewritten, rule)
        if updated != rewritten:
            rewritten = updated
            continue

        if original != rewritten:
            updated = _apply_dynamic_route_rule(original, rule)
            if updated != original:
                rewritten = updated

    return rewritten


def _apply_dynamic_route_rule(value: str, rule: dict[str, str]) -> str:
    pattern = re.compile(rule["from"])
    replacement = re.sub(r"\$(\d+)", r"\\g<\1>", rule["to"])
    return pattern.sub(lambda match: match.expand(replacement), value)


def _format_filesystem_route_url(route_path: str, config: dict) -> str:
    relative_dir = route_path.strip("/")
    template = str(config.get("url_template") or "/{relative_dir}")
    url_path = template.replace("{relative_dir}", relative_dir)
    normalized_path = _normalize_filesystem_url_path(url_path)
    base_url = str(config.get("base_url") or "").strip()
    if not base_url:
        return normalized_path
    if normalized_path == "/":
        return base_url.rstrip("/") or "/"
    return f"{base_url.rstrip('/')}{normalized_path}"


def _normalize_filesystem_url_path(url_path: str) -> str:
    normalized = url_path.strip()
    if not normalized:
        return "/"
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    normalized = re.sub(r"/+", "/", normalized)
    if len(normalized) > 1:
        normalized = normalized.rstrip("/")
    return normalized or "/"


__all__ = [
    "ApiSpecInfo",
    "BuildDepsExtractor",
    "BuildDepsInfo",
    "ConfigInfo",
    "DockerComposeExtractor",
    "FileSystemRouteExtractor",
    "FilesystemRouteInfo",
    "GraphQlExtractor",
    "KubernetesExtractor",
    "LanguageExtractor",
    "OpenApiExtractor",
    "PrismaSchemaExtractor",
    "PrismaSchemaInfo",
    "ProtobufExtractor",
    "RegexExtractor",
    "SqlDdlExtractor",
    "SqlSchemaInfo",
    "TerraformExtractor",
    "TestExtractor",
    "TestInfo",
    "TreeSitterExtractor",
    "get_extractor",
]
