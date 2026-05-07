from __future__ import annotations

from pathlib import Path

import yaml

from codd.elicit.apply import ElicitApplyEngine, load_findings_from_file
from codd.elicit.finding import Finding
from codd.elicit.formatters.md import MdFormatter


def _finding(finding_id: str, *, name: str | None = None) -> Finding:
    return Finding(
        id=finding_id,
        kind="coverage_gap",
        severity="medium",
        name=name or f"Finding {finding_id}",
        question=f"Should {finding_id} be tracked?",
        related_requirement_ids=["REQ-1"],
        rationale=f"{finding_id} is not addressed yet.",
    )


def _review_file(path: Path, states: dict[str, str]) -> Path:
    findings = [_finding(finding_id) for finding_id in states]
    text = MdFormatter().format(findings)
    for finding_id, state in states.items():
        text = text.replace(f"- approval: [ ] `{finding_id}`", f"- approval: [{state}] `{finding_id}`")
    path.write_text(text, encoding="utf-8")
    return path


def _read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_apply_approve_checkbox_appends_requirements_and_removes_pending(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.md"
    requirements.write_text("# Requirements\n", encoding="utf-8")
    pending_path = tmp_path / ".codd" / "elicit" / "pending_findings.yaml"
    pending_path.parent.mkdir(parents=True)
    pending_path.write_text(
        yaml.safe_dump({"pending": [{"finding": _finding("F-1").to_dict()}]}, sort_keys=False),
        encoding="utf-8",
    )
    findings = load_findings_from_file(_review_file(tmp_path / "findings.md", {"F-1": "x"}))

    result = ElicitApplyEngine(tmp_path).apply(findings)

    requirements_text = requirements.read_text(encoding="utf-8")
    pending = _read_yaml(tmp_path / ".codd" / "elicit" / "pending_findings.yaml")
    ignored = _read_yaml(tmp_path / ".codd" / "elicit" / "ignored_findings.yaml")

    assert result.applied_count == 1
    assert "requirements.md" in result.files_updated
    assert "TODO [F-1]" in requirements_text
    assert "Should F-1 be tracked?" in requirements_text
    assert pending["pending"] == []
    assert ignored["ignored"] == []


def test_apply_reject_checkbox_records_ignored_without_changing_requirements(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.md"
    requirements.write_text("# Requirements\n", encoding="utf-8")
    findings = load_findings_from_file(_review_file(tmp_path / "findings.md", {"F-2": "r"}))

    result = ElicitApplyEngine(tmp_path).apply(findings)

    ignored = _read_yaml(tmp_path / ".codd" / "elicit" / "ignored_findings.yaml")
    pending = _read_yaml(tmp_path / ".codd" / "elicit" / "pending_findings.yaml")

    assert result.applied_count == 0
    assert requirements.read_text(encoding="utf-8") == "# Requirements\n"
    assert ignored["ignored"][0]["id"] == "F-2"
    assert pending["pending"] == []


def test_apply_pending_checkbox_keeps_pending_without_requirements_or_ignored(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.md"
    requirements.write_text("# Requirements\n", encoding="utf-8")
    findings = load_findings_from_file(_review_file(tmp_path / "findings.md", {"F-3": " "}))

    ElicitApplyEngine(tmp_path).apply(findings)

    pending = _read_yaml(tmp_path / ".codd" / "elicit" / "pending_findings.yaml")
    ignored = _read_yaml(tmp_path / ".codd" / "elicit" / "ignored_findings.yaml")

    assert requirements.read_text(encoding="utf-8") == "# Requirements\n"
    assert pending["pending"][0]["finding"]["id"] == "F-3"
    assert ignored["ignored"] == []


def test_apply_mixed_checkboxes_routes_approved_and_rejected(tmp_path: Path) -> None:
    requirements = tmp_path / "requirements.md"
    requirements.write_text("# Requirements\n", encoding="utf-8")
    states = {
        "F-A1": "x",
        "F-A2": "x",
        "F-A3": "x",
        "F-R1": "r",
        "F-R2": "r",
        "F-R3": "r",
    }
    findings = load_findings_from_file(_review_file(tmp_path / "findings.md", states))

    result = ElicitApplyEngine(tmp_path).apply(findings)

    requirements_text = requirements.read_text(encoding="utf-8")
    pending = _read_yaml(tmp_path / ".codd" / "elicit" / "pending_findings.yaml")
    ignored = _read_yaml(tmp_path / ".codd" / "elicit" / "ignored_findings.yaml")

    assert result.applied_count == 3
    assert [entry["id"] for entry in ignored["ignored"]] == ["F-R1", "F-R2", "F-R3"]
    assert pending["pending"] == []
    assert requirements_text.count("TODO [F-A") == 3
    assert "F-R1" not in requirements_text
