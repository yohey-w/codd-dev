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


def _write_delegated_codd_yaml(project: Path) -> None:
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
                            "name": "audit event",
                            "actor": "Operator",
                            "action": {
                                "id": "event.audit",
                                "verb": "record",
                                "outcomes": ["audit_event_visible"],
                            },
                            "coverage_status": "covered_by_lower_test",
                            "covered_by": [
                                {"type": "api_test", "ref": "tests/api/test_audit_event.py"}
                            ],
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


def test_coverage_obligations_json_outputs_candidates_and_selected_suite(tmp_path):
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
        "unselected_e2e_candidates",
        "excluded_obligations",
        "required_without_selected_candidate_ids",
    }
    assert payload["summary"]["total_obligations"] >= 2
    assert payload["summary"]["coverage_status_counts"]["uncovered"] >= 1
    assert payload["summary"]["generated_e2e_candidate_count"] >= 1
    assert payload["summary"]["selected_e2e_suite_count"] >= 1
    assert "uncovered_required_obligation_count" not in payload["summary"]
    assert payload["evidence_candidates"][0]["ref"] == "tests/e2e/operate_console.spec.ts"
    assert payload["unsupported_items"]
    assert isinstance(payload["generated_e2e_candidates"], list)
    assert isinstance(payload["selected_e2e_suite"], list)
    assert payload["generated_e2e_candidates"][0]["candidate_id"].startswith("candidate:e2e:")
    assert payload["selected_e2e_suite"][0]["status"] == "selected"
    assert "not_implemented_in_cmd_494" not in result.output


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
    assert missing_journey["generated_candidate_ids"] == [
        "candidate:e2e:obl_role_sequence_operator_missing_journey"
    ]
    assert missing_journey["selected_candidate_ids"] == [
        "candidate:e2e:obl_role_sequence_operator_missing_journey"
    ]
    assert missing_journey["covered_by"] == []
    assert missing_journey["excluded_reason"] is None


def test_coverage_obligations_markdown_outputs_candidate_and_selected_tables(tmp_path):
    _write_project(tmp_path)

    result = CliRunner().invoke(main, ["coverage-obligations", "--path", str(tmp_path), "--format", "markdown"])

    assert result.exit_code == 0, result.output
    assert "| obligation_id | kind | actor | coverage_status | source | generated_candidate_ids | selected_candidate_ids | excluded_reason |" in result.output
    assert "| obl:role_sequence:operator:missing_journey | role_sequence | Operator | uncovered |" in result.output
    assert "unsupported_item_count:" in result.output
    assert "## Generated E2E Candidates" in result.output
    assert "## Selected E2E Suite" in result.output
    assert "selected_e2e_suite_is_planning_only: true" in result.output
    assert "Selected E2E suite entries are planning artifacts only" in result.output
    assert "not_implemented_in_cmd_494" not in result.output


def test_doctor_outputs_coverage_obligation_planning_summary(tmp_path):
    _write_project(tmp_path)

    result = CliRunner().invoke(main, ["doctor", "--path", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Coverage obligations:" in result.output
    assert "generated E2E candidates" in result.output
    assert "selected planning entries" in result.output
    assert "selected E2E suite is planning-only" in result.output


def test_verify_outputs_coverage_obligation_planning_summary(tmp_path):
    _write_project(tmp_path)

    result = CliRunner().invoke(main, ["verify", "--path", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "[VERIFY SUMMARY]" in result.output
    assert "Coverage obligations:" in result.output
    assert "selected E2E suite is planning-only" in result.output


def test_coverage_obligations_json_excludes_valid_lower_level_delegation(tmp_path):
    _write_delegated_codd_yaml(tmp_path)

    result = CliRunner().invoke(main, ["coverage-obligations", "--path", str(tmp_path), "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    delegated = next(
        row
        for row in payload["trace_matrix"]
        if row["obligation_id"] == "obl:action_outcome:operator:event_audit"
    )
    assert delegated["coverage_status"] == "covered_by_lower_test"
    assert delegated["covered_by"] == [{"type": "api_test", "ref": "tests/api/test_audit_event.py"}]
    assert delegated["generated_candidate_ids"] == []
    assert delegated["selected_candidate_ids"] == []
    assert delegated["excluded_reason"] == "delegated_to_lower_test"
    assert payload["excluded_obligations"][0]["reason_code"] == "delegated_to_lower_test"
