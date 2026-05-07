"""Tests for codd extract output isolation."""

from pathlib import Path

import yaml
from click.testing import CliRunner

from codd.cli import main
from codd.extractor import run_extract


def _write_minimal_project(project: Path) -> None:
    src = project / "src"
    src.mkdir(parents=True)
    (src / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")


def test_run_extract_defaults_to_hidden_extract_dir(tmp_path):
    _write_minimal_project(tmp_path)

    result = run_extract(tmp_path, "python", ["src"])

    assert result.output_dir == tmp_path / ".codd" / "extract"
    assert (tmp_path / ".codd" / "extract" / "system-context.md").exists()
    assert not (tmp_path / "codd").exists()


def test_cli_default_output_does_not_create_target_codd_dir(tmp_path):
    _write_minimal_project(tmp_path)

    result = CliRunner().invoke(
        main,
        ["extract", "--path", str(tmp_path), "--language", "python", "--source-dirs", "src"],
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / ".codd" / "extract" / "system-context.md").exists()
    assert (tmp_path / ".codd" / "codd.yaml").exists()
    assert not (tmp_path / "codd").exists()
    assert "Output: .codd/extract/" in result.output


def test_cli_uses_hidden_output_when_target_has_codd_source_package(tmp_path):
    _write_minimal_project(tmp_path)
    package = tmp_path / "codd"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        ["extract", "--path", str(tmp_path), "--language", "python", "--source-dirs", "src"],
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / ".codd" / "extract" / "system-context.md").exists()
    assert (tmp_path / "codd" / "__init__.py").exists()
    assert not (tmp_path / "codd" / "extracted").exists()


def test_cli_allows_explicit_output_inside_target_tree(tmp_path):
    _write_minimal_project(tmp_path)
    custom_output = tmp_path / "docs" / "generated"

    result = CliRunner().invoke(
        main,
        [
            "extract",
            "--path",
            str(tmp_path),
            "--language",
            "python",
            "--source-dirs",
            "src",
            "--output",
            str(custom_output),
        ],
    )

    assert result.exit_code == 0, result.output
    assert (custom_output / "system-context.md").exists()
    assert "Output: docs/generated/" in result.output


def test_cli_resolves_relative_output_under_target_project(tmp_path):
    _write_minimal_project(tmp_path)

    result = CliRunner().invoke(
        main,
        [
            "extract",
            "--path",
            str(tmp_path),
            "--language",
            "python",
            "--source-dirs",
            "src",
            "--output",
            "docs/generated",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "docs" / "generated" / "system-context.md").exists()
    assert "Output: docs/generated/" in result.output


def test_cli_default_config_uses_hidden_graph_path(tmp_path):
    _write_minimal_project(tmp_path)

    result = CliRunner().invoke(
        main,
        ["extract", "--path", str(tmp_path), "--language", "python", "--source-dirs", "src"],
    )

    config = yaml.safe_load((tmp_path / ".codd" / "codd.yaml").read_text(encoding="utf-8"))
    assert result.exit_code == 0, result.output
    assert config["graph"]["path"] == ".codd/scan"
