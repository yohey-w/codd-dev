"""Generic tests for requirement-to-operation reconciliation.

Every fixture is framework- and project-agnostic: synthetic ``operation_flow``
mappings plus Markdown requirement documents for an imaginary inventory CLI
tool. No real-project path or vocabulary appears in any assertion. The core
invariant is symmetric: the check stays silent when a requirement unit IS
anchored to the declared operation universe (reference, out-of-scope marker,
route, or term overlap) and fires when it is NOT.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from codd.cli import main
from codd.requirement_reconciliation import (
    DEFAULT_OUT_OF_SCOPE_MARKERS,
    declared_operation_ids,
    declared_operation_routes,
    declared_operation_tokens,
    detect_dangling_operation_references,
    detect_unreconciled_units,
    discover_requirement_docs,
    parse_requirement_units,
    requirement_reconciliation_settings,
    requirement_reconciliation_warnings,
)


def _flow(*operations: dict) -> dict:
    return {"operations": list(operations)}


def _flows(*operations: dict) -> list[tuple[str, dict]]:
    return [("synthetic", _flow(*operations))]


# --- Declared universe --------------------------------------------------------


def test_declared_ids_tokens_and_routes() -> None:
    flows = _flows(
        {"id": "import_records_csv", "verb": "import", "target": "record", "route": "/records/import"},
        {"id": "list_records", "verb": "list", "target": "record", "routes": ["/records", "/records/:id"]},
    )

    assert "import_records_csv" in declared_operation_ids(flows)
    tokens = declared_operation_tokens(flows)
    assert {"import", "records", "csv", "record"} <= tokens
    routes = declared_operation_routes(flows)
    assert "/records/import" in routes
    assert "/records/{}" in routes  # :id parameter normalized


# --- Check A: dangling operation references ------------------------------------


def test_dangling_reference_detected() -> None:
    doc = "The archive command (operation_flow.archive_record) moves a record away."
    declared = declared_operation_ids(_flows({"id": "list_records", "verb": "list", "target": "record"}))

    dangling = detect_dangling_operation_references([("docs/requirements.md", doc)], declared)

    assert len(dangling) == 1
    assert dangling[0].reference == "archive_record"
    assert "dangling_requirement_reference" in dangling[0].message
    assert "docs/requirements.md" in dangling[0].message


def test_resolving_reference_stays_silent() -> None:
    doc = "Listing (operation_flow.list_records) shows every record."
    declared = declared_operation_ids(_flows({"id": "list_records", "verb": "list", "target": "record"}))

    assert detect_dangling_operation_references([("docs/requirements.md", doc)], declared) == ()


def test_duplicate_dangling_references_deduplicated() -> None:
    doc = "operation_flow.archive_record here and operation_flow.archive_record there."
    dangling = detect_dangling_operation_references([("r.md", doc)], frozenset())
    assert len(dangling) == 1


# --- Unit parsing ---------------------------------------------------------------


_MARKED_TABLE = """
## Functional requirements

| Capability | Behaviour |
|---|---|
| Record listing | Show all records (operation_flow.list_records) |
| 完全に非英字の説明 | 別言語のみで書かれた要求内容 |
"""


def test_table_units_parsed_with_section_and_header_skipped() -> None:
    units = parse_requirement_units(_MARKED_TABLE, "r.md")

    assert [unit.label for unit in units] == ["Record listing", "完全に非英字の説明"]
    assert all(unit.section == "Functional requirements" for unit in units)
    assert all(unit.source == "r.md" for unit in units)


def test_table_without_marker_or_section_is_out_of_scope() -> None:
    doc = """
## Schedule

| Phase | Date |
|---|---|
| Kickoff | 2030-01-01 |
"""
    assert parse_requirement_units(doc, "r.md") == []


def test_sections_config_widens_scope() -> None:
    doc = """
## Functional requirements

| Capability | Behaviour |
|---|---|
| Export | Emit all data |
"""
    assert parse_requirement_units(doc, "r.md") == []
    units = parse_requirement_units(doc, "r.md", sections=("functional",))
    assert [unit.label for unit in units] == ["Export"]


# --- Check B: reconciliation anchors --------------------------------------------


def test_unit_without_anchor_in_marked_table_fires() -> None:
    flows = _flows({"id": "list_records", "verb": "list", "target": "record"})

    unreconciled = detect_unreconciled_units([("r.md", _MARKED_TABLE)], flows)

    assert len(unreconciled) == 1
    assert unreconciled[0].unit.label == "完全に非英字の説明"
    message = unreconciled[0].message
    assert "requirement_reconciliation" in message
    assert "Functional requirements" in message


def test_unit_with_resolving_reference_is_reconciled() -> None:
    flows = _flows({"id": "list_records", "verb": "list", "target": "record"})
    unreconciled = detect_unreconciled_units([("r.md", _MARKED_TABLE)], flows)
    assert all(item.unit.label != "Record listing" for item in unreconciled)


def test_unit_with_route_anchor_is_reconciled() -> None:
    doc = """
## Screens

| Screen | Behaviour |
|---|---|
| Detail | 詳細は `/records/[id]` で開く (operation_flow.view_record) |
| Importer | 取り込みは `/records/import` から行う |
"""
    flows = _flows(
        {"id": "view_record", "verb": "view", "target": "record", "route": "/records/:id"},
        {"id": "ingest", "verb": "import", "target": "stock", "route": "/records/import"},
    )

    assert detect_unreconciled_units([("r.md", doc)], flows) == ()


def test_unit_with_term_overlap_is_reconciled() -> None:
    doc = """
## Functional requirements

| Capability | Behaviour |
|---|---|
| Bulk loading | CSV 一括取り込みに対応する (operation_flow.import_records_csv) |
| Certificates | 修了時に PDF を自動発行する |
"""
    flows = _flows(
        {"id": "import_records_csv", "verb": "import", "target": "record"},
        {"id": "issue_certificate_pdf", "verb": "generate", "target": "certificate"},
    )

    # "CSV" matches import_records_csv tokens; "PDF" matches issue_certificate_pdf.
    assert detect_unreconciled_units([("r.md", doc)], flows) == ()


def test_unit_term_overlap_via_runtime_tokens() -> None:
    doc = """
## Functional requirements

| Capability | Behaviour |
|---|---|
| Reporting | summary 出力に対応 (operation_flow.list_records 参照) |
| Snapshots | snapshot を保存する |
"""
    flows = _flows({"id": "list_records", "verb": "list", "target": "record"})

    without_runtime = detect_unreconciled_units([("r.md", doc)], flows)
    assert [item.unit.label for item in without_runtime] == ["Snapshots"]

    with_runtime = detect_unreconciled_units(
        [("r.md", doc)], flows, extra_tokens=frozenset({"snapshot"})
    )
    assert with_runtime == ()


def test_out_of_scope_marker_exempts_unit() -> None:
    doc = """
## Functional requirements

| Capability | Behaviour |
|---|---|
| Listing | operation_flow.list_records covers this |
| Telepathy | mind reading (out of scope) |
| 遠隔操作 | 別端末からの操作（将来対応） |
"""
    flows = _flows({"id": "list_records", "verb": "list", "target": "record"})

    assert detect_unreconciled_units([("r.md", doc)], flows) == ()


def test_custom_out_of_scope_markers_replace_defaults() -> None:
    doc = """
## Functional requirements

| Capability | Behaviour |
|---|---|
| Listing | operation_flow.list_records covers this |
| Telepathy | mind reading [deferred] |
"""
    flows = _flows({"id": "list_records", "verb": "list", "target": "record"})

    default_run = detect_unreconciled_units([("r.md", doc)], flows)
    assert [item.unit.label for item in default_run] == ["Telepathy"]

    custom_run = detect_unreconciled_units(
        [("r.md", doc)], flows, out_of_scope_markers=("[deferred]",)
    )
    assert custom_run == ()


# --- Warning entry point ----------------------------------------------------------


def test_warnings_dormant_without_declared_operations() -> None:
    doc = "see operation_flow.ghost_operation"
    assert requirement_reconciliation_warnings([("r.md", doc)], [], {}) == []
    assert (
        requirement_reconciliation_warnings(
            [("r.md", doc)], [("synthetic", {"operations": []})], {}
        )
        == []
    )


def test_warnings_disabled_via_config() -> None:
    flows = _flows({"id": "list_records", "verb": "list", "target": "record"})
    doc = "see operation_flow.ghost_operation"
    config = {"requirement_reconciliation": {"enabled": False}}
    assert requirement_reconciliation_warnings([("r.md", doc)], flows, config) == []


def test_warnings_combine_dangling_and_units() -> None:
    flows = _flows({"id": "list_records", "verb": "list", "target": "record"})

    warnings = requirement_reconciliation_warnings([("r.md", _MARKED_TABLE)], flows, {})

    assert len(warnings) == 1  # marked-table unit; list_records reference resolves
    assert "requirement_reconciliation" in warnings[0]

    dangling_doc = _MARKED_TABLE.replace("list_records", "ghost_operation")
    warnings = requirement_reconciliation_warnings([("r.md", dangling_doc)], flows, {})
    assert any("dangling_requirement_reference" in message for message in warnings)


def test_max_unit_warnings_overflow_aggregates() -> None:
    rows = "\n".join(f"| 単位{i} | 非英字説明{i} |" for i in range(5))
    doc = f"""
## Functional requirements

| Capability | Behaviour |
|---|---|
| Listing | operation_flow.list_records covers this |
{rows}
"""
    flows = _flows({"id": "list_records", "verb": "list", "target": "record"})
    config = {"requirement_reconciliation": {"max_unit_warnings": 2}}

    warnings = requirement_reconciliation_warnings([("r.md", doc)], flows, config)

    unit_warnings = [w for w in warnings if "Requirement unit" in w]
    assert len(unit_warnings) == 2
    assert any("3 more unreconciled" in w for w in warnings)


# --- Settings -----------------------------------------------------------------------


def test_settings_defaults() -> None:
    settings = requirement_reconciliation_settings({})
    assert settings.enabled is True
    assert settings.docs == ()
    assert settings.sections == ()
    assert settings.out_of_scope_markers == DEFAULT_OUT_OF_SCOPE_MARKERS
    assert settings.max_unit_warnings == 30


def test_settings_empty_marker_list_keeps_defaults() -> None:
    settings = requirement_reconciliation_settings(
        {"requirement_reconciliation": {"out_of_scope_markers": []}}
    )
    assert settings.out_of_scope_markers == DEFAULT_OUT_OF_SCOPE_MARKERS


def test_settings_overrides() -> None:
    settings = requirement_reconciliation_settings(
        {
            "requirement_reconciliation": {
                "enabled": False,
                "docs": ["docs/spec.md"],
                "sections": ["Functional"],
                "out_of_scope_markers": ["[deferred]"],
                "max_unit_warnings": 5,
            }
        }
    )
    assert settings.enabled is False
    assert settings.docs == ("docs/spec.md",)
    assert settings.sections == ("Functional",)
    assert settings.out_of_scope_markers == ("[deferred]",)
    assert settings.max_unit_warnings == 5


# --- Doc discovery -------------------------------------------------------------------


def test_discover_requirement_docs_default_and_configured(tmp_path: Path) -> None:
    (tmp_path / "docs" / "requirements").mkdir(parents=True)
    default_doc = tmp_path / "docs" / "requirements" / "spec.md"
    default_doc.write_text("# spec", encoding="utf-8")
    top_level = tmp_path / "requirements.md"
    top_level.write_text("# top", encoding="utf-8")

    discovered = discover_requirement_docs(tmp_path, {})
    assert default_doc in discovered
    assert top_level in discovered

    other = tmp_path / "docs" / "other.md"
    other.write_text("# other", encoding="utf-8")
    configured = discover_requirement_docs(
        tmp_path, {"requirement_reconciliation": {"docs": ["docs/other.md"]}}
    )
    assert configured == [other]


# --- codd doctor integration ----------------------------------------------------------


def _doctor_project(tmp_path: Path, requirements_md: str, extra: str = "") -> Path:
    project = tmp_path / "app"
    codd_dir = project / "codd"
    codd_dir.mkdir(parents=True)
    (codd_dir / "codd.yaml").write_text(
        f"""
version: "0.1.0"
project:
  name: app
  language: python
operation_flow:
  operations:
    - id: list_records
      verb: list
      target: record
      actor: operator
{extra}
""".lstrip(),
        encoding="utf-8",
    )
    req_dir = project / "docs" / "requirements"
    req_dir.mkdir(parents=True)
    (req_dir / "requirements.md").write_text(requirements_md, encoding="utf-8")
    return project


def test_doctor_warns_on_unreconciled_unit_and_dangling_reference(tmp_path: Path) -> None:
    project = _doctor_project(
        tmp_path,
        """
## Functional requirements

| Capability | Behaviour |
|---|---|
| Listing | operation_flow.list_records covers this |
| Ghost | operation_flow.ghost_operation does not exist |
| 完全非英字 | 別言語のみの説明 |
""",
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "CoDD doctor: WARN" in result.output
    assert "dangling_requirement_reference" in result.output
    assert "ghost_operation" in result.output
    assert "requirement_reconciliation" in result.output
    assert "完全非英字" in result.output


def test_doctor_silent_when_requirements_reconciled(tmp_path: Path) -> None:
    project = _doctor_project(
        tmp_path,
        """
## Functional requirements

| Capability | Behaviour |
|---|---|
| Listing | operation_flow.list_records covers this |
| Telepathy | mind reading (out of scope) |
""",
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "dangling_requirement_reference" not in result.output
    assert "requirement_reconciliation" not in result.output


def test_doctor_opt_out_suppresses_reconciliation(tmp_path: Path) -> None:
    project = _doctor_project(
        tmp_path,
        """
## Functional requirements

| Capability | Behaviour |
|---|---|
| Ghost | operation_flow.ghost_operation does not exist |
""",
        extra="""
requirement_reconciliation:
  enabled: false
""",
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "dangling_requirement_reference" not in result.output
