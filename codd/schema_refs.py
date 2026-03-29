"""R5.2 — Schema-code dependency detection for codd extract.

Detects ORM model definitions (SQLAlchemy, Django, Prisma) and raw SQL
references in source code. Links source modules to schema tables.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codd.extractor import ProjectFacts


@dataclass
class SchemaRef:
    """A reference from source code to a database table or model."""
    table_or_model: str
    kind: str           # "sqlalchemy" | "django" | "prisma" | "raw_sql"
    file: str
    line: int


# ── Detection patterns ──────────────────────────────────

# SQLAlchemy: __tablename__ = 'users'
_SQLA_TABLENAME_RE = re.compile(
    r"""__tablename__\s*=\s*['"](\w+)['"]""",
)

# Django: class User(models.Model)
_DJANGO_MODEL_RE = re.compile(
    r"""class\s+(\w+)\s*\(\s*(?:models\.Model|AbstractUser|AbstractBaseUser)""",
)

# Prisma client: prisma.user.find_many() etc
_PRISMA_CLIENT_RE = re.compile(
    r"""prisma\.(\w+)\.\s*(?:find_many|find_first|find_unique|create|update|delete|count|aggregate|group_by)""",
)

# Raw SQL: SELECT/INSERT/UPDATE/DELETE ... FROM/INTO/TABLE tablename
_RAW_SQL_RE = re.compile(
    r"""(?:SELECT\s+.*?\s+FROM|INSERT\s+INTO|UPDATE|DELETE\s+FROM|CREATE\s+TABLE|ALTER\s+TABLE|DROP\s+TABLE)\s+[`"']?(\w+)[`"']?""",
    re.IGNORECASE,
)


def detect_schema_refs(content: str, file_path: str) -> list[SchemaRef]:
    """Detect schema/model references in source code."""
    refs: list[SchemaRef] = []
    lines = content.splitlines()

    for line_no, line in enumerate(lines, 1):
        # SQLAlchemy
        m = _SQLA_TABLENAME_RE.search(line)
        if m:
            refs.append(SchemaRef(
                table_or_model=m.group(1),
                kind="sqlalchemy",
                file=file_path,
                line=line_no,
            ))

        # Django
        m = _DJANGO_MODEL_RE.search(line)
        if m:
            refs.append(SchemaRef(
                table_or_model=m.group(1),
                kind="django",
                file=file_path,
                line=line_no,
            ))

        # Prisma client
        m = _PRISMA_CLIENT_RE.search(line)
        if m:
            refs.append(SchemaRef(
                table_or_model=m.group(1),
                kind="prisma",
                file=file_path,
                line=line_no,
            ))

        # Raw SQL in string literals
        # Only match inside quotes to avoid false positives
        if any(kw in line.upper() for kw in ("SELECT", "INSERT", "UPDATE", "DELETE", "CREATE TABLE", "ALTER TABLE")):
            for m2 in _RAW_SQL_RE.finditer(line):
                table = m2.group(1)
                # Filter out SQL keywords that got captured as table names
                if table.upper() not in {"SET", "INTO", "FROM", "WHERE", "AND", "OR",
                                          "TABLE", "INDEX", "VIEW", "VALUES", "NULL",
                                          "NOT", "EXISTS", "AS", "ON", "JOIN", "LEFT",
                                          "RIGHT", "INNER", "OUTER", "GROUP", "ORDER",
                                          "BY", "HAVING", "LIMIT", "OFFSET", "UNION",
                                          "ALL", "DISTINCT", "CASE", "WHEN", "THEN",
                                          "ELSE", "END", "IF", "BEGIN", "COMMIT"}:
                    refs.append(SchemaRef(
                        table_or_model=table,
                        kind="raw_sql",
                        file=file_path,
                        line=line_no,
                    ))

    return refs


def build_schema_refs(facts: ProjectFacts, project_root: Path) -> None:
    """Populate ``schema_refs`` on every module in *facts*."""
    for mod in facts.modules.values():
        all_refs: list[SchemaRef] = []
        for rel_file in mod.files:
            full = project_root / rel_file
            try:
                content = full.read_text(errors="ignore")
            except Exception:
                continue
            refs = detect_schema_refs(content, rel_file)
            all_refs.extend(refs)
        mod.schema_refs = all_refs
