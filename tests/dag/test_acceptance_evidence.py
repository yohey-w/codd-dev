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
        },
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
        extra_config={"acceptance_evidence": {"runtime_severity": "amber"}},
    )
    result = _run_check(root)
    assert result.passed is True
    assert result.status == "warn"
    assert _violations(result, "runtime_evidence_not_executable")


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
