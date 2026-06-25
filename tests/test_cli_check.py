"""Tests for the aggregated ``codd check`` health entry point (RF3)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import click
import pytest
import yaml
from click.testing import CliRunner

import codd.cli as cli_module
from codd.cli import main
from codd.dag import auto_repair as auto_repair_module
from codd.dag import runner as dag_runner

_CLICK_VERSION = tuple(int(part) for part in click.__version__.split(".")[:2])


def _split_stream_runner() -> CliRunner:
    """CliRunner that keeps stdout/stderr separate across the supported click range."""
    if _CLICK_VERSION < (8, 2):
        return CliRunner(mix_stderr=False)
    return CliRunner()


@dataclass
class _CheckResult:
    check_name: str
    severity: str = "red"
    passed: bool = True
    status: str = ""
    checked_count: int | None = None
    missing_impl_files: list[str] = field(default_factory=list)
    unreachable_nodes: list[str] = field(default_factory=list)


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    (tmp_path / "codd").mkdir()
    (tmp_path / "codd" / "codd.yaml").write_text("project_name: demo\n", encoding="utf-8")
    return tmp_path


def _patch_dag(monkeypatch, results):
    monkeypatch.setattr(
        dag_runner,
        "run_all_checks",
        lambda project_root, settings=None, check_names=None: results,
    )


def _patch_doctor(monkeypatch, warnings):
    monkeypatch.setattr(cli_module, "_doctor_warnings", lambda project_root: list(warnings))


def _write_codd_project(root: Path) -> Path:
    root.mkdir()
    (root / "codd").mkdir()
    (root / "codd" / "codd.yaml").write_text("project_name: demo\n", encoding="utf-8")
    return root


def _patch_coverage_check_dependencies(monkeypatch) -> list[Path]:
    from codd.lexicon_cli.threshold import ThresholdConfig

    loaded_paths: list[Path] = []

    monkeypatch.setattr(
        "codd.lexicon_cli.reporter.CoverageReporter.build",
        lambda self, lexicons, with_ai=False, ai_command=None: SimpleNamespace(
            totals={"covered": 0, "axes": 0, "covered_pct": 0.0}
        ),
    )
    monkeypatch.setattr("codd.lexicon_cli.threshold.evaluate", lambda report, config: [])

    def fake_load_thresholds(path: Path | None) -> ThresholdConfig:
        loaded_paths.append(path)
        return ThresholdConfig()

    monkeypatch.setattr("codd.lexicon_cli.threshold.load_thresholds", fake_load_thresholds)
    return loaded_paths


def test_check_green_project_exits_zero(project: Path, monkeypatch) -> None:
    _patch_doctor(monkeypatch, [])
    _patch_dag(monkeypatch, [_CheckResult("node_completeness"), _CheckResult("edge_validity")])

    result = CliRunner().invoke(main, ["check", "--path", str(project)])

    assert result.exit_code == 0
    assert "PASS — no warnings" in result.output
    assert "PASS  node_completeness [red]" in result.output
    assert "Summary: 0 gate(s) failed, 0 advisory finding(s)" in result.output


@pytest.mark.parametrize("escape_kind", ["absolute", "dotdot", "symlink"])
def test_coverage_threshold_file_rejects_escaping_evidence(
    tmp_path: Path, monkeypatch, escape_kind: str
) -> None:
    project_root = _write_codd_project(tmp_path / "project")
    outside = tmp_path / "outside-thresholds.yaml"
    outside.write_text("coverage:\n  thresholds:\n    default:\n      covered_text_match_pct: 100\n", encoding="utf-8")
    if escape_kind == "absolute":
        raw_threshold = outside
    elif escape_kind == "dotdot":
        raw_threshold = project_root / ".." / outside.name
    else:
        raw_threshold = project_root / "thresholds.yaml"
        raw_threshold.symlink_to(outside)

    loaded_paths = _patch_coverage_check_dependencies(monkeypatch)

    result = CliRunner().invoke(
        main,
        ["coverage", "check", "--path", str(project_root), "--threshold-file", str(raw_threshold)],
    )

    assert result.exit_code == 2
    assert "outside the project root" in result.output
    assert loaded_paths == []


def test_coverage_threshold_file_accepts_in_root_evidence(tmp_path: Path, monkeypatch) -> None:
    project_root = _write_codd_project(tmp_path / "project")
    threshold_file = project_root / "thresholds.yaml"
    threshold_file.write_text(
        "coverage:\n  thresholds:\n    default:\n      covered_text_match_pct: 0\n",
        encoding="utf-8",
    )
    loaded_paths = _patch_coverage_check_dependencies(monkeypatch)

    result = CliRunner().invoke(
        main,
        ["coverage", "check", "--path", str(project_root), "--threshold-file", str(threshold_file)],
    )

    assert result.exit_code == 0
    assert loaded_paths == [threshold_file.resolve()]


def test_diff_extract_input_rejects_absolute_outside_evidence(tmp_path: Path) -> None:
    project_root = _write_codd_project(tmp_path / "project")
    outside = tmp_path / "outside-extracted.md"
    outside.write_text("# extracted from another project\n", encoding="utf-8")

    with pytest.raises(ValueError, match="extract-input.*outside the project root"):
        cli_module._resolve_diff_extract_input(project_root, outside)


def test_diff_extract_input_rejects_default_symlink_escape(tmp_path: Path) -> None:
    project_root = _write_codd_project(tmp_path / "project")
    outside = tmp_path / "outside-extracted.md"
    outside.write_text("# extracted from another project\n", encoding="utf-8")
    default_extract = project_root / ".codd" / "extract" / "extracted.md"
    default_extract.parent.mkdir(parents=True)
    default_extract.symlink_to(outside)

    with pytest.raises(ValueError, match="extract-input.*outside the project root"):
        cli_module._resolve_diff_extract_input(project_root, None)


def test_diff_extract_input_accepts_in_root_default_evidence(tmp_path: Path) -> None:
    project_root = _write_codd_project(tmp_path / "project")
    default_extract = project_root / ".codd" / "extract" / "extracted.md"
    default_extract.parent.mkdir(parents=True)
    default_extract.write_text("# extracted facts\n", encoding="utf-8")

    assert cli_module._resolve_diff_extract_input(project_root, None) == default_extract.resolve()


def test_check_red_dag_failure_exits_nonzero(project: Path, monkeypatch) -> None:
    _patch_doctor(monkeypatch, [])
    _patch_dag(
        monkeypatch,
        [_CheckResult("node_completeness", passed=False, missing_impl_files=["app/page.tsx"])],
    )

    result = CliRunner().invoke(main, ["check", "--path", str(project)])

    assert result.exit_code == 1
    assert "FAIL  node_completeness [red]" in result.output
    assert "Run 'codd dag verify' for details." in result.output
    assert "Summary: 1 gate(s) failed, 0 advisory finding(s)" in result.output


def test_check_advisory_findings_keep_exit_zero(project: Path, monkeypatch) -> None:
    _patch_doctor(monkeypatch, ["mutating endpoint without outcome target"])
    _patch_dag(
        monkeypatch,
        [
            _CheckResult(
                "transitive_closure",
                severity="amber",
                passed=True,
                unreachable_nodes=["src/orphan.ts"],
            )
        ],
    )

    result = CliRunner().invoke(main, ["check", "--path", str(project)])

    assert result.exit_code == 0
    assert "WARNING: mutating endpoint without outcome target" in result.output
    assert "Run 'codd doctor' for details." in result.output
    assert "Summary: 0 gate(s) failed, 2 advisory finding(s)" in result.output


def test_check_disabled_contract_is_noop_section(project: Path, monkeypatch) -> None:
    _patch_doctor(monkeypatch, [])
    _patch_dag(monkeypatch, [_CheckResult("edge_validity")])

    result = CliRunner().invoke(main, ["check", "--path", str(project)])

    assert result.exit_code == 0
    assert "skipped: artifact_contract is disabled (opt-in)" in result.output


def test_check_format_json_parses(project: Path, monkeypatch) -> None:
    _patch_doctor(monkeypatch, ["one advisory"])
    _patch_dag(monkeypatch, [_CheckResult("edge_validity")])
    runner = _split_stream_runner()

    result = runner.invoke(main, ["check", "--path", str(project), "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["doctor"] == ["one advisory"]
    assert payload["dag"][0]["check_name"] == "edge_validity"
    assert payload["contract"]["status"] == "skipped"
    # Pre-existing summary fields are unchanged; the positive-coverage materiality
    # overlay (Axis-P) is the only additive key.
    summary = payload["summary"]
    assert summary["gates_failed"] == 0
    assert summary["advisories"] == 1
    assert summary["vacuous"] == 0
    assert summary["coverage"] == {"contracts": 0, "covered": 0, "pending": 0, "gap": 0}
    assert set(summary) == {"gates_failed", "advisories", "vacuous", "coverage"}


def test_check_summary_flags_vacuous_pass(project: Path, monkeypatch) -> None:
    # A check that PASSED having verified 0 items must be surfaced as vacuous in
    # the embedded dag-verify summary too — the same materiality overlay that the
    # standalone 'dag verify' applies. Otherwise the same run hides the vacuous
    # pass under 'check' (the "start here" command) while showing it under
    # 'dag verify' — a diagnostic gap.
    _patch_doctor(monkeypatch, [])
    _patch_dag(
        monkeypatch,
        [
            _CheckResult(
                "ui_coherence_for_one_to_many",
                severity="amber",
                status="pass",
                checked_count=0,
            )
        ],
    )

    result = CliRunner().invoke(main, ["check", "--path", str(project)])

    assert result.exit_code == 0
    assert "verified nothing (vacuous)" in result.output
    assert "ui_coherence_for_one_to_many" in result.output


def test_check_no_vacuous_note_when_checks_did_work(project: Path, monkeypatch) -> None:
    # Regression: a pass that actually verified items (checked_count > 0) and a
    # legacy result that reports no checked_count at all must NOT be flagged.
    _patch_doctor(monkeypatch, [])
    _patch_dag(
        monkeypatch,
        [
            _CheckResult("node_completeness", status="pass", checked_count=3),
            _CheckResult("edge_validity"),  # legacy: checked_count is None
        ],
    )

    result = CliRunner().invoke(main, ["check", "--path", str(project)])

    assert result.exit_code == 0
    assert "verified nothing (vacuous)" not in result.output


def test_check_json_summary_includes_vacuous_count(project: Path, monkeypatch) -> None:
    # The JSON payload mirrors the text overlay: a machine consumer can read the
    # vacuous accounting off summary, not only by recomputing it from checked_count.
    _patch_doctor(monkeypatch, [])
    _patch_dag(
        monkeypatch,
        [
            _CheckResult(
                "ui_coherence_for_one_to_many",
                severity="amber",
                status="pass",
                checked_count=0,
            )
        ],
    )
    runner = _split_stream_runner()

    result = runner.invoke(main, ["check", "--path", str(project), "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["summary"]["vacuous"] == 1


def test_check_format_json_red_failure_reported_in_summary(project: Path, monkeypatch) -> None:
    _patch_doctor(monkeypatch, [])
    _patch_dag(monkeypatch, [_CheckResult("ci_health", passed=False)])
    runner = _split_stream_runner()

    result = runner.invoke(main, ["check", "--path", str(project), "--format", "json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["summary"]["gates_failed"] == 1


def test_check_full_skips_unconfigured_policy_and_coverage(project: Path, monkeypatch) -> None:
    _patch_doctor(monkeypatch, [])
    _patch_dag(monkeypatch, [_CheckResult("edge_validity")])

    result = CliRunner().invoke(main, ["check", "--path", str(project), "--full"])

    assert result.exit_code == 0
    assert "skipped: no policies configured in codd.yaml" in result.output
    assert "skipped: no coverage.thresholds configured in codd.yaml" in result.output
    assert "Summary: 0 gate(s) failed" in result.output


def test_check_full_json_includes_policy_and_coverage_sections(project: Path, monkeypatch) -> None:
    _patch_doctor(monkeypatch, [])
    _patch_dag(monkeypatch, [_CheckResult("edge_validity")])
    runner = _split_stream_runner()

    result = runner.invoke(main, ["check", "--path", str(project), "--full", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["policy"]["status"] == "skipped"
    assert payload["coverage"]["status"] == "skipped"


def test_check_fix_runs_auto_repair_in_apply_mode(project: Path, monkeypatch) -> None:
    _patch_doctor(monkeypatch, [])
    _patch_dag(monkeypatch, [_CheckResult("edge_validity")])

    @dataclass
    class _Outcome:
        applied: list = field(default_factory=list)
        skipped: list = field(default_factory=list)

    calls: dict[str, object] = {}

    def fake_apply_auto_repair(project_root, results, dry_run=True):
        calls["project_root"] = project_root
        calls["dry_run"] = dry_run
        return _Outcome()

    monkeypatch.setattr(auto_repair_module, "apply_auto_repair", fake_apply_auto_repair)

    result = CliRunner().invoke(main, ["check", "--path", str(project), "--fix"])

    assert result.exit_code == 0
    assert calls["dry_run"] is False
    assert calls["project_root"] == project.resolve()
    assert "Applied 0 auto-repair(s):" in result.output


def test_check_requires_codd_dir(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["check", "--path", str(tmp_path)])

    assert result.exit_code == 1
    assert "Run 'codd init' first" in result.output


def test_main_help_points_to_check() -> None:
    result = CliRunner().invoke(main, ["--help"])

    assert result.exit_code == 0
    assert "Health: codd check (start here)" in result.output


def test_check_help_mentions_preflight_stays_separate() -> None:
    result = CliRunner().invoke(main, ["check", "--help"])

    assert result.exit_code == 0
    assert "codd preflight" in result.output


# --- Axis-P Phase A2: positive coverage materiality (additive) -------------


def _write_dag_json_with_contracts(project: Path) -> None:
    """Write a real .codd/dag.json declaring assorted contract entries.

    The aggregated check reads contract declarations from the persisted DAG
    (the dag-verify stage writes it for real runs); here we provide it directly
    so the materiality counter has explicit declarations to count regardless of
    the mocked ``run_all_checks``.
    """
    payload = {
        "version": "1",
        "project_root": str(project.resolve()),
        "nodes": [
            {
                "id": "docs/design.md",
                "kind": "design_doc",
                "path": "docs/design.md",
                "attributes": {
                    "user_journeys": [
                        {"name": "checkout", "criticality": "high"},
                        {"name": "refund", "criticality": "medium"},
                    ],
                    "resource_contracts": [{"resource": "order_id"}],
                    "frontmatter": {
                        "codd": {
                            "capability_contracts": [{"capability": "place_order"}],
                            "aggregation_policies": [{"field_id": "total"}],
                        }
                    },
                },
            },
            {"id": "src/app.ts", "kind": "impl", "path": "src/app.ts", "attributes": {}},
        ],
        "edges": [],
        "cycles": [],
        "coverage_axes": [{"id": "axis_locale", "name": "locale"}],
    }
    dag_path = project / ".codd" / "dag.json"
    dag_path.parent.mkdir(parents=True, exist_ok=True)
    dag_path.write_text(json.dumps(payload), encoding="utf-8")


def _write_lexicon_with_decisions(project: Path) -> None:
    lexicon = {
        "node_vocabulary": [{"id": "page", "description": "a page"}],
        "naming_conventions": [],
        "design_principles": [],
        "coverage_decisions": [
            {"id": "d_confirmed", "question": "q1?", "status": "CONFIRMED"},
            {"id": "d_recommended", "question": "q2?", "status": "RECOMMENDED_PROCEEDING"},
            {"id": "d_ask", "question": "q3?", "status": "ASK"},
            {"id": "d_ask2", "question": "q4?", "status": "ASK"},
        ],
    }
    (project / "project_lexicon.yaml").write_text(yaml.safe_dump(lexicon), encoding="utf-8")


def test_check_summary_includes_positive_coverage_materiality(project: Path, monkeypatch) -> None:
    # Axis-P: 'check' surfaces positive coverage materiality alongside the
    # existing negative findings — contract declarations counted from the DAG and
    # coverage decisions counted from the lexicon, additively.
    _patch_doctor(monkeypatch, [])
    _patch_dag(monkeypatch, [_CheckResult("edge_validity")])
    _write_dag_json_with_contracts(project)
    _write_lexicon_with_decisions(project)

    result = CliRunner().invoke(main, ["check", "--path", str(project)])

    assert result.exit_code == 0
    # 2 journeys + 1 resource + 1 capability + 1 aggregation + 1 coverage_axis = 6.
    # decisions: 1 CONFIRMED -> covered, (1 RECOMMENDED_PROCEEDING + 2 ASK) -> pending,
    # 2 ASK -> gap.
    assert "Coverage: 6 contract(s), 1 covered, 3 pending, 2 gap(s)" in result.output
    # Existing summary line is unchanged and still present.
    assert "Summary: 0 gate(s) failed, 0 advisory finding(s)" in result.output


def test_check_json_summary_includes_positive_coverage_materiality(project: Path, monkeypatch) -> None:
    _patch_doctor(monkeypatch, [])
    _patch_dag(monkeypatch, [_CheckResult("edge_validity")])
    _write_dag_json_with_contracts(project)
    _write_lexicon_with_decisions(project)
    runner = _split_stream_runner()

    result = runner.invoke(main, ["check", "--path", str(project), "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["summary"]["coverage"] == {
        "contracts": 6,
        "covered": 1,
        "pending": 3,
        "gap": 2,
    }
    # Pre-existing summary keys remain untouched (additive).
    assert payload["summary"]["gates_failed"] == 0
    assert payload["summary"]["advisories"] == 0
    assert payload["summary"]["vacuous"] == 0


def test_check_summary_coverage_all_zero_when_nothing_declared(project: Path, monkeypatch) -> None:
    # Regression: a project with no contract declarations and no coverage
    # decisions reports all-zero coverage, and the existing summary line is
    # byte-for-byte unchanged (no behavioural drift for the common case).
    _patch_doctor(monkeypatch, [])
    _patch_dag(monkeypatch, [_CheckResult("edge_validity")])

    result = CliRunner().invoke(main, ["check", "--path", str(project)])

    assert result.exit_code == 0
    assert "Coverage: 0 contract(s), 0 covered, 0 pending, 0 gap(s)" in result.output
    assert "Summary: 0 gate(s) failed, 0 advisory finding(s)" in result.output


def test_check_json_summary_coverage_all_zero_when_nothing_declared(project: Path, monkeypatch) -> None:
    _patch_doctor(monkeypatch, [])
    _patch_dag(monkeypatch, [_CheckResult("edge_validity")])
    runner = _split_stream_runner()

    result = runner.invoke(main, ["check", "--path", str(project), "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["summary"]["coverage"] == {
        "contracts": 0,
        "covered": 0,
        "pending": 0,
        "gap": 0,
    }
