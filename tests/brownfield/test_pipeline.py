from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from click.testing import CliRunner
import pytest

from codd.brownfield.pipeline import BrownfieldPipeline, BrownfieldResult, format_brownfield_result
from codd.cli import main
from codd.elicit.finding import ElicitResult, Finding


@dataclass
class ExtractStub:
    output_dir: Path
    generated_files: list[Path]


class RecordingDiffEngine:
    def __init__(self, findings: list[Finding] | None = None) -> None:
        self.findings = findings or []
        self.calls: list[dict[str, Any]] = []

    def run_diff(self, extract_input: Path, requirements_path: Path, ignored_findings=None) -> list[Finding]:
        self.calls.append(
            {
                "extract_input": extract_input,
                "requirements_path": requirements_path,
                "ignored_findings": ignored_findings,
            }
        )
        return self.findings


class RecordingElicitEngine:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def run(self, project_root: Path, lexicon_config=None) -> Any:
        self.calls.append({"project_root": project_root, "lexicon_config": lexicon_config})
        return self.result


def _finding(finding_id: str, *, source: str = "greenfield") -> Finding:
    return Finding.from_dict(
        {
            "id": finding_id,
            "kind": "gap",
            "severity": "medium",
            "name": f"Finding {finding_id}",
            "question": "What should be clarified?",
            "details": {"evidence": finding_id},
            "source": source,
            "rationale": "Coverage review found a gap.",
        }
    )


def _extract_runner(order: list[str] | None = None):
    def run(project_root: Path, output: str) -> ExtractStub:
        if order is not None:
            order.append("extract")
        output_dir = Path(output)
        generated = output_dir / "system-context.md"
        generated.parent.mkdir(parents=True, exist_ok=True)
        generated.write_text("# System\nFacts\n", encoding="utf-8")
        return ExtractStub(output_dir=output_dir, generated_files=[generated])

    return run


def test_pipeline_runs_extract_diff_elicit_in_order(tmp_path: Path) -> None:
    (tmp_path / ".codd").mkdir()
    (tmp_path / ".codd" / "requirements.md").write_text("# Requirements\n", encoding="utf-8")
    order: list[str] = []
    diff = RecordingDiffEngine([_finding("D-1", source="extract_brownfield")])
    elicit = RecordingElicitEngine([_finding("E-1")])

    pipeline = BrownfieldPipeline(
        extract_runner=_extract_runner(order),
        diff_engine_factory=lambda root: order.append("diff") or diff,
        elicit_engine_factory=lambda: order.append("elicit") or elicit,
    )

    result = pipeline.run(tmp_path)

    assert order == ["extract", "diff", "elicit"]
    assert [finding.id for finding in result.merged_findings] == ["D-1", "E-1"]


def test_pipeline_uses_hidden_extract_output(tmp_path: Path) -> None:
    captured: dict[str, str] = {}

    def run_extract(project_root: Path, output: str) -> ExtractStub:
        captured["output"] = output
        output_dir = Path(output)
        output_dir.mkdir(parents=True)
        return ExtractStub(output_dir=output_dir, generated_files=[])

    pipeline = BrownfieldPipeline(
        extract_runner=run_extract,
        diff_engine_factory=lambda root: RecordingDiffEngine(),
        elicit_engine_factory=lambda: RecordingElicitEngine([]),
    )

    result = pipeline.run(tmp_path)

    assert captured["output"] == str(tmp_path / ".codd" / "extract")
    assert result.extract_output == tmp_path / ".codd" / "extract"


def test_pipeline_creates_aggregate_extract_file_from_generated_docs(tmp_path: Path) -> None:
    def run_extract(project_root: Path, output: str) -> ExtractStub:
        output_dir = Path(output)
        first = output_dir / "system-context.md"
        second = output_dir / "modules" / "core.md"
        first.parent.mkdir(parents=True)
        second.parent.mkdir(parents=True)
        first.write_text("# System\nA\n", encoding="utf-8")
        second.write_text("# Core\nB\n", encoding="utf-8")
        return ExtractStub(output_dir=output_dir, generated_files=[second, first])

    pipeline = BrownfieldPipeline(
        extract_runner=run_extract,
        diff_engine_factory=lambda root: RecordingDiffEngine(),
        elicit_engine_factory=lambda: RecordingElicitEngine([]),
    )

    result = pipeline.run(tmp_path)
    text = result.extract_input.read_text(encoding="utf-8")

    assert result.extract_input == tmp_path / ".codd" / "extract" / "extracted.md"
    assert "<!-- source: modules/core.md -->" in text
    assert "<!-- source: system-context.md -->" in text


def test_pipeline_uses_default_requirements_when_present(tmp_path: Path) -> None:
    (tmp_path / ".codd").mkdir()
    requirements = tmp_path / ".codd" / "requirements.md"
    requirements.write_text("# Requirements\n", encoding="utf-8")
    diff = RecordingDiffEngine()

    pipeline = BrownfieldPipeline(
        extract_runner=_extract_runner(),
        diff_engine_factory=lambda root: diff,
        elicit_engine_factory=lambda: RecordingElicitEngine([]),
    )

    result = pipeline.run(tmp_path)

    assert result.requirements_path == requirements
    assert diff.calls[0]["requirements_path"] == requirements


def test_pipeline_skips_diff_when_default_requirements_missing(tmp_path: Path) -> None:
    diff = RecordingDiffEngine([_finding("D-1", source="extract_brownfield")])

    pipeline = BrownfieldPipeline(
        extract_runner=_extract_runner(),
        diff_engine_factory=lambda root: diff,
        elicit_engine_factory=lambda: RecordingElicitEngine([]),
    )

    result = pipeline.run(tmp_path)

    assert result.diff_findings == []
    assert diff.calls == []


def test_pipeline_raises_for_explicit_missing_requirements(tmp_path: Path) -> None:
    pipeline = BrownfieldPipeline(
        extract_runner=_extract_runner(),
        diff_engine_factory=lambda root: RecordingDiffEngine(),
        elicit_engine_factory=lambda: RecordingElicitEngine([]),
    )

    with pytest.raises(FileNotFoundError, match="requirements"):
        pipeline.run(tmp_path, requirements_path="docs/missing.md")


def test_pipeline_resolves_explicit_relative_requirements(tmp_path: Path) -> None:
    requirements = tmp_path / "docs" / "requirements.md"
    requirements.parent.mkdir()
    requirements.write_text("# Requirements\n", encoding="utf-8")
    diff = RecordingDiffEngine()

    pipeline = BrownfieldPipeline(
        extract_runner=_extract_runner(),
        diff_engine_factory=lambda root: diff,
        elicit_engine_factory=lambda: RecordingElicitEngine([]),
    )

    result = pipeline.run(tmp_path, requirements_path=Path("docs/requirements.md"))

    assert result.requirements_path == requirements
    assert diff.calls[0]["requirements_path"] == requirements


def test_pipeline_loads_default_lexicon_when_present(tmp_path: Path) -> None:
    lexicon_dir = tmp_path / ".codd" / "lexicon"
    lexicon_dir.mkdir(parents=True)
    loaded = object()
    captured: dict[str, Path] = {}
    elicit = RecordingElicitEngine([])

    def load_lexicon(path: Path) -> object:
        captured["path"] = path
        return loaded

    pipeline = BrownfieldPipeline(
        extract_runner=_extract_runner(),
        diff_engine_factory=lambda root: RecordingDiffEngine(),
        elicit_engine_factory=lambda: elicit,
        lexicon_loader=load_lexicon,
    )

    result = pipeline.run(tmp_path)

    assert result.lexicon_path == lexicon_dir
    assert captured["path"] == lexicon_dir
    assert elicit.calls[0]["lexicon_config"] is loaded


def test_pipeline_uses_discovery_mode_when_default_lexicon_missing(tmp_path: Path) -> None:
    elicit = RecordingElicitEngine([])

    pipeline = BrownfieldPipeline(
        extract_runner=_extract_runner(),
        diff_engine_factory=lambda root: RecordingDiffEngine(),
        elicit_engine_factory=lambda: elicit,
    )

    result = pipeline.run(tmp_path)

    assert result.lexicon_path is None
    assert elicit.calls[0]["lexicon_config"] is None


def test_pipeline_raises_for_explicit_missing_lexicon(tmp_path: Path) -> None:
    pipeline = BrownfieldPipeline(
        extract_runner=_extract_runner(),
        diff_engine_factory=lambda root: RecordingDiffEngine(),
        elicit_engine_factory=lambda: RecordingElicitEngine([]),
    )

    with pytest.raises(FileNotFoundError, match="lexicon"):
        pipeline.run(tmp_path, lexicon_path="missing-lexicon")


def test_pipeline_dedupes_merged_findings_by_id_with_diff_first(tmp_path: Path) -> None:
    (tmp_path / ".codd").mkdir()
    (tmp_path / ".codd" / "requirements.md").write_text("# Requirements\n", encoding="utf-8")
    diff_finding = _finding("SHARED", source="extract_brownfield")
    elicit_finding = _finding("SHARED")

    pipeline = BrownfieldPipeline(
        extract_runner=_extract_runner(),
        diff_engine_factory=lambda root: RecordingDiffEngine([diff_finding]),
        elicit_engine_factory=lambda: RecordingElicitEngine([elicit_finding, _finding("E-2")]),
    )

    result = pipeline.run(tmp_path)

    assert result.merged_findings == [diff_finding, _finding("E-2")]


def test_pipeline_accepts_elicit_result_object(tmp_path: Path) -> None:
    elicit_result = ElicitResult(
        findings=[_finding("E-1")],
        all_covered=False,
        lexicon_coverage_report={"auth": "gap"},
    )

    pipeline = BrownfieldPipeline(
        extract_runner=_extract_runner(),
        diff_engine_factory=lambda root: RecordingDiffEngine(),
        elicit_engine_factory=lambda: RecordingElicitEngine(elicit_result),
    )

    result = pipeline.run(tmp_path)

    assert result.elicit_findings == [_finding("E-1")]
    assert result.elicit_result is elicit_result
    assert result.to_dict()["elicit"]["lexicon_coverage_report"] == {"auth": "gap"}


def test_pipeline_validates_target_exists(tmp_path: Path) -> None:
    pipeline = BrownfieldPipeline(
        extract_runner=_extract_runner(),
        diff_engine_factory=lambda root: RecordingDiffEngine(),
        elicit_engine_factory=lambda: RecordingElicitEngine([]),
    )

    with pytest.raises(FileNotFoundError, match="target path"):
        pipeline.run(tmp_path / "missing")


def test_result_to_dict_serializes_findings(tmp_path: Path) -> None:
    result = BrownfieldResult(
        extract_output=tmp_path / ".codd" / "extract",
        extract_input=tmp_path / ".codd" / "extract" / "extracted.md",
        diff_findings=[_finding("D-1", source="extract_brownfield")],
        elicit_findings=[_finding("E-1")],
        merged_findings=[_finding("D-1", source="extract_brownfield"), _finding("E-1")],
    )

    payload = result.to_dict()

    assert payload["summary"]["merged_findings"] == 2
    assert payload["merged_findings"][0]["id"] == "D-1"


def test_format_brownfield_result_outputs_json_and_markdown(tmp_path: Path) -> None:
    result = BrownfieldResult(
        extract_output=tmp_path / ".codd" / "extract",
        diff_findings=[],
        elicit_findings=[_finding("E-1")],
        merged_findings=[_finding("E-1")],
    )

    assert json.loads(format_brownfield_result(result, "json"))["summary"]["merged_findings"] == 1
    assert "# Brownfield Report" in format_brownfield_result(result, "md")


def test_cli_brownfield_help_works() -> None:
    result = CliRunner().invoke(main, ["brownfield", "--help"])

    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output
    assert "--requirements" in result.output


def test_cli_brownfield_writes_integrated_report(tmp_path: Path, monkeypatch) -> None:
    class FakePipeline:
        def __init__(self, ai_command=None) -> None:
            self.ai_command = ai_command

        def run(self, target_path: Path, requirements_path=None, lexicon_path=None) -> BrownfieldResult:
            return BrownfieldResult(
                extract_output=target_path / ".codd" / "extract",
                diff_findings=[],
                elicit_findings=[_finding("E-1")],
                merged_findings=[_finding("E-1")],
                requirements_path=requirements_path,
                lexicon_path=lexicon_path,
            )

    monkeypatch.setattr("codd.brownfield.pipeline.BrownfieldPipeline", FakePipeline)

    result = CliRunner().invoke(main, ["brownfield", str(tmp_path), "--format", "json"])

    assert result.exit_code == 0, result.output
    report = tmp_path / ".codd" / "brownfield_report.json"
    assert json.loads(report.read_text(encoding="utf-8"))["merged_findings"][0]["id"] == "E-1"
    assert "Brownfield pipeline complete" in result.output
