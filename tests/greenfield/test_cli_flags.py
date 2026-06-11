"""CLI gap tests for the greenfield autopilot terrain.

Covers the three core gaps closed for G1:
  1. ``codd generate --all-waves``
  2. ``codd verify --repair-mode automatic|hitl`` (repair.approval_mode override)
  3. deterministic task enumeration (``codd implement list-tasks`` /
     :func:`codd.implementer.list_implement_tasks`)
plus the ``codd greenfield`` thin CLI wrapper.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner
import pytest
import yaml

import codd.cli as cli_module
from codd.cli import main
from codd.repair.approval_repair import apply_repair_mode

from tests.greenfield.conftest import make_stub_project


def _project_with_waves(tmp_path: Path, wave_keys: list[int]) -> Path:
    project = tmp_path / "proj"
    codd_dir = project / "codd"
    codd_dir.mkdir(parents=True)
    config = {
        "project": {"name": "proj", "language": "python"},
        "ai_command": "fake-ai --run",
        "wave_config": {
            str(key): [
                {
                    "node_id": f"design:wave-{key}",
                    "output": f"docs/design/wave_{key}.md",
                    "title": f"Wave {key} Doc",
                }
            ]
            for key in wave_keys
        },
    }
    (codd_dir / "codd.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return project


# ═══════════════════════════════════════════════════════════
# codd generate --all-waves
# ═══════════════════════════════════════════════════════════

def test_generate_all_waves_runs_every_wave_in_order(tmp_path: Path, monkeypatch) -> None:
    project = _project_with_waves(tmp_path, [2, 1, 3])
    called: list[int] = []

    def fake_generate_wave(project_root, wave, force=False, ai_command=None, feedback=None):
        called.append(wave)
        return [
            SimpleNamespace(
                path=project_root / "docs" / "design" / f"wave_{wave}.md",
                node_id=f"design:wave-{wave}",
                status="generated",
            )
        ]

    monkeypatch.setattr("codd.generator.generate_wave", fake_generate_wave)
    result = CliRunner().invoke(main, ["generate", "--all-waves", "--path", str(project)])

    assert result.exit_code == 0, result.output
    assert called == [1, 2, 3]  # numeric order, regardless of YAML key order
    assert "All waves complete (3 wave(s)): 3 generated, 0 skipped" in result.output


def test_generate_all_waves_stops_on_first_failure_and_reports_per_wave(tmp_path: Path, monkeypatch) -> None:
    project = _project_with_waves(tmp_path, [1, 2, 3])
    called: list[int] = []

    def fake_generate_wave(project_root, wave, force=False, ai_command=None, feedback=None):
        called.append(wave)
        if wave == 2:
            raise ValueError("AI command failed: scripted")
        return [
            SimpleNamespace(
                path=project_root / "docs" / "design" / f"wave_{wave}.md",
                node_id=f"design:wave-{wave}",
                status="generated",
            )
        ]

    monkeypatch.setattr("codd.generator.generate_wave", fake_generate_wave)
    result = CliRunner().invoke(main, ["generate", "--all-waves", "--path", str(project)])

    assert result.exit_code == 1
    assert called == [1, 2]  # wave 3 never runs
    assert "Wave 1: 1 generated, 0 skipped" in result.output
    assert "Error: wave 2: AI command failed: scripted" in result.output
    assert "Stopped at wave 2; completed wave(s): 1" in result.output


def test_generate_all_waves_is_mutually_exclusive_with_wave(tmp_path: Path) -> None:
    project = _project_with_waves(tmp_path, [1])
    result = CliRunner().invoke(main, ["generate", "--wave", "1", "--all-waves", "--path", str(project)])
    assert result.exit_code != 0
    assert "--all-waves cannot be used with --wave" in result.output


def test_generate_requires_wave_or_all_waves(tmp_path: Path) -> None:
    project = _project_with_waves(tmp_path, [1])
    result = CliRunner().invoke(main, ["generate", "--path", str(project)])
    assert result.exit_code != 0
    assert "Pass --wave N or --all-waves." in result.output


def test_generate_single_wave_still_works(tmp_path: Path, monkeypatch) -> None:
    project = _project_with_waves(tmp_path, [1, 2])
    called: list[int] = []

    def fake_generate_wave(project_root, wave, force=False, ai_command=None, feedback=None):
        called.append(wave)
        return []

    monkeypatch.setattr("codd.generator.generate_wave", fake_generate_wave)
    result = CliRunner().invoke(main, ["generate", "--wave", "2", "--path", str(project)])
    assert result.exit_code == 0, result.output
    assert called == [2]


# ═══════════════════════════════════════════════════════════
# codd verify --repair-mode
# ═══════════════════════════════════════════════════════════

def test_apply_repair_mode_automatic_maps_to_auto_with_explicit_optin() -> None:
    config = {"repair": {"approval_mode": "required", "max_attempts": 5}}
    merged = apply_repair_mode(config, "automatic")
    assert merged["repair"]["approval_mode"] == "auto"
    assert merged["repair"]["allow_auto"]["require_explicit_optin"] is True
    assert merged["repair"]["max_attempts"] == 5
    # original is never mutated
    assert config["repair"]["approval_mode"] == "required"
    assert "allow_auto" not in config["repair"]


def test_apply_repair_mode_hitl_maps_to_required() -> None:
    merged = apply_repair_mode({"repair": {"approval_mode": "auto"}}, "hitl")
    assert merged["repair"]["approval_mode"] == "required"


def test_apply_repair_mode_keeps_existing_allow_auto_settings() -> None:
    config = {"repair": {"allow_auto": {"require_explicit_optin": True, "max_files_per_proposal": 2}}}
    merged = apply_repair_mode(config, "automatic")
    assert merged["repair"]["allow_auto"]["max_files_per_proposal"] == 2


def test_apply_repair_mode_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="unknown repair mode"):
        apply_repair_mode({}, "yolo")


def test_verify_repair_mode_requires_auto_repair(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["verify", "--repair-mode", "automatic", "--path", str(tmp_path)])
    assert result.exit_code != 0
    assert "--repair-mode requires --auto-repair" in result.output


def _repair_project(tmp_path: Path) -> Path:
    project = tmp_path / "repairable"
    codd_dir = project / "codd"
    codd_dir.mkdir(parents=True)
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(
            {
                "project": {"name": "repairable", "language": "python"},
                "ai_command": "fake-ai --run",
                "repair": {"approval_mode": "required", "max_attempts": 4},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return project


@pytest.mark.parametrize(
    ("mode", "expected_approval"),
    [("automatic", "auto"), ("hitl", "required")],
)
def test_verify_repair_mode_overrides_config_for_the_run(
    tmp_path: Path, monkeypatch, mode: str, expected_approval: str
) -> None:
    project = _repair_project(tmp_path)
    captured: dict[str, object] = {}

    def fake_verify_once(**kwargs):
        return cli_module._CliVerificationResult(passed=False, exit_code=1, failure=None)

    def fake_repair_loop(project_root, failure, *, repair_config, **kwargs):
        captured["repair_config"] = repair_config
        return SimpleNamespace(status="REPAIR_SUCCESS", history_session_dir=project_root / ".codd" / "repair_history")

    monkeypatch.setattr(cli_module, "_run_verify_once", fake_verify_once)
    monkeypatch.setattr(cli_module, "_run_repair_loop", fake_repair_loop)

    result = CliRunner().invoke(
        main, ["verify", "--auto-repair", "--repair-mode", mode, "--path", str(project)]
    )

    assert result.exit_code == 0, result.output
    repair = captured["repair_config"]["repair"]  # type: ignore[index]
    assert repair["approval_mode"] == expected_approval
    if mode == "automatic":
        assert repair["allow_auto"]["require_explicit_optin"] is True
    # the on-disk config is untouched
    on_disk = yaml.safe_load((project / "codd" / "codd.yaml").read_text(encoding="utf-8"))
    assert on_disk["repair"]["approval_mode"] == "required"


def test_verify_without_repair_mode_keeps_config_approval_mode(tmp_path: Path, monkeypatch) -> None:
    project = _repair_project(tmp_path)
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        cli_module,
        "_run_verify_once",
        lambda **kwargs: cli_module._CliVerificationResult(passed=False, exit_code=1, failure=None),
    )

    def fake_repair_loop(project_root, failure, *, repair_config, **kwargs):
        captured["repair_config"] = repair_config
        return SimpleNamespace(status="REPAIR_SUCCESS", history_session_dir=project_root)

    monkeypatch.setattr(cli_module, "_run_repair_loop", fake_repair_loop)
    result = CliRunner().invoke(main, ["verify", "--auto-repair", "--path", str(project)])
    assert result.exit_code == 0, result.output
    assert captured["repair_config"]["repair"]["approval_mode"] == "required"  # type: ignore[index]


# ═══════════════════════════════════════════════════════════
# Task enumeration: list_implement_tasks + codd implement list-tasks
# ═══════════════════════════════════════════════════════════

def _write_task_project(tmp_path: Path, implement_mapping: dict | None = None) -> Path:
    project = tmp_path / "tasks-proj"
    codd_dir = project / "codd"
    codd_dir.mkdir(parents=True)
    config: dict = {
        "project": {"name": "tasks-proj", "language": "python"},
        "ai_command": "fake-ai --run",
    }
    if implement_mapping is not None:
        config["implement"] = {"default_output_paths": implement_mapping}
    (codd_dir / "codd.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return project


def _write_derived_cache(project: Path, *task_ids: str, approved: bool = True) -> None:
    from codd.llm.plan_deriver import DerivedTask, DerivedTaskCacheRecord, write_derived_task_cache

    tasks = [
        DerivedTask.from_dict(
            {
                "id": task_id,
                "title": task_id.replace("_", " "),
                "description": "Derived implementation task.",
                "source_design_doc": "docs/design/contract.md",
                "v_model_layer": "detailed",
                "approved": approved,
            }
        )
        for task_id in task_ids
    ]
    write_derived_task_cache(
        project / ".codd" / "derived_tasks" / "docs_design_contract.md.yaml",
        DerivedTaskCacheRecord("stub", "key", "doc", "template", "now", ["docs/design/contract.md"], tasks),
    )


def test_list_implement_tasks_lists_all_configured_targets(tmp_path: Path) -> None:
    from codd.implementer import list_implement_tasks

    project = _write_task_project(
        tmp_path,
        {"docs/design/auth.md": ["src/auth"], "docs/design/billing.md": ["src/billing"]},
    )
    tasks = list_implement_tasks(project)
    assert [task["task_id"] for task in tasks] == ["docs/design/auth.md", "docs/design/billing.md"]
    assert all(task["source"] == "configured" for task in tasks)


def test_list_implement_tasks_falls_back_to_approved_derived_tasks(tmp_path: Path) -> None:
    from codd.implementer import list_implement_tasks

    project = _write_task_project(tmp_path)
    _write_derived_cache(project, "derived_a", "derived_b")
    tasks = list_implement_tasks(project)
    assert [task["task_id"] for task in tasks] == ["derived_a", "derived_b"]
    assert all(task["source"] == "derived" for task in tasks)
    assert tasks[0]["design_node"] == "docs/design/contract.md"


def test_list_implement_tasks_ignores_unapproved_derived_tasks(tmp_path: Path) -> None:
    from codd.implementer import list_implement_tasks

    project = _write_task_project(tmp_path)
    _write_derived_cache(project, "derived_a", approved=False)
    assert list_implement_tasks(project) == []


def test_list_implement_tasks_configured_takes_precedence_over_derived(tmp_path: Path) -> None:
    from codd.implementer import list_implement_tasks

    project = _write_task_project(tmp_path, {"docs/design/auth.md": ["src/auth"]})
    _write_derived_cache(project, "derived_a")
    tasks = list_implement_tasks(project)
    assert [task["task_id"] for task in tasks] == ["docs/design/auth.md"]


def test_cli_implement_list_tasks_text_and_json(tmp_path: Path) -> None:
    project = _write_task_project(
        tmp_path,
        {"docs/design/auth.md": ["src/auth"], "docs/design/billing.md": ["src/billing"]},
    )
    result = CliRunner().invoke(main, ["implement", "list-tasks", "--path", str(project)])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == ["docs/design/auth.md", "docs/design/billing.md"]

    result = CliRunner().invoke(main, ["implement", "list-tasks", "--path", str(project), "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload[0] == {"task_id": "docs/design/auth.md", "design_node": "docs/design/auth.md", "source": "configured"}


def test_cli_implement_list_tasks_empty_message(tmp_path: Path) -> None:
    project = _write_task_project(tmp_path)
    result = CliRunner().invoke(main, ["implement", "list-tasks", "--path", str(project)])
    assert result.exit_code == 0, result.output
    assert "No implement tasks found" in result.output


# ═══════════════════════════════════════════════════════════
# codd greenfield (thin CLI wrapper)
# ═══════════════════════════════════════════════════════════

def test_cli_greenfield_help_states_the_philosophy() -> None:
    result = CliRunner().invoke(main, ["greenfield", "--help"])
    assert result.exit_code == 0, result.output
    assert "requirements in, system out" in result.output
    assert "auto-approved" in result.output
    assert "--resume" in result.output
    assert "--ntfy-topic" in result.output


def test_cli_greenfield_dry_run(tmp_path: Path, stub_ai) -> None:
    project = make_stub_project(tmp_path, stub_ai["command"])
    result = CliRunner().invoke(main, ["greenfield", "--path", str(project), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output
    assert "codd plan --init --force" in result.output
    assert stub_ai["calls"]() == []


def test_cli_greenfield_full_run_with_scripted_ai(tmp_path: Path, stub_ai) -> None:
    project = make_stub_project(tmp_path, stub_ai["command"])
    result = CliRunner().invoke(main, ["greenfield", "--path", str(project), "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{"):])
    assert payload["status"] == "success"
    assert (project / "src" / "core" / "core.py").is_file()


def test_cli_greenfield_failure_exits_nonzero_with_report(tmp_path: Path) -> None:
    target = tmp_path / "bare"
    target.mkdir()
    result = CliRunner().invoke(main, ["greenfield", "--path", str(target)])
    assert result.exit_code == 1
    assert "Failed stage: init" in result.output
    assert "codd greenfield --resume" in result.output
