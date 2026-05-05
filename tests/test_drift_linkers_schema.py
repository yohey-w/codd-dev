import textwrap

from codd import drift_linkers
from codd.coherence_engine import EventBus
from codd.drift_linkers import schema as schema_module
from codd.drift_linkers.schema import SchemaDriftLinker
from codd.parsing import PrismaSchemaInfo


def _write_catalog(project_root, body: str) -> None:
    (project_root / "expected_catalog.yaml").write_text(textwrap.dedent(body), encoding="utf-8")


def _write_design(project_root, rel_path: str = "docs/design/database_design.md") -> None:
    path = project_root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Database Design\n", encoding="utf-8")


def _write_prisma(project_root, body: str) -> None:
    path = project_root / "prisma/schema.prisma"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def _run(project_root, settings=None):
    return SchemaDriftLinker(project_root / "expected_catalog.yaml", project_root, settings or {}).run()


def test_schema_drift_linker_registered():
    assert drift_linkers.get_registry()["schema"] is SchemaDriftLinker


def test_no_catalog_skip_with_warn(tmp_path):
    result = _run(tmp_path)

    assert result.status == "skipped"
    assert result.has_drift is False
    assert any("WARN" in warning and "expected_catalog.yaml" in warning for warning in result.warnings)


def test_no_prisma_schema_skip_with_warn(tmp_path):
    _write_catalog(
        tmp_path,
        """\
        db_tables:
          - name: User
            columns:
              - name: id
                type: String
        """,
    )
    _write_design(tmp_path)

    result = _run(tmp_path)

    assert result.status == "skipped"
    assert any("WARN" in warning and "Prisma schema" in warning for warning in result.warnings)


def test_missing_table_detected(tmp_path):
    _write_catalog(
        tmp_path,
        """\
        db_tables:
          - name: User
            columns:
              - name: id
                type: String
          - name: Course
            columns:
              - name: id
                type: String
        """,
    )
    _write_design(tmp_path)
    _write_prisma(
        tmp_path,
        """\
        model User {
          id String @id
        }
        """,
    )

    result = _run(tmp_path)

    assert result.status == "drift"
    assert result.missing_tables == ["Course"]
    assert result.extra_tables == []


def test_extra_table_detected(tmp_path):
    _write_catalog(
        tmp_path,
        """\
        db_tables:
          - name: User
            columns:
              - name: id
                type: String
        """,
    )
    _write_design(tmp_path)
    _write_prisma(
        tmp_path,
        """\
        model User {
          id String @id
        }

        model Course {
          id String @id
        }
        """,
    )

    result = _run(tmp_path)

    assert result.status == "drift"
    assert result.extra_tables == ["Course"]


def test_column_mismatch_detected(tmp_path):
    _write_catalog(
        tmp_path,
        """\
        db_tables:
          - name: User
            columns:
              - name: id
                type: String
              - name: email
                type: String
              - name: age
                type: Int
        """,
    )
    _write_design(tmp_path)
    _write_prisma(
        tmp_path,
        """\
        model User {
          id    String @id
          email Int
          name  String
        }
        """,
    )

    result = _run(tmp_path)

    assert result.status == "drift"
    assert {
        (diff["column"], diff["status"], diff["expected_type"], diff["actual_type"])
        for diff in result.column_diffs
    } == {
        ("age", "missing_in_prisma", "Int", None),
        ("email", "type_mismatch", "String", "Int"),
        ("name", "extra_in_prisma", None, "String"),
    }


def test_exact_match_no_drift(tmp_path):
    _write_catalog(
        tmp_path,
        """\
        db_tables:
          - name: User
            columns:
              - name: id
                type: String
              - name: email
                type: String
        """,
    )
    _write_design(tmp_path)
    _write_prisma(
        tmp_path,
        """\
        model User {
          id    String @id
          email String
        }
        """,
    )

    result = _run(tmp_path)

    assert result.status == "ok"
    assert result.has_drift is False
    assert result.events == []


def test_drift_event_published(tmp_path):
    _write_catalog(
        tmp_path,
        """\
        db_tables:
          - name: User
            columns:
              - name: id
                type: String
          - name: Course
            columns:
              - name: id
                type: String
        """,
    )
    _write_design(tmp_path)
    _write_prisma(
        tmp_path,
        """\
        model User {
          id String @id
        }
        """,
    )
    bus = EventBus()

    result = _run(tmp_path, {"event_bus": bus})

    assert [event.kind for event in bus.published_events()] == ["schema_drift"]
    assert result.events[0].payload["missing_tables"] == ["Course"]
    assert result.events[0].source_artifact == "design_doc"
    assert result.events[0].target_artifact == "implementation"


def test_generality_web_default_design_file_path(tmp_path):
    _write_catalog(
        tmp_path,
        """\
        db_tables:
          - name: User
            columns:
              - name: id
                type: String
        """,
    )
    _write_prisma(
        tmp_path,
        """\
        model User {
          id String @id
        }
        """,
    )

    missing_design = _run(tmp_path)
    assert missing_design.status == "skipped"
    assert "docs/design/database_design.md" in missing_design.warnings[0]

    _write_design(tmp_path)
    result = _run(tmp_path)

    assert result.status == "ok"


def test_prisma_extractor_reused(tmp_path, monkeypatch):
    _write_catalog(
        tmp_path,
        """\
        db_tables:
          - name: User
            columns:
              - name: id
                type: String
        """,
    )
    _write_design(tmp_path)
    _write_prisma(tmp_path, "model Ignored { id String @id }\n")
    calls = []

    class FakeExtractor:
        def extract_schema(self, content, file_path):
            calls.append((content, file_path))
            return PrismaSchemaInfo(
                file_path=str(file_path),
                models=[{"name": "User", "fields": [{"name": "id", "type": "String"}]}],
            )

    monkeypatch.setattr(schema_module, "PrismaSchemaExtractor", FakeExtractor)

    result = _run(tmp_path)

    assert result.status == "ok"
    assert calls and calls[0][1] == tmp_path / "prisma/schema.prisma"


def test_malformed_catalog_entries_warn_without_crash(tmp_path):
    _write_catalog(
        tmp_path,
        """\
        db_tables:
          - columns: not-a-list
          - name: User
            columns:
              - type: String
              - name: id
                type: String
        """,
    )
    _write_design(tmp_path)
    _write_prisma(
        tmp_path,
        """\
        model User {
          id String @id
        }
        """,
    )

    result = _run(tmp_path)

    assert result.status == "ok"
    assert any("missing name" in warning for warning in result.warnings)
