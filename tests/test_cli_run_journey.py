from __future__ import annotations

import json
from pathlib import Path

import yaml
from click.testing import CliRunner

from codd.cli import main
from codd.deployment.providers import VerificationResult
import codd.deployment.providers.verification.cdp_browser as cdp_browser_module


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _write_project(
    project_root: Path,
    *,
    templates: dict | None = None,
    journeys: list[dict] | None = None,
) -> None:
    config: dict = {
        "project": {"type": "web"},
        "dag": {
            "design_doc_patterns": ["docs/design/*.md"],
            "impl_file_patterns": [],
            "test_file_patterns": [],
            "plan_task_file": "docs/design/implementation_plan.md",
            "lexicon_file": "project_lexicon.yaml",
        },
    }
    if templates is not None:
        config["verification"] = {"templates": templates}

    _write(project_root / "codd" / "codd.yaml", yaml.safe_dump(config, sort_keys=False))
    if journeys is not None:
        frontmatter = yaml.safe_dump({"user_journeys": journeys}, explicit_start=True, sort_keys=False)
        _write(project_root / "docs" / "design" / "flow.md", frontmatter + "---\n# Flow\n")


def _journey(name: str = "primary_flow", target: str = "/start") -> dict:
    return {
        "name": name,
        "criticality": "critical",
        "steps": [
            {"action": "navigate", "target": target},
            {"action": "expect_url", "contains": target},
        ],
        "required_capabilities": [],
        "expected_outcome_refs": [],
    }


def _patch_browser(monkeypatch, calls: list[dict], *, passed: bool = True, output: str = "journey ok") -> None:
    class FakeCdpBrowser:
        def __init__(self, config=None):
            self.config = config

        def execute(self, command: str) -> VerificationResult:
            calls.append({"config": self.config, "command": json.loads(command)})
            return VerificationResult(passed, output)

    monkeypatch.setattr(cdp_browser_module, "CdpBrowser", FakeCdpBrowser)


def test_run_journey_executes_named_journey(tmp_path, monkeypatch):
    calls: list[dict] = []
    _patch_browser(monkeypatch, calls)
    _write_project(
        tmp_path,
        templates={"cdp_browser": {"browser": {"engine": "mock"}}},
        journeys=[_journey("secondary_flow", "/other"), _journey("primary_flow", "/start")],
    )

    result = CliRunner().invoke(main, ["dag", "run-journey", "primary_flow", "--project-path", str(tmp_path)])

    assert result.exit_code == 0
    assert "journey ok" in result.output
    assert calls[0]["config"] == {"browser": {"engine": "mock"}}
    assert calls[0]["command"]["journey"] == "primary_flow"
    assert calls[0]["command"]["target"] == "/start"
    assert calls[0]["command"]["steps"] == _journey("primary_flow", "/start")["steps"]


def test_run_journey_pass_exits_zero(tmp_path, monkeypatch):
    calls: list[dict] = []
    _patch_browser(monkeypatch, calls, passed=True, output="pass output")
    _write_project(
        tmp_path,
        templates={"cdp_browser": {"browser": {"engine": "mock"}}},
        journeys=[_journey()],
    )

    result = CliRunner().invoke(main, ["dag", "run-journey", "primary_flow", "--project-path", str(tmp_path)])

    assert result.exit_code == 0
    assert "pass output" in result.output


def test_run_journey_fail_exits_one(tmp_path, monkeypatch):
    calls: list[dict] = []
    _patch_browser(monkeypatch, calls, passed=False, output="fail output")
    _write_project(
        tmp_path,
        templates={"cdp_browser": {"browser": {"engine": "mock"}}},
        journeys=[_journey()],
    )

    result = CliRunner().invoke(main, ["dag", "run-journey", "primary_flow", "--project-path", str(tmp_path)])

    assert result.exit_code == 1
    assert "fail output" in result.output


def test_run_journey_unknown_name_exits_two(tmp_path, monkeypatch):
    calls: list[dict] = []
    _patch_browser(monkeypatch, calls)
    _write_project(
        tmp_path,
        templates={"cdp_browser": {"browser": {"engine": "mock"}}},
        journeys=[_journey("known_flow")],
    )

    result = CliRunner().invoke(main, ["dag", "run-journey", "missing_flow", "--project-path", str(tmp_path)])

    assert result.exit_code == 2
    assert "user_journey not found" in result.output
    assert calls == []


def test_run_journey_missing_cdp_browser_config_exits_two(tmp_path, monkeypatch):
    calls: list[dict] = []
    _patch_browser(monkeypatch, calls)
    _write_project(tmp_path, templates={}, journeys=[_journey()])

    result = CliRunner().invoke(main, ["dag", "run-journey", "primary_flow", "--project-path", str(tmp_path)])

    assert result.exit_code == 2
    assert "verification.templates.cdp_browser config not found" in result.output
    assert calls == []


def test_run_journey_config_section_selects_named_template(tmp_path, monkeypatch):
    calls: list[dict] = []
    _patch_browser(monkeypatch, calls)
    _write_project(
        tmp_path,
        templates={
            "cdp_browser": {"browser": {"engine": "default"}},
            "alternate": {"browser": {"engine": "selected"}},
        },
        journeys=[_journey()],
    )

    result = CliRunner().invoke(
        main,
        [
            "dag",
            "run-journey",
            "primary_flow",
            "--project-path",
            str(tmp_path),
            "--config-section",
            "alternate",
        ],
    )

    assert result.exit_code == 0
    assert calls[0]["config"] == {"browser": {"engine": "selected"}}
    assert calls[0]["command"]["config"] == {"browser": {"engine": "selected"}}


def test_run_journey_missing_named_config_section_exits_two(tmp_path, monkeypatch):
    calls: list[dict] = []
    _patch_browser(monkeypatch, calls)
    _write_project(
        tmp_path,
        templates={"cdp_browser": {"browser": {"engine": "mock"}}},
        journeys=[_journey()],
    )

    result = CliRunner().invoke(
        main,
        [
            "dag",
            "run-journey",
            "primary_flow",
            "--project-path",
            str(tmp_path),
            "--config-section",
            "missing",
        ],
    )

    assert result.exit_code == 2
    assert "verification.templates.missing config not found" in result.output
    assert calls == []


def test_run_journey_path_alias_works(tmp_path, monkeypatch):
    calls: list[dict] = []
    _patch_browser(monkeypatch, calls)
    _write_project(
        tmp_path,
        templates={"cdp_browser": {"browser": {"engine": "mock"}}},
        journeys=[_journey()],
    )

    result = CliRunner().invoke(main, ["dag", "run-journey", "primary_flow", "--path", str(tmp_path)])

    assert result.exit_code == 0
    assert calls[0]["command"]["project_root"] == str(tmp_path.resolve())
