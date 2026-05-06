from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
import yaml

import codd.cli as cli_module
from codd.cli import main


def _write_project(path: Path, payload: dict | None = None) -> Path:
    project = path / "project"
    codd_dir = project / "codd"
    codd_dir.mkdir(parents=True)
    config = {
        "project": {"name": "demo", "language": "python"},
        "scan": {"source_dirs": ["src/"], "doc_dirs": ["docs/"], "config_files": [], "exclude": []},
    }
    if payload:
        config.update(payload)
    (codd_dir / "codd.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return project


def test_version_command_prints_installed_version(monkeypatch):
    monkeypatch.setattr(cli_module, "_installed_codd_version", lambda: "1.30.0")

    result = CliRunner().invoke(main, ["version"])

    assert result.exit_code == 0, result.output
    assert result.output == "codd 1.30.0\n"


def test_version_check_without_requirement_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "_installed_codd_version", lambda: "1.30.0")
    project = _write_project(tmp_path)

    result = CliRunner().invoke(main, ["version", "--check", "--path", str(project)])

    assert result.exit_code == 0, result.output
    assert "Version check: no codd_required_version configured" in result.output


def test_version_check_passes_matching_specifier(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "_installed_codd_version", lambda: "1.30.0")
    project = _write_project(tmp_path, {"codd_required_version": ">=1.30.0"})

    result = CliRunner().invoke(main, ["version", "--check", "--path", str(project)])

    assert result.exit_code == 0, result.output
    assert "Version check: PASS (requires >=1.30.0)" in result.output


def test_version_check_warns_but_exits_zero_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "_installed_codd_version", lambda: "1.9.3")
    project = _write_project(tmp_path, {"codd_required_version": ">=1.30.0"})

    result = CliRunner().invoke(main, ["version", "--check", "--path", str(project)])

    assert result.exit_code == 0, result.output
    assert "WARN: project requires codd >=1.30.0, installed 1.9.3" in result.output
    assert "Version check: FAIL (requires >=1.30.0)" in result.output


def test_version_check_strict_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "_installed_codd_version", lambda: "1.9.3")
    project = _write_project(tmp_path, {"codd_required_version": ">=1.30.0"})

    result = CliRunner().invoke(main, ["version", "--check", "--strict", "--path", str(project)])

    assert result.exit_code == 1
    assert "WARN: project requires codd >=1.30.0, installed 1.9.3" in result.output


def test_subcommand_startup_warns_from_project_config(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "_installed_codd_version", lambda: "1.9.3")
    project = _write_project(tmp_path, {"codd_required_version": ">=1.30.0"})
    monkeypatch.chdir(project)

    result = CliRunner().invoke(main, ["plan", "--waves"])

    assert result.exit_code == 0, result.output
    assert "WARN: project requires codd >=1.30.0, installed 1.9.3" in result.output


def test_subcommand_startup_respects_project_strict(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "_installed_codd_version", lambda: "1.9.3")
    project = _write_project(
        tmp_path,
        {
            "codd_required_version": ">=1.30.0",
            "codd_required_version_strict": True,
        },
    )
    monkeypatch.chdir(project)

    result = CliRunner().invoke(main, ["plan", "--waves"])

    assert result.exit_code == 1
    assert "WARN: project requires codd >=1.30.0, installed 1.9.3" in result.output
