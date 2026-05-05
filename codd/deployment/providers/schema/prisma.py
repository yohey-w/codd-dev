"""Prisma schema provider for deployment runtime state extraction."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

from codd.deployment import RuntimeStateKind, RuntimeStateNode
from codd.deployment.providers import SchemaProvider, register_schema_provider
from codd.parsing import PrismaSchemaExtractor, PrismaSchemaInfo


_DEFAULT_SEED_FILES = (
    Path("prisma") / "seed.ts",
    Path("prisma") / "seed.js",
    Path("prisma") / "seed.mjs",
)
_SEED_SUFFIXES = (".ts", ".js", ".mjs")


@register_schema_provider("prisma")
class PrismaSchemaProvider(SchemaProvider):
    """Extract Prisma schema, seed, and migration runtime states."""

    def extract_schema(self, project_root: Path) -> dict:
        """Return Prisma schema metadata, or ``{}`` when schema.prisma is absent."""

        root = Path(project_root)
        schema_path = root / "prisma" / "schema.prisma"
        if not schema_path.is_file():
            return {}

        content = schema_path.read_text(encoding="utf-8")
        schema = PrismaSchemaExtractor().extract_schema(content, schema_path)
        if schema is None:
            return {}

        table_names = _extract_model_table_names(content)
        return _schema_info_to_dict(schema, root, table_names)

    def detect_seed_files(self, project_root: Path) -> list[Path]:
        """Return existing Prisma seed files declared by convention or package.json."""

        root = Path(project_root)
        candidates = [root / relative_path for relative_path in _DEFAULT_SEED_FILES]
        candidates.extend(_seed_candidates_from_package_json(root))
        return _existing_unique_paths(candidates)

    def detect_migrations(self, project_root: Path) -> list[Path]:
        """Return Prisma migration SQL files under prisma/migrations."""

        migrations_dir = Path(project_root) / "prisma" / "migrations"
        if not migrations_dir.is_dir():
            return []

        migration_files = [
            path
            for path in migrations_dir.rglob("*.sql")
            if path.name == "migration.sql" or path.name.endswith("_migration.sql")
        ]
        return sorted(migration_files)

    def build_runtime_states(self, project_root: Path) -> list[RuntimeStateNode]:
        """Build runtime states from Prisma schema, seed, and migration artifacts."""

        root = Path(project_root)
        states: list[RuntimeStateNode] = []

        schema = self.extract_schema(root)
        for model in schema.get("models", []):
            target = model["target"]
            states.append(
                RuntimeStateNode(
                    identifier=f"runtime:db:schema:{target}",
                    kind=RuntimeStateKind.DB_SCHEMA,
                    target=target,
                    expected_value={
                        "model": model["name"],
                        "schema_file": schema["path"],
                    },
                    actual_check_command="npx prisma migrate status",
                )
            )

        seed_files = self.detect_seed_files(root)
        if seed_files:
            states.append(
                RuntimeStateNode(
                    identifier="runtime:db:seed:db_seed",
                    kind=RuntimeStateKind.DB_SEED,
                    target="db_seed",
                    expected_value={"files": [_relative_posix(path, root) for path in seed_files]},
                    actual_check_command="npx prisma db seed",
                )
            )

        migration_files = self.detect_migrations(root)
        if migration_files:
            states.append(
                RuntimeStateNode(
                    identifier="runtime:db:schema:db_migrations",
                    kind=RuntimeStateKind.DB_SCHEMA,
                    target="db_migrations",
                    expected_value={"files": [_relative_posix(path, root) for path in migration_files]},
                    actual_check_command="npx prisma migrate status",
                )
            )

        return states


def _schema_info_to_dict(schema: PrismaSchemaInfo, root: Path, table_names: dict[str, str]) -> dict[str, Any]:
    return {
        "path": _relative_posix(Path(schema.file_path), root),
        "models": [
            {
                "name": model["name"],
                "target": table_names.get(model["name"], _default_table_name(model["name"])),
                "fields": model["fields"],
                "relations": model["relations"],
            }
            for model in schema.models
        ],
        "enums": schema.enums,
    }


def _extract_model_table_names(content: str) -> dict[str, str]:
    table_names: dict[str, str] = {}
    for match in re.finditer(r"\bmodel\s+(\w+)\s*\{(?P<body>.*?)\n\}", content, flags=re.DOTALL):
        model_name = match.group(1)
        body = match.group("body")
        map_match = re.search(r'@@map\(\s*["\']([^"\']+)["\']\s*\)', body)
        table_names[model_name] = map_match.group(1) if map_match else _default_table_name(model_name)
    return table_names


def _default_table_name(model_name: str) -> str:
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", model_name).lower()
    if snake.endswith(("s", "x", "z", "ch", "sh")):
        return f"{snake}es"
    if snake.endswith("y") and len(snake) > 1 and snake[-2] not in "aeiou":
        return f"{snake[:-1]}ies"
    return f"{snake}s"


def _seed_candidates_from_package_json(root: Path) -> list[Path]:
    package_json = root / "package.json"
    if not package_json.is_file():
        return []

    try:
        payload = json.loads(package_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []

    seed_command = payload.get("prisma", {}).get("seed")
    if not isinstance(seed_command, str):
        return []

    candidates: list[Path] = []
    for token in _split_seed_command(seed_command):
        normalized = token.strip("'\"")
        if normalized.endswith(_SEED_SUFFIXES):
            candidates.append(root / normalized)
    return candidates


def _split_seed_command(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _existing_unique_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        normalized = path.resolve()
        if normalized in seen or not path.is_file():
            continue
        seen.add(normalized)
        unique.append(path)
    return sorted(unique)


def _relative_posix(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()
