from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
import yaml

from codd.cli import main
from codd.elicit.apply import ElicitApplyEngine, load_findings_from_file
from codd.elicit.finding import Finding
from codd.elicit.formatters.json_fmt import JsonFormatter
from codd.elicit.formatters.md import MdFormatter


def _finding(finding_id: str = "F-1", **overrides) -> Finding:
    payload = {
        "id": finding_id,
        "kind": "coverage_gap",
        "severity": "medium",
        "name": "Review this gap",
        "question": "Should this be tracked?",
        "details": {},
        "related_requirement_ids": ["REQ-1"],
        "rationale": "It is not addressed yet.",
    }
    payload.update(overrides)
    return Finding(**payload)


def _read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_apply_writes_generic_state_files(tmp_path: Path) -> None:
    result = ElicitApplyEngine(tmp_path).apply([_finding()])

    assert result.applied_count == 1
    assert result.skipped_count == 0
    assert ".codd/elicit/ignored_findings.yaml" in result.files_updated
    assert ".codd/elicit/pending_findings.yaml" in result.files_updated
    assert ".codd/elicit/elicit_history.yaml" in result.files_updated
    assert "findings.md" in result.files_updated


def test_apply_records_pending_findings(tmp_path: Path) -> None:
    ElicitApplyEngine(tmp_path).apply([_finding()])

    pending = _read_yaml(tmp_path / ".codd" / "elicit" / "pending_findings.yaml")

    assert pending["pending"][0]["finding"]["id"] == "F-1"
    assert pending["pending"][0]["finding"]["kind"] == "coverage_gap"
    assert pending["pending"][0]["last_review_at"] is None


def test_apply_appends_history_record(tmp_path: Path) -> None:
    engine = ElicitApplyEngine(tmp_path)
    engine.apply([_finding("F-1")])
    engine.apply([_finding("F-2")])

    history = _read_yaml(tmp_path / ".codd" / "elicit" / "elicit_history.yaml")

    assert len(history["sessions"]) == 2
    assert history["sessions"][1]["approved"] == 1
    assert history["sessions"][1]["findings_total"] == 1


def test_apply_records_rejected_findings_as_ignored(tmp_path: Path) -> None:
    rejected = _finding("F-2", details={"decision": "rejected", "reason": "not relevant"})

    result = ElicitApplyEngine(tmp_path).apply([rejected])

    ignored = _read_yaml(tmp_path / ".codd" / "elicit" / "ignored_findings.yaml")
    assert result.applied_count == 0
    assert result.skipped_count == 1
    assert ignored["ignored"][0]["id"] == "F-2"
    assert ignored["ignored"][0]["reason"] == "not relevant"


def test_apply_skips_deferred_findings(tmp_path: Path) -> None:
    deferred = _finding("F-3", details={"decision": "deferred"})

    result = ElicitApplyEngine(tmp_path).apply([deferred])

    pending = _read_yaml(tmp_path / ".codd" / "elicit" / "pending_findings.yaml")
    history = _read_yaml(tmp_path / ".codd" / "elicit" / "elicit_history.yaml")
    assert result.applied_count == 0
    assert result.skipped_count == 1
    assert pending["pending"] == []
    assert history["sessions"][0]["deferred"] == 1


def test_apply_skips_duplicate_pending_ids(tmp_path: Path) -> None:
    engine = ElicitApplyEngine(tmp_path)
    engine.apply([_finding("F-1")])

    result = engine.apply([_finding("F-1")])

    pending = _read_yaml(tmp_path / ".codd" / "elicit" / "pending_findings.yaml")
    assert result.applied_count == 0
    assert result.skipped_count == 1
    assert len(pending["pending"]) == 1


def test_apply_writes_findings_markdown_for_approved_items(tmp_path: Path) -> None:
    ElicitApplyEngine(tmp_path).apply([_finding("F-1")])

    text = (tmp_path / "findings.md").read_text(encoding="utf-8")
    assert "## F-1 - Review this gap" in text
    assert "- kind: `coverage_gap`" in text


def test_load_findings_from_json_file(tmp_path: Path) -> None:
    path = tmp_path / "findings.json"
    path.write_text(JsonFormatter().format([_finding("F-1")]), encoding="utf-8")

    assert load_findings_from_file(path) == [_finding("F-1")]


def test_load_findings_from_markdown_file(tmp_path: Path) -> None:
    path = tmp_path / "findings.md"
    path.write_text(MdFormatter().format([_finding("F-1")]), encoding="utf-8")

    assert load_findings_from_file(path) == [_finding("F-1")]


def test_cli_elicit_apply_json_input(tmp_path: Path) -> None:
    input_file = tmp_path / "findings.json"
    input_file.write_text(JsonFormatter().format([_finding("F-1")]), encoding="utf-8")

    result = CliRunner().invoke(main, ["elicit", "apply", str(input_file), "--path", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Elicit apply complete: applied=1, skipped=0" in result.output
    assert (tmp_path / ".codd" / "elicit" / "pending_findings.yaml").exists()


def test_cli_elicit_apply_markdown_input_with_explicit_format(tmp_path: Path) -> None:
    input_file = tmp_path / "review.txt"
    input_file.write_text(MdFormatter().format([_finding("F-1")]), encoding="utf-8")

    result = CliRunner().invoke(
        main,
        ["elicit", "apply", str(input_file), "--format", "md", "--path", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert "Updated: findings.md" in result.output


def test_cli_elicit_apply_invalid_input_exits_nonzero(tmp_path: Path) -> None:
    input_file = tmp_path / "findings.json"
    input_file.write_text(json.dumps({"id": "F-1"}), encoding="utf-8")

    result = CliRunner().invoke(main, ["elicit", "apply", str(input_file), "--path", str(tmp_path)])

    assert result.exit_code == 1
    assert "Finding JSON input must be an array" in result.output
