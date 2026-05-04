from __future__ import annotations

import yaml
from click.testing import CliRunner

from codd.cli import main
from codd.routes_extractor import generate_mermaid_screen_flow


ROUTE_CONFIGS = [
    {
        "base_dir": "app/",
        "page_pattern": "page.{tsx,jsx}",
        "api_pattern": "route.{ts,js}",
        "url_template": "/{relative_dir}",
        "dynamic_segment": {"from": r"\[(.+)\]", "to": r":$1"},
        "ignore_segment": [r"\(.*\)"],
    }
]


def _write_route_fixture(project):
    files = [
        "app/page.tsx",
        "app/central-admin/page.tsx",
        "app/central-admin/courses/page.tsx",
        "app/tenant-admin/page.tsx",
        "app/learner/page.tsx",
        "app/api/health/route.ts",
    ]
    for file_name in files:
        path = project / file_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("export default function Page() { return null }\n", encoding="utf-8")


def _write_codd_config(project):
    codd_dir = project / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump({"filesystem_routes": ROUTE_CONFIGS}, sort_keys=False),
        encoding="utf-8",
    )


def test_mermaid_output_contains_role_subgraphs(tmp_path):
    _write_route_fixture(tmp_path)

    result = generate_mermaid_screen_flow(tmp_path, ROUTE_CONFIGS)

    assert "graph LR" in result.mermaid
    assert 'subgraph central_admin["Central Admin"]' in result.mermaid
    assert 'subgraph api["API"]' in result.mermaid
    assert '"/central-admin/courses"' in result.mermaid
    assert '"/api/health"' in result.mermaid


def test_route_count_matches_fixture(tmp_path):
    _write_route_fixture(tmp_path)

    result = generate_mermaid_screen_flow(tmp_path, ROUTE_CONFIGS)

    assert result.route_count == 6


def test_dynamic_segment_is_rendered_as_param(tmp_path):
    _write_route_fixture(tmp_path)
    dynamic_page = tmp_path / "app" / "learner" / "[id]" / "page.tsx"
    dynamic_page.parent.mkdir(parents=True, exist_ok=True)
    dynamic_page.write_text("export default function Page() { return null }\n", encoding="utf-8")

    result = generate_mermaid_screen_flow(tmp_path, ROUTE_CONFIGS)

    assert '"/learner/:id"' in result.mermaid


def test_empty_route_configs_returns_minimal_graph(tmp_path):
    result = generate_mermaid_screen_flow(tmp_path, [])

    assert result.route_count == 0
    assert result.mermaid == "graph LR"


def test_extract_routes_cli_help_and_output_file(tmp_path):
    _write_route_fixture(tmp_path)
    _write_codd_config(tmp_path)
    output_file = tmp_path / "docs" / "extracted" / "screen-flow.md"
    runner = CliRunner()

    help_result = runner.invoke(main, ["extract", "--layer", "routes", "--help"])
    result = runner.invoke(
        main,
        [
            "extract",
            "--path",
            str(tmp_path),
            "--layer",
            "routes",
            "--format",
            "mermaid",
            "--output-file",
            str(output_file),
        ],
    )

    assert help_result.exit_code == 0
    assert "--layer" in help_result.output
    assert result.exit_code == 0
    assert "Extracted 6 routes" in result.output
    assert output_file.read_text(encoding="utf-8").startswith("```mermaid\ngraph LR\n")
