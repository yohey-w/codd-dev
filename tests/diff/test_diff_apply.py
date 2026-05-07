from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from codd.cli import main
from codd.diff.apply import DiffApplyEngine
from codd.elicit.finding import Finding
from codd.elicit.formatters.json_fmt import JsonFormatter


def _finding(finding_id: str = "DIFF-1", **overrides) -> Finding:
    payload = {
        "id": finding_id,
        "kind": "comparison_gap",
        "severity": "medium",
        "name": "Review this behavior",
        "question": "Should this be recorded?",
        "details": {"category": "unknown"},
        "related_requirement_ids": ["REQ-1"],
        "rationale": "The compared artifacts do not line up.",
    }
    payload.update(overrides)
    return Finding(**payload)


def test_apply_returns_empty_result_for_empty_input(tmp_path: Path) -> None:
    result = DiffApplyEngine(tmp_path).apply([])

    assert result.applied_count == 0
    assert result.skipped_count == 0
    assert result.files_updated == []


def test_apply_routes_implementation_only_category_to_requirements(tmp_path: Path) -> None:
    finding = _finding("DIFF-IMPL-1", details={"category": "implementation_only"})

    result = DiffApplyEngine(tmp_path).apply([finding])

    requirements = tmp_path / "docs" / "requirements" / "requirements.md"
    text = requirements.read_text(encoding="utf-8")
    assert result.applied_count == 1
    assert "docs/requirements/requirements.md" in result.files_updated
    assert "暗黙要件確認" in text
    assert "[DIFF-IMPL-1] Review this behavior" in text
    assert "Question: Should this be recorded?" in text


def test_apply_routes_requirement_only_category_to_plan(tmp_path: Path) -> None:
    finding = _finding("DIFF-REQ-1", details={"category": "requirement_only"})

    result = DiffApplyEngine(tmp_path).apply([finding])

    plan = tmp_path / "impl_plan.md"
    text = plan.read_text(encoding="utf-8")
    assert result.files_updated == ["impl_plan.md"]
    assert "Gap Resolution Candidates" in text
    assert "- [ ] [DIFF-REQ-1] Review this behavior" in text


def test_apply_uses_existing_implementation_plan_when_present(tmp_path: Path) -> None:
    plan = tmp_path / "docs" / "plan" / "implementation_plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# Plan\n", encoding="utf-8")
    finding = _finding("DIFF-REQ-2", details={"category": "requirement_only"})

    result = DiffApplyEngine(tmp_path).apply([finding])

    assert "docs/plan/implementation_plan.md" in result.files_updated
    assert "[DIFF-REQ-2]" in plan.read_text(encoding="utf-8")


def test_apply_routes_drift_category_to_resolution_file(tmp_path: Path) -> None:
    finding = _finding(
        "DIFF-DELTA-1",
        details={
            "category": "drift",
            "evidence_extracted": "Actual value is 14",
            "evidence_requirements": "Required value is 16",
            "discrepancy": "14 vs 16",
        },
    )

    result = DiffApplyEngine(tmp_path).apply([finding])

    output = tmp_path / "drift_resolutions.md"
    text = output.read_text(encoding="utf-8")
    assert result.files_updated == ["drift_resolutions.md"]
    assert "# Resolution Candidates" in text
    assert "evidence extracted: Actual value is 14" in text
    assert "discrepancy: 14 vs 16" in text


def test_apply_falls_back_for_unknown_category(tmp_path: Path) -> None:
    finding = _finding("DIFF-OTHER-1", details={"category": "novel_category"})

    result = DiffApplyEngine(tmp_path).apply([finding])

    fallback = tmp_path / "drift_findings.md"
    text = fallback.read_text(encoding="utf-8")
    assert result.files_updated == ["drift_findings.md"]
    assert "<!-- codd:finding" in text
    assert "## DIFF-OTHER-1 - Review this behavior" in text


def test_apply_skips_duplicate_finding_ids(tmp_path: Path) -> None:
    engine = DiffApplyEngine(tmp_path)
    engine.apply([_finding("DIFF-IMPL-1", details={"category": "implementation_only"})])

    result = engine.apply([_finding("DIFF-IMPL-1", details={"category": "implementation_only"})])

    requirements = tmp_path / "docs" / "requirements" / "requirements.md"
    assert result.applied_count == 0
    assert result.skipped_count == 1
    assert requirements.read_text(encoding="utf-8").count("DIFF-IMPL-1") == 1


def test_cli_diff_apply_json_input(tmp_path: Path) -> None:
    input_file = tmp_path / "findings.json"
    input_file.write_text(
        JsonFormatter().format([_finding("DIFF-REQ-1", details={"category": "requirement_only"})]),
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["diff", "apply", str(input_file), "--path", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Diff apply complete: applied=1, skipped=0" in result.output
    assert (tmp_path / "impl_plan.md").exists()


def test_cli_diff_apply_invalid_input_exits_nonzero(tmp_path: Path) -> None:
    input_file = tmp_path / "findings.json"
    input_file.write_text(json.dumps({"id": "DIFF-1"}), encoding="utf-8")

    result = CliRunner().invoke(main, ["diff", "apply", str(input_file), "--path", str(tmp_path)])

    assert result.exit_code == 1
    assert "Finding JSON input must be an array" in result.output
