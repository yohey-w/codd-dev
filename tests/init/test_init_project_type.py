"""``codd init --project-type`` — capability wiring at the natural home (FX2).

The 2026-06 real-AI greenfield dogfood built a CLI app, but because ``codd
init`` never recorded a project type the generation pipeline fell back to the
full web capability set and produced browser-test residue
(``test_web_surface_browser.py``) for an app with no browser. These tests pin
the wiring: ``--project-type`` is validated against the registry
(``codd.project_types``) and persisted to ``required_artifacts.project_type``
— the key the generator/implementer/artifact deriver already consult.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
import yaml

from codd.cli import main
from codd.generator import _resolve_generation_capabilities
from codd.config import load_project_config


def _init(tmp_path: Path, *extra: str):
    target = tmp_path / "proj"
    target.mkdir()
    result = CliRunner().invoke(
        main,
        ["init", "proj", "--language", "python", "--dest", str(target), "--no-suggest-lexicons", *extra],
    )
    return target, result


def test_init_without_project_type_keeps_legacy_config(tmp_path: Path) -> None:
    target, result = _init(tmp_path)
    assert result.exit_code == 0, result.output
    config = yaml.safe_load((target / "codd" / "codd.yaml").read_text(encoding="utf-8"))
    assert "required_artifacts" not in config
    # untyped projects keep the historical web fallback (backward compat)
    capabilities = _resolve_generation_capabilities(load_project_config(target), target)
    assert capabilities.e2e_modality == "browser"


def test_init_with_cli_project_type_records_it_and_resolves_cli_capabilities(tmp_path: Path) -> None:
    target, result = _init(tmp_path, "--project-type", "cli")
    assert result.exit_code == 0, result.output
    assert "Project type: cli" in result.output
    config = yaml.safe_load((target / "codd" / "codd.yaml").read_text(encoding="utf-8"))
    assert config["required_artifacts"]["project_type"] == "cli"
    # the recorded type drives capability resolution: no browser e2e for a CLI app
    capabilities = _resolve_generation_capabilities(load_project_config(target), target)
    assert capabilities.e2e_modality == "cli"
    assert capabilities.user_interface is False


def test_init_with_unknown_project_type_warns_and_falls_back_to_generic(tmp_path: Path) -> None:
    target, result = _init(tmp_path, "--project-type", "spaceship")
    assert result.exit_code == 0, result.output
    assert "Warning" in result.output and "spaceship" in result.output
    config = yaml.safe_load((target / "codd" / "codd.yaml").read_text(encoding="utf-8"))
    assert config["required_artifacts"]["project_type"] == "generic"


def test_init_project_type_preserves_template_comments(tmp_path: Path) -> None:
    target, result = _init(tmp_path, "--project-type", "cli")
    assert result.exit_code == 0, result.output
    text = (target / "codd" / "codd.yaml").read_text(encoding="utf-8")
    assert "# CoDD プロジェクト設定" in text  # template comments survive the append


def test_init_requirements_import_into_existing_project_records_project_type(tmp_path: Path) -> None:
    target, first = _init(tmp_path)
    assert first.exit_code == 0, first.output
    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n\nA CLI tool.\n", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        [
            "init",
            "proj",
            "--language",
            "python",
            "--dest",
            str(target),
            "--requirements",
            str(spec),
            "--project-type",
            "cli",
            "--no-suggest-lexicons",
        ],
    )

    assert result.exit_code == 0, result.output
    config = yaml.safe_load((target / "codd" / "codd.yaml").read_text(encoding="utf-8"))
    assert config["required_artifacts"]["project_type"] == "cli"


def test_init_project_type_does_not_double_write(tmp_path: Path) -> None:
    target, first = _init(tmp_path, "--project-type", "cli")
    assert first.exit_code == 0, first.output
    spec = tmp_path / "spec.md"
    spec.write_text("# Spec\n", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        [
            "init",
            "proj",
            "--language",
            "python",
            "--dest",
            str(target),
            "--requirements",
            str(spec),
            "--project-type",
            "web",
            "--no-suggest-lexicons",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "already set: cli" in result.output
    text = (target / "codd" / "codd.yaml").read_text(encoding="utf-8")
    assert text.count("required_artifacts:") == 1
    config = yaml.safe_load(text)
    assert config["required_artifacts"]["project_type"] == "cli"  # first writer wins
