from __future__ import annotations

from pathlib import Path
import shutil

from click.testing import CliRunner
import yaml

from codd.cli import main
from codd.init.lexicon_suggest import (
    append_suggested_lexicons,
    describe_lexicons,
    load_stack_map,
    suggest_lexicons,
)
from codd.lexicon import validate_lexicon


REPO_ROOT = Path(__file__).parents[2]
FIXTURE = Path(__file__).parent / "fixtures" / "sample_react_fastapi_prisma"


def test_stack_map_loads_entries() -> None:
    entries = load_stack_map(REPO_ROOT / "codd_plugins" / "stack_map.yaml")

    assert entries
    assert all(entry.hint_pattern for entry in entries)
    assert all(entry.suggested_lexicons for entry in entries)


def test_stack_map_covers_all_bundled_lexicons() -> None:
    entries = load_stack_map(REPO_ROOT / "codd_plugins" / "stack_map.yaml")
    mapped = {lexicon for entry in entries for lexicon in entry.suggested_lexicons}
    bundled = {
        path.name
        for path in (REPO_ROOT / "codd_plugins" / "lexicons").iterdir()
        if path.is_dir()
    }

    assert bundled - mapped == set()


def test_suggest_lexicons_aggregates_and_deduplicates() -> None:
    entries = load_stack_map(REPO_ROOT / "codd_plugins" / "stack_map.yaml")

    suggestions = suggest_lexicons(["react", "fastapi", "prisma"], entries)

    assert suggestions.count("web_security_owasp") == 1
    assert {
        "web_responsive",
        "web_a11y_wcag22_aa",
        "web_performance_core_web_vitals",
        "api_rest_openapi",
        "web_security_owasp",
        "data_relational_iso_sql",
    }.issubset(suggestions)


def test_describe_lexicons_reads_manifest_descriptions() -> None:
    descriptions = describe_lexicons(["api_rest_openapi"], REPO_ROOT / "codd_plugins" / "lexicons")

    assert "REST" in descriptions["api_rest_openapi"]


def test_append_suggested_lexicons_creates_valid_project_lexicon(tmp_path: Path) -> None:
    path = append_suggested_lexicons(tmp_path, ["web_responsive", "api_rest_openapi"])

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    validate_lexicon(data)
    assert data["suggested_lexicons"] == ["web_responsive", "api_rest_openapi"]


def test_append_suggested_lexicons_deduplicates_existing_file(tmp_path: Path) -> None:
    (tmp_path / "project_lexicon.yaml").write_text(
        yaml.safe_dump(
            {
                "node_vocabulary": [],
                "naming_conventions": [],
                "design_principles": [],
                "suggested_lexicons": ["web_responsive"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    append_suggested_lexicons(tmp_path, ["web_responsive", "api_rest_openapi"])
    data = yaml.safe_load((tmp_path / "project_lexicon.yaml").read_text(encoding="utf-8"))

    assert data["suggested_lexicons"] == ["web_responsive", "api_rest_openapi"]


def test_codd_init_suggest_lexicons_updates_project_lexicon(tmp_path: Path) -> None:
    project = tmp_path / "sample"
    shutil.copytree(FIXTURE, project)

    result = CliRunner().invoke(
        main,
        [
            "init",
            "--project-name",
            "Sample",
            "--language",
            "python",
            "--dest",
            str(project),
            "--suggest-lexicons",
        ],
        input="\n",
    )

    assert result.exit_code == 0, result.output
    assert "Detected signals: package.json, requirements.txt" in result.output
    assert "Detected hints: react, prisma, fastapi" in result.output
    data = yaml.safe_load((project / "project_lexicon.yaml").read_text(encoding="utf-8"))
    assert {
        "web_responsive",
        "api_rest_openapi",
        "data_relational_iso_sql",
    }.issubset(set(data["suggested_lexicons"]))


def test_codd_init_no_suggest_lexicons_preserves_existing_flow(tmp_path: Path) -> None:
    project = tmp_path / "sample"
    shutil.copytree(FIXTURE, project)

    result = CliRunner().invoke(
        main,
        [
            "init",
            "--project-name",
            "Sample",
            "--language",
            "python",
            "--dest",
            str(project),
            "--no-suggest-lexicons",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Suggested lexicons:" not in result.output
    assert not (project / "project_lexicon.yaml").exists()

