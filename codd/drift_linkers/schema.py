"""Schema drift linker for expected catalog tables and Prisma schemas."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from codd.coherence_engine import DriftEvent, EventBus
from codd.drift_linkers import register_linker
from codd.parsing import PrismaSchemaExtractor, PrismaSchemaInfo


_coherence_bus: EventBus | None = None


@dataclass
class SchemaDriftResult:
    """Normalized result for database design vs Prisma drift checks."""

    status: str
    missing_tables: list[str] = field(default_factory=list)
    extra_tables: list[str] = field(default_factory=list)
    column_diffs: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    events: list[DriftEvent] = field(default_factory=list)

    @property
    def has_drift(self) -> bool:
        return bool(self.missing_tables or self.extra_tables or self.column_diffs)


def set_coherence_bus(bus: EventBus | None) -> None:
    """Set an opt-in bus used by SchemaDriftLinker."""

    global _coherence_bus
    _coherence_bus = bus


@register_linker("schema")
class SchemaDriftLinker:
    """Detect drift between expected_catalog.yaml db_tables and Prisma models."""

    def __init__(
        self,
        expected_catalog_path: str | Path,
        project_root: str | Path,
        settings: dict[str, Any] | None = None,
    ) -> None:
        self.expected_catalog_path = self._resolve_path(expected_catalog_path, project_root)
        self.project_root = Path(project_root)
        self.settings = settings or {}

    def run(self) -> SchemaDriftResult:
        expected_tables, warnings = self._load_expected_tables()
        if expected_tables is None:
            return SchemaDriftResult(status="skipped", warnings=warnings)

        design_path = self.design_file_path()
        if not design_path.is_file():
            return SchemaDriftResult(
                status="skipped",
                warnings=[*warnings, f"WARN: database design file not found: {design_path.as_posix()}"],
            )

        prisma_path = self.prisma_schema_path()
        if not prisma_path.is_file():
            return SchemaDriftResult(
                status="skipped",
                warnings=[*warnings, f"WARN: Prisma schema not found: {prisma_path.as_posix()}"],
            )

        schema = self._extract_prisma_schema(prisma_path)
        actual_tables = self._normalize_prisma_tables(schema)
        result = self._diff_tables(expected_tables, actual_tables, warnings)
        if result.has_drift:
            result.status = "drift"
            event = self._publish_schema_drift(result)
            if event is not None:
                result.events.append(event)
        else:
            result.status = "ok"
        return result

    def design_file_path(self) -> Path:
        design_files = self.settings.get("design_files") if isinstance(self.settings, dict) else None
        configured = None
        if isinstance(design_files, dict):
            configured = design_files.get("schema")
        configured = configured or self.settings.get("database_design_path", "docs/design/database_design.md")
        return self._resolve_path(configured, self.project_root)

    def prisma_schema_path(self) -> Path:
        schema_settings = self.settings.get("schema") if isinstance(self.settings, dict) else None
        configured = None
        if isinstance(schema_settings, dict):
            configured = schema_settings.get("prisma_path")
        configured = configured or self.settings.get("prisma_schema_path", "prisma/schema.prisma")
        return self._resolve_path(configured, self.project_root)

    def _load_expected_tables(self) -> tuple[dict[str, dict[str, str]], list[str]] | tuple[None, list[str]]:
        if not self.expected_catalog_path.is_file():
            return None, [f"WARN: expected_catalog.yaml not found: {self.expected_catalog_path.as_posix()}"]

        payload = yaml.safe_load(self.expected_catalog_path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            return None, ["WARN: expected_catalog.yaml must contain a YAML mapping"]

        raw_tables = payload.get("db_tables")
        if not isinstance(raw_tables, list) or not raw_tables:
            return None, ["WARN: expected_catalog.yaml missing non-empty db_tables section"]

        warnings: list[str] = []
        tables: dict[str, dict[str, str]] = {}
        for index, raw_table in enumerate(raw_tables):
            if not isinstance(raw_table, dict):
                warnings.append(f"WARN: db_tables[{index}] is not a mapping")
                continue
            table_name = str(raw_table.get("name", "")).strip()
            if not table_name:
                warnings.append(f"WARN: db_tables[{index}] missing name")
                continue
            columns = raw_table.get("columns", [])
            if columns is None:
                columns = []
            if not isinstance(columns, list):
                warnings.append(f"WARN: db_tables[{index}].columns is not a list")
                columns = []
            tables[table_name] = self._normalize_catalog_columns(columns, table_name, warnings)

        if not tables:
            return None, [*warnings, "WARN: expected_catalog.yaml db_tables contains no valid tables"]
        return tables, warnings

    def _normalize_catalog_columns(
        self,
        raw_columns: list[Any],
        table_name: str,
        warnings: list[str],
    ) -> dict[str, str]:
        columns: dict[str, str] = {}
        for index, raw_column in enumerate(raw_columns):
            if not isinstance(raw_column, dict):
                warnings.append(f"WARN: db_tables[{table_name}].columns[{index}] is not a mapping")
                continue
            column_name = str(raw_column.get("name", "")).strip()
            if not column_name:
                warnings.append(f"WARN: db_tables[{table_name}].columns[{index}] missing name")
                continue
            columns[column_name] = str(raw_column.get("type", "")).strip()
        return columns

    def _extract_prisma_schema(self, prisma_path: Path) -> PrismaSchemaInfo:
        content = prisma_path.read_text(encoding="utf-8", errors="ignore")
        return PrismaSchemaExtractor().extract_schema(content, prisma_path) or PrismaSchemaInfo(
            file_path=prisma_path.as_posix()
        )

    def _normalize_prisma_tables(self, schema: PrismaSchemaInfo) -> dict[str, dict[str, str]]:
        tables: dict[str, dict[str, str]] = {}
        for model in schema.models:
            table_name = str(model.get("name", "")).strip()
            if not table_name:
                continue
            fields = model.get("fields", [])
            columns: dict[str, str] = {}
            if isinstance(fields, list):
                for field_info in fields:
                    if not isinstance(field_info, dict):
                        continue
                    column_name = str(field_info.get("name", "")).strip()
                    if column_name:
                        columns[column_name] = str(field_info.get("type", "")).strip()
            tables[table_name] = columns
        return tables

    def _diff_tables(
        self,
        expected: dict[str, dict[str, str]],
        actual: dict[str, dict[str, str]],
        warnings: list[str],
    ) -> SchemaDriftResult:
        expected_names = set(expected)
        actual_names = set(actual)
        result = SchemaDriftResult(
            status="ok",
            missing_tables=sorted(expected_names - actual_names),
            extra_tables=sorted(actual_names - expected_names),
            warnings=list(warnings),
        )

        for table_name in sorted(expected_names & actual_names):
            expected_columns = expected[table_name]
            actual_columns = actual[table_name]
            expected_column_names = set(expected_columns)
            actual_column_names = set(actual_columns)
            for column_name in sorted(expected_column_names - actual_column_names):
                result.column_diffs.append(
                    {
                        "table": table_name,
                        "column": column_name,
                        "status": "missing_in_prisma",
                        "expected_type": expected_columns[column_name],
                        "actual_type": None,
                    }
                )
            for column_name in sorted(actual_column_names - expected_column_names):
                result.column_diffs.append(
                    {
                        "table": table_name,
                        "column": column_name,
                        "status": "extra_in_prisma",
                        "expected_type": None,
                        "actual_type": actual_columns[column_name],
                    }
                )
            for column_name in sorted(expected_column_names & actual_column_names):
                expected_type = expected_columns[column_name]
                actual_type = actual_columns[column_name]
                if expected_type != actual_type:
                    result.column_diffs.append(
                        {
                            "table": table_name,
                            "column": column_name,
                            "status": "type_mismatch",
                            "expected_type": expected_type,
                            "actual_type": actual_type,
                        }
                    )
        return result

    def _publish_schema_drift(self, result: SchemaDriftResult) -> DriftEvent | None:
        bus = self._event_bus()
        if bus is None:
            return None
        event = DriftEvent(
            source_artifact="design_doc",
            target_artifact="implementation",
            change_type="modified",
            payload={
                "description": "Database design and Prisma schema drift detected.",
                "missing_tables": result.missing_tables,
                "extra_tables": result.extra_tables,
                "column_diffs": result.column_diffs,
            },
            severity="amber",
            fix_strategy="hitl",
            kind="schema_drift",
        )
        bus.publish(event)
        return event

    def _event_bus(self) -> EventBus | None:
        configured = self.settings.get("event_bus") or self.settings.get("coherence_bus")
        return configured if isinstance(configured, EventBus) else _coherence_bus

    @staticmethod
    def _resolve_path(path: str | Path, project_root: str | Path) -> Path:
        candidate = Path(path)
        return candidate if candidate.is_absolute() else Path(project_root) / candidate
