from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
import yaml

from codd.cli import main
from codd.lexicon_cli.reporter import CoverageMatrixReport, CoverageRow
from codd.lexicon_cli.threshold import ThresholdConfig, evaluate, load_thresholds


REPO_ROOT = Path(__file__).parents[2]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons"


def _report(*rows: CoverageRow) -> CoverageMatrixReport:
    return CoverageMatrixReport(
        project_root="/tmp/project",
        generated_at="2026-05-08T00:00:00Z",
        mode="text-grep",
        rows=rows,
        totals={},
    )


def _row(lexicon_id: str, axis: str, status: str) -> CoverageRow:
    return CoverageRow(
        lexicon_id=lexicon_id,
        lexicon_name=lexicon_id,
        axis_type=axis,
        status=status,
        hit_count=1 if status != "unknown" else 0,
    )


def _write_yaml(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _first_lexicon_id() -> str:
    return sorted(path.name for path in LEXICON_ROOT.iterdir() if (path / "manifest.yaml").is_file())[0]


def _project(tmp_path: Path, lexicon_id: str) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    _write_yaml(
        project / "project_lexicon.yaml",
        {
            "node_vocabulary": [],
            "naming_conventions": [],
            "design_principles": [],
            "extends": [lexicon_id],
        },
    )
    (project / "requirements.md").write_text("no matching coverage terms", encoding="utf-8")
    return project


def test_threshold_default_no_enforcement_when_codd_yaml_absent(tmp_path: Path) -> None:
    config = load_thresholds(tmp_path / "missing.yaml")

    assert config == ThresholdConfig()


def test_threshold_load_default_pct_from_codd_yaml(tmp_path: Path) -> None:
    config_path = _write_yaml(
        tmp_path / "codd.yaml",
        {"coverage": {"thresholds": {"default": {"covered_text_match_pct": 25}}}},
    )

    assert load_thresholds(config_path).default_pct == 25.0


def test_threshold_per_lexicon_override(tmp_path: Path) -> None:
    config_path = _write_yaml(
        tmp_path / "codd.yaml",
        {"coverage": {"thresholds": {"per_lexicon": {"sample": {"covered_text_match_pct": 50}}}}},
    )

    assert load_thresholds(config_path).per_lexicon == {"sample": 50.0}


def test_threshold_per_axis_override(tmp_path: Path) -> None:
    config_path = _write_yaml(
        tmp_path / "codd.yaml",
        {
            "coverage": {
                "thresholds": {
                    "per_axis": {
                        "sample": {
                            "axis_one": {"covered_text_match_pct": 75},
                        }
                    }
                }
            }
        },
    )

    assert load_thresholds(config_path).per_axis == {"sample": {"axis_one": 75.0}}


def test_threshold_evaluate_no_violations_when_below_threshold_zero() -> None:
    violations = evaluate(_report(_row("sample", "axis_one", "unknown")), ThresholdConfig())

    assert violations == []


def test_threshold_evaluate_violations_when_pct_below_threshold() -> None:
    violations = evaluate(
        _report(_row("sample", "axis_one", "unknown")),
        ThresholdConfig(default_pct=1),
    )

    assert len(violations) == 1
    assert violations[0].axis is None
    assert violations[0].observed_pct == 0.0


def test_threshold_evaluate_per_lexicon_violations() -> None:
    violations = evaluate(
        _report(_row("sample", "axis_one", "covered_text_match"), _row("sample", "axis_two", "unknown")),
        ThresholdConfig(default_pct=0, per_lexicon={"sample": 100}),
    )

    assert len(violations) == 1
    assert violations[0].lexicon_id == "sample"
    assert violations[0].observed_pct == 50.0


def test_threshold_evaluate_per_axis_violations() -> None:
    violations = evaluate(
        _report(_row("sample", "axis_one", "unknown")),
        ThresholdConfig(per_axis={"sample": {"axis_one": 100}}),
    )

    assert len(violations) == 1
    assert violations[0].axis == "axis_one"


def test_coverage_check_cmd_exit_0_on_pass(tmp_path: Path) -> None:
    project = _project(tmp_path, _first_lexicon_id())

    result = CliRunner().invoke(main, ["coverage", "check", "--path", str(project)])

    assert result.exit_code == 0, result.output
    assert "Coverage check PASS" in result.output


def test_coverage_check_cmd_exit_1_on_violation(tmp_path: Path) -> None:
    project = _project(tmp_path, _first_lexicon_id())

    result = CliRunner().invoke(main, ["coverage", "check", "--path", str(project), "--threshold", "100"])

    assert result.exit_code == 1
    assert "Coverage check FAIL" in result.output


def test_coverage_check_cmd_json_output(tmp_path: Path) -> None:
    project = _project(tmp_path, _first_lexicon_id())

    result = CliRunner().invoke(
        main,
        ["coverage", "check", "--path", str(project), "--threshold", "100", "--format", "json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "fail"
    assert payload["violations"]


def test_coverage_check_cmd_exit_zero_on_violation(tmp_path: Path) -> None:
    project = _project(tmp_path, _first_lexicon_id())

    result = CliRunner().invoke(
        main,
        ["coverage", "check", "--path", str(project), "--threshold", "100", "--exit-zero"],
    )

    assert result.exit_code == 0
    assert "Coverage check FAIL" in result.output
