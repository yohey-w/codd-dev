from __future__ import annotations

import json
from pathlib import Path

import yaml
from click.testing import CliRunner

from codd.cli import main


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _doc_with_frontmatter(frontmatter: dict, body: str = "# Console\n") -> str:
    return yaml.safe_dump(frontmatter, explicit_start=True, sort_keys=False) + "---\n" + body


def _write_codd_yaml(project: Path) -> None:
    _write(
        project / "codd" / "codd.yaml",
        yaml.safe_dump(
            {
                "version": "0.1.0",
                "scan": {
                    "source_dirs": [],
                    "test_dirs": ["tests"],
                    "doc_dirs": ["docs"],
                    "config_files": [],
                },
                "graph": {"store": "jsonl", "path": "codd/scan"},
                "runtime": {
                    "action_outcome_targets": [
                        {
                            "name": "operate console",
                            "actor": "Operator",
                            "action": {
                                "id": "console.operate",
                                "verb": "operate",
                                "outcomes": ["visible_console"],
                            },
                            "command": "pytest tests/e2e/operate_console.spec.ts",
                        }
                    ]
                },
            },
            sort_keys=False,
        ),
    )


def _write_project(project: Path) -> None:
    _write_codd_yaml(project)
    _write(project / "docs" / "design" / "console.md", _doc_with_frontmatter({}))
    _write(project / "tests" / "e2e" / "operate_console.spec.ts", "test('operate console', () => {});\n")


def test_coverage_obligations_json_outputs_trace_summary_and_future_todos(tmp_path):
    _write_project(tmp_path)

    result = CliRunner().invoke(main, ["coverage-obligations", "--path", str(tmp_path), "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) >= {
        "summary",
        "trace_matrix",
        "obligations",
        "evidence_candidates",
        "unsupported_items",
        "generated_e2e_candidates",
        "selected_e2e_suite",
    }
    assert payload["summary"]["total_obligations"] >= 2
    assert payload["summary"]["coverage_status_counts"]["uncovered"] >= 1
    assert payload["evidence_candidates"][0]["ref"] == "tests/e2e/operate_console.spec.ts"
    assert payload["unsupported_items"]
    assert payload["generated_e2e_candidates"]["status"] == "future_todo"
    assert payload["generated_e2e_candidates"]["reason"] == "not_implemented_in_cmd_494"
    assert payload["selected_e2e_suite"]["status"] == "future_todo"
    assert payload["selected_e2e_suite"]["reason"] == "not_implemented_in_cmd_494"


def test_actor_without_journey_is_uncovered_in_json_trace(tmp_path):
    _write_project(tmp_path)

    result = CliRunner().invoke(main, ["coverage-obligations", "--path", str(tmp_path), "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    missing_journey = next(
        row
        for row in payload["trace_matrix"]
        if row["obligation_id"] == "obl:role_sequence:operator:missing_journey"
    )
    assert missing_journey["kind"] == "role_sequence"
    assert missing_journey["actor"] == "Operator"
    assert missing_journey["coverage_status"] == "uncovered"
    assert "codd.yaml#runtime.action_outcome_targets[0]" in missing_journey["source"]


def test_coverage_obligations_markdown_outputs_matrix_and_future_todos(tmp_path):
    _write_project(tmp_path)

    result = CliRunner().invoke(main, ["coverage-obligations", "--path", str(tmp_path), "--format", "markdown"])

    assert result.exit_code == 0, result.output
    assert "| obligation_id | kind | actor | coverage_status | source |" in result.output
    assert "| obl:role_sequence:operator:missing_journey | role_sequence | Operator | uncovered |" in result.output
    assert "unsupported_item_count:" in result.output
    assert "generated_e2e_candidates: future_todo (not_implemented_in_cmd_494)" in result.output
    assert "selected_e2e_suite: future_todo (not_implemented_in_cmd_494)" in result.output
