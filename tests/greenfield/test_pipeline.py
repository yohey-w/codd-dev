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
    # FX3: the build contains an executable test + detectable pytest config...
    assert (project / "src" / "core" / "test_core.py").is_file()
    assert "[tool.pytest" in (project / "pyproject.toml").read_text(encoding="utf-8")
    # session records every stage as complete; verify/check are hard gates
    session = load_session(project)
    assert session["result"]["status"] == "success"
    statuses = {name: session["stages"][name]["status"] for name in STAGES}
    assert statuses["plan"] == "done"
    assert statuses["generate"] == "done"
    assert statuses["implement"] == "done"
    assert statuses["verify"] == "done"
    assert statuses["check"] == "done"
    # ...and the verify stage REALLY executed it: autopilot must never certify
    # an unexecuted build (the 2026-06 dogfood false-green).
    assert "tests executed" in session["stages"]["verify"]["detail"]
    assert "pytest" in session["stages"]["verify"]["detail"]
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


def test_e2e_shared_output_root_repo_root_ci_and_project_type(tmp_path: Path, stub_ai) -> None:
    """FX2 regression harness for the 2026-06 real-AI dogfood findings.

    A fresh CLI-app build with NO pre-seeded CI workflow and NO configured
    implement mapping (the derived-task path). Pins all three structural
    fixes at once:
      1. the stub-emitted ``.github/workflows/ci.yml`` FILE block lands at the
         REPO ROOT (a green final ``codd check`` proves ci_health sees it);
      2. both derived tasks write into the SAME canonical ``src/`` layout —
         no fragmented ``src/<task_id>/`` app copies;
      3. ``--project-type cli`` is recorded in codd.yaml + the session and
         resolves to CLI capabilities (no browser-test guidance).
    """
    target = tmp_path / "fresh-cli-app"
    target.mkdir()
    spec = tmp_path / "spec.md"
    spec.write_text("# Fresh CLI App\n\nStore numbers and add them via a CLI.\n", encoding="utf-8")

    pipeline = GreenfieldPipeline(
        project_name="fresh-cli-app",
        language="python",
        requirements=spec,
        project_type="cli",
        ai_command=stub_ai["command"],
    )
    result = pipeline.run(target)

    assert result.status == "success", format_greenfield_result(result, "text")

    # 1. The CI workflow landed at the repo root — there was no pre-seeded CI,
    #    so the final check stage (ci_health) being green proves the rerooted
    #    workflow is the one CI actually discovers.
    workflow = target / ".github" / "workflows" / "ci.yml"
    assert workflow.is_file()
    assert "pull_request" in workflow.read_text(encoding="utf-8")

    # 2. ONE coherent app at ONE location: both derived tasks share src/.
    assert (target / "src" / "core.py").is_file()
    assert (target / "src" / "cli.py").is_file()
    src_entries = sorted(item.name for item in (target / "src").iterdir())
    assert not any(name.startswith("implement_") for name in src_entries), src_entries
    assert not (target / "src" / ".github").exists()  # no confined CI residue
    # both implement units really ran against the shared root
    assert stub_ai["calls"]().count("output:src") == 2
    session = load_session(target)
    assert set(session["stages"]["implement"]["units"]) == {
        "implement_core_module",
        "implement_cli_module",
    }

    # 3. project_type recorded in codd.yaml + session; capability resolution
    #    is CLI-appropriate (the generation/implement prompts consume this).
    config = yaml.safe_load((target / "codd" / "codd.yaml").read_text(encoding="utf-8"))
    assert config["required_artifacts"]["project_type"] == "cli"
    assert session["options"]["project_type"] == "cli"
    from codd.config import load_project_config
    from codd.generator import _resolve_generation_capabilities

    capabilities = _resolve_generation_capabilities(load_project_config(target), target)
    assert capabilities.e2e_modality == "cli"
    assert capabilities.user_interface is False


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


def test_resume_restores_project_type_from_session(tmp_path: Path) -> None:
    # --project-type must survive --resume exactly like ai_command does: a
    # resumed run without the flag keeps the type the original run recorded.
    project = _initialized_project(tmp_path)

    pipeline = GreenfieldPipeline(
        project_type="cli",
        **_fake_runners([], fail_on="generate:2"),
    )
    assert pipeline.run(project).status == "failed"
    assert load_session(project)["options"]["project_type"] == "cli"

    resumed = GreenfieldPipeline(**_fake_runners([]))  # no --project-type this time
    assert resumed.run(project, resume=True).status == "success"
    assert resumed.project_type == "cli"


def test_project_type_on_existing_project_is_recorded_in_codd_yaml(tmp_path: Path) -> None:
    # The init stage is skipped on an already-initialized project, but a
    # provided project type must still reach codd.yaml so every downstream
    # stage resolves capabilities correctly.
    project = _initialized_project(tmp_path)
    result = GreenfieldPipeline(project_type="cli", **_fake_runners([])).run(project)
    assert result.status == "success"
    config = yaml.safe_load((project / "codd" / "codd.yaml").read_text(encoding="utf-8"))
    assert config["required_artifacts"]["project_type"] == "cli"
    assert "project_type cli" in load_session(project)["stages"]["init"]["detail"]


# ═══════════════════════════════════════════════════════════
# VB coverage gate: enforced ONCE per implement STAGE, not per task
# (regression for F-vb-gate-per-task-granularity)
# ═══════════════════════════════════════════════════════════
#
# The verifiable-behavior coverage gate is PROJECT-WIDE: it reconciles every VB
# id declared across the test documents against `codd: covers vb=` markers
# anywhere in the suite. A multi-task greenfield run implements task-by-task, so
# an EARLY task (e.g. test fixtures/helpers) that legitimately writes no
# covering tests would, under per-task enforcement, see ~0 project coverage and
# hard-fail — even though a LATER task adds the covering tests. The fix moves
# the gate to a single project-wide check after ALL implement tasks complete.

_VB_DOC = """# Test Plan

| VB | Description | Scenario |
| --- | --- | --- |
| VB-add | adds two numbers | add(2, 3) == 5 |
| VB-cli | cli returns zero | main() == 0 |
"""


def _vb_project(tmp_path: Path, *, greenfield_config: dict | None = None) -> Path:
    """A stub project that declares two verifiable behaviors in docs/test."""
    project = make_stub_project(tmp_path, "stub-ai-cli --print", greenfield_config=greenfield_config)
    vb_doc = project / "docs" / "test" / "behaviors.md"
    vb_doc.parent.mkdir(parents=True, exist_ok=True)
    vb_doc.write_text(_VB_DOC, encoding="utf-8")
    return project


def _two_task_lister(project_root):
    """Task A writes test fixtures (no covers markers); task B writes the
    covering tests. Task A is enumerated FIRST — the exact early-task case that
    used to hard-fail the project-wide gate."""
    return [
        ImplementTaskRef(task_id="implement_test_fixtures", design_node="docs/design/core_design.md"),
        ImplementTaskRef(task_id="implement_covering_tests", design_node="docs/design/cli_design.md"),
    ]


def _vb_runners(calls: list[str], *, task_b_covers: bool):
    """DI runners for a VB-coverage scenario.

    Everything except implement is a recording no-op so the test isolates the
    real ``_stage_implement`` + project-wide gate. The implement task runner is
    the REAL default runner's contract: task A writes a fixtures file with NO
    covers marker; task B optionally writes the covering tests.
    """

    runners = _fake_runners(calls)
    runners["task_lister"] = _two_task_lister

    def implement_task_runner(project_root, task, **kwargs):
        calls.append(f"implement:{task.task_id}")
        tests_dir = project_root / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        if task.task_id == "implement_test_fixtures":
            # An early test-related task that legitimately covers no VB yet.
            (tests_dir / "conftest.py").write_text(
                "import pytest\n\n\n@pytest.fixture\ndef sample():\n    return 5\n",
                encoding="utf-8",
            )
            return "1 file(s) generated"
        if task_b_covers:
            (tests_dir / "test_behaviors.py").write_text(
                "# codd: covers vb=VB-add\n"
                "def test_add():\n    assert True\n\n\n"
                "# codd: covers vb=VB-cli\n"
                "def test_cli():\n    assert True\n",
                encoding="utf-8",
            )
        else:
            # A later task that STILL writes no covering markers.
            (tests_dir / "test_behaviors.py").write_text(
                "def test_nothing():\n    assert True\n",
                encoding="utf-8",
            )
        return "1 file(s) generated"

    runners["implement_task_runner"] = implement_task_runner
    return runners


def test_vb_gate_does_not_fail_early_task_and_passes_once_later_task_covers(tmp_path: Path) -> None:
    """Task A (fixtures, no covers) must NOT fail; the project-wide gate passes
    once task B's covers markers exist."""
    project = _vb_project(tmp_path)
    calls: list[str] = []
    result = GreenfieldPipeline(**_vb_runners(calls, task_b_covers=True)).run(project)

    assert result.status == "success", format_greenfield_result(result, "text")
    # Both tasks ran, in order, and NEITHER was marked failed.
    session = load_session(project)
    units = session["stages"]["implement"]["units"]
    assert units == {"implement_test_fixtures": "done", "implement_covering_tests": "done"}
    assert calls.index("implement:implement_test_fixtures") < calls.index("implement:implement_covering_tests")
    assert session["stages"]["implement"]["status"] == "done"


def test_vb_gate_fails_at_final_stage_when_no_task_ever_covers(tmp_path: Path) -> None:
    """When NO task covers the VBs, the failure surfaces at the FINAL project-wide
    gate (after every task ran), not at the early fixtures task."""
    project = _vb_project(tmp_path)
    calls: list[str] = []
    result = GreenfieldPipeline(**_vb_runners(calls, task_b_covers=False)).run(project)

    assert result.status == "failed"
    assert result.failed_stage == "implement"
    # Crucially: BOTH tasks completed before the gate fired — the early task did
    # not hard-fail. The implement stage as a whole is what failed.
    assert "implement:implement_test_fixtures" in calls
    assert "implement:implement_covering_tests" in calls
    session = load_session(project)
    units = session["stages"]["implement"]["units"]
    assert units["implement_test_fixtures"] == "done"
    assert units["implement_covering_tests"] == "done"
    # The recorded error names the VB coverage gate and lists the gap.
    detail = session["stages"]["implement"]["detail"]
    assert "verifiable-behavior coverage gate" in detail
    # verify/check never ran — the stage gate stopped the pipeline.
    assert "verify" not in calls
    assert "check" not in calls


def test_vb_gate_skipped_when_coverage_gate_option_false(tmp_path: Path) -> None:
    """``greenfield.coverage_gate: false`` (the owner opted out) skips the final
    project-wide gate even when VBs are uncovered."""
    project = _vb_project(tmp_path, greenfield_config={"coverage_gate": False})
    calls: list[str] = []
    # task_b_covers=False would fail the gate if it ran; coverage_gate off skips it.
    result = GreenfieldPipeline(**_vb_runners(calls, task_b_covers=False)).run(project)

    assert result.status == "success", format_greenfield_result(result, "text")
    session = load_session(project)
    assert session["stages"]["implement"]["status"] == "done"
    # The opt-out also flows through the constructor flag.
    calls_ctor: list[str] = []
    project2 = _vb_project(tmp_path / "ctor")
    result2 = GreenfieldPipeline(coverage_gate=False, **_vb_runners(calls_ctor, task_b_covers=False)).run(project2)
    assert result2.status == "success", format_greenfield_result(result2, "text")


def test_vb_gate_default_runner_does_not_enforce_per_task(tmp_path: Path) -> None:
    """The greenfield default implement-task runner must NOT hard-fail a single
    task against the project-wide VB universe.

    Exercises the REAL ``_default_implement_task_runner`` (not a DI seam) with a
    DI seam only for the AI step derivation. Task A writes test fixtures with no
    covers marker; with per-task enforcement this raised
    ``StageError(... coverage gate failed)``. After the fix the per-task run
    returns normally — the gate is owed once at stage end instead.
    """
    project = _vb_project(tmp_path)
    pipeline = GreenfieldPipeline()
    task = ImplementTaskRef(task_id="implement_test_fixtures", design_node="docs/design/core_design.md")

    # Stub the implement machinery so we test ONLY the gating decision, not real
    # codegen: write a fixtures file (test-related, zero covers markers).
    import codd.greenfield.pipeline as pipeline_mod

    def fake_implement_tasks(project_root, **kwargs):
        tests_dir = Path(project_root) / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / "conftest.py").write_text("FIXTURE = 1\n", encoding="utf-8")

        class _Result:
            error = None
            generated_files = [tests_dir / "conftest.py"]
            output_paths = ["tests/conftest.py"]

        return [_Result()]

    monkey = pytest.MonkeyPatch()
    monkey.setattr("codd.implementer.implement_tasks", fake_implement_tasks)
    # Skip AI-backed step derivation (advisory; returns 0 cleanly).
    monkey.setattr(pipeline_mod, "_derive_and_approve_steps", lambda *a, **k: 0)
    # Pin the configured output path for the design node to the tests dir.
    monkey.setattr(pipeline_mod, "_output_paths_for_task", lambda config, task: ["tests/"])
    try:
        # coverage_gate=True would, under the OLD per-task behavior, raise.
        detail = pipeline._default_implement_task_runner(
            project,
            task,
            ai_command=None,
            coverage_gate=True,
            chunk_size=None,
            timeout_per_chunk=600,
        )
    finally:
        monkey.undo()

    assert "file(s) generated" in detail  # returned normally, did NOT raise


def test_standalone_implement_per_task_gate_unchanged(tmp_path: Path) -> None:
    """The standalone ``codd implement run --task`` per-task gate is UNCHANGED.

    The fix only changes how the GREENFIELD PIPELINE invokes implement; a user
    running one task explicitly still gets the per-task project-wide gate. This
    pins that the CLI helper the standalone command calls
    (``codd.cli._enforce_implement_coverage_gate``) still hard-fails (SystemExit)
    when a test-related task leaves declared VBs uncovered.
    """
    project = _vb_project(tmp_path)
    # A test-related output with no covering markers — the standalone gate must
    # still reject this per task (its semantics are deliberately untouched).
    tests_dir = project / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test_partial.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")

    import codd.cli as cli_module

    with pytest.raises(SystemExit) as excinfo:
        cli_module._enforce_implement_coverage_gate(
            project,
            design_node="docs/test/behaviors.md",
            output_paths=["tests/test_partial.py"],
            opt_out=False,
            rerun=None,
        )
    assert excinfo.value.code == 1

    # And opt-out (the standalone --no-coverage-gate flag) still bypasses it.
    cli_module._enforce_implement_coverage_gate(
        project,
        design_node="docs/test/behaviors.md",
        output_paths=["tests/test_partial.py"],
        opt_out=True,
        rerun=None,
    )


# ═══════════════════════════════════════════════════════════
# Contract-aware task-done verification (D6 cross-CLI false-GREEN)
# ═══════════════════════════════════════════════════════════
#
# A greenfield task must be marked "done" only when the implementer produced the
# KIND of artifact the task DECLARED (via expected_outputs). A test-writing task
# that emitted only application code (a real cross-CLI failure: a CLI laid the
# tests under the app's source root and produced zero test files) used to pass
# "produced >=1 parseable file"; the stage-end VB gate then took the blame. The
# check drives ONLY off declared expected_outputs + config roots — never task
# names, vendor CLI, test_kinds, or hardcoded path literals. It must also NEVER
# false-RED a correct build (source tasks, colocated tests, no-output skeletons).

def _run_contract_task(
    project: Path,
    task: ImplementTaskRef,
    files_to_write: list[str],
):
    """Drive the REAL ``_default_implement_task_runner`` with a fake implementer
    that writes exactly ``files_to_write`` (project-relative). Returns the
    runner's detail string; raises ``StageError`` if the contract check fails.
    """
    import codd.greenfield.pipeline as pipeline_mod

    def fake_implement_tasks(project_root, **kwargs):
        written: list[Path] = []
        for rel in files_to_write:
            path = Path(project_root) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# generated\n", encoding="utf-8")
            written.append(path)

        class _Result:
            error = None
            generated_files = written
            output_paths = list(files_to_write)

        return [_Result()]

    pipeline = GreenfieldPipeline()
    monkey = pytest.MonkeyPatch()
    monkey.setattr("codd.implementer.implement_tasks", fake_implement_tasks)
    monkey.setattr(pipeline_mod, "_derive_and_approve_steps", lambda *a, **k: 0)
    # Pin output paths so the test isolates the CONTRACT check, not routing.
    monkey.setattr(pipeline_mod, "_output_paths_for_task", lambda config, task: ["src/"])
    try:
        return pipeline._default_implement_task_runner(
            project,
            task,
            ai_command=None,
            coverage_gate=False,
            chunk_size=None,
            timeout_per_chunk=600,
        )
    finally:
        monkey.undo()


def test_contract_check_fails_test_task_that_produced_only_source(tmp_path: Path) -> None:
    """(a) A task whose declared outputs are TESTS but which produced only source
    files must FAIL the contract check (this is the D6 false-GREEN)."""
    project = make_stub_project(tmp_path, "stub-ai-cli --print")
    task = ImplementTaskRef(
        task_id="add_persistence_tests",
        design_node="docs/design/core_design.md",
        expected_outputs=("tests/test_persistence.py",),
        test_kinds=("unit",),
    )
    with pytest.raises(StageError) as excinfo:
        _run_contract_task(project, task, files_to_write=["src/core/persistence.py"])
    message = str(excinfo.value)
    assert "add_persistence_tests" in message
    assert "test" in message  # names the missing kind
    assert "did not generate the intended artifact type" in message


def test_contract_check_passes_test_task_that_produced_tests(tmp_path: Path) -> None:
    """(b) A test-deliverable task that produced an actual test file PASSES."""
    project = make_stub_project(tmp_path, "stub-ai-cli --print")
    task = ImplementTaskRef(
        task_id="add_persistence_tests",
        design_node="docs/design/core_design.md",
        expected_outputs=("tests/test_persistence.py",),
        test_kinds=("unit",),
    )
    detail = _run_contract_task(project, task, files_to_write=["tests/test_persistence.py"])
    assert "file(s) generated" in detail


def test_contract_check_passes_source_task_that_produced_source(tmp_path: Path) -> None:
    """(c) A SOURCE task that produced source PASSES — and Python ``.py`` source
    must NOT be misread as a test (the suffix classifier alone would). This is
    the consistent-layout case that must never false-RED."""
    project = make_stub_project(tmp_path, "stub-ai-cli --print")
    task = ImplementTaskRef(
        task_id="implement_core_module",
        design_node="docs/design/core_design.md",
        expected_outputs=("src",),
        test_kinds=("unit",),  # coverage metadata — must NOT make it a test task
    )
    # Emits a source file AND a colocated sibling test (the keystone shape).
    detail = _run_contract_task(
        project, task, files_to_write=["src/core/core.py", "src/core/test_core.py"]
    )
    assert "file(s) generated" in detail


def test_contract_check_exempts_task_with_no_declared_outputs(tmp_path: Path) -> None:
    """(d) A task that declares NO recognisable output kind (skeleton / bare
    artifact name) imposes NO requirement — never a false-RED."""
    project = make_stub_project(tmp_path, "stub-ai-cli --print")
    task = ImplementTaskRef(
        task_id="scaffold_project",
        design_node="docs/design/core_design.md",
        expected_outputs=(),  # nothing declared
        test_kinds=(),
    )
    # Even producing an unclassifiable artifact passes (no declared contract).
    detail = _run_contract_task(project, task, files_to_write=["README.md"])
    assert "file(s) generated" in detail

    # A bare artifact name (under no configured root, not test-shaped) is also
    # UNKNOWN → exempt.
    task_bare = ImplementTaskRef(
        task_id="produce_artifact",
        design_node="docs/design/core_design.md",
        expected_outputs=("some_artifact",),
        test_kinds=(),
    )
    detail_bare = _run_contract_task(project, task_bare, files_to_write=["build/some_artifact"])
    assert "file(s) generated" in detail_bare


def test_test_only_task_output_paths_resolve_to_test_dirs(tmp_path: Path) -> None:
    """(e) P2 routing: a TEST-only task's output_paths resolve to the configured
    ``scan.test_dirs`` (not the shared source root), so the implementer writes
    tests where the design intends them."""
    from codd.config import load_project_config
    from codd.greenfield.pipeline import _output_paths_for_task

    project = make_stub_project(tmp_path, "stub-ai-cli --print")
    config = load_project_config(project)

    test_task = ImplementTaskRef(
        task_id="add_usage_tests",
        design_node="docs/design/core_design.md",
        expected_outputs=("tests/test_usage.py",),
        test_kinds=("unit",),
    )
    assert _output_paths_for_task(config, test_task) == ["tests/"]

    # A SOURCE task keeps the existing (source) default — NOT rerouted to tests/.
    source_task = ImplementTaskRef(
        task_id="implement_core_module",
        design_node="docs/design/core_design.md",
        expected_outputs=("src",),
        test_kinds=("unit",),
    )
    assert _output_paths_for_task(config, source_task) != ["tests/"]

    # A colocated test (declared under a source root) is respected, NOT moved.
    colocated = ImplementTaskRef(
        task_id="add_colocated_test",
        design_node="docs/design/core_design.md",
        expected_outputs=("src/core/test_core.py",),
        test_kinds=("unit",),
    )
    assert _output_paths_for_task(config, colocated) != ["tests/"]


# ═══════════════════════════════════════════════════════════
# verify stage ensures a runnable test setup (the "runnable tests" guarantee)
# ═══════════════════════════════════════════════════════════
#
# The 2026-06 real-AI dogfood gap: a live build reached verify with real pytest
# tests under tests/, but no test config existed (no pyproject/pytest.ini/...),
# so detect_test_command returned None and verify "executed nothing" — the
# autopilot correctly refused to certify an unexecuted build, but the build was
# unverifiable through no fault of the tests. The verify stage now
# DETERMINISTICALLY scaffolds the stack's test-runner config first (idempotent,
# non-clobbering), so a known stack is always runnable independent of AI luck.


def _python_project_with_tests_but_no_config(tmp_path: Path) -> Path:
    """A project mid-pipeline: source + real pytest tests, but NO test config.

    Reproduces the dogfood state the verify stage must rescue. Uses the configured
    scan dirs (src/, tests/) so the ensured pythonpath/testpaths derive from
    config, not literals.
    """
    project = make_stub_project(tmp_path, "stub-ai-cli --print")
    (project / "src").mkdir(parents=True, exist_ok=True)
    (project / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    (project / "tests").mkdir(parents=True, exist_ok=True)
    (project / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    return project


def test_verify_stage_scaffolds_runner_then_executes(tmp_path: Path) -> None:
    from codd.test_detection import detect_test_command

    project = _python_project_with_tests_but_no_config(tmp_path)
    # Pre-condition: the live gap — tests exist but nothing is detectable.
    assert detect_test_command(project) is None

    record: dict = {"status": "pending", "detail": ""}
    GreenfieldPipeline()._stage_verify(project, record, {"max_repair_attempts": 1})

    # The stage scaffolded a detectable, runnable config (derived from scan dirs)
    pyproject = project / "pyproject.toml"
    assert pyproject.is_file()
    text = pyproject.read_text(encoding="utf-8")
    assert "[tool.pytest.ini_options]" in text
    assert 'testpaths = ["tests"]' in text
    assert 'pythonpath = ["src", "."]' in text
    assert detect_test_command(project) == "pytest --tb=short -q"
    # ...and verify REALLY executed the generated tests (not structural-only).
    assert "tests executed" in record["detail"]
    assert "pytest" in record["detail"]


def test_verify_stage_anti_false_green_fails_when_tests_fail(tmp_path: Path) -> None:
    """Scaffolding makes the runner DETECTABLE; it must not mask real failures."""
    project = _python_project_with_tests_but_no_config(tmp_path)
    # Make the (now runnable) test fail.
    (project / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 999\n",
        encoding="utf-8",
    )

    record: dict = {"status": "pending", "detail": ""}
    with pytest.raises(StageError):
        GreenfieldPipeline()._stage_verify(project, record, {"max_repair_attempts": 1})
    # the runner was still ensured (detectable) — the failure is an HONEST one.
    assert (project / "pyproject.toml").is_file()


def test_verify_stage_does_not_clobber_existing_config(tmp_path: Path) -> None:
    project = _python_project_with_tests_but_no_config(tmp_path)
    original = (
        '[project]\nname = "demo"\n\n'
        '[tool.pytest.ini_options]\n'
        'pythonpath = ["src"]\n'
        'addopts = "-p no:cacheprovider"\n'
    )
    (project / "pyproject.toml").write_text(original, encoding="utf-8")

    record: dict = {"status": "pending", "detail": ""}
    GreenfieldPipeline()._stage_verify(project, record, {"max_repair_attempts": 1})

    # An author/AI-provided config is authoritative — left byte-for-byte intact.
    assert (project / "pyproject.toml").read_text(encoding="utf-8") == original
    assert "tests executed" in record["detail"]


def test_verify_stage_unknown_stack_still_gates_honesty(tmp_path: Path) -> None:
    """A stack without an ensurer must NOT be certified when nothing executes.

    The scaffold is stack-specific; for an unsupported language the verify
    honesty gate (FX3) is still the authority and refuses to certify an
    unexecuted build.
    """
    project = make_stub_project(tmp_path, "stub-ai-cli --print")
    # Override the recorded language to one with no test-runner ensurer.
    config = yaml.safe_load((project / "codd" / "codd.yaml").read_text(encoding="utf-8"))
    config["project"]["language"] = "rust"
    (project / "codd" / "codd.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    record: dict = {"status": "pending", "detail": ""}
    with pytest.raises(StageError, match="executed nothing"):
        GreenfieldPipeline()._stage_verify(project, record, {"max_repair_attempts": 1})
    # no pyproject scaffolded for an unsupported stack.
    assert not (project / "pyproject.toml").exists()
