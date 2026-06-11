"""Greenfield autopilot pipeline tests.

The keystone test (``test_e2e_full_autopilot_with_scripted_ai``) runs the
ENTIRE pipeline on a synthetic project with a scripted, vendor-neutral AI
command: requirements → plan (wave_config) → generated design docs →
implemented source files → verify → final check — green, with no real LLM
anywhere. It proves the orchestration contract end to end and that nothing in
the pipeline depends on a particular AI CLI.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest
import yaml

from codd.greenfield.pipeline import (
    STAGES,
    GreenfieldPipeline,
    ImplementTaskRef,
    StageError,
    format_greenfield_result,
    load_session,
    new_session,
    save_session,
    session_path,
)

from tests.greenfield.conftest import make_stub_project, write_ci_workflow


# ═══════════════════════════════════════════════════════════
# Keystone: full E2E with a scripted AI command
# ═══════════════════════════════════════════════════════════

def test_e2e_full_autopilot_with_scripted_ai(tmp_path: Path, stub_ai) -> None:
    project = make_stub_project(tmp_path, stub_ai["command"])

    result = GreenfieldPipeline().run(project)

    assert result.status == "success", format_greenfield_result(result, "text")
    # plan wrote wave_config into codd.yaml
    config = yaml.safe_load((project / "codd" / "codd.yaml").read_text(encoding="utf-8"))
    assert set(str(key) for key in config["wave_config"]) == {"1", "2"}
    # generate produced both wave documents
    core_design = project / "docs" / "design" / "core_design.md"
    cli_design = project / "docs" / "design" / "cli_design.md"
    assert core_design.is_file() and "## 1." in core_design.read_text(encoding="utf-8")
    assert cli_design.is_file()
    # implement wrote real source files under the configured output path
    core_file = project / "src" / "core" / "core.py"
    assert core_file.is_file()
    assert "def add(a, b):" in core_file.read_text(encoding="utf-8")
    # session records every stage as complete; verify/check are hard gates
    session = load_session(project)
    assert session["result"]["status"] == "success"
    statuses = {name: session["stages"][name]["status"] for name in STAGES}
    assert statuses["plan"] == "done"
    assert statuses["generate"] == "done"
    assert statuses["implement"] == "done"
    assert statuses["verify"] == "done"
    assert statuses["check"] == "done"
    assert all(status in {"done", "warning", "skipped"} for status in statuses.values())
    # the scripted AI really drove every AI-facing stage
    calls = set(stub_ai["calls"]())
    assert "plan_init" in calls
    assert "generate" in calls
    assert "implement" in calls


def test_e2e_fresh_directory_initializes_then_builds(tmp_path: Path, stub_ai) -> None:
    """ensure-init path: an empty directory + name/language/requirements."""
    target = tmp_path / "fresh-app"
    target.mkdir()
    write_ci_workflow(target)  # a repo usually has CI; ci_health gates on it
    spec = tmp_path / "spec.md"
    spec.write_text("# Fresh App\n\nStore numbers and add them via a CLI.\n", encoding="utf-8")

    pipeline = GreenfieldPipeline(
        project_name="fresh-app",
        language="python",
        requirements=spec,
        ai_command=stub_ai["command"],
    )
    result = pipeline.run(target)

    assert result.status == "success", format_greenfield_result(result, "text")
    assert (target / "codd" / "codd.yaml").is_file()
    assert (target / "docs" / "requirements" / "requirements.md").is_file()
    assert (target / "docs" / "design" / "core_design.md").is_file()
    session = load_session(target)
    assert session["stages"]["init"]["status"] == "done"


# ═══════════════════════════════════════════════════════════
# Resume from checkpoint
# ═══════════════════════════════════════════════════════════

def _fake_runners(calls: list[str], *, fail_on: str | None = None):
    """DI runners that record invocations; one may be scripted to fail."""

    def init_runner(project_root, **kwargs):
        calls.append("init")

    def elicit_runner(project_root, **kwargs):
        calls.append("elicit")
        return "findings=0"

    def plan_runner(project_root, **kwargs):
        calls.append("plan")
        return 2

    def wave_lister(project_root):
        return [1, 2]

    def generate_wave_runner(project_root, wave, **kwargs):
        unit = f"generate:{wave}"
        calls.append(unit)
        if fail_on == unit:
            raise ValueError(f"scripted failure for wave {wave}")
        return "1 generated, 0 skipped"

    def task_lister(project_root):
        return [ImplementTaskRef(task_id="docs/design/core_design.md", design_node="docs/design/core_design.md")]

    def implement_task_runner(project_root, task, **kwargs):
        unit = f"implement:{task.task_id}"
        calls.append(unit)
        if fail_on == unit:
            raise StageError(f"scripted failure for task {task.task_id}")
        return "1 file(s) generated"

    def verify_runner(project_root, **kwargs):
        calls.append("verify")
        return "verification passed"

    def propagate_runner(project_root, **kwargs):
        calls.append("propagate")
        return "committed=0"

    def check_runner(project_root):
        calls.append("check")
        return "health check passed"

    return {
        "init_runner": init_runner,
        "elicit_runner": elicit_runner,
        "plan_runner": plan_runner,
        "wave_lister": wave_lister,
        "generate_wave_runner": generate_wave_runner,
        "task_lister": task_lister,
        "implement_task_runner": implement_task_runner,
        "verify_runner": verify_runner,
        "propagate_runner": propagate_runner,
        "check_runner": check_runner,
    }


def _initialized_project(tmp_path: Path) -> Path:
    project = make_stub_project(tmp_path, "stub-ai-cli --print")
    return project


def test_resume_continues_from_first_incomplete_unit(tmp_path: Path) -> None:
    project = _initialized_project(tmp_path)

    # First run dies after wave 1 (wave 2 fails).
    first_calls: list[str] = []
    pipeline = GreenfieldPipeline(**_fake_runners(first_calls, fail_on="generate:2"))
    result = pipeline.run(project)
    assert result.status == "failed"
    assert result.failed_stage == "generate"
    assert result.failed_unit == "2"
    session = load_session(project)
    assert session["stages"]["generate"]["units"] == {"1": "done", "2": "failed"}
    assert "generate:1" in first_calls

    # Resume: completed stages and wave 1 are NOT re-run; the rest completes.
    resume_calls: list[str] = []
    pipeline = GreenfieldPipeline(**_fake_runners(resume_calls))
    result = pipeline.run(project, resume=True)

    assert result.status == "success", format_greenfield_result(result, "text")
    assert "init" not in resume_calls
    assert "plan" not in resume_calls
    assert "generate:1" not in resume_calls  # wave 1 checkpointed as done
    assert "generate:2" in resume_calls
    assert "implement:docs/design/core_design.md" in resume_calls
    assert resume_calls[-1] == "check"
    assert load_session(project)["result"]["status"] == "success"


def test_resume_without_session_starts_fresh(tmp_path: Path) -> None:
    project = _initialized_project(tmp_path)
    calls: list[str] = []
    result = GreenfieldPipeline(**_fake_runners(calls)).run(project, resume=True)
    assert result.status == "success"
    assert "plan" in calls


# ═══════════════════════════════════════════════════════════
# Failure reporting
# ═══════════════════════════════════════════════════════════

def test_stage_failure_report_names_stage_unit_and_commands(tmp_path: Path) -> None:
    project = _initialized_project(tmp_path)
    calls: list[str] = []
    result = GreenfieldPipeline(**_fake_runners(calls, fail_on="implement:docs/design/core_design.md")).run(project)

    assert result.status == "failed"
    assert result.failed_stage == "implement"
    assert result.failed_unit == "docs/design/core_design.md"
    assert result.inspect_command == "codd implement run --task docs/design/core_design.md"
    assert result.resume_command == "codd greenfield --resume"
    text = format_greenfield_result(result, "text")
    assert "Failed stage: implement" in text
    assert "codd greenfield --resume" in text
    payload = result.to_dict()
    assert payload["failure"]["stage"] == "implement"
    assert payload["failure"]["inspect_command"].startswith("codd implement run")
    # checkpoint written: session reflects the failure
    session = load_session(project)
    assert session["result"]["failed_stage"] == "implement"


def test_elicit_failure_never_blocks_the_pipeline(tmp_path: Path) -> None:
    project = _initialized_project(tmp_path)
    calls: list[str] = []
    runners = _fake_runners(calls)

    def broken_elicit(project_root, **kwargs):
        raise RuntimeError("elicit exploded")

    runners["elicit_runner"] = broken_elicit
    result = GreenfieldPipeline(**runners).run(project)

    assert result.status == "success"
    session = load_session(project)
    assert session["stages"]["elicit"]["status"] == "warning"
    assert "elicit exploded" in session["stages"]["elicit"]["detail"]


def test_no_elicit_flag_skips_the_stage(tmp_path: Path) -> None:
    project = _initialized_project(tmp_path)
    calls: list[str] = []
    result = GreenfieldPipeline(elicit=False, **_fake_runners(calls)).run(project)
    assert result.status == "success"
    assert "elicit" not in calls
    assert load_session(project)["stages"]["elicit"]["status"] == "skipped"


def test_propagate_failure_degrades_to_warning(tmp_path: Path) -> None:
    project = _initialized_project(tmp_path)
    calls: list[str] = []
    runners = _fake_runners(calls)

    def broken_propagate(project_root, **kwargs):
        raise ValueError("No verify state found. Run 'codd propagate --verify' first.")

    runners["propagate_runner"] = broken_propagate
    result = GreenfieldPipeline(**runners).run(project)
    assert result.status == "success"
    assert load_session(project)["stages"]["propagate"]["status"] == "warning"


def test_uninitialized_project_without_name_fails_with_clear_message(tmp_path: Path) -> None:
    target = tmp_path / "bare"
    target.mkdir()
    result = GreenfieldPipeline().run(target)
    assert result.status == "failed"
    assert result.failed_stage == "init"
    assert "--project-name" in (result.error or "")


# ═══════════════════════════════════════════════════════════
# Dry run
# ═══════════════════════════════════════════════════════════

def test_dry_run_prints_plan_without_invoking_ai(tmp_path: Path, stub_ai) -> None:
    project = make_stub_project(tmp_path, stub_ai["command"])
    # Pre-seed wave_config so the plan is fully resolvable.
    config_path = project / "codd" / "codd.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["wave_config"] = {
        "1": [{"node_id": "design:core-design", "output": "docs/design/core_design.md", "title": "Core Design"}]
    }
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    result = GreenfieldPipeline().run(project, dry_run=True)

    assert result.status == "dry-run"
    assert [stage.name for stage in result.stages] == list(STAGES)
    text = format_greenfield_result(result, "text")
    assert "codd plan --init --force" in text
    assert "waves 1" in text
    assert "docs/design/core_design.md" in text  # tasks listed
    assert stub_ai["calls"]() == []  # no AI invocation
    assert not session_path(project).exists()  # no checkpoint written


# ═══════════════════════════════════════════════════════════
# ntfy notifications (notify-only, never blocking)
# ═══════════════════════════════════════════════════════════

def test_ntfy_posts_start_stage_failure_and_success(tmp_path: Path) -> None:
    project = _initialized_project(tmp_path)
    posted: list[tuple[str, str]] = []

    def notifier(topic: str, message: str) -> bool:
        posted.append((topic, message))
        return True

    calls: list[str] = []
    result = GreenfieldPipeline(ntfy_topic="codd-test", notifier=notifier, **_fake_runners(calls)).run(project)
    assert result.status == "success"
    topics = {topic for topic, _ in posted}
    assert topics == {"codd-test"}
    messages = [message for _, message in posted]
    assert any("greenfield start" in message for message in messages)
    assert any("plan done" in message for message in messages)
    assert any("SUCCESS" in message for message in messages)

    # failure path posts too
    posted.clear()
    (project / ".codd" / "greenfield_session.yaml").unlink()
    result = GreenfieldPipeline(
        ntfy_topic="codd-test", notifier=notifier, **_fake_runners([], fail_on="generate:2")
    ).run(project)
    assert result.status == "failed"
    assert any("FAILED at generate" in message for _, message in posted)


def test_ntfy_failures_never_block_the_pipeline(tmp_path: Path) -> None:
    project = _initialized_project(tmp_path)

    def broken_notifier(topic: str, message: str) -> bool:
        raise OSError("network down")

    result = GreenfieldPipeline(
        ntfy_topic="codd-test", notifier=broken_notifier, **_fake_runners([])
    ).run(project)
    assert result.status == "success"


def test_no_topic_means_no_notifications(tmp_path: Path) -> None:
    project = _initialized_project(tmp_path)
    posted: list[str] = []
    result = GreenfieldPipeline(
        notifier=lambda topic, message: posted.append(message) or True, **_fake_runners([])
    ).run(project)
    assert result.status == "success"
    assert posted == []


# ═══════════════════════════════════════════════════════════
# Generality gate: AI-CLI agnosticism
# ═══════════════════════════════════════════════════════════

def test_pipeline_source_never_references_vendor_clis() -> None:
    """The orchestrator must stay AI-CLI agnostic: everything resolves through
    the project's configured ai_command. No vendor token may appear in the
    module source (the resolution layer is the only place names may live)."""
    import codd.greenfield.pipeline as pipeline_module

    source = Path(pipeline_module.__file__).read_text(encoding="utf-8")
    assert not re.search(r"claude|codex", source, re.IGNORECASE)


def test_vendor_style_ai_command_string_passes_through_untouched(tmp_path: Path) -> None:
    """A Codex-style ai_command override reaches every stage verbatim."""
    project = _initialized_project(tmp_path)
    vendor_command = "codex exec --model gpt-5.5 --sandbox workspace-write -"
    received: dict[str, object] = {}
    calls: list[str] = []
    runners = _fake_runners(calls)

    def recording_plan(project_root, **kwargs):
        received["plan"] = kwargs.get("ai_command")
        return 1

    def recording_generate(project_root, wave, **kwargs):
        received["generate"] = kwargs.get("ai_command")
        return "ok"

    def recording_implement(project_root, task, **kwargs):
        received["implement"] = kwargs.get("ai_command")
        return "ok"

    def recording_verify(project_root, **kwargs):
        received["verify"] = kwargs.get("ai_command")
        return "ok"

    runners["plan_runner"] = recording_plan
    runners["generate_wave_runner"] = recording_generate
    runners["wave_lister"] = lambda root: [1]
    runners["implement_task_runner"] = recording_implement
    runners["verify_runner"] = recording_verify

    result = GreenfieldPipeline(ai_command=vendor_command, **runners).run(project)
    assert result.status == "success"
    assert received == {
        "plan": vendor_command,
        "generate": vendor_command,
        "implement": vendor_command,
        "verify": vendor_command,
    }


# ═══════════════════════════════════════════════════════════
# Session schema + options
# ═══════════════════════════════════════════════════════════

def test_session_roundtrip_and_schema(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / ".codd").mkdir(parents=True)
    session = new_session({"elicit": True})
    assert session["version"] == 1
    assert list(session["stages"]) == list(STAGES)
    assert all(record["status"] == "pending" for record in session["stages"].values())
    path = save_session(project, session)
    assert path == project / ".codd" / "greenfield_session.yaml"
    loaded = load_session(project)
    assert loaded is not None
    assert loaded["stages"]["init"]["status"] == "pending"
    assert loaded["result"]["status"] == "running"


def test_options_resolution_precedence(tmp_path: Path) -> None:
    """Explicit constructor option > codd.yaml greenfield section > default."""
    project = make_stub_project(
        tmp_path,
        "stub-ai-cli --print",
        greenfield_config={"max_repair_attempts": 3, "elicit": False, "ntfy_topic": "from-config"},
    )
    pipeline = GreenfieldPipeline(max_repair_attempts=7)
    options = pipeline._resolve_options(project)
    assert options["max_repair_attempts"] == 7  # constructor wins
    assert options["elicit"] is False  # config wins over default
    assert options["ntfy_topic"] == "from-config"
    assert options["coverage_gate"] is True  # built-in default
    assert options["propagate_commit"] is True


def test_defaults_yaml_declares_greenfield_section() -> None:
    defaults = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "codd" / "defaults.yaml").read_text(encoding="utf-8")
    )
    section = defaults.get("greenfield")
    assert isinstance(section, dict)
    assert section["elicit"] is True
    assert section["max_repair_attempts"] == 10
    assert section["coverage_gate"] is True
    assert section["ntfy_topic"] == ""
    assert section["propagate_commit"] is True


def test_result_json_format(tmp_path: Path) -> None:
    import json

    project = _initialized_project(tmp_path)
    result = GreenfieldPipeline(**_fake_runners([])).run(project)
    payload = json.loads(format_greenfield_result(result, "json"))
    assert payload["status"] == "success"
    assert [stage["name"] for stage in payload["stages"]] == list(STAGES)
    with pytest.raises(ValueError, match="unsupported greenfield format"):
        format_greenfield_result(result, "md")


def test_resume_restores_ai_command_from_session(tmp_path: Path) -> None:
    # A resumed run must keep using the ai_command the original run recorded.
    # Found in the 2026-06-11 real-AI dogfood: `codd greenfield --resume`
    # without --ai-cmd silently fell back to the project-config default and
    # switched models mid-pipeline.
    project = _initialized_project(tmp_path)

    pipeline = GreenfieldPipeline(
        ai_command="custom-ai --print",
        **_fake_runners([], fail_on="generate:2"),
    )
    assert pipeline.run(project).status == "failed"

    resumed = GreenfieldPipeline(**_fake_runners([]))  # no --ai-cmd this time
    result = resumed.run(project, resume=True)

    assert result.status == "success"
    assert resumed.ai_command == "custom-ai --print"
    assert load_session(project)["options"]["ai_command"] == "custom-ai --print"


def test_resume_explicit_ai_command_override_wins(tmp_path: Path) -> None:
    project = _initialized_project(tmp_path)
    pipeline = GreenfieldPipeline(
        ai_command="original-ai --print",
        **_fake_runners([], fail_on="generate:2"),
    )
    assert pipeline.run(project).status == "failed"

    resumed = GreenfieldPipeline(
        ai_command="override-ai --print", **_fake_runners([])
    )
    assert resumed.run(project, resume=True).status == "success"
    assert resumed.ai_command == "override-ai --print"
