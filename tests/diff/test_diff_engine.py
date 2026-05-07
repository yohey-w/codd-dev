from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import pytest

from codd.diff import DiffEngine
from codd.elicit.finding import Finding


@dataclass
class LexiconConfigStub:
    lexicon_name: str = "sample"
    recommended_kinds: list[str] | None = None


class FakeLlm:
    def __init__(self, output: str):
        self.output = output
        self.prompts: list[str] = []

    def invoke(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.output


class FakeCompleteLlm:
    def __init__(self, output: str):
        self.output = output
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.output


def _finding_payload(
    finding_id: str,
    *,
    kind: str = "implementation_only",
    severity: str = "medium",
    category: str | None = None,
) -> dict:
    return {
        "id": finding_id,
        "kind": kind,
        "severity": severity,
        "name": f"Finding {finding_id}",
        "question": "Should this be reconciled?",
        "details": {
            "category": category or kind,
            "evidence_extracted": "extracted evidence",
            "evidence_requirements": "requirements evidence",
            "discrepancy": "mismatch",
        },
        "related_requirement_ids": ["REQ-1"],
        "rationale": "The two sources disagree.",
    }


def _three_category_payloads() -> list[dict]:
    return [
        _finding_payload("DIFF-1", kind="implementation_only"),
        _finding_payload("DIFF-2", kind="requirement_only"),
        _finding_payload("DIFF-3", kind="drift"),
    ]


def _write_inputs(project_root: Path) -> tuple[Path, Path]:
    extracted = project_root / "codd" / "extracted.md"
    requirements = project_root / "docs" / "requirements" / "requirements.md"
    extracted.parent.mkdir(parents=True)
    requirements.parent.mkdir(parents=True)
    extracted.write_text("# Extracted\nRuntime behavior A\n", encoding="utf-8")
    requirements.write_text("# Requirements\nREQ-1 behavior B\n", encoding="utf-8")
    return extracted, requirements


def test_engine_initializes_with_project_root(tmp_path: Path) -> None:
    engine = DiffEngine(None, tmp_path)

    assert engine.project_root == tmp_path
    assert engine.template_path.name == "diff_prompt.md"


def test_package_exports_shared_finding_model() -> None:
    from codd.diff import Finding as ExportedFinding
    from codd.diff import Severity

    assert ExportedFinding is Finding
    assert Severity is not None


def test_build_prompt_injects_inputs_and_ignored_ids(tmp_path: Path) -> None:
    extracted, requirements = _write_inputs(tmp_path)

    prompt = DiffEngine(None, tmp_path).build_prompt(
        extracted,
        requirements,
        ignored_findings=["DIFF-OLD"],
    )

    assert "Runtime behavior A" in prompt
    assert "REQ-1 behavior B" in prompt
    assert "DIFF-OLD" in prompt
    assert "{{extracted_content}}" not in prompt


def test_build_prompt_accepts_ignored_id_sets(tmp_path: Path) -> None:
    extracted, requirements = _write_inputs(tmp_path)

    prompt = DiffEngine(None, tmp_path).build_prompt(
        extracted,
        requirements,
        ignored_findings={"DIFF-OLD"},
    )

    assert "DIFF-OLD" in prompt


def test_build_prompt_accepts_relative_paths(tmp_path: Path) -> None:
    _write_inputs(tmp_path)

    prompt = DiffEngine(None, tmp_path).build_prompt(
        Path("codd/extracted.md"),
        Path("docs/requirements/requirements.md"),
    )

    assert "Runtime behavior A" in prompt
    assert "REQ-1 behavior B" in prompt


def test_build_prompt_loads_project_lexicon_file(tmp_path: Path) -> None:
    extracted, requirements = _write_inputs(tmp_path)
    (tmp_path / "project_lexicon.yaml").write_text("terms:\n  - sample\n", encoding="utf-8")

    prompt = DiffEngine(None, tmp_path).build_prompt(extracted, requirements)

    assert "terms:" in prompt
    assert "sample" in prompt


def test_build_prompt_uses_callable_lexicon_loader(tmp_path: Path) -> None:
    extracted, requirements = _write_inputs(tmp_path)

    prompt = DiffEngine(
        None,
        tmp_path,
        lexicon_loader=lambda root: f"lexicon for {root.name}",
    ).build_prompt(extracted, requirements)

    assert f"lexicon for {tmp_path.name}" in prompt


def test_build_prompt_formats_loaded_lexicon_config(tmp_path: Path) -> None:
    extracted, requirements = _write_inputs(tmp_path)
    loader = LexiconConfigStub(recommended_kinds=["implicit_contract"])

    prompt = DiffEngine(None, tmp_path, lexicon_loader=loader).build_prompt(extracted, requirements)

    assert "loaded_lexicon: sample" in prompt
    assert "implicit_contract" in prompt


def test_context_size_is_limited(tmp_path: Path) -> None:
    extracted, requirements = _write_inputs(tmp_path)
    extracted.write_text("A" * 200, encoding="utf-8")

    prompt = DiffEngine(None, tmp_path, max_context_chars=40).build_prompt(extracted, requirements)

    assert "A" * 80 not in prompt


def test_run_diff_invokes_llm_and_returns_findings(tmp_path: Path) -> None:
    extracted, requirements = _write_inputs(tmp_path)
    llm = FakeLlm(json.dumps([_finding_payload("DIFF-1")]))

    findings = DiffEngine(llm, tmp_path).run_diff(extracted, requirements)

    assert [finding.id for finding in findings] == ["DIFF-1"]
    assert "Runtime behavior A" in llm.prompts[0]


def test_run_diff_accepts_callable_llm(tmp_path: Path) -> None:
    extracted, requirements = _write_inputs(tmp_path)

    findings = DiffEngine(
        lambda prompt: json.dumps([_finding_payload("DIFF-1")]),
        tmp_path,
    ).run_diff(extracted, requirements)

    assert findings[0].id == "DIFF-1"


def test_run_diff_accepts_complete_llm(tmp_path: Path) -> None:
    extracted, requirements = _write_inputs(tmp_path)
    llm = FakeCompleteLlm(json.dumps([_finding_payload("DIFF-1")]))

    findings = DiffEngine(llm, tmp_path).run_diff(extracted, requirements)

    assert findings[0].id == "DIFF-1"
    assert llm.prompts


def test_deserialize_accepts_three_categories() -> None:
    findings = DiffEngine(None, Path(".")).deserialize(json.dumps(_three_category_payloads()))

    assert [finding.details["category"] for finding in findings] == [
        "implementation_only",
        "requirement_only",
        "drift",
    ]


def test_deserialize_accepts_fenced_json_array() -> None:
    raw = "```json\n" + json.dumps([_finding_payload("DIFF-1")]) + "\n```"

    assert DiffEngine(None, Path(".")).deserialize(raw)[0].id == "DIFF-1"


def test_deserialize_extracts_array_from_surrounding_text() -> None:
    raw = "Result:\n" + json.dumps([_finding_payload("DIFF-1")]) + "\nDone"

    assert DiffEngine(None, Path(".")).deserialize(raw)[0].id == "DIFF-1"


def test_deserialize_defaults_source_to_extract_brownfield() -> None:
    finding = DiffEngine(None, Path(".")).deserialize(json.dumps([_finding_payload("DIFF-1")]))[0]

    assert finding.source == "extract_brownfield"


def test_deserialize_rejects_non_array() -> None:
    with pytest.raises(ValueError, match="JSON array"):
        DiffEngine(None, Path(".")).deserialize(json.dumps({"id": "DIFF-1"}))


def test_deserialize_rejects_missing_array() -> None:
    with pytest.raises(ValueError, match="JSON array"):
        DiffEngine(None, Path(".")).deserialize("no structured output")


def test_deserialize_rejects_non_mapping_entries() -> None:
    with pytest.raises(ValueError, match="mappings"):
        DiffEngine(None, Path(".")).deserialize(json.dumps(["DIFF-1"]))


def test_ignored_findings_are_filtered_after_llm_output(tmp_path: Path) -> None:
    extracted, requirements = _write_inputs(tmp_path)
    llm = FakeLlm(json.dumps([_finding_payload("DIFF-1"), _finding_payload("DIFF-2")]))

    findings = DiffEngine(llm, tmp_path).run_diff(
        extracted,
        requirements,
        ignored_findings=["DIFF-1"],
    )

    assert [finding.id for finding in findings] == ["DIFF-2"]
    assert "DIFF-1" in llm.prompts[0]
