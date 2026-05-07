"""Tests for codd extract --init frontmatter metadata."""

from pathlib import Path

import yaml
from click.testing import CliRunner

from codd.cli import main
from codd.extractor import add_extract_init_frontmatter


def _write_minimal_project(project: Path) -> None:
    src = project / "src"
    (src / "feature").mkdir(parents=True)
    (src / "feature" / "__init__.py").write_text("", encoding="utf-8")
    (src / "feature" / "service.py").write_text(
        "class FeatureService:\n"
        "    def run(self):\n"
        "        return True\n",
        encoding="utf-8",
    )


def _read_markdown_codd(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    frontmatter = text.split("---", 2)[1]
    return yaml.safe_load(frontmatter)["codd"]


def test_extract_init_adds_metadata_to_system_context(tmp_path):
    _write_minimal_project(tmp_path)

    result = CliRunner().invoke(
        main,
        ["extract", "--init", "--path", str(tmp_path), "--language", "python", "--source-dirs", "src"],
    )

    codd = _read_markdown_codd(tmp_path / ".codd" / "extract" / "system-context.md")
    assert result.exit_code == 0, result.output
    assert codd["version"] == "1.0"
    assert "T" in codd["extracted_at"]
    assert codd["source"] == tmp_path.resolve().as_posix()


def test_extract_init_preserves_design_metadata(tmp_path):
    _write_minimal_project(tmp_path)

    result = CliRunner().invoke(
        main,
        ["extract", "--init", "--path", str(tmp_path), "--language", "python", "--source-dirs", "src"],
    )

    codd = _read_markdown_codd(tmp_path / ".codd" / "extract" / "system-context.md")
    assert result.exit_code == 0, result.output
    assert codd["node_id"] == "design:extract:system-context"
    assert codd["type"] == "design"
    assert codd["confidence"] == 0.65


def test_extract_init_adds_metadata_to_module_doc(tmp_path):
    _write_minimal_project(tmp_path)

    result = CliRunner().invoke(
        main,
        ["extract", "--init", "--path", str(tmp_path), "--language", "python", "--source-dirs", "src"],
    )

    codd = _read_markdown_codd(tmp_path / ".codd" / "extract" / "modules" / "feature.md")
    assert result.exit_code == 0, result.output
    assert codd["version"] == "1.0"
    assert codd["source"] == tmp_path.resolve().as_posix()
    assert codd["modules"] == ["feature"]


def test_extract_without_init_keeps_existing_extraction_source(tmp_path):
    _write_minimal_project(tmp_path)

    result = CliRunner().invoke(
        main,
        ["extract", "--path", str(tmp_path), "--language", "python", "--source-dirs", "src"],
    )

    codd = _read_markdown_codd(tmp_path / ".codd" / "extract" / "system-context.md")
    assert result.exit_code == 0, result.output
    assert codd["source"] == "extracted"
    assert "version" not in codd
    assert "extracted_at" not in codd


def test_extract_init_honors_custom_output(tmp_path):
    _write_minimal_project(tmp_path)
    output_dir = tmp_path / "brownfield"

    result = CliRunner().invoke(
        main,
        [
            "extract",
            "--init",
            "--path",
            str(tmp_path),
            "--language",
            "python",
            "--source-dirs",
            "src",
            "--output",
            str(output_dir),
        ],
    )

    codd = _read_markdown_codd(output_dir / "system-context.md")
    assert result.exit_code == 0, result.output
    assert codd["version"] == "1.0"
    assert codd["source"] == tmp_path.resolve().as_posix()


def test_frontmatter_helper_adds_codd_section_to_yaml(tmp_path):
    output = tmp_path / "extract_result.yaml"
    output.write_text("meta:\n  generated_at: now\n", encoding="utf-8")

    add_extract_init_frontmatter(
        [output],
        {"version": "1.0", "extracted_at": "2026-05-07T23:30:00+09:00", "source": "/repo"},
    )

    data = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert data["meta"]["generated_at"] == "now"
    assert data["codd"] == {
        "version": "1.0",
        "extracted_at": "2026-05-07T23:30:00+09:00",
        "source": "/repo",
    }


def test_frontmatter_helper_preserves_existing_yaml_codd_fields(tmp_path):
    output = tmp_path / "extract_result.yaml"
    output.write_text("codd:\n  node_id: design:extract\n", encoding="utf-8")

    add_extract_init_frontmatter(
        [output],
        {"version": "1.0", "extracted_at": "2026-05-07T23:30:00+09:00", "source": "/repo"},
    )

    codd = yaml.safe_load(output.read_text(encoding="utf-8"))["codd"]
    assert codd["node_id"] == "design:extract"
    assert codd["version"] == "1.0"
    assert codd["source"] == "/repo"


def test_frontmatter_helper_adds_markdown_frontmatter_when_missing(tmp_path):
    output = tmp_path / "notes.md"
    output.write_text("# Notes\n", encoding="utf-8")

    add_extract_init_frontmatter(
        [output],
        {"version": "1.0", "extracted_at": "2026-05-07T23:30:00+09:00", "source": "/repo"},
    )

    codd = _read_markdown_codd(output)
    assert codd["version"] == "1.0"
    assert codd["source"] == "/repo"
    assert output.read_text(encoding="utf-8").endswith("# Notes\n")
