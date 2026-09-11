"""Tests for the acceptance-evidence invariant (parser + ``acceptance_evidence`` check).

The fixtures are SYNTHETIC miniatures of the shape that produced the invariant:
a requirement table whose acceptance column states an outcome, an operation the
criterion is anchored to, an implementer the requirement id is written into, a
test that exercises that implementer, and a route entry point that does not
import it. No file is copied from any real project — the miniature exists so the
check's behaviour is pinned by structure, not by one repository's contents.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import yaml

from codd.acceptance_evidence import (
    acceptance_settings,
    load_acceptance_criteria,
    parse_acceptance_criteria,
    runtime_smoke_enabled,
)
from codd.dag.builder import build_dag, reset_dag_cache
from codd.dag.checks import get_registry
from codd.dag.checks.acceptance_evidence import AcceptanceEvidenceCheck
from codd.dag.runner import run_all_checks

REQUIREMENTS_HEADER = "| ID | 要件 | 検収条件 |\n| --- | --- | --- |\n"


def _requirements_doc(rows: str, *, header: str = REQUIREMENTS_HEADER) -> str:
    return "## 3. 機能要件\n\n" + header + rows + "\n"


def _write_project(
    tmp_path: Path,
    *,
    requirements: str,
    operations: list[dict] | None = None,
    runtime_smoke: dict | None = None,
    extra_config: dict | None = None,
    files: dict[str, str] | None = None,
    require_vb_table: bool = False,
) -> Path:
    root = tmp_path / "project"
    (root / "docs" / "requirements").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "requirements" / "requirements.md").write_text(requirements, encoding="utf-8")

    config: dict = {
        "version": "0.1.0",
        "project": {"name": "fixture", "language": "typescript"},
        "scan": {"source_dirs": ["src/", "tools/"], "test_dirs": ["tests/"], "doc_dirs": ["docs/"]},
        "requirement_reconciliation": {
            "enabled": True,
            "docs": ["docs/requirements/requirements.md"],
            # In-scope by section heading, so a fixture row does not have to cite
            # an operation just to be audited.
            "sections": ["機能要件"],
        },
        # Off by default in these fixtures so each test exercises ONE rule; the
        # default-true behaviour has its own tests below.
        "test_coverage": {"require_vb_table": require_vb_table},
    }
    if operations is not None:
        config["operation_flow"] = {"operations": operations}
    if runtime_smoke is not None:
        config["runtime_smoke"] = runtime_smoke
    if extra_config:
        config.update(extra_config)
    (root / "codd").mkdir(parents=True, exist_ok=True)
    (root / "codd" / "codd.yaml").write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")

    for relative, content in (files or {}).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content), encoding="utf-8")

    reset_dag_cache(root)
    return root


def _run_check(root: Path):
    reset_dag_cache(root)
    results = run_all_checks(root, check_names=["acceptance_evidence"])
    assert len(results) == 1
    return results[0]


def _violations(result, kind: str) -> list[dict]:
    return [item for item in result.violations if item.get("type") == kind]


# ---------------------------------------------------------------------------
# registration / parsing
# ---------------------------------------------------------------------------


def test_check_is_registered():
    assert get_registry()["acceptance_evidence"] is AcceptanceEvidenceCheck


def test_parses_acceptance_column_of_an_operation_traceable_table():
    text = _requirements_doc(
        "| R-1 | 印刷用の一覧を出す | A4 1枚に15人が並ぶ。`operation_flow.sheet_print` |\n"
    )
    criteria = parse_acceptance_criteria(text, "requirements.md")
    assert [c.req_id for c in criteria] == ["R-1"]
    assert criteria[0].operation_refs == ("sheet_print",)
    assert "15人" in criteria[0].text


def test_table_without_an_acceptance_column_declares_nothing():
    """A requirement table that never states acceptance must not invent criteria."""

    text = (
        "## 3. 機能要件\n\n| ID | 要件 |\n| --- | --- |\n"
        "| R-1 | 印刷用の一覧を出す `operation_flow.sheet_print` |\n"
    )
    assert parse_acceptance_criteria(text, "requirements.md") == []


def test_out_of_scope_table_is_silent():
    """Scope is the shared requirement_reconciliation rule: no operation reference,
    no configured section -> the table is not audited at all."""

    text = _requirements_doc("| R-1 | 印刷用の一覧を出す | A4 1枚に15人が並ぶ |\n")
    assert parse_acceptance_criteria(text, "requirements.md") == []
    assert len(parse_acceptance_criteria(text, "requirements.md", sections=("機能要件",))) == 1


def test_empty_and_non_id_rows_are_skipped():
    text = _requirements_doc(
        "| R-1 | 出す | — |\n"
        "| これは説明文です | 出す | `operation_flow.sheet_print` が動く |\n"
        "| R-2 | 出す | 15人が並ぶ `operation_flow.sheet_print` |\n"
    )
    assert [c.req_id for c in parse_acceptance_criteria(text, "requirements.md")] == ["R-2"]


def test_blank_cells_do_not_shift_column_identity():
    """Positional cells: a blank middle cell must not slide the acceptance column."""

    text = _requirements_doc(
        "| R-1 |  | 15人が並ぶ `operation_flow.sheet_print` |\n",
        header="| ID | 要件 | 検収条件 |\n| --- | --- | --- |\n",
    )
    criteria = parse_acceptance_criteria(text, "requirements.md")
    assert criteria[0].text.startswith("15人")


def test_english_acceptance_header_and_configured_extra_header():
    english = (
        "## Functional\n\n| ID | Requirement | Acceptance |\n| --- | --- | --- |\n"
        "| R-1 | print a sheet | 15 per page, `operation_flow.sheet_print` |\n"
    )
    assert len(parse_acceptance_criteria(english, "r.md")) == 1

    custom = (
        "## Functional\n\n| ID | Requirement | 検品 |\n| --- | --- | --- |\n"
        "| R-1 | print a sheet | 15 per page, `operation_flow.sheet_print` |\n"
    )
    assert parse_acceptance_criteria(custom, "r.md") == []
    settings = acceptance_settings({"acceptance_evidence": {"acceptance_columns": ["検品"]}})
    assert len(
        parse_acceptance_criteria(
            custom, "r.md", acceptance_columns=settings.effective_acceptance_columns
        )
    ) == 1


def test_load_acceptance_criteria_reads_configured_requirement_docs(tmp_path):
    root = _write_project(
        tmp_path,
        requirements=_requirements_doc("| R-1 | 出す | 15人 `operation_flow.sheet_print` |\n"),
        operations=[{"id": "sheet_print", "actor": "operator", "verb": "print", "target": "sheet"}],
    )
    from codd.config import load_project_config

    criteria = load_acceptance_criteria(root, load_project_config(root))
    assert [c.req_id for c in criteria] == ["R-1"]


# ---------------------------------------------------------------------------
# stage 1 — (a) a runtime obligation in a project whose runtime stage is off
# ---------------------------------------------------------------------------


def test_runtime_smoke_enabled_reads_the_project_switch():
    assert runtime_smoke_enabled({"runtime_smoke": {"enabled": True}}) is True
    assert runtime_smoke_enabled({"runtime_smoke": {"enabled": False}}) is False
    assert runtime_smoke_enabled({}) is False  # absent section == never executed


def test_runtime_obligation_without_runtime_smoke_is_red(tmp_path):
    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(
            "| R-1 | 印刷用の一覧を出す | 15人が並ぶ <sub>`operation_flow.sheet_print`</sub> |\n"
        ),
        operations=[
            {"id": "sheet_print", "actor": "operator", "verb": "print", "target": "sheet", "route": "/admin"}
        ],
    )
    result = _run_check(root)
    assert result.passed is False
    assert result.severity == "red"
    assert result.checked_count == 1
    found = _violations(result, "runtime_evidence_not_executable")
    assert [item["req_id"] for item in found] == ["R-1"]
    assert "runtime_smoke" in found[0]["message"]


def test_runtime_obligation_with_runtime_smoke_enabled_is_clean(tmp_path):
    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(
            "| R-1 | 印刷用の一覧を出す | 15人が並ぶ <sub>`operation_flow.sheet_print`</sub> |\n"
        ),
        operations=[{"id": "sheet_print", "actor": "operator", "verb": "print", "target": "sheet"}],
        runtime_smoke={"enabled": True, "dev_server": {"url": "http://localhost:3000"}},
    )
    result = _run_check(root)
    assert _violations(result, "runtime_evidence_not_executable") == []


def test_dangling_operation_reference_is_not_a_runtime_obligation(tmp_path):
    """A reference that resolves to nothing is requirement_reconciliation's finding;
    counting it here too would report one defect twice."""

    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(
            "| R-1 | 出す | 15人が並ぶ <sub>`operation_flow.does_not_exist`</sub> |\n"
        ),
        operations=[{"id": "sheet_print", "actor": "operator", "verb": "print", "target": "sheet"}],
    )
    assert _violations(_run_check(root), "runtime_evidence_not_executable") == []


def test_explicit_verified_by_overrides_the_inferred_runtime_obligation(tmp_path):
    """Once a project adopts `verified_by`, the declaration wins over inference."""

    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(
            "| R-1 | 出す | 15人が並ぶ `operation_flow.sheet_print` | test:sheet |\n",
            header="| ID | 要件 | 検収条件 | verified_by |\n| --- | --- | --- | --- |\n",
        ),
        operations=[{"id": "sheet_print", "actor": "operator", "verb": "print", "target": "sheet"}],
    )
    assert _violations(_run_check(root), "runtime_evidence_not_executable") == []


def test_explicit_runtime_verified_by_is_red_even_when_no_operation_is_referenced(tmp_path):
    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(
            "| R-1 | 出す | 15人が並ぶ `operation_flow.sheet_print` | runtime:print_sheet |\n",
            header="| ID | 要件 | 検収条件 | verified_by |\n| --- | --- | --- | --- |\n",
        ),
        operations=[{"id": "sheet_print", "actor": "operator", "verb": "print", "target": "sheet"}],
    )
    found = _violations(_run_check(root), "runtime_evidence_not_executable")
    assert found and found[0]["targets"] == ["print_sheet"]


def test_runtime_severity_is_configurable_to_amber(tmp_path):
    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(
            "| R-1 | 出す | 15人が並ぶ <sub>`operation_flow.sheet_print`</sub> |\n"
        ),
        operations=[{"id": "sheet_print", "actor": "operator", "verb": "print", "target": "sheet"}],
        extra_config={
            "acceptance_evidence": {"runtime_severity": "amber", "unbound_severity": "amber"}
        },
    )
    result = _run_check(root)
    assert result.passed is True
    assert result.status == "warn"
    found = _violations(result, "runtime_evidence_not_executable")
    assert found and found[0]["severity"] == "amber"


def test_project_without_acceptance_criteria_is_dormant_not_green(tmp_path):
    """No criteria == nothing to certify: skip (checked_count 0), never a
    'verified' pass that the materiality overlay would read as a clean run."""

    root = _write_project(
        tmp_path,
        requirements="## 3. 機能要件\n\n本文だけの文書。\n",
        operations=[{"id": "sheet_print", "actor": "operator", "verb": "print", "target": "sheet"}],
    )
    result = _run_check(root)
    assert result.skipped is True
    assert result.status == "skip"
    assert result.checked_count == 0


def test_disabled_section_skips_the_check(tmp_path):
    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(
            "| R-1 | 出す | 15人が並ぶ <sub>`operation_flow.sheet_print`</sub> |\n"
        ),
        operations=[{"id": "sheet_print", "actor": "operator", "verb": "print", "target": "sheet"}],
        extra_config={"acceptance_evidence": {"enabled": False}},
    )
    assert _run_check(root).skipped is True


def test_truncated_findings_still_name_every_criterion(tmp_path):
    rows = "".join(
        f"| R-{index} | 出す | 条件 <sub>`operation_flow.sheet_print`</sub> |\n" for index in range(1, 8)
    )
    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(rows),
        operations=[{"id": "sheet_print", "actor": "operator", "verb": "print", "target": "sheet"}],
        extra_config={"acceptance_evidence": {"max_findings": 2}},
    )
    result = _run_check(root)
    truncated = _violations(result, "findings_truncated")
    assert truncated, "an overflow must be reported"
    assert "R-7" in truncated[0]["message"]


def test_dag_build_is_not_required_for_the_operation_universe(tmp_path):
    """The check reads codd.yaml operations directly, so it works on a project
    whose DAG carries no design documents."""

    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(
            "| R-1 | 出す | 条件 <sub>`operation_flow.sheet_print`</sub> |\n"
        ),
        operations=[{"id": "sheet_print", "actor": "operator", "verb": "print", "target": "sheet"}],
    )
    dag = build_dag(root)
    result = AcceptanceEvidenceCheck(dag=dag, project_root=root, settings={}).run(
        codd_config=yaml.safe_load((root / "codd" / "codd.yaml").read_text(encoding="utf-8"))
    )
    assert _violations(result, "runtime_evidence_not_executable")


# ---------------------------------------------------------------------------
# stage 2 — (a) wiring: every criterion reaches machine-checked evidence
# ---------------------------------------------------------------------------

ROW_WITH_OPERATION = "| R-1 | 出す | 15人が並ぶ <sub>`operation_flow.sheet_print`</sub> |\n"
SHEET_OPERATION = [{"id": "sheet_print", "actor": "operator", "verb": "print", "target": "sheet", "route": "/admin"}]
RUNTIME_ON = {"enabled": True, "dev_server": {"url": "http://localhost:3000"}}


def test_no_vb_registry_is_red_when_the_project_has_criteria_to_certify(tmp_path):
    """The "empty registry passes with a notice" rule made empty the safest state."""

    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(ROW_WITH_OPERATION),
        operations=SHEET_OPERATION,
        runtime_smoke=RUNTIME_ON,
        require_vb_table=True,
    )
    result = _run_check(root)
    assert result.passed is False
    assert _violations(result, "vb_registry_missing")


def test_require_vb_table_false_restores_the_old_pass(tmp_path):
    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(ROW_WITH_OPERATION),
        operations=SHEET_OPERATION,
        runtime_smoke=RUNTIME_ON,
        require_vb_table=False,
    )
    assert _violations(_run_check(root), "vb_registry_missing") == []


def test_criterion_bound_to_nothing_is_red(tmp_path):
    root = _write_project(
        tmp_path,
        requirements=_requirements_doc("| R-1 | 出す | 15人が並ぶ `operation_flow.sheet_print` |\n"),
        operations=SHEET_OPERATION,
        runtime_smoke=RUNTIME_ON,
        files={"tests/unit/other.test.ts": "test('other', () => {});\n"},
    )
    # runtime_smoke is ON, so the operation anchor is executable evidence.
    assert _violations(_run_check(root), "unbound_acceptance") == []

    root2 = _write_project(
        tmp_path / "b",
        requirements=_requirements_doc("| R-1 | 出す | 15人 `operation_flow.sheet_print` | |\n",
                                       header="| ID | 要件 | 検収条件 | verified_by |\n| --- | --- | --- | --- |\n"),
        operations=SHEET_OPERATION,
        runtime_smoke=RUNTIME_ON,
    )
    found = _violations(_run_check(root2), "unbound_acceptance")
    assert [item["req_id"] for item in found] == ["R-1"]


def test_a_test_file_naming_the_requirement_id_binds_the_criterion(tmp_path):
    """The brownfield path: no document surgery, just the id in the test."""

    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(
            "| R-1 | 出す | 15人 | |\n",
            header="| ID | 要件 | 検収条件 | verified_by |\n| --- | --- | --- | --- |\n",
        ),
        operations=SHEET_OPERATION,
        runtime_smoke=RUNTIME_ON,
        extra_config={"requirement_reconciliation": {"enabled": True, "sections": ["機能要件"],
                                                     "docs": ["docs/requirements/requirements.md"]}},
        files={"tests/unit/sheet.test.ts": "// R-1: 15 per sheet\ntest('sheet', () => {});\n"},
    )
    assert _violations(_run_check(root), "unbound_acceptance") == []


def test_requirement_id_anchor_matches_whole_tokens_only(tmp_path):
    """`R-1` must not be bound by a file that only mentions `R-12`."""

    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(
            "| R-1 | 出す | 15人 | |\n",
            header="| ID | 要件 | 検収条件 | verified_by |\n| --- | --- | --- | --- |\n",
        ),
        operations=SHEET_OPERATION,
        runtime_smoke=RUNTIME_ON,
        extra_config={"requirement_reconciliation": {"enabled": True, "sections": ["機能要件"],
                                                     "docs": ["docs/requirements/requirements.md"]}},
        files={"tests/unit/sheet.test.ts": "// R-12 only\ntest('sheet', () => {});\n"},
    )
    assert [item["req_id"] for item in _violations(_run_check(root), "unbound_acceptance")] == ["R-1"]


def test_verified_by_test_pointer_must_resolve(tmp_path):
    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(
            "| R-1 | 出す | 15人 `operation_flow.sheet_print` | test:nosuchtest |\n",
            header="| ID | 要件 | 検収条件 | verified_by |\n| --- | --- | --- | --- |\n",
        ),
        operations=SHEET_OPERATION,
        runtime_smoke=RUNTIME_ON,
        files={"tests/unit/sheet.test.ts": "test('sheet', () => {});\n"},
    )
    assert _violations(_run_check(root), "unresolved_evidence")

    root2 = _write_project(
        tmp_path / "b",
        requirements=_requirements_doc(
            "| R-1 | 出す | 15人 `operation_flow.sheet_print` | test:sheet |\n",
            header="| ID | 要件 | 検収条件 | verified_by |\n| --- | --- | --- | --- |\n",
        ),
        operations=SHEET_OPERATION,
        runtime_smoke=RUNTIME_ON,
        files={"tests/unit/sheet.test.ts": "test('sheet', () => {});\n"},
    )
    result = _run_check(root2)
    assert _violations(result, "unresolved_evidence") == []
    assert _violations(result, "unbound_acceptance") == []


def test_a_covered_vb_row_naming_the_requirement_binds_it(tmp_path):
    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(
            "| R-1 | 出す | 15人 | |\n",
            header="| ID | 要件 | 検収条件 | verified_by |\n| --- | --- | --- | --- |\n",
        ),
        operations=SHEET_OPERATION,
        runtime_smoke=RUNTIME_ON,
        require_vb_table=True,
        extra_config={"requirement_reconciliation": {"enabled": True, "sections": ["機能要件"],
                                                     "docs": ["docs/requirements/requirements.md"]}},
        files={
            "docs/test/test_strategy.md": (
                "# Test strategy\n\n| VB ID | behavior | Requirement |\n| --- | --- | --- |\n"
                "| VB-R-1 | 15 per sheet | R-1 |\n"
            ),
            "tests/unit/sheet.test.ts": "// codd: covers vb=VB-R-1\ntest('sheet', () => {});\n",
        },
    )
    result = _run_check(root)
    assert _violations(result, "vb_registry_missing") == []
    assert _violations(result, "unbound_acceptance") == []


def test_an_uncovered_vb_row_does_not_launder_into_acceptance(tmp_path):
    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(
            "| R-1 | 出す | 15人 | |\n",
            header="| ID | 要件 | 検収条件 | verified_by |\n| --- | --- | --- | --- |\n",
        ),
        operations=SHEET_OPERATION,
        runtime_smoke=RUNTIME_ON,
        require_vb_table=True,
        extra_config={"requirement_reconciliation": {"enabled": True, "sections": ["機能要件"],
                                                     "docs": ["docs/requirements/requirements.md"]}},
        files={
            "docs/test/test_strategy.md": (
                "# Test strategy\n\n| VB ID | behavior | Requirement |\n| --- | --- | --- |\n"
                "| VB-R-1 | 15 per sheet | R-1 |\n"
            ),
        },
    )
    assert _violations(_run_check(root), "unbound_acceptance")


# ---------------------------------------------------------------------------
# stage 2 — (d) freshness: a manual verdict expires with its implementation
# ---------------------------------------------------------------------------


def _manual_project(tmp_path, *, critical: str = "") -> Path:
    header = "| ID | 要件 | 検収条件 | verified_by | critical |\n| --- | --- | --- | --- | --- |\n"
    return _write_project(
        tmp_path,
        requirements=_requirements_doc(
            f"| R-1 | 出す | 15人 `operation_flow.sheet_print` | manual:yohey | {critical} |\n",
            header=header,
        ),
        operations=SHEET_OPERATION,
        runtime_smoke=RUNTIME_ON,
        files={"tools/make-sheet.ts": "// R-1 sheet builder\nexport const build = () => 15;\n"},
    )


def test_manual_evidence_without_a_record_is_red(tmp_path):
    result = _run_check(_manual_project(tmp_path))
    assert _violations(result, "manual_evidence_missing")


def test_recorded_manual_evidence_binds_and_then_goes_stale(tmp_path):
    from codd.acceptance_record import load_ledger, record_acceptance

    root = _manual_project(tmp_path)
    record_acceptance(root, "R-1", status="pass", by="yohey", implementation_paths=["tools/make-sheet.ts"])
    result = _run_check(root)
    assert _violations(result, "manual_evidence_missing") == []
    assert _violations(result, "stale_manual_evidence") == []
    assert _violations(result, "unbound_acceptance") == []
    assert load_ledger(root)["R-1"].by == "yohey"

    (root / "tools" / "make-sheet.ts").write_text(
        "// R-1 sheet builder\nexport const build = () => 20;\n", encoding="utf-8"
    )
    stale = _violations(_run_check(root), "stale_manual_evidence")
    assert stale and "R-1" in stale[0]["message"]


def test_a_new_implementer_also_invalidates_the_manual_record(tmp_path):
    from codd.acceptance_record import record_acceptance

    root = _manual_project(tmp_path)
    record_acceptance(root, "R-1", status="pass", by="yohey", implementation_paths=["tools/make-sheet.ts"])
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "panel.tsx").write_text("// R-1 shipped here too\n", encoding="utf-8")
    assert _violations(_run_check(root), "stale_manual_evidence")


def test_critical_criterion_on_manual_evidence_alone_is_amber(tmp_path):
    from codd.acceptance_record import record_acceptance

    root = _manual_project(tmp_path, critical="true")
    record_acceptance(root, "R-1", status="pass", by="yohey", implementation_paths=["tools/make-sheet.ts"])
    result = _run_check(root)
    found = _violations(result, "critical_manual_only")
    assert found and found[0]["severity"] == "amber"
    assert result.passed is True


def test_a_malformed_ledger_reads_as_no_evidence(tmp_path):
    from codd.acceptance_record import ledger_path

    root = _manual_project(tmp_path)
    ledger_path(root).write_text("{ not json", encoding="utf-8")
    assert _violations(_run_check(root), "manual_evidence_missing")


# ---------------------------------------------------------------------------
# stage 2 — `codd acceptance sync` derives the registry the gate demands
# ---------------------------------------------------------------------------


def test_sync_derives_one_vb_row_per_criterion_and_is_idempotent(tmp_path):
    from codd.acceptance_evidence import build_evidence_context
    from codd.acceptance_sync import sync_vb_registry
    from codd.config import load_project_config

    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(
            "| R-1 | 出す | A4 1枚に15人 <br>並ぶ `operation_flow.sheet_print` |\n"
            "| R-2 | 消す | 消える `operation_flow.sheet_print` |\n"
        ),
        operations=SHEET_OPERATION,
        runtime_smoke=RUNTIME_ON,
    )
    config = load_project_config(root)
    context = build_evidence_context(root, config)
    first = sync_vb_registry(root, context.criteria, config=config)
    assert first.created is True
    assert first.added == ("VB-R-1", "VB-R-2")
    text = first.path.read_text(encoding="utf-8")
    assert "| VB-R-1 |" in text and "<br>" not in text

    second = sync_vb_registry(root, context.criteria, config=config)
    assert second.added == ()
    assert second.already_present == ("VB-R-1", "VB-R-2")
    assert first.path.read_text(encoding="utf-8") == text


def test_sync_dry_run_writes_nothing(tmp_path):
    from codd.acceptance_evidence import build_evidence_context
    from codd.acceptance_sync import sync_vb_registry
    from codd.config import load_project_config

    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(ROW_WITH_OPERATION),
        operations=SHEET_OPERATION,
        runtime_smoke=RUNTIME_ON,
    )
    result = sync_vb_registry(root, build_evidence_context(root, load_project_config(root)).criteria,
                              dry_run=True)
    assert result.added == ("VB-R-1",)
    assert not result.path.exists()


# ---------------------------------------------------------------------------
# stage 2 — the implement-time gate honours the same switch
# ---------------------------------------------------------------------------


def test_implement_gate_fails_on_an_empty_registry_when_criteria_exist(tmp_path):
    from codd.config import load_project_config
    from codd.verifiable_behavior_audit import run_implement_coverage_gate

    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(ROW_WITH_OPERATION),
        operations=SHEET_OPERATION,
        runtime_smoke=RUNTIME_ON,
        require_vb_table=True,
        files={"tests/unit/sheet.test.ts": "test('sheet', () => {});\n"},
    )
    messages: list[str] = []
    passed = run_implement_coverage_gate(
        root,
        config=load_project_config(root),
        design_node=None,
        output_paths=["tests/unit/sheet.test.ts"],
        echo=messages.append,
        echo_error=messages.append,
    )
    assert passed is False
    assert any("require_vb_table" in message for message in messages)


def test_implement_gate_still_passes_a_project_with_nothing_to_certify(tmp_path):
    from codd.config import load_project_config
    from codd.verifiable_behavior_audit import run_implement_coverage_gate

    root = _write_project(
        tmp_path,
        requirements="## 3. 機能要件\n\n本文だけの文書。\n",
        operations=SHEET_OPERATION,
        require_vb_table=True,
        files={"tests/unit/sheet.test.ts": "test('sheet', () => {});\n"},
    )
    messages: list[str] = []
    passed = run_implement_coverage_gate(
        root,
        config=load_project_config(root),
        design_node=None,
        output_paths=["tests/unit/sheet.test.ts"],
        echo=messages.append,
        echo_error=messages.append,
    )
    assert passed is True
    assert any("nothing to audit" in message for message in messages)


# ---------------------------------------------------------------------------
# stage 3 — (c) the evidence must reference the criterion's NAMED parameters
# ---------------------------------------------------------------------------


def test_numeric_literal_tokenizer_ignores_numbers_inside_other_tokens():
    from codd.acceptance_evidence import numeric_literals

    assert numeric_literals("A4 1枚に15人（5行×3列）") == ["1", "15", "5", "3"]
    # ids, dates, versions, paths and markup are not stated values
    assert numeric_literals("F-E2 v1.7 2026-08-22 <sub>note</sub> /r/token") == []
    assert numeric_literals("0.5 秒以内") == ["0.5"]


def test_bare_literal_without_declared_params_is_amber(tmp_path):
    root = _write_project(
        tmp_path,
        requirements=_requirements_doc("| R-1 | 出す | A4 1枚に15人が並ぶ `operation_flow.sheet_print` |\n"),
        operations=SHEET_OPERATION,
        runtime_smoke=RUNTIME_ON,
    )
    result = _run_check(root)
    found = _violations(result, "undeclared_numeric")
    assert found and found[0]["literals"] == ["1", "15"]
    assert found[0]["severity"] == "amber"


def test_declaring_params_silences_the_nudge(tmp_path):
    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(
            "| R-1 | 出す | A4 1枚に15人が並ぶ `operation_flow.sheet_print` | per_sheet=15 |\n",
            header="| ID | 要件 | 検収条件 | params |\n| --- | --- | --- | --- |\n",
        ),
        operations=SHEET_OPERATION,
        runtime_smoke=RUNTIME_ON,
    )
    assert _violations(_run_check(root), "undeclared_numeric") == []


def test_declared_param_missing_from_the_bound_evidence_is_red(tmp_path):
    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(
            "| R-1 | 出す | 1枚に15人 | test:sheet | per_sheet=15 |\n",
            header="| ID | 要件 | 検収条件 | verified_by | params |\n| --- | --- | --- | --- | --- |\n",
        ),
        operations=SHEET_OPERATION,
        runtime_smoke=RUNTIME_ON,
        files={"tests/unit/sheet.test.ts": "test('sheet', () => { expect(rows()).toBe(20); });\n"},
    )
    found = _violations(_run_check(root), "param_not_referenced")
    assert found and found[0]["params"] == ["per_sheet"]
    assert found[0]["severity"] == "red"


def test_evidence_reading_the_param_by_name_passes(tmp_path):
    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(
            "| R-1 | 出す | 1枚に15人 | test:sheet | per_sheet=15 |\n",
            header="| ID | 要件 | 検収条件 | verified_by | params |\n| --- | --- | --- | --- | --- |\n",
        ),
        operations=SHEET_OPERATION,
        runtime_smoke=RUNTIME_ON,
        files={
            "tests/unit/sheet.test.ts": (
                "import { spec } from './spec';\n"
                "test('sheet', () => { expect(rows()).toBe(spec.per_sheet); });\n"
            )
        },
    )
    result = _run_check(root)
    assert _violations(result, "param_not_referenced") == []
    assert _violations(result, "undeclared_numeric") == []


def test_params_without_any_bound_evidence_report_the_binding_not_the_param(tmp_path):
    """An unbound criterion's parameter has nowhere to be referenced yet — the
    finding is the missing binding, and reporting both would be noise."""

    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(
            "| R-1 | 出す | 1枚に15人 | | per_sheet=15 |\n",
            header="| ID | 要件 | 検収条件 | verified_by | params |\n| --- | --- | --- | --- | --- |\n",
        ),
        operations=SHEET_OPERATION,
        runtime_smoke=RUNTIME_ON,
    )
    result = _run_check(root)
    assert _violations(result, "param_not_referenced") == []
    assert _violations(result, "unbound_acceptance")


# ---------------------------------------------------------------------------
# stage 4 — (b) the evidence must run through the SHIPPED PATH
# ---------------------------------------------------------------------------

FILESYSTEM_ROUTES = [
    {
        "base_dir": "src/app/",
        "page_pattern": "page.{tsx,jsx}",
        "api_pattern": "route.{ts,js}",
        "url_template": "/{relative_dir}",
        "dynamic_segment": {"from": "\\[(.+)\\]", "to": ":$1"},
    }
]


def _shipped_path_project(tmp_path, *, entry_imports_sheet: bool) -> Path:
    """The miniature of the failure: a page users press, and a script a test covers."""

    page = (
        "import { panel } from './panels';\nexport default function Page() { return panel(); }\n"
        if not entry_imports_sheet
        else "import { build } from '../../../tools/make-sheet';\nexport default function Page() { return build(); }\n"
    )
    return _write_project(
        tmp_path,
        requirements=_requirements_doc(
            "| R-1 | 印刷 | 1枚に15人 <sub>`operation_flow.sheet_print`</sub> |\n"
        ),
        operations=SHEET_OPERATION,
        runtime_smoke=RUNTIME_ON,
        extra_config={"filesystem_routes": FILESYSTEM_ROUTES},
        files={
            "src/app/admin/page.tsx": page,
            "src/app/admin/panels.tsx": "export const panel = () => 'sheet';\n",
            "tools/make-sheet.ts": "// R-1 sheet builder\nexport const build = () => 15;\n",
            "tests/unit/sheet.test.ts": (
                "import { build } from '../../tools/make-sheet';\n"
                "test('sheet', () => { expect(build()).toBe(15); });\n"
            ),
        },
    )


def test_evidence_that_never_reaches_the_entry_point_is_amber(tmp_path):
    result = _run_check(_shipped_path_project(tmp_path, entry_imports_sheet=False))
    found = _violations(result, "off_shipped_path")
    assert found, "the test covers a script the entry point does not import"
    assert found[0]["severity"] == "amber"
    assert "tools/make-sheet.ts" in found[0]["off_path"]
    assert found[0]["entries"] == ["src/app/admin/page.tsx"]
    assert result.passed is True  # amber: visible, not blocking


def test_evidence_on_the_shipped_path_is_clean(tmp_path):
    result = _run_check(_shipped_path_project(tmp_path, entry_imports_sheet=True))
    assert _violations(result, "off_shipped_path") == []
    assert _violations(result, "multiple_implementers") == []


def test_an_implementation_on_each_side_of_the_path_is_flagged(tmp_path):
    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(
            "| R-1 | 印刷 | 1枚に15人 <sub>`operation_flow.sheet_print`</sub> |\n"
        ),
        operations=SHEET_OPERATION,
        runtime_smoke=RUNTIME_ON,
        extra_config={"filesystem_routes": FILESYSTEM_ROUTES},
        files={
            "src/app/admin/page.tsx": "import { panel } from './panels';\nexport default () => panel();\n",
            # on the shipped path AND claims the requirement
            "src/app/admin/panels.tsx": "// R-1 printed here\nexport const panel = () => 20;\n",
            # off the path, claims the same requirement, and owns the only test
            "tools/make-sheet.ts": "// R-1 sheet builder\nexport const build = () => 15;\n",
            "tests/unit/sheet.test.ts": (
                "import { build } from '../../tools/make-sheet';\n"
                "test('sheet', () => { expect(build()).toBe(15); });\n"
            ),
        },
    )
    found = _violations(_run_check(root), "multiple_implementers")
    assert found
    assert found[0]["on_path"] == ["src/app/admin/panels.tsx"]
    assert "tools/make-sheet.ts" in found[0]["off_path"]


def test_allow_multiple_implementers_silences_only_that_finding(tmp_path):
    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(
            "| R-1 | 印刷 | 1枚に15人 <sub>`operation_flow.sheet_print`</sub> |\n"
        ),
        operations=SHEET_OPERATION,
        runtime_smoke=RUNTIME_ON,
        extra_config={
            "filesystem_routes": FILESYSTEM_ROUTES,
            "acceptance_evidence": {"allow_multiple_implementers": True},
        },
        files={
            "src/app/admin/page.tsx": "import { panel } from './panels';\nexport default () => panel();\n",
            "src/app/admin/panels.tsx": "// R-1 printed here\nexport const panel = () => 20;\n",
            "tools/make-sheet.ts": "// R-1 sheet builder\nexport const build = () => 15;\n",
            "tests/unit/sheet.test.ts": (
                "import { build } from '../../tools/make-sheet';\n"
                "test('sheet', () => { expect(build()).toBe(15); });\n"
            ),
        },
    )
    result = _run_check(root)
    assert _violations(result, "multiple_implementers") == []
    assert _violations(result, "off_shipped_path")


def test_an_unresolvable_entry_point_is_reported_as_unknown_not_silence(tmp_path):
    """No route -> no closure -> the question is unanswered. Say so."""

    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(
            "| R-1 | 印刷 | 1枚に15人 <sub>`operation_flow.sheet_print`</sub> |\n"
        ),
        operations=[{"id": "sheet_print", "actor": "operator", "verb": "print", "target": "sheet"}],
        runtime_smoke=RUNTIME_ON,
        files={
            "tools/make-sheet.ts": "// R-1 sheet builder\nexport const build = () => 15;\n",
            "tests/unit/sheet.test.ts": (
                "import { build } from '../../tools/make-sheet';\n"
                "test('sheet', () => { expect(build()).toBe(15); });\n"
            ),
        },
    )
    found = _violations(_run_check(root), "reachability_unknown")
    assert found and found[0]["severity"] == "amber"


def test_a_criterion_with_no_evidence_at_all_does_not_add_path_noise(tmp_path):
    """Nothing is offered as proof, so (b) has nothing to place — (a) already speaks."""

    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(
            "| R-1 | 印刷 | 1枚に15人 | |\n",
            header="| ID | 要件 | 検収条件 | verified_by |\n| --- | --- | --- | --- |\n",
        ),
        operations=SHEET_OPERATION,
        runtime_smoke=RUNTIME_ON,
        extra_config={"filesystem_routes": FILESYSTEM_ROUTES},
        files={"src/app/admin/page.tsx": "export default () => null;\n"},
    )
    result = _run_check(root)
    assert _violations(result, "off_shipped_path") == []
    assert _violations(result, "reachability_unknown") == []
    assert _violations(result, "unbound_acceptance")


def test_explicit_entry_file_overrides_route_inference(tmp_path):
    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(
            "| R-1 | 印刷 | 1枚に15人 <sub>`operation_flow.sheet_print`</sub> |\n"
        ),
        operations=[
            {
                "id": "sheet_print",
                "actor": "operator",
                "verb": "print",
                "target": "sheet",
                "entry_file": "tools/make-sheet.ts",
            }
        ],
        runtime_smoke=RUNTIME_ON,
        files={
            "tools/make-sheet.ts": "// R-1 sheet builder\nexport const build = () => 15;\n",
            "tests/unit/sheet.test.ts": (
                "import { build } from '../../tools/make-sheet';\n"
                "test('sheet', () => { expect(build()).toBe(15); });\n"
            ),
        },
    )
    result = _run_check(root)
    assert _violations(result, "reachability_unknown") == []
    assert _violations(result, "off_shipped_path") == []


def test_manual_only_evidence_is_not_asked_to_reference_a_parameter(tmp_path):
    """A person's verdict is not a file with a symbol in it — a red nobody can clear."""

    from codd.acceptance_record import record_acceptance

    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(
            "| R-1 | 出す | 1枚に15人 `operation_flow.sheet_print` | manual:yohey | per_sheet=15 |\n",
            header="| ID | 要件 | 検収条件 | verified_by | params |\n| --- | --- | --- | --- | --- |\n",
        ),
        operations=SHEET_OPERATION,
        runtime_smoke=RUNTIME_ON,
        files={"tools/make-sheet.ts": "// R-1 sheet builder\nexport const build = () => 15;\n"},
    )
    record_acceptance(root, "R-1", status="pass", by="yohey", implementation_paths=["tools/make-sheet.ts"])
    result = _run_check(root)
    assert _violations(result, "param_not_referenced") == []
    assert _violations(result, "manual_evidence_missing") == []
    assert _violations(result, "stale_manual_evidence") == []


def test_the_cap_is_per_finding_type_so_a_loud_red_cannot_hide_an_amber(tmp_path):
    """A global cap would let 50 reds push the shipped-path amber out of the output."""

    rows = "".join(
        f"| R-{index} | 出す | 条件 <sub>`operation_flow.sheet_print`</sub> |\n" for index in range(1, 6)
    )
    rows += "| R-9 | 印刷 | 1枚に15人 <sub>`operation_flow.sheet_print`</sub> |\n"
    root = _write_project(
        tmp_path,
        requirements=_requirements_doc(rows),
        operations=SHEET_OPERATION,
        extra_config={
            "filesystem_routes": FILESYSTEM_ROUTES,
            "acceptance_evidence": {"max_findings": 2},
        },
        files={
            "src/app/admin/page.tsx": "export default () => null;\n",
            "tools/make-sheet.ts": "// R-9 sheet builder\nexport const build = () => 15;\n",
            "tests/unit/sheet.test.ts": (
                "import { build } from '../../tools/make-sheet';\n"
                "test('sheet', () => { expect(build()).toBe(15); });\n"
            ),
        },
    )
    result = _run_check(root)
    assert len(_violations(result, "runtime_evidence_not_executable")) == 2  # capped
    assert _violations(result, "off_shipped_path"), "an amber class must survive the cap"
    assert "off_shipped_path 1" in result.message  # per-type tally is always visible
