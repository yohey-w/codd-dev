from __future__ import annotations

import json

import pytest

from codd.elicit.apply import load_findings_from_file
from codd.elicit.finding import Finding
from codd.elicit.formatters.interactive import InteractiveFormatter
from codd.elicit.formatters.json_fmt import JsonFormatter
from codd.elicit.formatters.md import MdFormatter


def _finding(finding_id: str = "F-1", **overrides) -> Finding:
    payload = {
        "id": finding_id,
        "kind": "coverage_gap",
        "severity": "high",
        "name": "Missing coverage",
        "question": "Should this be covered?",
        "details": {"decision": "approve", "confidence": 0.91},
        "related_requirement_ids": ["REQ-1"],
        "rationale": "The source material leaves this open.",
    }
    payload.update(overrides)
    return Finding(**payload)


def test_md_formatter_outputs_reviewable_sections() -> None:
    text = MdFormatter().format([_finding()])

    assert text.startswith("# Findings")
    assert "## F-1 - Missing coverage" in text
    assert "- id: `F-1`" in text
    assert "- kind: `coverage_gap`" in text
    assert "- severity: `high`" in text
    assert "- question: Should this be covered?" in text
    assert "confidence: 0.91" in text


def test_md_formatter_outputs_empty_state() -> None:
    assert MdFormatter().format([]) == "# Findings\n\nNo findings.\n"


def test_md_formatter_embeds_roundtrip_metadata(tmp_path) -> None:
    path = tmp_path / "findings.md"
    path.write_text(MdFormatter().format([_finding()]), encoding="utf-8")

    findings = load_findings_from_file(path)

    assert findings == [_finding()]


def test_md_formatter_parse_checked_approval_lines() -> None:
    raw = "- approval: [x] `F-1`\n- approval: [ ] `F-2`\n- approval: [X] F-3\n"

    assert MdFormatter().parse_approval(raw) == ["F-1", "F-3"]


def test_md_formatter_parse_json_approval_array() -> None:
    assert MdFormatter().parse_approval('["F-1", "F-2"]') == ["F-1", "F-2"]


def test_json_formatter_outputs_finding_array() -> None:
    text = JsonFormatter().format([_finding()])
    payload = json.loads(text)

    assert payload == [_finding().to_dict()]


def test_json_formatter_parse_approval_array() -> None:
    assert JsonFormatter().parse_approval('["F-1", "F-2"]') == ["F-1", "F-2"]


def test_json_formatter_rejects_non_array_approval() -> None:
    with pytest.raises(ValueError, match="JSON array"):
        JsonFormatter().parse_approval('{"approved": ["F-1"]}')


def test_json_formatter_rejects_non_string_ids() -> None:
    with pytest.raises(ValueError, match="finding ID strings"):
        JsonFormatter().parse_approval('["F-1", 2]')


def test_interactive_formatter_outputs_inline_prompt() -> None:
    text = InteractiveFormatter().format([_finding()])

    assert "[1/1] F-1" in text
    assert "kind: coverage_gap" in text
    assert "Approve? [Y/n/d]" in text


def test_interactive_formatter_parse_sequential_responses() -> None:
    formatter = InteractiveFormatter()
    formatter.format([_finding("F-1"), _finding("F-2"), _finding("F-3")])

    assert formatter.parse_approval("Y\nn\nd\n") == ["F-1"]


def test_interactive_formatter_parse_keyed_responses() -> None:
    raw = "F-1: yes\nF-2: no\nF-3: defer\nF-4: approve\n"

    assert InteractiveFormatter().parse_approval(raw) == ["F-1", "F-4"]


def test_interactive_formatter_collects_mocked_input() -> None:
    formatter = InteractiveFormatter()
    answers = iter(["", "n", "d", "yes"])
    prompts: list[str] = []

    approved = formatter.collect_approvals(
        [_finding("F-1"), _finding("F-2"), _finding("F-3"), _finding("F-4")],
        input_func=lambda prompt: next(answers),
        output_func=prompts.append,
    )

    assert approved == ["F-1", "F-4"]
    assert len(prompts) == 4


def test_finding_from_dict_defaults_optional_fields() -> None:
    finding = Finding.from_dict({"id": "F-1", "kind": "gap", "severity": "medium"})

    assert finding.name is None
    assert finding.details == {}
    assert finding.source == "greenfield"


def test_finding_from_dict_rejects_invalid_severity() -> None:
    with pytest.raises(ValueError, match="severity"):
        Finding.from_dict({"id": "F-1", "kind": "gap", "severity": "urgent"})
