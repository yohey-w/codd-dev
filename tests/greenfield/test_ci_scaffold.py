"""greenfield ci_scaffold stage — author an authentic CI workflow so a freshly
built system is CI-ready and the final ``check`` gate's ci_health requirement is
met honestly.

Surfaced by the C-Go greenfield dogfood: a clean Go build reached green on
verify + Phase-2 mutation probes (false-green escape = 0) but failed the final
``check`` stage on ``ci_workflow_missing`` — greenfield produced a system its own
check stage rejected. The fix (per GPT consult) is a DETERMINISTIC scaffold, not
AI-freeform (which could emit a hollow ``run: true`` that games ci_health) and
not an auto-opt-out (which would silently declare the system needs no CI).

The cardinal property under test: the generated CI runs the project's REAL test
command (``detect_test_command`` — the SAME source verify uses), so CI
authenticity == verify authenticity by construction (anti-false-green).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codd.dag.checks.ci_health import CiConfig, CiHealthCheck
from codd.greenfield.pipeline import (
    STAGES,
    STATUS_SKIPPED,
    GreenfieldPipeline,
    StageError,
    _default_ci_scaffold_runner,
)
from codd.test_detection import detect_test_command


def _go_project(root: Path) -> Path:
    (root / "go.mod").write_text("module cgo-itemapi\n\ngo 1.21\n", encoding="utf-8")
    return root


def _write_codd_yaml(root: Path, language: str) -> None:
    # A real greenfield project always has codd.yaml with project.language; the
    # v2.70 contract-driven ci_scaffold reads CI setup steps from that language's
    # profile (not a marker-file table).
    codd_dir = root / "codd"
    codd_dir.mkdir(exist_ok=True)
    (codd_dir / "codd.yaml").write_text(
        f"project:\n  name: x\n  language: {language}\n", encoding="utf-8"
    )


def _run_step_commands(workflow_path: Path) -> list[str]:
    payload = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    steps = payload["jobs"]["test"]["steps"]
    return [s["run"] for s in steps if isinstance(s, dict) and "run" in s]


# ── stage wiring ────────────────────────────────────────────


def test_ci_scaffold_runs_after_verify_before_check():
    # It must produce CI BEFORE the check gate inspects it, and after verify
    # (which proves the test command exists).
    assert "ci_scaffold" in STAGES
    assert STAGES.index("verify") < STAGES.index("ci_scaffold") < STAGES.index("check")


# ── authentic generation (anti-false-green core) ────────────


def test_go_project_scaffolds_ci_that_passes_ci_health(tmp_path):
    _go_project(tmp_path)
    detail = _default_ci_scaffold_runner(tmp_path)
    assert "generated" in detail

    result = CiHealthCheck().check(tmp_path, CiConfig())
    assert result.passed is True
    assert result.status == "pass"
    assert result.findings == []
    assert result.workflow_files == [".github/workflows/ci.yml"]


def test_generated_ci_runs_the_real_test_command(tmp_path):
    # CI authenticity == verify authenticity: the workflow's run step is exactly
    # what detect_test_command (verify's source) returns — not a hollow no-op.
    _go_project(tmp_path)
    _default_ci_scaffold_runner(tmp_path)
    runs = _run_step_commands(tmp_path / ".github/workflows/ci.yml")
    expected = detect_test_command(tmp_path)
    assert expected == "go test ./..."
    assert expected in runs
    # No gamed/hollow command slipped in.
    assert not any(r.strip() in {"true", "echo ok", ":"} for r in runs)


def test_generated_ci_has_required_triggers(tmp_path):
    _go_project(tmp_path)
    _default_ci_scaffold_runner(tmp_path)
    payload = yaml.safe_load((tmp_path / ".github/workflows/ci.yml").read_text())
    # PyYAML quotes the "on" key (YAML 1.1 bool), so it round-trips as a string.
    triggers = payload.get("on") or payload.get(True)
    assert set(triggers) == {"push", "pull_request"}


def test_go_project_gets_setup_go_from_profile(tmp_path):
    # Contract-driven: codd.yaml language=go → the go profile's ci.setup_steps
    # (actions/setup-go) are used. No marker-file table in the pipeline.
    _go_project(tmp_path)
    _write_codd_yaml(tmp_path, "go")
    _default_ci_scaffold_runner(tmp_path)
    text = (tmp_path / ".github/workflows/ci.yml").read_text()
    assert "actions/setup-go" in text


def test_node_project_gets_setup_node_and_real_test(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"name": "x", "scripts": {"test": "vitest run"}}', encoding="utf-8"
    )
    _write_codd_yaml(tmp_path, "typescript")  # setup steps come from the profile
    _default_ci_scaffold_runner(tmp_path)
    text = (tmp_path / ".github/workflows/ci.yml").read_text()
    assert "actions/setup-node" in text
    assert "npm run test" in text  # detect_test_command's node mapping


def test_python_project_gets_setup_python_and_real_test(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    _write_codd_yaml(tmp_path, "python")  # setup steps come from the profile
    _default_ci_scaffold_runner(tmp_path)
    text = (tmp_path / ".github/workflows/ci.yml").read_text()
    assert "actions/setup-python" in text
    assert "pytest" in text


def test_unprofiled_project_gets_no_setup_steps_but_still_authentic(tmp_path):
    # No codd.yaml / no language profile → no toolchain bootstrap (honest
    # pluggable default), but the workflow still runs the real test command.
    _go_project(tmp_path)  # go.mod → detect_test_command works, but no codd.yaml
    _default_ci_scaffold_runner(tmp_path)
    text = (tmp_path / ".github/workflows/ci.yml").read_text()
    assert "actions/setup-go" not in text  # no profile resolved → no setup
    assert "go test ./..." in text  # still authentic (real test command)


# ── honest refusals (no false-green, no silent opt-out) ─────


def test_undetectable_test_command_fails_honestly(tmp_path):
    # An unknown project has no authentic test command → refuse to fabricate CI.
    # Honest RED beats a hollow workflow that would falsely satisfy ci_health.
    with pytest.raises(StageError) as excinfo:
        _default_ci_scaffold_runner(tmp_path)
    assert "cannot determine the project's test command" in str(excinfo.value)
    assert not (tmp_path / ".github/workflows/ci.yml").exists()


# ── idempotence / explicit opt-out ──────────────────────────


def test_existing_workflow_left_untouched(tmp_path):
    _go_project(tmp_path)
    wf = tmp_path / ".github" / "workflows" / "release.yml"
    wf.parent.mkdir(parents=True)
    sentinel = "name: release\n'on':\n  push: null\n"
    wf.write_text(sentinel, encoding="utf-8")

    detail = _default_ci_scaffold_runner(tmp_path)
    assert detail.startswith("skipped")
    assert wf.read_text(encoding="utf-8") == sentinel  # untouched
    assert not (tmp_path / ".github/workflows/ci.yml").exists()


def test_opt_out_provider_none_skips_scaffold(tmp_path):
    _go_project(tmp_path)  # would otherwise generate
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text("ci:\n  provider: none\n", encoding="utf-8")

    detail = _default_ci_scaffold_runner(tmp_path)
    assert detail.startswith("skipped")
    assert "ci.provider=none" in detail
    assert not (tmp_path / ".github/workflows/ci.yml").exists()


# ── stage-method status semantics ───────────────────────────


def test_stage_marks_skipped_when_workflow_present(tmp_path):
    _go_project(tmp_path)
    wf = tmp_path / ".github" / "workflows" / "ci.yml"
    wf.parent.mkdir(parents=True)
    wf.write_text("name: ci\n'on':\n  push: null\n", encoding="utf-8")

    pipeline = GreenfieldPipeline()
    record: dict = {"status": "pending", "detail": ""}
    pipeline._stage_ci_scaffold(tmp_path, record, {})
    assert record["status"] == STATUS_SKIPPED
    assert record["detail"].startswith("skipped")


def test_stage_uses_injected_runner(tmp_path):
    calls: list[Path] = []

    def fake(project_root: Path, *, ai_command=None) -> str:
        calls.append(project_root)
        return "generated fake ci"

    pipeline = GreenfieldPipeline(ci_scaffold_runner=fake)
    record: dict = {"status": "pending", "detail": ""}
    pipeline._stage_ci_scaffold(tmp_path, record, {})
    assert calls == [tmp_path]
    assert record["detail"] == "generated fake ci"
    assert record["status"] == "pending"  # not skipped → loop will mark done
