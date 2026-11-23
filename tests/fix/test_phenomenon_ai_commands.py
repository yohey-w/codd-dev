"""Exercise phenomenon fix command routing through the real pipeline and adapters."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from click.testing import CliRunner

from codd.ai_invoke import DEFAULT_AI_COMMAND
from codd.cli import main
from codd.deployment.providers import ai_command_factory
from codd.deployment.providers.ai_command import CodexAppServerAiCommand, SubprocessAiCommand
from codd.fix import impl_propagation, phenomenon_fixer


STAGE_PROMPTS = {
    "fix_phenomenon_parse": "You are CoDD's PHENOMENON parser.",
    "fix_candidate_score": "You are CoDD's semantic matcher.",
    "fix_design_update": "You are CoDD's design document updater.",
    "fix_risk_classify": "You are CoDD's risk classifier.",
    "fix_impl_propagate": "You are CoDD's implementation propagator.",
}
PHENOMENON = "Login error wording is unclear"
DESIGN_PATH = "design/login.md"
SHARED_COMMAND = 'shared-ai --label "shared model"'
GLOBAL_COMMAND = 'global-ai --label "global model"'


def _design_text(impl_path: str) -> str:
    return f"""---
title: Login
description: Login error wording
user_journeys:
  - id: u1
    name: Retry after an error
    criticality: high
    steps: []
    required_capabilities: []
    expected_outcome_refs: []
acceptance_criteria:
  - id: c1
    description: Show an error on failed login
codd:
  node_id: login
  band: green
---
# Login

See `{impl_path}` for the response message.
"""


@pytest.fixture
def scripted_project(tmp_path, monkeypatch):
    """Create a local project and record real subprocess adapter argv with no launches."""
    monkeypatch.delenv("CODD_LLM_MODEL", raising=False)
    monkeypatch.setattr(phenomenon_fixer, "default_check_runner", lambda _root: [])
    monkeypatch.setattr(impl_propagation, "default_check_runner", lambda _root: [])
    monkeypatch.setattr(impl_propagation, "_run_local_tests", lambda _root, _config: [])

    def prepare(config, *, planned=False):
        impl_path = "src/web/login_notice.py" if planned else "src/messages.py"
        design = _design_text(impl_path)
        original_code = 'login_notice = "Try again."\n' if planned else 'MESSAGE = "Try again."\n'
        updated_code = original_code.replace("Try again.", "Check your credentials and try again.")
        files = {
            ".codd/codd.yaml": yaml.safe_dump({
                "scan": {"source_dirs": ["src/"]},
                "dag": {
                    "design_doc_patterns": ["design/**/*.md"],
                    "impl_file_patterns": ["src/**/*.py"],
                },
                **config,
            }),
            DESIGN_PATH: design,
            impl_path: original_code,
        }
        for rel_path, content in files.items():
            path = tmp_path / rel_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        analysis = {"intent": "improvement", "subject_terms": ["login"], "ambiguity_score": 0.05}
        if planned:
            analysis.update(fields=["login_notice"], surfaces=["web"])
        responses = {
            "fix_phenomenon_parse": json.dumps(analysis),
            "fix_candidate_score": "{}",
            "fix_design_update": design + "Use clearer wording when a request fails.\n",
            "fix_risk_classify": '{"risky": false, "categories": []}',
            "fix_impl_propagate": f"```python {impl_path}\n{updated_code}```\n",
        }

        def reply(prompt):
            for stage, prefix in STAGE_PROMPTS.items():
                if prompt.startswith(prefix):
                    return stage, responses[stage]
            pytest.fail(f"Unexpected AI prompt: {prompt[:100]}")

        calls = []
        original_init = SubprocessAiCommand.__init__

        def tracked_init(adapter, *args, **kwargs):
            def runner(argv, *, input, cwd, **_kwargs):
                stage, output = reply(input)
                workspace = Path(cwd)
                assert workspace != tmp_path
                assert workspace.is_dir() and not any(workspace.iterdir())
                calls.append((stage, argv, workspace, adapter))
                return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")

            original_init(adapter, *args, **{**kwargs, "runner": runner})

        monkeypatch.setattr(SubprocessAiCommand, "__init__", tracked_init)
        return tmp_path, calls, reply

    return prepare


def _run_fix(project_root):
    result = phenomenon_fixer.run_phenomenon_fix(
        project_root, PHENOMENON, non_interactive=True, on_ambiguity="top1", max_attempts=1
    )
    assert not result.aborted, result.abort_reason
    assert result.applied_paths == [DESIGN_PATH]
    assert result.propagation is not None
    assert result.propagation.verified
    assert len(result.propagation.written_paths) == 1
    return result


@pytest.mark.parametrize(
    ("stage", "planned"),
    [
        *(pytest.param(stage, False, id=stage) for stage in STAGE_PROMPTS),
        pytest.param("fix_impl_propagate", True, id="fix_impl_propagate_impact_plan"),
    ],
)
def test_named_stage_overrides_only_its_command(scripted_project, stage, planned):
    stage_command = 'stage-ai --label "stage model"'
    value = {"command": stage_command} if stage == "fix_design_update" else stage_command
    root, calls, _ = scripted_project({
        "ai_command": GLOBAL_COMMAND,
        "ai_commands": {"fix": SHARED_COMMAND, stage: value},
    }, planned=planned)

    result = _run_fix(root)

    assert (result.impact_plan is not None) == planned
    assert [(name, argv) for name, argv, _, _ in calls] == [
        (name, shlex.split(stage_command if name == stage else SHARED_COMMAND))
        for name in STAGE_PROMPTS
    ]


def test_fix_cli_ai_cmd_overrides_all_stage_commands(scripted_project):
    root, calls, _ = scripted_project({
        "ai_command": GLOBAL_COMMAND,
        "ai_commands": {"fix": SHARED_COMMAND, **dict.fromkeys(STAGE_PROMPTS, "stage-ai")},
    })

    result = CliRunner().invoke(main, [
        "fix", PHENOMENON, "--path", str(root), "--no-push", "--non-interactive",
        "--on-ambiguity", "top1", "--ai-cmd", 'cli-ai --label "cli model"',
    ])

    assert result.exit_code == 0, result.output
    assert [(stage, argv) for stage, argv, _, _ in calls] == [
        (stage, ["cli-ai", "--label", "cli model"]) for stage in STAGE_PROMPTS
    ]


@pytest.mark.parametrize("stage_settings", [{}, dict.fromkeys(STAGE_PROMPTS)], ids=["absent", "null"])
def test_unset_stages_share_the_legacy_fix_adapter(scripted_project, monkeypatch, stage_settings):
    root, calls, _ = scripted_project({
        "ai_command": GLOBAL_COMMAND,
        "ai_commands": {"fix": SHARED_COMMAND, **stage_settings},
    })
    resolve = Mock(wraps=phenomenon_fixer._resolve_ai_command)
    create_adapter = Mock(wraps=phenomenon_fixer.get_ai_command)
    monkeypatch.setattr(phenomenon_fixer, "_resolve_ai_command", resolve)
    monkeypatch.setattr(phenomenon_fixer, "get_ai_command", create_adapter)

    _run_fix(root)

    assert resolve.call_count == 1
    assert resolve.call_args.kwargs == {"command_name": "fix"}
    assert create_adapter.call_count == 1
    assert [stage for stage, _, _, _ in calls] == list(STAGE_PROMPTS)
    assert len({id(adapter) for _, _, _, adapter in calls}) == 1
    workspace = calls[0][2]
    assert workspace.name.startswith("codd-fix-ai-")
    assert create_adapter.call_args.kwargs == {
        "project_root": workspace, "command_override": SHARED_COMMAND,
    }
    assert [(argv, cwd) for _, argv, cwd, _ in calls] == [
        (["shared-ai", "--label", "shared model"], workspace) for _stage in STAGE_PROMPTS
    ]


@pytest.mark.parametrize(
    ("config", "command"),
    [
        pytest.param({"ai_command": GLOBAL_COMMAND}, GLOBAL_COMMAND, id="ai_command"),
        pytest.param({}, DEFAULT_AI_COMMAND, id="built_in"),
    ],
)
def test_fix_falls_back_to_global_then_builtin(scripted_project, config, command):
    root, calls, _ = scripted_project(config)

    _run_fix(root)

    assert [(stage, argv) for stage, argv, _, _ in calls] == [
        (stage, shlex.split(command)) for stage in STAGE_PROMPTS
    ]


def test_named_stage_uses_subprocess_alongside_shared_app_server(scripted_project, monkeypatch):
    root, subprocess_calls, reply = scripted_project({
        "ai_commands": {"fix_risk_classify": "risk-ai --model custom"},
        "codex_app_server": {"enabled": True, "transport": "stdio"},
    })
    app_server_calls = []

    class RecordingAppServer:
        def invoke(self, prompt):
            stage, response = reply(prompt)
            app_server_calls.append(stage)
            return response

    create_app_server = Mock(return_value=RecordingAppServer())
    monkeypatch.setattr(ai_command_factory.shutil, "which", lambda _binary: "/fake/ai-command")
    monkeypatch.setattr(CodexAppServerAiCommand, "from_config", create_app_server)

    _run_fix(root)

    assert create_app_server.call_count == 1
    assert app_server_calls == [stage for stage in STAGE_PROMPTS if stage != "fix_risk_classify"]
    assert [(stage, argv) for stage, argv, _, _ in subprocess_calls] == [
        ("fix_risk_classify", ["risk-ai", "--model", "custom"]),
    ]


def test_doctor_recognizes_all_fix_stage_command_keys(scripted_project):
    root, _, _ = scripted_project({
        "ai_commands": dict.fromkeys(STAGE_PROMPTS, "stage-ai"),
        "unknown_test_config_key": True,
    })

    result = CliRunner().invoke(main, ["doctor", "--path", str(root)])

    assert result.exit_code == 0, result.output
    assert "unknown config key 'unknown_test_config_key'" in result.output
    assert all(f"ai_commands.{stage}" not in result.output for stage in STAGE_PROMPTS)
