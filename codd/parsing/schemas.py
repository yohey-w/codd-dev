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

# SQL は外部キーを2通りで書ける。両方を拾わないと FK 数を過少に報告する。
#   表制約:  FOREIGN KEY (a) REFERENCES parent (id)
#   列制約:  a uuid not null references parent (id)      ← FOREIGN KEY 語が無い
# 列制約は PostgreSQL / MySQL / SQLite いずれでも一般的な書き方であり、
# これを取りこぼすと「参照整合性が無い」という誤った所見が出る。
_TABLE_LEVEL_FK = re.compile(
    r"(?:CONSTRAINT\s+(?P<name>\w+)\s+)?FOREIGN\s+KEY\s*\((?P<columns>[^)]+)\)"
    r"\s+REFERENCES\s+(?P<ref_table>[^\s(]+)\s*\((?P<ref_columns>[^)]+)\)",
    re.IGNORECASE,
)

# 列制約。ひとつの列定義の中から「列名」と「参照先」を取り出す。
# 定義の切り出しは正規表現ではなく _sql_table_definitions が行う——
# numeric(10,2) の "," や default 'a,b' の "," は区切りではないので、
# 正規表現でカンマを区切りとみなすと列名として "2" や "b" を拾ってしまう。
# 参照列の指定は省略できる（親の主キーに解決される）ので任意扱いにする。
_COLUMN_LEVEL_FK = re.compile(
    r"^\s*(?:CONSTRAINT\s+(?P<name>\w+)\s+)?"
    r'(?P<column>"[^"]+"|`[^`]+`|\[[^\]]+\]|\w+)(?!\w)',
    re.IGNORECASE,
)

_REFERENCES_CLAUSE = re.compile(
    r"\bREFERENCES\s+(?P<ref_table>\"[^\"]+\"|`[^`]+`|\[[^\]]+\]|[\w.]+)"
    r"\s*(?:\(\s*(?P<ref_columns>[^)]+)\))?",
    re.IGNORECASE,
)

_FK_COLUMN_RESERVED = {
    "foreign",
    "key",
    "constraint",
    "primary",
    "unique",
    "check",
    "references",
    "exclude",
    "like",
}

# 引用符の開き文字 -> 閉じ文字。SQL のエスケープは引用符の二重化（'' や ""）。
# 角括弧は T-SQL の識別子（閉じは "]"、エスケープは "]]"）。
_SQL_QUOTES = {"\'": "\'", '"': '"', "`": "`", "[": "]"}

# PostgreSQL のドル引用（$$ ... $$ / $tag$ ... $tag$）。
_DOLLAR_QUOTE = re.compile(r"\$(\w*)\$")

def _dollar_quote_tag(text: str, index: int) -> str | None:
    match = _DOLLAR_QUOTE.match(text, index)
    return match.group(0) if match else None

def _skip_sql_quoted(text: str, index: int) -> int:
    """text[index] の引用符から、その閉じ引用符の次の位置までを返す。

    引用の中身は「コメントでも区切りでもない」ただの文字として飛ばす。
    ここを甘くすると、値の中の "--" を行コメントの開始と誤り、
    そこから行末までを捨てて既存の外部キーまで消してしまう。
    """
    tag = _dollar_quote_tag(text, index)
    if tag is not None:  # $$ ... $$ / $tag$ ... $tag$
        end = text.find(tag, index + len(tag))
        return len(text) if end == -1 else end + len(tag)

    quote = text[index]
    if quote == "\'":
        # バックスラッシュを方言差の分かれ道として扱う。
        #   MySQL:      'it\\'s'  → \\' はエスケープで、文字列はまだ閉じない
        #   PostgreSQL: 'C:\\'    → \\ はただの文字で、次の ' で閉じる
        # 見分けはつかないので、まずエスケープありで読み、それだと
        # 閉じないときだけエスケープなしで読み直す。
        # 「閉じない」＝残り全部を文字列とみなす＝後続のFKを全部失う、なので
        # 閉じる読み方があるならそちらを採る。
        escaped = _scan_quoted(text, index, quote, quote, backslash_escapes=True)
        if escaped < len(text):
            return escaped
        return _scan_quoted(text, index, quote, quote, backslash_escapes=False)
    return _scan_quoted(text, index, quote, _SQL_QUOTES[quote], backslash_escapes=False)

def _scan_quoted(text: str, index: int, quote: str, closer: str, *, backslash_escapes: bool) -> int:
    cursor = index + 1
    while cursor < len(text):
        if backslash_escapes and text[cursor] == "\\" and cursor + 1 < len(text):
            cursor += 2
            continue
        if text[cursor] == closer:
            if cursor + 1 < len(text) and text[cursor + 1] == closer:
                cursor += 2  # 二重化されたエスケープ。まだ閉じていない
                continue
            return cursor + 1
        cursor += 1
    return len(text)  # 閉じていない引用符。末尾まで文字列として扱う

def _is_double_minus_operator(text: str, index: int) -> bool:
    """`1--2`（1 から -2 を引く）の "--" か。

    MySQL は "--" の直後に空白を要求するのでこれは演算子。PostgreSQL / SQLite は
    空白なしの `--コメント` もコメントなので、空白の有無だけでは切れない。
    そこで「両側が式に見えるとき」だけ演算子と読む——直前が値の終わりで、
    直後が数値か開き括弧のときに限る。`--コメント`（直後が文字）はコメントのまま。
    """
    if index == 0:
        return False
    before = text[index - 1]
    after = text[index + 2] if index + 2 < len(text) else ""
    return (before.isalnum() or before in "_)") and (after.isdigit() or after == "(")

def _strip_sql_comments(statement_text: str) -> str:
    """コメントを取り除く。文字列リテラルと引用符つき識別子の中は触らない。

    列定義の直前にコメント行が挟まると、区切り（"," や "("）と列名の間に
    別の行が入り、列制約の外部キーを取りこぼす。

    ただし "--" や "/*" は文字列の中にも現れる（例: default \'--\'）。
    そこをコメントの開始と誤ると、そこから行末までが消え、
    本来拾えていた表制約の外部キーまで検出できなくなる。
    """
    out: list[str] = []
    cursor = 0
    length = len(statement_text)
    while cursor < length:
        char = statement_text[cursor]
        if char in _SQL_QUOTES or _dollar_quote_tag(statement_text, cursor):
            end = _skip_sql_quoted(statement_text, cursor)
            out.append(statement_text[cursor:end])
            cursor = end
            continue
        if statement_text.startswith("--", cursor) and not _is_double_minus_operator(
            statement_text, cursor
        ):
            newline = statement_text.find("\n", cursor)
            cursor = length if newline == -1 else newline  # 改行は残す
            continue
        if statement_text.startswith("/*", cursor):
            end = statement_text.find("*/", cursor + 2)
            cursor = length if end == -1 else end + 2
            continue
        out.append(char)
        cursor += 1
    return "".join(out)

def _sql_table_definitions(statement_text: str) -> list[str]:
    """CREATE TABLE の括弧内を、深さ0のカンマで定義単位に割る。

    numeric(10,2) の "," は型パラメータの区切り、default \'a,b\' の "," は
    ただの文字。どちらも列定義の区切りではない。ここを取り違えると
    カンマの右隣（"2" や "b"）を列名として拾う。
    """
    start = -1
    depth = 0
    cursor = 0
    length = len(statement_text)
    definitions: list[str] = []
    current: list[str] = []
    while cursor < length:
        char = statement_text[cursor]
        if char in _SQL_QUOTES or _dollar_quote_tag(statement_text, cursor):
            end = _skip_sql_quoted(statement_text, cursor)
            if start != -1:
                current.append(statement_text[cursor:end])
            cursor = end
            continue
        if char == "(":
            depth += 1
            if depth == 1 and start == -1:
                start = cursor  # 列定義リストの開き括弧。中身はまだ取らない
                cursor += 1
                continue
        elif char == ")":
            depth -= 1
            if depth == 0 and start != -1:
                definitions.append("".join(current))
                return [d.strip() for d in definitions if d.strip()]
        elif char == "," and depth == 1:
            definitions.append("".join(current))
            current = []
            cursor += 1
            continue
        if start != -1:
            current.append(char)
        cursor += 1
    if start != -1 and current:
        definitions.append("".join(current))
    return [d.strip() for d in definitions if d.strip()]

def _blank_string_literals(definition: str) -> str:
    """文字列リテラルの中身を空白で埋める（長さは変えない）。

    `default \'references parent(id)\'` のような値を外部キーと読み違えないため。
    引用符つき識別子（" と `）は列名・テーブル名なので残す。
    """
    out: list[str] = []
    cursor = 0
    while cursor < len(definition):
        if definition[cursor] == "\'":
            end = _skip_sql_quoted(definition, cursor)
            out.append(" " * (end - cursor))
            cursor = end
            continue
        out.append(definition[cursor])
        cursor += 1
    return "".join(out)

_HAS_FOREIGN_KEY = re.compile(r"\bFOREIGN\s+KEY\b", re.IGNORECASE)

# TODO: `ALTER TABLE t ADD COLUMN p uuid REFERENCES parent (id)` の列制約は
# まだ拾えない（列定義リストの括弧が無いため）。本PR以前も 0 本で、回帰ではない。


def _regex_foreign_keys(statement_text: str, table_name: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    statement_text = _strip_sql_comments(statement_text)

    # 表制約は文字列の中身を空白で潰した版に対して探す。
    # `default 'FOREIGN KEY (fake) REFERENCES fake (id)'` のような値を
    # 外部キーとして数えないため。長さは変わらないので位置はずれない。
    for match in _TABLE_LEVEL_FK.finditer(_blank_string_literals(statement_text)):
        matches.append(
            {
                "name": match.group("name") or "",
                "table": table_name,
                "columns": _split_csv(match.group("columns")),
                "references_table": match.group("ref_table"),
                "references_columns": _split_csv(match.group("ref_columns")),
            }
        )

    for definition in _sql_table_definitions(statement_text):
        if _HAS_FOREIGN_KEY.search(definition):
            continue  # 表制約。上のループで拾い済み
        reference = _REFERENCES_CLAUSE.search(_blank_string_literals(definition))
        if reference is None:
            continue
        head = _COLUMN_LEVEL_FK.match(definition)
        if head is None:
            continue
        column = _strip_identifier_quotes(head.group("column"))
        if not column or column.lower() in _FK_COLUMN_RESERVED:
            continue
        ref_columns_raw = reference.group("ref_columns")
        matches.append(
            {
                "name": head.group("name") or "",
                "table": table_name,
                "columns": [column],
                "references_table": _strip_identifier_quotes(reference.group("ref_table")),
                "references_columns": _split_csv(ref_columns_raw) if ref_columns_raw else [],
            }
        )

    return matches

def _strip_identifier_quotes(identifier: str) -> str:
    value = (identifier or "").strip()
    if len(value) >= 2 and value[0] in '"`[' and value[-1] in '"`]':
        return value[1:-1].strip()
    return value

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
        # 外部キーだけは改行を潰す前にコメントを落とす。潰したあとでは
        # 行コメント "--" の終端（改行）が消え、文以降の既存FKまで削れる。
        fk_source = _normalize_ws(_strip_sql_comments(_node_text(content_bytes, node)))
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
            for foreign_key in _regex_foreign_keys(fk_source, table_name):
                _append_foreign_key(schema, foreign_key, seen_foreign_keys)
        elif node.type == "alter_table":
            table_name = _sql_first_object_name(content_bytes, node)
            if table_name:
                for foreign_key in _regex_foreign_keys(fk_source, table_name):
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
        schema.foreign_keys.extend(_regex_foreign_keys(_normalize_ws(_strip_sql_comments(table_match.group(0))), table_name))

    for statement in re.findall(r"ALTER\s+TABLE\s+.*?;", content, re.IGNORECASE | re.DOTALL):
        match = re.search(r"ALTER\s+TABLE\s+([^\s;]+)", statement, re.IGNORECASE)
        if match:
            schema.foreign_keys.extend(_regex_foreign_keys(_normalize_ws(_strip_sql_comments(statement)), match.group(1)))

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
