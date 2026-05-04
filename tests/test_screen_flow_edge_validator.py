"""Tests for codd validate --screen-flow --edges."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from codd.cli import main
from codd.coverage_metrics import check_edge_coverage_gate
from codd.screen_flow_validator import EdgeCoverageResult, validate_screen_flow_edges


def _write_edges(project_root, edges: list[dict[str, str]]) -> None:
    transitions_path = project_root / "docs" / "extracted" / "screen-transitions.yaml"
    transitions_path.parent.mkdir(parents=True)
    transitions_path.write_text(yaml.safe_dump({"edges": edges}, sort_keys=False), encoding="utf-8")


def _result(ratio: float, *, unreachable: list[str] | None = None) -> EdgeCoverageResult:
    return EdgeCoverageResult(
        total_edges=1,
        covered_nodes={"/covered"},
        orphan_nodes=[],
        dead_end_nodes=[],
        unreachable_nodes=unreachable or [],
        coverage_ratio=ratio,
    )


def test_validate_edges_no_transitions_yaml(tmp_path):
    result = validate_screen_flow_edges(tmp_path, ["/login", "/dashboard"])

    assert result.total_edges == 0
    assert result.coverage_ratio == 1.0
    assert result.unreachable_nodes == ["/login", "/dashboard"]


def test_validate_edges_full_coverage(tmp_path):
    _write_edges(
        tmp_path,
        [
            {"from": "/login", "to": "/dashboard"},
            {"from": "/dashboard", "to": "/settings"},
            {"from": "/settings", "to": "/login"},
        ],
    )

    result = validate_screen_flow_edges(tmp_path, ["/login", "/dashboard", "/settings"])

    assert result.coverage_ratio == 1.0
    assert result.orphan_nodes == []
    assert result.dead_end_nodes == []
    assert result.unreachable_nodes == []


def test_validate_edges_orphan_detection(tmp_path):
    _write_edges(tmp_path, [{"from": "/home", "to": "/profile"}])

    result = validate_screen_flow_edges(tmp_path, ["/home", "/profile"])

    assert result.orphan_nodes == ["/profile"]


def test_validate_edges_dead_end_detection(tmp_path):
    _write_edges(tmp_path, [{"from": "/start", "to": "/finish"}])

    result = validate_screen_flow_edges(tmp_path, ["/start", "/finish"])

    assert result.dead_end_nodes == ["/start"]


def test_validate_edges_unreachable_detection(tmp_path):
    _write_edges(tmp_path, [{"from": "/a", "to": "/b"}])

    result = validate_screen_flow_edges(tmp_path, ["/a", "/b", "/c"])

    assert result.coverage_ratio == pytest.approx(2 / 3)
    assert result.unreachable_nodes == ["/c"]


def test_check_edge_coverage_gate_pass():
    assert check_edge_coverage_gate(_result(0.75)) is True


def test_check_edge_coverage_gate_fail():
    assert check_edge_coverage_gate(_result(0.49)) is False


def test_check_edge_coverage_gate_codd_yaml_override():
    config = {"screen_flow": {"edge_coverage_threshold": 0.8}}

    assert check_edge_coverage_gate(_result(0.75), config) is False
    config["screen_flow"]["edge_coverage_threshold"] = 0.7
    assert check_edge_coverage_gate(_result(0.75), config) is True


def test_check_edge_coverage_gate_warns_on_unreachable():
    with pytest.warns(UserWarning, match="Screen-flow nodes not covered"):
        assert check_edge_coverage_gate(_result(1.0, unreachable=["/missing"])) is True


def test_generality_no_framework_hardcode():
    source = (Path(__file__).parents[1] / "codd" / "screen_flow_validator.py").read_text(encoding="utf-8")
    framework_re = re.compile(r"\b(next|nuxt|remix|sveltekit)\b", re.IGNORECASE)

    conditional_lines = [
        line
        for line in source.splitlines()
        if line.lstrip().startswith(("if ", "elif ")) and framework_re.search(line)
    ]
    assert conditional_lines == []


def test_edge_coverage_result_ratio_empty_flow(tmp_path):
    _write_edges(tmp_path, [{"from": "/a", "to": "/b"}])

    result = validate_screen_flow_edges(tmp_path, [])

    assert result.coverage_ratio == 1.0


def test_cli_validate_screen_flow_edges_passes_full_coverage(tmp_path):
    (tmp_path / "screen-flow.md").write_text("- /login\n- /dashboard\n", encoding="utf-8")
    _write_edges(
        tmp_path,
        [
            {"from": "/login", "to": "/dashboard"},
            {"from": "/dashboard", "to": "/login"},
        ],
    )

    result = CliRunner().invoke(main, ["validate", "--screen-flow", "--edges", "--path", str(tmp_path)])

    assert result.exit_code == 0
    assert "Screen-flow edge coverage: 100%" in result.output


def test_cli_validate_screen_flow_edges_fails_on_orphan(tmp_path):
    (tmp_path / "screen-flow.md").write_text("- /home\n- /profile\n", encoding="utf-8")
    _write_edges(tmp_path, [{"from": "/home", "to": "/profile"}])

    result = CliRunner().invoke(main, ["validate", "--screen-flow", "--edges", "--path", str(tmp_path)])

    assert result.exit_code == 1
    assert "Orphan nodes: /profile" in result.output
