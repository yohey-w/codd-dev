"""SQL DDL and Prisma schema extraction."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from codd.parsing._shared import (
    PrismaSchemaInfo,
    RegexExtractor,
    SqlSchemaInfo,
    _find_named_blocks,
    _normalize_ws,
    _split_csv,
)
from codd.parsing.treesitter import (
    TreeSitterExtractor,
    _build_parser,
    _field_text,
    _iter_named_nodes,
    _node_text,
)

if TYPE_CHECKING:
    from codd.extractor import CallEdge, ModuleInfo, Symbol


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
