"""Tests for dynamic wave_config generation from required_artifacts."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from click.testing import CliRunner
import pytest
import yaml

from codd.cli import main
from codd.planner import backup_codd_yaml, generate_wave_config_from_artifacts
import codd.required_artifacts_deriver as deriver_module


def _artifact(
    artifact_id: str,
    *,
    title: str | None = None,
    depends_on: list[str] | None = None,
    output: str | None = None,
) -> dict:
    artifact = {
        "id": artifact_id,
        "title": title or artifact_id.rsplit(":", 1)[-1].replace("_", " ").title(),
        "depends_on": depends_on or [],
        "scope": "Test scope",
        "rationale": "Test rationale",
        "source": "ai_derived",
    }
    if output:
        artifact["output"] = output
    return artifact


def _valid_lexicon() -> dict:
    return {
        "node_vocabulary": [],
        "naming_conventions": [],
        "design_principles": [],
        "coverage_decisions": [],
        "required_artifacts": [],
    }


def _write_project(project: Path, *, wave_config: dict | None = None) -> Path:
    codd_dir = project / "codd"
    codd_dir.mkdir()
    config: dict = {
        "version": "0.1.0",
        "project": {"name": "dynamic-wave-test", "language": "python"},
        "ai_command": "mock-ai --print",
    }
    if wave_config is not None:
        config["wave_config"] = deepcopy(wave_config)
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    (project / "project_lexicon.yaml").write_text(
        yaml.safe_dump(_valid_lexicon(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return codd_dir / "codd.yaml"


def _mock_deriver(monkeypatch: pytest.MonkeyPatch, artifacts: list[dict]) -> None:
    class FakeRequiredArtifactsDeriver:
        def __init__(self, project_root: Path, ai_command: str):
            self.project_root = project_root
            self.ai_command = ai_command

        def derive(self, requirement_docs: list[str], coverage_decisions: list) -> list[dict]:
            return deepcopy(artifacts)

    monkeypatch.setattr(deriver_module, "RequiredArtifactsDeriver", FakeRequiredArtifactsDeriver)


def test_generate_wave_config_from_artifacts_empty_returns_empty_config():
    assert generate_wave_config_from_artifacts([]) == {}


def test_generate_wave_config_from_artifacts_single_artifact_goes_to_wave_1():
    config = generate_wave_config_from_artifacts([_artifact("design:system_design")])

    assert list(config) == ["1"]
    assert config["1"][0]["node_id"] == "design:system_design"
    assert config["1"][0]["output"] == "docs/design/system_design.md"


def test_generate_wave_config_from_artifacts_chain_dependencies_create_sequential_waves():
    artifacts = [
        _artifact("design:requirements"),
        _artifact("design:system_design", depends_on=["design:requirements"]),
        _artifact("design:api_design", depends_on=["design:system_design"]),
    ]

    config = generate_wave_config_from_artifacts(artifacts)

    assert [config[str(wave)][0]["node_id"] for wave in (1, 2, 3)] == [
        "design:requirements",
        "design:system_design",
        "design:api_design",
    ]


def test_generate_wave_config_from_artifacts_parallel_dependencies_share_wave():
    artifacts = [
        _artifact("design:system_design"),
        _artifact("design:api_design", depends_on=["design:system_design"]),
        _artifact("design:database_design", depends_on=["design:system_design"]),
    ]

    config = generate_wave_config_from_artifacts(artifacts)

    assert config["1"][0]["node_id"] == "design:system_design"
    assert {entry["node_id"] for entry in config["2"]} == {
        "design:api_design",
        "design:database_design",
    }


def test_generate_wave_config_from_artifacts_preserves_existing_config_in_append_mode():
    existing = {
        "1": [
            {
                "node_id": "design:requirements",
                "output": "docs/design/requirements.md",
                "title": "Requirements",
                "custom": "keep-me",
            }
        ]
    }
    original = deepcopy(existing)

    config = generate_wave_config_from_artifacts(
        [_artifact("design:requirements")],
        existing_wave_config=existing,
    )

    assert config == original
    assert existing == original


def test_generate_wave_config_from_artifacts_does_not_add_duplicate_existing_artifact():
    existing = {
        "1": [
            {
                "node_id": "design:requirements",
                "output": "docs/design/requirements.md",
                "title": "Requirements",
            }
        ]
    }

    config = generate_wave_config_from_artifacts(
        [_artifact("design:requirements")],
        existing_wave_config=existing,
    )

    assert sum(len(entries) for entries in config.values()) == 1


def test_generate_wave_config_from_artifacts_appends_new_artifacts_after_existing_waves():
    existing = {
        "1": [{"node_id": "design:requirements", "output": "docs/design/requirements.md", "title": "Requirements"}],
        "2": [{"node_id": "design:system_design", "output": "docs/design/system_design.md", "title": "System"}],
    }

    config = generate_wave_config_from_artifacts(
        [
            _artifact("design:requirements"),
            _artifact("design:system_design", depends_on=["design:requirements"]),
            _artifact("design:screen_flow_design", depends_on=["design:system_design"]),
        ],
        existing_wave_config=existing,
    )

    assert config["3"][0]["node_id"] == "design:screen_flow_design"
    assert config["1"] == existing["1"]
    assert config["2"] == existing["2"]


def test_backup_codd_yaml_creates_bak_next_to_config(tmp_path):
    config_path = _write_project(tmp_path, wave_config={})
    config_path.write_text("project:\n  name: backup-test\n", encoding="utf-8")

    backup_path = backup_codd_yaml(tmp_path)

    assert backup_path == tmp_path / "codd" / "codd.yaml.bak"
    assert backup_path.read_text(encoding="utf-8") == "project:\n  name: backup-test\n"


def test_backup_codd_yaml_is_graceful_when_config_is_missing(tmp_path):
    backup_path = backup_codd_yaml(tmp_path)

    assert backup_path == tmp_path / "codd.yaml.bak"
    assert not backup_path.exists()


def test_cli_plan_derive_without_regenerate_preserves_wave_config(monkeypatch, tmp_path):
    existing = {
        "1": [{"node_id": "design:custom", "output": "docs/design/custom.md", "title": "Custom"}],
    }
    config_path = _write_project(tmp_path, wave_config=existing)
    original = config_path.read_text(encoding="utf-8")
    _mock_deriver(monkeypatch, [_artifact("design:system_design")])

    result = CliRunner().invoke(main, ["plan", "--path", str(tmp_path), "--derive"])

    assert result.exit_code == 0
    assert config_path.read_text(encoding="utf-8") == original
    assert not (tmp_path / "codd" / "codd.yaml.bak").exists()


def test_cli_plan_derive_with_regenerate_backs_up_and_rewrites_wave_config(monkeypatch, tmp_path):
    existing = {
        "1": [{"node_id": "design:custom", "output": "docs/design/custom.md", "title": "Custom"}],
    }
    config_path = _write_project(tmp_path, wave_config=existing)
    original = config_path.read_text(encoding="utf-8")
    _mock_deriver(
        monkeypatch,
        [
            _artifact("design:requirements"),
            _artifact("design:system_design", depends_on=["design:requirements"]),
        ],
    )

    result = CliRunner().invoke(
        main,
        ["plan", "--path", str(tmp_path), "--derive", "--regenerate-wave-config"],
    )

    assert result.exit_code == 0
    assert "Regenerating wave_config from required_artifacts..." in result.output
    assert (tmp_path / "codd" / "codd.yaml.bak").read_text(encoding="utf-8") == original
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["wave_config"]["1"][0]["node_id"] == "design:requirements"
    assert config["wave_config"]["2"][0]["node_id"] == "design:system_design"
    assert "design:custom" not in str(config["wave_config"])


def test_cli_regenerate_wave_config_requires_derive(tmp_path):
    _write_project(tmp_path, wave_config={})

    result = CliRunner().invoke(main, ["plan", "--path", str(tmp_path), "--regenerate-wave-config"])

    assert result.exit_code != 0
    assert "--regenerate-wave-config requires --derive" in result.output


def test_dynamic_wave_config_generation_has_no_framework_specific_core_logic():
    source = Path("codd/planner.py").read_text(encoding="utf-8")

    for framework_name in ("Next.js", "React", "Prisma", "Django", "Rails", "Nuxt", "SvelteKit"):
        assert framework_name not in source


def test_codd_yaml_wave_config_override_is_respected_by_default(monkeypatch, tmp_path):
    override = {
        "7": [{"node_id": "design:operator_override", "output": "docs/design/operator.md", "title": "Operator"}],
    }
    config_path = _write_project(tmp_path, wave_config=override)
    _mock_deriver(monkeypatch, [_artifact("design:operator_override"), _artifact("design:new")])

    result = CliRunner().invoke(main, ["plan", "--path", str(tmp_path), "--derive"])

    assert result.exit_code == 0
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["wave_config"] == override
