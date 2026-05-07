"""Coverage-mode tests for ElicitResult + ElicitEngine + formatters."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codd.elicit.engine import ElicitEngine
from codd.elicit.finding import ElicitResult, Finding
from codd.elicit.formatters.json_fmt import JsonFormatter
from codd.elicit.formatters.md import MdFormatter


def _finding(**kwargs):
    base = {
        "id": "F-1",
        "kind": "spec_hole",
        "severity": "medium",
        "name": "missing acceptance",
        "details": {"dimension": "acceptance"},
    }
    base.update(kwargs)
    return Finding.from_dict(base)


# ----- ElicitResult.from_payload -----


def test_from_payload_legacy_array_returns_findings_only():
    payload = [
        {"id": "F-1", "kind": "spec_hole", "severity": "medium"},
    ]
    result = ElicitResult.from_payload(payload)
    assert len(result.findings) == 1
    assert result.all_covered is False
    assert result.lexicon_coverage_report == {}


def test_from_payload_object_with_coverage_report():
    payload = {
        "all_covered": False,
        "lexicon_coverage_report": {"stakeholder": "covered", "goal": "gap"},
        "findings": [
            {"id": "F-1", "kind": "spec_hole", "severity": "high"},
        ],
    }
    result = ElicitResult.from_payload(payload)
    assert result.all_covered is False
    assert result.lexicon_coverage_report == {"stakeholder": "covered", "goal": "gap"}
    assert len(result.findings) == 1


def test_from_payload_all_covered_empty_findings():
    payload = {
        "all_covered": True,
        "lexicon_coverage_report": {"stakeholder": "covered", "goal": "implicit"},
        "findings": [],
    }
    result = ElicitResult.from_payload(payload)
    assert result.all_covered is True
    assert result.findings == []


def test_from_payload_all_covered_forced_false_when_findings_exist():
    payload = {
        "all_covered": True,
        "lexicon_coverage_report": {"goal": "gap"},
        "findings": [{"id": "F-1", "kind": "spec_hole", "severity": "medium"}],
    }
    result = ElicitResult.from_payload(payload)
    assert result.all_covered is False


def test_from_payload_invalid_type_raises():
    with pytest.raises(ValueError):
        ElicitResult.from_payload("invalid")


# ----- ElicitEngine.deserialize_result -----


def test_engine_deserialize_object_payload():
    engine = ElicitEngine(ai_command="noop")
    raw = json.dumps(
        {
            "all_covered": True,
            "lexicon_coverage_report": {"stakeholder": "covered"},
            "findings": [],
        }
    )
    result = engine.deserialize_result(raw)
    assert isinstance(result, ElicitResult)
    assert result.all_covered is True
    assert result.lexicon_coverage_report == {"stakeholder": "covered"}


def test_engine_deserialize_legacy_array_payload():
    engine = ElicitEngine(ai_command="noop")
    raw = json.dumps([{"id": "F-1", "kind": "spec_hole", "severity": "low"}])
    with pytest.raises(ValueError):
        engine.deserialize_result(raw)


def test_engine_deserialize_legacy_array_payload_valid():
    engine = ElicitEngine(ai_command="noop")
    raw = json.dumps([{"id": "F-1", "kind": "spec_hole", "severity": "info"}])
    result = engine.deserialize_result(raw)
    assert len(result.findings) == 1
    assert result.all_covered is False


def test_engine_run_promotes_all_covered_when_no_gap(tmp_path: Path, monkeypatch):
    engine = ElicitEngine(ai_command="noop")
    payload = {
        "all_covered": False,
        "lexicon_coverage_report": {"stakeholder": "covered", "goal": "implicit"},
        "findings": [],
    }
    monkeypatch.setattr(engine, "build_prompt", lambda root, lexicon_config=None: "prompt")
    monkeypatch.setattr(engine, "invoke", lambda prompt, root: json.dumps(payload))
    result = engine.run(tmp_path)
    assert result.all_covered is True


def test_engine_run_keeps_all_covered_false_when_gap_present(tmp_path: Path, monkeypatch):
    engine = ElicitEngine(ai_command="noop")
    payload = {
        "all_covered": False,
        "lexicon_coverage_report": {"goal": "gap", "stakeholder": "covered"},
        "findings": [],
    }
    monkeypatch.setattr(engine, "build_prompt", lambda root, lexicon_config=None: "prompt")
    monkeypatch.setattr(engine, "invoke", lambda prompt, root: json.dumps(payload))
    result = engine.run(tmp_path)
    assert result.all_covered is False


# ----- formatter behaviour -----


def test_md_formatter_displays_all_covered_message():
    result = ElicitResult(
        all_covered=True,
        lexicon_coverage_report={"stakeholder": "covered", "goal": "implicit"},
        findings=[],
    )
    output = MdFormatter().format(result)
    assert "All lexicon categories covered" in output
    assert "Lexicon coverage" in output
    assert "stakeholder" in output


def test_md_formatter_renders_coverage_report_with_gap_findings():
    result = ElicitResult(
        all_covered=False,
        lexicon_coverage_report={"goal": "gap", "stakeholder": "covered"},
        findings=[_finding()],
    )
    output = MdFormatter().format(result)
    assert "Lexicon coverage" in output
    assert "F-1" in output
    assert "**gap**" in output
    assert "**covered**" in output


def test_md_formatter_handles_legacy_findings_list():
    output = MdFormatter().format([_finding()])
    assert "F-1" in output


def test_json_formatter_dumps_full_result_payload():
    result = ElicitResult(
        all_covered=False,
        lexicon_coverage_report={"goal": "gap"},
        findings=[_finding()],
    )
    output = json.loads(JsonFormatter().format(result))
    assert output["all_covered"] is False
    assert output["lexicon_coverage_report"] == {"goal": "gap"}
    assert len(output["findings"]) == 1


def test_json_formatter_legacy_list_payload_remains_array():
    output = json.loads(JsonFormatter().format([_finding()]))
    assert isinstance(output, list)
    assert output[0]["id"] == "F-1"
