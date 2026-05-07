from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
import yaml

from codd.cli import main
from codd.lexicon_cli.formatters.html import format_coverage_report_html
from codd.lexicon_cli.formatters.json_fmt import to_json
from codd.lexicon_cli.formatters.md import format_coverage_report_md, format_diff_md
from codd.lexicon_cli.inspector import LexiconInspector
from codd.lexicon_cli.reporter import CoverageReporter


REPO_ROOT = Path(__file__).parents[2]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons"


def _first_lexicon_id() -> str:
    return sorted(path.name for path in LEXICON_ROOT.iterdir() if (path / "manifest.yaml").is_file())[0]


def _project(tmp_path: Path, lexicon_id: str | None = None) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    if lexicon_id:
        (project / "project_lexicon.yaml").write_text(
            yaml.safe_dump(
                {
                    "node_vocabulary": [],
                    "naming_conventions": [],
                    "design_principles": [],
                    "extends": [lexicon_id],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
    (project / "requirements.md").write_text("coverage placeholder", encoding="utf-8")
    return project


def test_reporter_uses_installed_lexicons_for_all_when_present(tmp_path: Path) -> None:
    lexicon_id = _first_lexicon_id()
    project = _project(tmp_path, lexicon_id)

    report = CoverageReporter(project, LEXICON_ROOT).build("all")

    assert report.totals["lexicons"] == 1
    assert {row.lexicon_id for row in report.rows} == {lexicon_id}


def test_reporter_all_falls_back_to_available_without_project_lexicon(tmp_path: Path) -> None:
    project = _project(tmp_path)

    report = CoverageReporter(project, LEXICON_ROOT).build("all")

    assert report.totals["lexicons"] >= 1


def test_reporter_accepts_comma_separated_lexicons(tmp_path: Path) -> None:
    ids = sorted(path.name for path in LEXICON_ROOT.iterdir() if (path / "manifest.yaml").is_file())[:2]
    project = _project(tmp_path)

    report = CoverageReporter(project, LEXICON_ROOT).build(",".join(ids))

    assert {row.lexicon_id for row in report.rows}.issubset(set(ids))


def test_reporter_totals_count_unknown_status(tmp_path: Path) -> None:
    lexicon_id = _first_lexicon_id()
    project = _project(tmp_path, lexicon_id)

    report = CoverageReporter(project, LEXICON_ROOT).build("all")

    assert report.totals["axes"] == len(report.rows)
    assert report.totals["unknown"] >= 0


def test_json_formatter_serializes_report(tmp_path: Path) -> None:
    lexicon_id = _first_lexicon_id()
    report = CoverageReporter(_project(tmp_path, lexicon_id), LEXICON_ROOT).build("all")

    payload = json.loads(to_json(report))

    assert payload["totals"]["lexicons"] == 1


def test_markdown_formatters_render_expected_headers(tmp_path: Path) -> None:
    lexicon_id = _first_lexicon_id()
    project = _project(tmp_path, lexicon_id)
    diff = LexiconInspector(project, LEXICON_ROOT).inspect(lexicon_id)
    report = CoverageReporter(project, LEXICON_ROOT).build("all")

    assert "# Lexicon Diff:" in format_diff_md(diff)
    assert "# Coverage Matrix Report" in format_coverage_report_md(report)


def test_html_formatter_is_self_contained(tmp_path: Path) -> None:
    lexicon_id = _first_lexicon_id()
    report = CoverageReporter(_project(tmp_path, lexicon_id), LEXICON_ROOT).build("all")

    html = format_coverage_report_html(report)

    assert "<style>" in html
    assert "<table>" in html


def test_cli_lexicon_list_installed(tmp_path: Path) -> None:
    lexicon_id = _first_lexicon_id()
    project = _project(tmp_path, lexicon_id)

    result = CliRunner().invoke(main, ["lexicon", "list", "--installed", "--path", str(project)])

    assert result.exit_code == 0, result.output
    assert "Installed (1):" in result.output
    assert lexicon_id in result.output


def test_cli_lexicon_list_available(tmp_path: Path) -> None:
    project = _project(tmp_path)

    result = CliRunner().invoke(main, ["lexicon", "list", "--available", "--path", str(project)])

    assert result.exit_code == 0, result.output
    assert "Available (" in result.output


def test_cli_lexicon_list_rejects_conflicting_flags(tmp_path: Path) -> None:
    project = _project(tmp_path)

    result = CliRunner().invoke(
        main,
        ["lexicon", "list", "--installed", "--available", "--path", str(project)],
    )

    assert result.exit_code == 1
    assert "choose only one" in result.output


def test_cli_lexicon_install_updates_project_lexicon(tmp_path: Path) -> None:
    lexicon_id = _first_lexicon_id()
    project = _project(tmp_path)

    result = CliRunner().invoke(main, ["lexicon", "install", lexicon_id, "--path", str(project)])

    assert result.exit_code == 0, result.output
    data = yaml.safe_load((project / "project_lexicon.yaml").read_text(encoding="utf-8"))
    assert lexicon_id in data["extends"]


def test_cli_lexicon_diff_markdown(tmp_path: Path) -> None:
    lexicon_id = _first_lexicon_id()
    project = _project(tmp_path, lexicon_id)

    result = CliRunner().invoke(main, ["lexicon", "diff", lexicon_id, "--path", str(project)])

    assert result.exit_code == 0, result.output
    assert "# Lexicon Diff:" in result.output


def test_cli_lexicon_diff_json(tmp_path: Path) -> None:
    lexicon_id = _first_lexicon_id()
    project = _project(tmp_path, lexicon_id)

    result = CliRunner().invoke(
        main,
        ["lexicon", "diff", lexicon_id, "--path", str(project), "--format", "json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["lexicon_id"] == lexicon_id


def test_cli_coverage_report_markdown(tmp_path: Path) -> None:
    lexicon_id = _first_lexicon_id()
    project = _project(tmp_path, lexicon_id)

    result = CliRunner().invoke(main, ["coverage", "report", "--path", str(project), "--format", "md"])

    assert result.exit_code == 0, result.output
    assert "# Coverage Matrix Report" in result.output


def test_cli_coverage_report_json(tmp_path: Path) -> None:
    lexicon_id = _first_lexicon_id()
    project = _project(tmp_path, lexicon_id)

    result = CliRunner().invoke(main, ["coverage", "report", "--path", str(project), "--format", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["totals"]["lexicons"] == 1


def test_cli_coverage_report_writes_html_output(tmp_path: Path) -> None:
    lexicon_id = _first_lexicon_id()
    project = _project(tmp_path, lexicon_id)
    output = project / "coverage.html"

    result = CliRunner().invoke(
        main,
        ["coverage", "report", "--path", str(project), "--format", "html", "--output", str(output)],
    )

    assert result.exit_code == 0, result.output
    assert output.exists()
    assert "<table>" in output.read_text(encoding="utf-8")
