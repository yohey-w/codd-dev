from __future__ import annotations

import importlib
import json

from codd.dag import checks as dag_checks
from codd.deployment import RuntimeStateKind
from codd.deployment.providers import SCHEMA_PROVIDERS, SchemaProvider
from codd.deployment.providers.schema import prisma as prisma_module
from codd.deployment.providers.schema.prisma import PrismaSchemaProvider


def _write_prisma_schema(project_root, body: str) -> None:
    prisma_dir = project_root / "prisma"
    prisma_dir.mkdir(parents=True, exist_ok=True)
    (prisma_dir / "schema.prisma").write_text(body, encoding="utf-8")


def _minimal_schema(*models: str) -> str:
    return "\n\n".join(models)


def test_prisma_provider_registered():
    assert SCHEMA_PROVIDERS["prisma"] is prisma_module.PrismaSchemaProvider


def test_extract_schema_detects_user_model(tmp_path):
    _write_prisma_schema(
        tmp_path,
        _minimal_schema(
            """
model User {
  id    String @id
  email String @unique
}
""".strip()
        ),
    )

    schema = PrismaSchemaProvider().extract_schema(tmp_path)

    assert schema["path"] == "prisma/schema.prisma"
    assert schema["models"][0]["name"] == "User"
    assert schema["models"][0]["target"] == "users"
    assert [field["name"] for field in schema["models"][0]["fields"]] == ["id", "email"]


def test_extract_schema_missing_file_returns_empty_dict(tmp_path):
    assert PrismaSchemaProvider().extract_schema(tmp_path) == {}


def test_detect_seed_files_finds_prisma_seed_ts(tmp_path):
    seed = tmp_path / "prisma" / "seed.ts"
    seed.parent.mkdir(parents=True)
    seed.write_text("export async function seed() {}\n", encoding="utf-8")

    assert PrismaSchemaProvider().detect_seed_files(tmp_path) == [seed]


def test_detect_seed_files_finds_package_json_prisma_seed(tmp_path):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    seed = scripts_dir / "seed.mjs"
    seed.write_text("export default async function seed() {}\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"prisma": {"seed": "node scripts/seed.mjs"}}),
        encoding="utf-8",
    )

    assert PrismaSchemaProvider().detect_seed_files(tmp_path) == [seed]


def test_detect_seed_files_missing_returns_empty_list(tmp_path):
    assert PrismaSchemaProvider().detect_seed_files(tmp_path) == []


def test_detect_migrations_finds_prisma_migration_sql(tmp_path):
    migrations = tmp_path / "prisma" / "migrations" / "20260505000000_init"
    migrations.mkdir(parents=True)
    migration = migrations / "migration.sql"
    named_migration = migrations / "users_migration.sql"
    ignored = migrations / "notes.sql"
    migration.write_text("-- init\n", encoding="utf-8")
    named_migration.write_text("-- users\n", encoding="utf-8")
    ignored.write_text("-- ignored\n", encoding="utf-8")

    assert PrismaSchemaProvider().detect_migrations(tmp_path) == [migration, named_migration]


def test_detect_migrations_missing_returns_empty_list(tmp_path):
    assert PrismaSchemaProvider().detect_migrations(tmp_path) == []


def test_build_runtime_states_creates_db_schema_and_seed_nodes(tmp_path):
    _write_prisma_schema(
        tmp_path,
        _minimal_schema(
            """
model User {
  id String @id
}
""".strip()
        ),
    )
    seed = tmp_path / "prisma" / "seed.ts"
    seed.write_text("await prisma.user.create({ data: {} })\n", encoding="utf-8")

    states = PrismaSchemaProvider().build_runtime_states(tmp_path)

    assert [(state.kind, state.target) for state in states] == [
        (RuntimeStateKind.DB_SCHEMA, "users"),
        (RuntimeStateKind.DB_SEED, "db_seed"),
    ]
    assert states[0].identifier == "runtime:db:schema:users"
    assert states[1].expected_value == {"files": ["prisma/seed.ts"]}


def test_build_runtime_states_all_missing_returns_empty_list(tmp_path):
    assert PrismaSchemaProvider().build_runtime_states(tmp_path) == []


def test_build_runtime_states_full_osato_lms_like_prisma_project(tmp_path):
    _write_prisma_schema(
        tmp_path,
        _minimal_schema(
            """
model User {
  id       String @id
  tenantId String
}
""".strip(),
            """
model Tenant {
  id   String @id
  name String
  @@map("tenants")
}
""".strip(),
        ),
    )
    seed = tmp_path / "prisma" / "seed.ts"
    seed.write_text("await prisma.user.create({ data: {} })\n", encoding="utf-8")
    migration_dir = tmp_path / "prisma" / "migrations" / "20260505000000_init"
    migration_dir.mkdir(parents=True)
    (migration_dir / "migration.sql").write_text("CREATE TABLE users (id text);\n", encoding="utf-8")

    states = PrismaSchemaProvider().build_runtime_states(tmp_path)

    assert len(states) == 4
    assert [(state.kind, state.target) for state in states] == [
        (RuntimeStateKind.DB_SCHEMA, "users"),
        (RuntimeStateKind.DB_SCHEMA, "tenants"),
        (RuntimeStateKind.DB_SEED, "db_seed"),
        (RuntimeStateKind.DB_SCHEMA, "db_migrations"),
    ]


def test_existing_schema_provider_registry_entries_are_preserved(monkeypatch):
    sentinel = object()
    monkeypatch.setitem(SCHEMA_PROVIDERS, "existing_schema", sentinel)

    importlib.reload(prisma_module)

    assert SCHEMA_PROVIDERS["existing_schema"] is sentinel
    assert SCHEMA_PROVIDERS["prisma"] is prisma_module.PrismaSchemaProvider


def test_importing_prisma_provider_does_not_mutate_dag_check_registry(monkeypatch):
    existing_registry = {
        "node_completeness": object,
        "edge_validity": RuntimeStateKind,
        "task_completion": SchemaProvider,
        "depends_on_consistency": PrismaSchemaProvider,
        "transitive_closure": SCHEMA_PROVIDERS.__class__,
    }
    monkeypatch.setattr(dag_checks, "_REGISTRY", existing_registry.copy())

    importlib.reload(prisma_module)

    assert dag_checks.get_registry() == existing_registry


def test_prisma_provider_satisfies_schema_provider_contract():
    assert isinstance(PrismaSchemaProvider(), SchemaProvider)
