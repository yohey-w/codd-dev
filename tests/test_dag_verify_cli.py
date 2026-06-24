from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import click
from click.testing import CliRunner

from codd.cli import main
from codd.dag import runner as dag_runner

_CLICK_VERSION = tuple(int(part) for part in click.__version__.split(".")[:2])


def _split_stream_runner() -> CliRunner:
    """CliRunner that keeps stdout/stderr separate across the supported click range.

    The project supports ``click>=8.0``. click 8.2 always separates the
    streams; on 8.0/8.1 we must opt in via ``mix_stderr=False`` (a kwarg that
    8.2 removed). This keeps the stderr-routed notice out of the stdout JSON.
    """
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


def _patch_results(monkeypatch, results, calls=None):
    def fake_run_all_checks(project_root: Path, settings=None, check_names=None):
        if calls is not None:
            calls.append(
                {
                    "project_root": project_root,
                    "settings": settings,
                    "check_names": check_names,
                }
            )
        return results

    monkeypatch.setattr(dag_runner, "run_all_checks", fake_run_all_checks)


def test_verify_all_checks_pass(tmp_path, monkeypatch):
    _patch_results(
        monkeypatch,
        [
            _CheckResult("node_completeness"),
            _CheckResult("edge_validity"),
            _CheckResult("depends_on_consistency"),
        ],
    )

    result = CliRunner().invoke(main, ["dag", "verify", "--project-path", str(tmp_path)])

    assert result.exit_code == 0
    assert "PASS  node_completeness [red]" in result.output


def test_verify_red_fail_exits_1(tmp_path, monkeypatch):
    _patch_results(
        monkeypatch,
        [_CheckResult("node_completeness", passed=False, missing_impl_files=["app/admin/page.tsx"])],
    )

    result = CliRunner().invoke(main, ["dag", "verify", "--project-path", str(tmp_path)])

    assert result.exit_code == 1
    assert "FAIL  node_completeness [red]" in result.output
    assert "1 check(s) FAILED" in result.output


def test_verify_amber_warn_exits_0(tmp_path, monkeypatch):
    _patch_results(
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

    result = CliRunner().invoke(main, ["dag", "verify", "--project-path", str(tmp_path)])

    assert result.exit_code == 0
    assert "WARN (severity=amber, deploy allowed)" in result.output


def test_verify_specific_check_only(tmp_path, monkeypatch):
    calls = []
    _patch_results(monkeypatch, [_CheckResult("node_completeness")], calls)

    result = CliRunner().invoke(
        main,
        ["dag", "verify", "--project-path", str(tmp_path), "--check", "node_completeness"],
    )

    assert result.exit_code == 0
    assert calls[0]["check_names"] == ["node_completeness"]


def test_verify_json_format(tmp_path, monkeypatch):
    _patch_results(monkeypatch, [_CheckResult("edge_validity")])

    result = CliRunner().invoke(
        main,
        ["dag", "verify", "--project-path", str(tmp_path), "--format", "json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.output)[0]["check_name"] == "edge_validity"


# --- JSON materiality overlay -------------------------------------------------
#
# The text summary surfaces vacuous passes (PASS that verified 0 items) and
# renders amber-with-findings as WARN. The `--format json` output is what CI
# consumes, so it must carry the same signals — otherwise a vacuous pass or an
# amber-with-findings result is a false-green to a JSON consumer.


def test_verify_json_flags_vacuous_pass(tmp_path, monkeypatch):
    # A check that PASSED having verified 0 items must be marked vacuous in the
    # JSON too, so a CI consumer can tell a green run actually checked nothing.
    _patch_results(
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

    result = CliRunner().invoke(
        main,
        ["dag", "verify", "--project-path", str(tmp_path), "--format", "json"],
    )

    assert result.exit_code == 0
    entry = json.loads(result.output)[0]
    assert entry["check_name"] == "ui_coherence_for_one_to_many"
    assert entry.get("vacuous") is True


def test_verify_json_amber_findings_is_warn(tmp_path, monkeypatch):
    # An amber check that passed but carries findings shows as WARN in text; the
    # JSON must expose a warn-equivalent signal so a CI consumer does not read it
    # as a clean pass.
    _patch_results(
        monkeypatch,
        [
            _CheckResult(
                "transitive_closure",
                severity="amber",
                passed=True,
                status="pass",
                unreachable_nodes=["src/orphan.ts"],
            )
        ],
    )

    result = CliRunner().invoke(
        main,
        ["dag", "verify", "--project-path", str(tmp_path), "--format", "json"],
    )

    assert result.exit_code == 0
    entry = json.loads(result.output)[0]
    # raw signals must be preserved (backward compat)
    assert entry["passed"] is True
    assert entry["severity"] == "amber"
    # ...but a warn-equivalent marker must be present and discoverable.
    assert entry.get("is_warn") is True
    assert entry.get("effective_status") == "warn"


def test_verify_json_normal_pass_unchanged(tmp_path, monkeypatch):
    # Regression guard: an ordinary pass (verified items, no findings) must NOT
    # gain the vacuous / warn markers — the overlay only fires on the bad cases.
    _patch_results(
        monkeypatch,
        [
            _CheckResult(
                "edge_validity",
                severity="red",
                passed=True,
                status="pass",
                checked_count=7,
            )
        ],
    )

    result = CliRunner().invoke(
        main,
        ["dag", "verify", "--project-path", str(tmp_path), "--format", "json"],
    )

    assert result.exit_code == 0
    entry = json.loads(result.output)[0]
    assert entry["check_name"] == "edge_validity"
    assert entry["passed"] is True
    assert "vacuous" not in entry
    assert "is_warn" not in entry
    assert "effective_status" not in entry


def test_verify_empty_dag(tmp_path, monkeypatch):
    _patch_results(monkeypatch, [])

    result = CliRunner().invoke(main, ["dag", "verify", "--project-path", str(tmp_path)])

    assert result.exit_code == 0
    assert "FAILED" not in result.output


def test_verify_output_shows_check_names(tmp_path, monkeypatch):
    _patch_results(monkeypatch, [_CheckResult("task_completion")])

    result = CliRunner().invoke(main, ["dag", "verify", "--project-path", str(tmp_path)])

    assert result.exit_code == 0
    assert "task_completion" in result.output


def test_verify_multiple_check_filter(tmp_path, monkeypatch):
    calls = []
    _patch_results(
        monkeypatch,
        [_CheckResult("node_completeness"), _CheckResult("edge_validity")],
        calls,
    )

    result = CliRunner().invoke(
        main,
        [
            "dag",
            "verify",
            "--project-path",
            str(tmp_path),
            "--check",
            "node_completeness",
            "--check",
            "edge_validity",
        ],
    )

    assert result.exit_code == 0
    assert calls[0]["check_names"] == ["node_completeness", "edge_validity"]


def test_verify_nonexistent_project_error(tmp_path):
    missing = tmp_path / "missing"

    result = CliRunner().invoke(main, ["dag", "verify", "--project-path", str(missing)])

    assert result.exit_code == 1
    assert "project root not found" in result.output


def test_verify_runner_called_with_project_root(tmp_path, monkeypatch):
    calls = []
    _patch_results(monkeypatch, [_CheckResult("node_completeness")], calls)

    result = CliRunner().invoke(main, ["dag", "verify", "--project-path", str(tmp_path)])

    assert result.exit_code == 0
    assert calls[0]["project_root"] == tmp_path.resolve()


# --- enabled_checks allowlist visibility -------------------------------------
#
# enabled_checks is an explicit allowlist (project-type defaults or a codd.yaml
# dag: override). Checks registered after the list was written silently never
# ran; `codd dag verify` now prints a notice so the gap is a visible choice.


def _write_pinned_project(tmp_path) -> None:
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        "project:\n  name: demo\ndag:\n  enabled_checks:\n    - node_completeness\n",
        encoding="utf-8",
    )


def test_verify_notice_lists_unselected_checks(tmp_path, monkeypatch):
    _write_pinned_project(tmp_path)
    _patch_results(monkeypatch, [_CheckResult("node_completeness")])

    result = CliRunner().invoke(main, ["dag", "verify", "--project-path", str(tmp_path)])

    assert result.exit_code == 0
    assert "not selected by enabled_checks" in result.output
    assert "dependency_freshness" in result.output


def test_verify_no_notice_with_explicit_check_flag(tmp_path, monkeypatch):
    _write_pinned_project(tmp_path)
    _patch_results(monkeypatch, [_CheckResult("node_completeness")])

    result = CliRunner().invoke(
        main,
        ["dag", "verify", "--project-path", str(tmp_path), "--check", "node_completeness"],
    )

    assert result.exit_code == 0
    assert "not selected by enabled_checks" not in result.output


def test_verify_no_notice_without_allowlist(tmp_path, monkeypatch):
    # generic project type: no enabled_checks default → all checks run.
    _patch_results(monkeypatch, [_CheckResult("node_completeness")])

    result = CliRunner().invoke(main, ["dag", "verify", "--project-path", str(tmp_path)])

    assert result.exit_code == 0
    assert "not selected by enabled_checks" not in result.output


def test_verify_shows_skip_distinctly_not_pass(tmp_path, monkeypatch):
    # A skipped check verified nothing — it must show as SKIP, never PASS, so a
    # run with silent skips is not indistinguishable from a clean green run.
    _patch_results(monkeypatch, [_CheckResult("dependency_freshness", status="skip")])

    result = CliRunner().invoke(main, ["dag", "verify", "--project-path", str(tmp_path)])

    assert result.exit_code == 0
    assert "SKIP  dependency_freshness" in result.output
    assert "PASS  dependency_freshness" not in result.output


def test_verify_summary_shows_skip_count(tmp_path, monkeypatch):
    # The summary surfaces how many checks verified nothing (the skip count),
    # so a run riddled with silent skips is visibly not a full verification.
    _patch_results(monkeypatch, [_CheckResult("dependency_freshness", status="skip")])

    result = CliRunner().invoke(main, ["dag", "verify", "--project-path", str(tmp_path)])

    assert result.exit_code == 0
    assert "1 check(s) SKIP" in result.output


def test_verify_summary_flags_vacuous_pass(tmp_path, monkeypatch):
    # A check that PASSED having verified 0 items is shown as vacuous, never as a
    # plain pass — so a green run that actually checked nothing is visible.
    _patch_results(
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

    result = CliRunner().invoke(main, ["dag", "verify", "--project-path", str(tmp_path)])

    assert result.exit_code == 0
    assert "verified nothing (vacuous)" in result.output


def test_verify_json_stdout_stays_parseable_with_notice(tmp_path, monkeypatch):
    _write_pinned_project(tmp_path)
    _patch_results(monkeypatch, [_CheckResult("node_completeness")])

    runner = _split_stream_runner()
    result = runner.invoke(
        main,
        ["dag", "verify", "--project-path", str(tmp_path), "--format", "json"],
    )

    assert result.exit_code == 0
    # The notice is routed to stderr; the stdout JSON array must stay parseable.
    assert json.loads(result.stdout)[0]["check_name"] == "node_completeness"
    assert "not selected by enabled_checks" in result.stderr


def test_unselected_check_names_pinned_project(tmp_path):
    from codd.dag.runner import unselected_check_names

    _write_pinned_project(tmp_path)
    unselected = unselected_check_names(tmp_path)
    assert "dependency_freshness" in unselected
    assert "node_completeness" not in unselected
    assert unselected == sorted(unselected)


def test_unselected_check_names_without_allowlist(tmp_path):
    from codd.dag.runner import unselected_check_names

    assert unselected_check_names(tmp_path) == []


def test_dependency_freshness_in_default_allowlists():
    """The built-in project-type defaults must select the new check."""
    import yaml

    from codd.dag.builder import DEFAULTS_DIR

    for name in ("web", "cli", "iot", "mobile"):
        payload = yaml.safe_load((DEFAULTS_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
        assert "dependency_freshness" in payload["enabled_checks"], name


def test_web_default_allowlist_selects_dependency_freshness(tmp_path):
    """A web-type project without a codd.yaml pin runs the check by default."""
    from codd.dag.runner import unselected_check_names

    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    unselected = unselected_check_names(tmp_path)
    assert "dependency_freshness" not in unselected
    # Remaining gap stays visible rather than silently shrinking to zero.
    assert "user_journey_coherence" in unselected
