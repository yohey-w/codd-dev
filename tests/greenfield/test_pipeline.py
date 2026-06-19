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

from tests.greenfield.conftest import (
    _package_name,
    make_stub_project,
    write_ci_workflow,
)


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
    # implement wrote real source files INTO the harness-owned package
    # (A-core: src/<package_name>/, package name derived from "stub-app").
    core_file = project / "src" / "stub_app" / "core.py"
    assert core_file.is_file()
    assert "def add(a, b):" in core_file.read_text(encoding="utf-8")
    # FX3 + A-core: the build contains an executable test under tests/ importing
    # the package ABSOLUTELY, and the scaffold ensured a runnable pyproject.
    assert (project / "tests" / "test_core.py").is_file()
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

    # The implement-oracle gate (owner-uniqueness + the Python composite oracle)
    # runs for REAL here — owner-uniqueness no longer false-RED's a normal Python
    # src-layout (it reasons over declared claims, not the permissive
    # source-root/package-root fallback), so this e2e exercises the full gate.

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

    # The implement-oracle gate runs for REAL here (owner-uniqueness no longer
    # false-RED's the normal Python src-layout, and the composite oracle accepts
    # the coherent stub output), so this harness exercises CI-rerooting /
    # shared-src-layout / project_type WITH the full gate active.

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

    # 2. ONE coherent app at ONE location: both derived tasks share the SAME
    #    harness-owned package (A-core: src/<package_name>/), not fragmented
    #    src/<task_id>/ copies. Package name derives from "fresh-cli-app".
    pkg_dir = target / "src" / "fresh_cli_app"
    assert (pkg_dir / "core.py").is_file()
    assert (pkg_dir / "cli.py").is_file()
    pkg_entries = sorted(item.name for item in pkg_dir.iterdir())
    assert not any(name.startswith("implement_") for name in pkg_entries), pkg_entries
    assert not (target / "src" / ".github").exists()  # no confined CI residue
    # both implement units really ran against the shared package root (the bare
    # source root "src" is routed into the package, so the recorded output is the
    # package dir).
    assert stub_ai["calls"]().count("output:src/fresh_cli_app") == 2
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


def test_default_propagate_runner_runs_on_clean_build(tmp_path: Path, monkeypatch) -> None:
    """The REAL propagate glue RUNS (no "No verify state found" skip) on a clean build.

    Regression for the greenfield propagate skip observed in the codex4 M2 run:
    ``_default_propagate_runner`` does ``run_verify`` then ``run_commit``. On a
    fresh build the diff is empty, so verify finds nothing — but it must still
    leave state for commit to consume, so the runner returns a real result
    instead of raising and being swallowed as a non-blocking skip.
    """
    from codd.greenfield.pipeline import _default_propagate_runner

    project = make_stub_project(tmp_path, "stub-ai-cli --print")

    # Empty git diff: a clean, just-built tree with nothing to reconcile.
    monkeypatch.setattr(
        "codd.propagator.subprocess.run",
        lambda *args, **kwargs: __import__("subprocess").CompletedProcess(
            args=args[0] if args else [], returncode=0, stdout="", stderr="",
        ),
    )

    # Must NOT raise "No verify state found"; returns an honest zero-doc result.
    detail = _default_propagate_runner(project, ai_command=None)
    assert "committed=0" in detail
    assert "No verify state" not in detail


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


def test_vb_gate_rerun_fires_and_test_scoped_repair_drives_coverage(tmp_path: Path) -> None:
    """The (previously dormant) coverage feedback loop is now WIRED: an uncovered
    VB at stage end triggers a TEST-scoped re-implementation that adds the genuine
    covering marker, and the stage then PASSES.

    The first implement pass (DI seam) writes a covering marker only for VB-add,
    leaving VB-cli uncovered. The stage gate's rerun goes through the real
    ``implement_tasks`` (patched here) which adds the missing VB-cli marker on a
    real asserting test — exercising the live gate→feedback→test-rerun path.
    """
    project = _vb_project(tmp_path)
    calls: list[str] = []
    runners = _fake_runners(calls)
    runners["task_lister"] = _two_task_lister

    def implement_task_runner(project_root, task, **kwargs):
        calls.append(f"implement:{task.task_id}")
        tests_dir = Path(project_root) / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        if task.task_id == "implement_covering_tests":
            # Covers VB-add only — VB-cli is left UNCOVERED on the first pass.
            (tests_dir / "test_behaviors.py").write_text(
                "# codd: covers vb=VB-add\ndef test_add():\n    assert add(2, 3) == 5\n",
                encoding="utf-8",
            )
        return "1 file(s) generated"

    runners["implement_task_runner"] = implement_task_runner

    rerun_feedback: list[str] = []

    def fake_implement_tasks(project_root, **kwargs):
        # The stage gate's TEST-scoped rerun: add the missing VB-cli marker on a
        # real asserting test (the honest repair the feedback asks for).
        rerun_feedback.append(kwargs.get("feedback", ""))
        tests_dir = Path(project_root) / "tests"
        (tests_dir / "test_cli.py").write_text(
            "# codd: covers vb=VB-cli\ndef test_cli():\n    assert main() == 0\n",
            encoding="utf-8",
        )
        return []

    monkey = pytest.MonkeyPatch()
    monkey.setattr("codd.implementer.implement_tasks", fake_implement_tasks)
    try:
        result = GreenfieldPipeline(**runners).run(project)
    finally:
        monkey.undo()

    assert result.status == "success", format_greenfield_result(result, "text")
    # The rerun fired and carried the real gap (VB-cli) in its feedback.
    assert rerun_feedback, "the coverage feedback rerun did not fire"
    assert all("VB-cli" in fb for fb in rerun_feedback)
    # Both VBs are now covered on disk (the honest repair landed).
    assert (project / "tests" / "test_cli.py").is_file()


def test_vb_authenticity_gate_hard_fails_on_marker_on_empty_test(tmp_path: Path) -> None:
    """ANTI-FALSE-GREEN at the STAGE level: a covering marker on an EMPTY test is
    rejected by the authenticity gate even though the coverage audit sees a marker.

    Both VBs carry a ``covers`` marker (coverage audit is satisfied), but VB-cli's
    test has no assertion — the authenticity gate must fail the implement stage.
    """
    project = _vb_project(tmp_path)
    calls: list[str] = []
    runners = _fake_runners(calls)
    runners["task_lister"] = _two_task_lister

    def implement_task_runner(project_root, task, **kwargs):
        calls.append(f"implement:{task.task_id}")
        tests_dir = Path(project_root) / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        if task.task_id == "implement_covering_tests":
            (tests_dir / "test_behaviors.py").write_text(
                "# codd: covers vb=VB-add\n"
                "def test_add():\n    assert add(2, 3) == 5\n\n"
                # VB-cli is "covered" by a marker on an EMPTY test — false coverage.
                "# codd: covers vb=VB-cli\n"
                "def test_cli():\n    pass\n",
                encoding="utf-8",
            )
        return "1 file(s) generated"

    runners["implement_task_runner"] = implement_task_runner

    # The rerun cannot fix it (it re-writes the same empty test), so the gate
    # ultimately fails at the authenticity check.
    def fake_implement_tasks(project_root, **kwargs):
        return []

    monkey = pytest.MonkeyPatch()
    monkey.setattr("codd.implementer.implement_tasks", fake_implement_tasks)
    try:
        result = GreenfieldPipeline(**runners).run(project)
    finally:
        monkey.undo()

    assert result.status == "failed"
    assert result.failed_stage == "implement"
    detail = load_session(project)["stages"]["implement"]["detail"]
    assert "marker-authenticity" in detail


def test_vb_gate_reruns_native_oracle_after_test_repair(tmp_path: Path) -> None:
    """A VB test rerun can break test↔helper symbol coherence, so the native
    oracle MUST be re-asserted after the rerun (pipeline order is oracle→VB).

    Patches the implement-oracle gate to record each invocation; an uncovered VB
    forces one rerun, after which the oracle must run again (≥2 total: the normal
    stage-end run + the post-VB-rerun re-assert)."""
    project = _vb_project(tmp_path)
    calls: list[str] = []
    runners = _fake_runners(calls)
    runners["task_lister"] = _two_task_lister

    def implement_task_runner(project_root, task, **kwargs):
        tests_dir = Path(project_root) / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        if task.task_id == "implement_covering_tests":
            (tests_dir / "test_behaviors.py").write_text(
                "# codd: covers vb=VB-add\ndef test_add():\n    assert add(2, 3) == 5\n",
                encoding="utf-8",
            )
        return "1 file(s) generated"

    runners["implement_task_runner"] = implement_task_runner

    def fake_implement_tasks(project_root, **kwargs):
        (Path(project_root) / "tests" / "test_cli.py").write_text(
            "# codd: covers vb=VB-cli\ndef test_cli():\n    assert main() == 0\n",
            encoding="utf-8",
        )
        return []

    oracle_runs: list[str] = []
    pipeline = GreenfieldPipeline(**runners)
    monkey = pytest.MonkeyPatch()
    monkey.setattr("codd.implementer.implement_tasks", fake_implement_tasks)
    monkey.setattr(
        pipeline, "_enforce_implement_oracle_gate", lambda *_a, **_k: oracle_runs.append("oracle")
    )
    try:
        result = pipeline.run(project)
    finally:
        monkey.undo()

    assert result.status == "success", format_greenfield_result(result, "text")
    # Oracle ran at stage end AND was re-asserted after the VB test rerun.
    assert len(oracle_runs) >= 2


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

    Reproduces the dogfood state the verify stage must rescue. A-core: the source
    lives in the harness-owned src-layout PACKAGE (``src/stub_app/``) and the test
    imports it PACKAGE-ABSOLUTELY, so the build is COHERENT and the import gate
    passes; the only missing piece is the runnable pyproject the scaffold ensures.
    Project name ``stub-app`` -> package ``stub_app``.
    """
    project = make_stub_project(tmp_path, "stub-ai-cli --print")
    pkg = project / "src" / "stub_app"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (project / "tests").mkdir(parents=True, exist_ok=True)
    (project / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (project / "tests" / "test_calc.py").write_text(
        "from stub_app.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
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
    # A-core anti-false-green: source root ONLY, never "." — tests run against
    # the real package, not flat modules on PYTHONPATH.
    assert 'pythonpath = ["src"]' in text
    assert '"."' not in text
    assert "--import-mode=importlib" in text
    assert detect_test_command(project) == "pytest --tb=short -q"
    # ...and verify REALLY executed the generated tests (not structural-only).
    assert "tests executed" in record["detail"]
    assert "pytest" in record["detail"]


def test_verify_stage_anti_false_green_fails_when_tests_fail(tmp_path: Path) -> None:
    """Scaffolding makes the runner DETECTABLE; it must not mask real failures."""
    project = _python_project_with_tests_but_no_config(tmp_path)
    # Make the (now runnable) test fail.
    (project / "tests" / "test_calc.py").write_text(
        "from stub_app.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 999\n",
        encoding="utf-8",
    )

    record: dict = {"status": "pending", "detail": ""}
    with pytest.raises(StageError):
        GreenfieldPipeline()._stage_verify(project, record, {"max_repair_attempts": 1})
    # the runner was still ensured (detectable) — the failure is an HONEST one.
    assert (project / "pyproject.toml").is_file()


def test_verify_stage_fails_honestly_on_incoherent_imports(tmp_path: Path) -> None:
    """A-core: source + tests disagreeing on package context FAIL before pytest.

    A flat-source / bare-basename build (the codex3 incoherence) must fail the
    import-coherence gate HONESTLY rather than crash pytest or pass by accident.
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

    record: dict = {"status": "pending", "detail": ""}
    with pytest.raises(StageError, match="import-coherence"):
        GreenfieldPipeline()._stage_verify(project, record, {"max_repair_attempts": 1})


def test_verify_stage_does_not_clobber_existing_config(tmp_path: Path) -> None:
    project = _python_project_with_tests_but_no_config(tmp_path)
    original = (
        '[project]\nname = "demo"\n\n'
        '[tool.pytest.ini_options]\n'
        'pythonpath = ["src"]\n'
        'addopts = "-p no:cacheprovider --import-mode=importlib"\n'
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


# ---------------------------------------------------------------------------
# a+ refinement: the greenfield propagate window must cover the REAL build
# delta. A fresh build has its generated source<->design UNTRACKED (and may
# have no commits at all), so a plain ``git diff HEAD`` sees nothing and bare
# 677639e would "reconcile zero docs" while the whole build silently drifts
# (false-green, confirmed in the codex4 run). The default runner now uses the
# fresh-build window, which includes untracked artifacts under the configured
# source/doc dirs.
# ---------------------------------------------------------------------------


def _git_init(project: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=project, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=project, check=True)


def test_greenfield_propagate_reconciles_untracked_build_not_zero(tmp_path: Path) -> None:
    """The default propagate runner reconciles an UNTRACKED fresh build (not zero).

    GPT Test 6 (end-to-end, uncommitted-build variant = the codex4 mode). A real
    git repo with NO commits and an untracked generated source file under a
    configured module + a matching design doc: the runner must surface that build
    delta through the fresh-build window and reconcile the design doc, instead of
    seeing an empty ``git diff HEAD`` and silently reconciling zero.
    """
    import subprocess
    import unittest.mock as mock

    from codd.greenfield.pipeline import _default_propagate_runner
    from codd.propagator import _load_verify_state

    project = make_stub_project(tmp_path, "stub-ai-cli --print", name="gf-build")

    # A generated source module + a design doc that covers it (module "core").
    (project / "src" / "core").mkdir(parents=True, exist_ok=True)
    (project / "src" / "core" / "service.py").write_text(
        "def run():\n    return 1\n", encoding="utf-8"
    )
    design = project / "docs" / "design" / "core_design.md"
    design.parent.mkdir(parents=True, exist_ok=True)
    fm = yaml.dump(
        {"codd": {"node_id": "design:core", "type": "design",
                  "title": "Core Design", "modules": ["core"]}},
        sort_keys=False,
    )
    design.write_text(f"---\n{fm}---\n\n# Core Design\n\n## 1. Overview\n\nCore.\n", encoding="utf-8")

    _git_init(project)  # NO commit: the whole build is untracked (HEAD absent).

    # The fresh-build window must report the untracked source as part of the
    # build delta — proof the runner is NOT blind to the generated build.
    from codd.propagator import GREENFIELD_BUILD_DIFF_TARGET, _get_changed_files

    build_changed = _get_changed_files(project, GREENFIELD_BUILD_DIFF_TARGET)
    assert "src/core/service.py" in build_changed
    assert "docs/design/core_design.md" in build_changed

    # No-graph project → the affected design doc is amber (HITL), so verify
    # leaves a docs_classified state (NOT an empty "reconciled zero" state). The
    # AI is never invoked on the amber path, so no stub is needed for that; we
    # stub the generator only defensively.
    real_run = subprocess.run

    def stub(command, *a, **kw):
        if command and command[0] == "git":
            return real_run(command, *a, **kw)
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="## 1. Overview\n\nCore.\n", stderr="")

    import codd.generator as gen

    with mock.patch.object(gen.subprocess, "run", stub):
        detail = _default_propagate_runner(project, ai_command=None)

    assert "No verify state" not in detail
    # State was consumed (cleared) by commit, proving the verify->commit ran.
    assert _load_verify_state(project) is None


# ═══════════════════════════════════════════════════════════
# test-helper symbol-import coherence gate (verify-stage hook)
# ═══════════════════════════════════════════════════════════


def _coherence_project(tmp_path: Path) -> tuple[Path, Path, str]:
    """A pre-initialized project with a coherent src package + test root.

    Returns (project_root, tests_dir, package_name). The verify-stage hook
    (:meth:`GreenfieldPipeline._enforce_import_coherence`) resolves the layout
    profile from this project's ``codd.yaml`` exactly as the real pipeline does.
    """
    project = make_stub_project(tmp_path, ai_command="true", name="coh-app")
    pkg_name = _package_name("coh-app")
    pkg = project / "src" / pkg_name
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "core.py").write_text("def add(x):\n    return x\n")
    tests = project / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    return project, tests, pkg_name


def test_verify_hook_fails_on_incoherent_test_helper_symbol(tmp_path: Path) -> None:
    """The verify-stage hook surfaces a missing test-helper symbol as a StageError
    BEFORE pytest — feeding the DIAGNOSE → REGENERATE path (not an opaque pytest
    collection crash). This is the integration point the gate is wired into.
    """
    project, tests, pkg_name = _coherence_project(tmp_path)
    (tests / "helpers.py").write_text("def run_cli(args):\n    return 0\n")
    # A generated test imports a helper symbol nothing defines (the dogfood bug).
    (tests / "test_core.py").write_text(
        f"from {pkg_name}.core import add\n"
        "from helpers import run_cli, combined_output\n\n\n"
        "def test_add():\n    assert add(1) == 1\n"
    )

    with pytest.raises(StageError) as excinfo:
        GreenfieldPipeline()._enforce_import_coherence(project)
    message = str(excinfo.value)
    assert "missing_test_helper_symbol" in message
    assert "combined_output" in message
    # Honest DIAGNOSE → REGENERATE stance; never auto-stubs.
    assert "REGENERATE" in message
    assert "stubs are never auto-created" in message


def test_verify_hook_passes_on_coherent_suite(tmp_path: Path) -> None:
    """A coherent suite (helpers define exactly what the tests import) clears BOTH
    coherence gates at the verify hook — no false-RED.
    """
    project, tests, pkg_name = _coherence_project(tmp_path)
    (tests / "helpers.py").write_text(
        "def run_cli(args):\n    return 0\n\n\ndef combined_output(r):\n    return ''\n"
    )
    (tests / "test_core.py").write_text(
        f"from {pkg_name}.core import add\n"
        "from helpers import run_cli, combined_output\n\n\n"
        "def test_add():\n    assert add(1) == 1\n"
    )
    # Must not raise.
    GreenfieldPipeline()._enforce_import_coherence(project)


# ═══════════════════════════════════════════════════════════
# e2e-contract (no-runtime-import) coherence gate (verify-stage hook)
# ═══════════════════════════════════════════════════════════


def _set_cli_project_type(project: Path) -> None:
    """Mark the stub project's type as ``cli`` so its e2e modality resolves to cli.

    The e2e-contract gate is modality-gated; the stub's default config carries no
    project_type (undecidable). Declaring ``required_artifacts.project_type: cli``
    is exactly what a real py-CLI greenfield run records, and what makes
    :func:`check_e2e_contract_coherence` activate.
    """
    config_path = project / "codd" / "codd.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    config.setdefault("required_artifacts", {})["project_type"] = "cli"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def test_verify_hook_fails_on_e2e_runtime_import(tmp_path: Path) -> None:
    """The verify-stage hook surfaces a runtime import under the e2e tree as a
    StageError BEFORE pytest — the e2e-no-runtime-import contract the model itself
    derived. This is the SIBLING of the symbol gate on the e2e-import-CONTRACT
    axis (the dogfood shape: a function-scoped runtime import in an e2e helper).
    """
    project, tests, pkg_name = _coherence_project(tmp_path)
    _set_cli_project_type(project)
    e2e = tests / "e2e"
    helpers = e2e / "helpers"
    helpers.mkdir(parents=True)
    (e2e / "__init__.py").write_text("")
    (helpers / "__init__.py").write_text("")
    # The exact dogfood violation: a function-scoped runtime import in a helper.
    (helpers / "cli.py").write_text(
        "def invoke_cli_unit(argv):\n"
        f"    from {pkg_name}.core import add\n"
        "    return add(argv)\n"
    )
    (e2e / "test_flow.py").write_text(
        "from .helpers.cli import invoke_cli_unit\n\n\n"
        "def test_run():\n    assert invoke_cli_unit(1) == 1\n"
    )

    with pytest.raises(StageError) as excinfo:
        GreenfieldPipeline()._enforce_import_coherence(project)
    message = str(excinfo.value)
    assert "e2e_runtime_import" in message
    assert pkg_name in message
    # Honest DIAGNOSE → REGENERATE; never auto-edits the helper or the test.
    assert "REGENERATE" in message
    assert "tests are never auto-edited" in message


def test_verify_hook_passes_on_subprocess_only_e2e(tmp_path: Path) -> None:
    """A subprocess-only e2e suite (no runtime import under the e2e tree) clears
    ALL three coherence gates at the verify hook — no false-RED.
    """
    project, tests, pkg_name = _coherence_project(tmp_path)
    _set_cli_project_type(project)
    e2e = tests / "e2e"
    helpers = e2e / "helpers"
    helpers.mkdir(parents=True)
    (e2e / "__init__.py").write_text("")
    (helpers / "__init__.py").write_text("")
    (helpers / "cli.py").write_text(
        "import subprocess\n\n\n"
        "def invoke_cli(argv):\n"
        "    return subprocess.run(['coh-app'] + argv, capture_output=True)\n"
    )
    (e2e / "test_flow.py").write_text(
        "from .helpers.cli import invoke_cli\n\n\n"
        "def test_run():\n    assert invoke_cli([]).returncode == 0\n"
    )
    # Must not raise.
    GreenfieldPipeline()._enforce_import_coherence(project)


# ═══════════════════════════════════════════════════════════
# Generate-time canonical VB-registry gate + bounded repair
# (model-independence: a weak model that references VB ids in AC tables but
# declares none in the canonical registry must be REPAIRED at generate or fail
# honestly — NEVER reach implement with a 0-declaration orphan storm.)
# ═══════════════════════════════════════════════════════════

# A canonical registry doc that declares ZERO VB rows (the weak-model failure),
# plus an acceptance-criteria doc that REFERENCES VB ids in a later column.
_EMPTY_CANONICAL = "# Test Strategy\n\nNarrative only; no VB rows declared.\n"
_AC_WITH_REFS = (
    "# Acceptance Criteria\n\n"
    "| AC | Description | Verifies |\n| --- | --- | --- |\n"
    "| AC-01 | login | VB-CLI-007 |\n| AC-02 | logout | VB-CLI-008 |\n"
)
_GOOD_CANONICAL = (
    "# Test Strategy\n\n"
    "| VB | Description | Scenario |\n| --- | --- | --- |\n"
    "| VB-CLI-007 | login works | login() |\n"
    "| VB-CLI-008 | logout works | logout() |\n"
)


def _registry_project(tmp_path: Path, *, canonical_body: str) -> Path:
    """A project that EXPECTS a VB registry (test_coverage.docs is configured)
    and whose docs/test/ already contains a canonical doc + an AC-refs doc."""
    project = make_stub_project(tmp_path, "stub-ai-cli --print")
    # Make the project "expect" a registry without relying on wave_config: pin
    # the canonical doc in test_coverage.docs (the planner does exactly this).
    config_path = project / "codd" / "codd.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["test_coverage"] = {"docs": ["docs/test/test_strategy.md"]}
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    docs_test = project / "docs" / "test"
    docs_test.mkdir(parents=True, exist_ok=True)
    (docs_test / "test_strategy.md").write_text(canonical_body, encoding="utf-8")
    (docs_test / "acceptance_criteria.md").write_text(_AC_WITH_REFS, encoding="utf-8")
    return project


def test_generate_vb_registry_gate_honest_red_when_repair_cannot_fix(tmp_path, monkeypatch):
    """Generate gate RAISES (honest-RED) when the canonical registry stays empty
    after the bounded repair attempts. The repair re-invokes generation SCOPED
    to the canonical doc; here that regeneration writes nothing useful, so the
    gate fails honestly instead of letting an empty registry reach implement."""
    project = _registry_project(tmp_path, canonical_body=_EMPTY_CANONICAL)

    import codd.generator as generator_module

    calls: list[dict] = []

    def fake_regenerate(project_root, *, node_id=None, output_path=None, feedback=None, ai_command=None):
        # A weak model that still fails to declare any VB rows (registry stays empty).
        calls.append({"output_path": output_path, "feedback": feedback})
        from codd.generator import GenerationResult

        return GenerationResult(node_id="test:test-strategy", path=project_root / output_path, status="generated")

    monkeypatch.setattr(generator_module, "regenerate_artifact", fake_regenerate)

    pipeline = GreenfieldPipeline()
    with pytest.raises(StageError) as excinfo:
        pipeline._enforce_generate_vb_registry_gate(project, {"coverage_gate": True})

    msg = str(excinfo.value)
    assert "VB-registry gate FAILED" in msg
    # The repair was SCOPED to the canonical doc and ran the bounded count (2).
    assert calls, "repair should have been attempted"
    assert all(c["output_path"] == "docs/test/test_strategy.md" for c in calls)
    assert len(calls) == 2  # bounded: 1–2 attempts
    # The repair feedback carried the unresolved AC references as CANDIDATES only
    # (never auto-inserted) + the canonical-only repair rules.
    fb = calls[0]["feedback"]
    assert "VB-CLI-007" in fb and "VB-CLI-008" in fb
    assert "Rewrite ONLY docs/test/test_strategy.md" in fb
    assert "Do NOT add or modify `codd: covers` markers" in fb


def test_generate_vb_registry_gate_bounded_repair_succeeds(tmp_path, monkeypatch):
    """Generate gate PASSES when the canonical-doc-scoped repair declares the
    missing behaviors. The MODEL writes the declarations (simulated here); the
    gate re-validates and clears."""
    project = _registry_project(tmp_path, canonical_body=_EMPTY_CANONICAL)

    import codd.generator as generator_module

    attempts: list[str] = []

    def fake_regenerate(project_root, *, node_id=None, output_path=None, feedback=None, ai_command=None):
        attempts.append(output_path)
        # The (simulated) model now declares the referenced behaviors canonically.
        (project_root / output_path).write_text(_GOOD_CANONICAL, encoding="utf-8")
        from codd.generator import GenerationResult

        return GenerationResult(node_id="test:test-strategy", path=project_root / output_path, status="generated")

    monkeypatch.setattr(generator_module, "regenerate_artifact", fake_regenerate)

    pipeline = GreenfieldPipeline()
    # Must NOT raise.
    pipeline._enforce_generate_vb_registry_gate(project, {"coverage_gate": True})
    assert attempts == ["docs/test/test_strategy.md"]  # one scoped repair sufficed
    # Anti-false-green: the repaired registry now declares behaviors, but they are
    # UNCOVERED (declaration != coverage) — no markers were added by the repair.
    from codd.verifiable_behavior_audit import build_vb_coverage_audit

    report = build_vb_coverage_audit(project, config=yaml.safe_load((project / "codd" / "codd.yaml").read_text()))
    assert {r.vb_id for r in report.rows} == {"VB-CLI-007", "VB-CLI-008"}
    assert all(r.coverage_status == "uncovered" for r in report.rows)


def test_generate_vb_registry_gate_passes_clean_registry_without_repair(tmp_path, monkeypatch):
    """A coherent registry (all AC refs declared) clears the gate with NO repair
    invocation."""
    project = _registry_project(tmp_path, canonical_body=_GOOD_CANONICAL)

    import codd.generator as generator_module

    def boom(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("repair must not run for a clean registry")

    monkeypatch.setattr(generator_module, "regenerate_artifact", boom)
    GreenfieldPipeline()._enforce_generate_vb_registry_gate(project, {"coverage_gate": True})


def test_generate_vb_registry_gate_skips_project_without_vb_surface(tmp_path, monkeypatch):
    """Shape 2: a project that neither plans nor declares a canonical registry is
    NOT gated at generate — no repair, no raise (model-independence ≠ forcing
    behaviors onto a project that has none)."""
    project = make_stub_project(tmp_path, "stub-ai-cli --print")  # design-only, no VB surface

    import codd.generator as generator_module

    monkeypatch.setattr(
        generator_module,
        "regenerate_artifact",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not repair")),
    )
    # Must NOT raise and must NOT attempt repair.
    GreenfieldPipeline()._enforce_generate_vb_registry_gate(project, {"coverage_gate": True})


def test_generate_vb_registry_gate_skips_when_coverage_gate_off(tmp_path, monkeypatch):
    """The registry contract tracks the coverage gate: --no-coverage-gate skips
    the generate registry gate too."""
    project = _registry_project(tmp_path, canonical_body=_EMPTY_CANONICAL)

    import codd.generator as generator_module

    monkeypatch.setattr(
        generator_module,
        "regenerate_artifact",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not repair when gate off")),
    )
    GreenfieldPipeline()._enforce_generate_vb_registry_gate(project, {"coverage_gate": False})


# ═══════════════════════════════════════════════════════════
# Greenfield empty-registry coverage hard-fail (rear guard)
# ═══════════════════════════════════════════════════════════


def test_stage_coverage_gate_greenfield_empty_registry_hard_fails(tmp_path):
    """When a project EXPECTS a VB registry but declares zero behaviors, the
    implement-stage coverage gate hard-fails (rows == []) — closing the path
    where verify could certify a build whose VB contract was never generated."""
    from codd.greenfield.pipeline import _enforce_stage_coverage_gate

    project = _registry_project(tmp_path, canonical_body=_EMPTY_CANONICAL)
    with pytest.raises(StageError) as excinfo:
        _enforce_stage_coverage_gate(project, coverage_gate=True, echo=lambda _m: None)
    assert "non-empty canonical VB registry" in str(excinfo.value)


def test_stage_coverage_gate_no_vb_surface_project_passes(tmp_path):
    """Shape 2: a project with NO VB surface (no canonical artifact/config/doc)
    must NOT trip the empty-registry hard-fail — the brownfield 'nothing to
    audit -> pass' spirit holds for a minimal greenfield project too."""
    from codd.greenfield.pipeline import _enforce_stage_coverage_gate

    project = make_stub_project(tmp_path, "stub-ai-cli --print")  # design-only
    # Must NOT raise (no VBs declared, but none expected either).
    _enforce_stage_coverage_gate(project, coverage_gate=True, echo=lambda _m: None)
