from pathlib import Path

import yaml
from click.testing import CliRunner

from codd.ask_user_question_adapter import _severity_at_or_above
from codd.cli import main
from codd.preflight import PreflightAuditor, PreflightCheck, PreflightResult


def _valid_task(**overrides):
    task = {
        "task_id": "subtask_preflight",
        "parent_cmd": "cmd_377",
        "purpose": "Implement a small CLI feature.",
        "acceptance_criteria": ["unit tests pass"],
        "project": "demo",
        "bloom_level": "L3",
        "rollback_strategy": "Revert the commit.",
        "danger_signals": ["test_fail"],
    }
    task.update(overrides)
    return task


def _write_task(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def _auditor(tmp_path: Path) -> PreflightAuditor:
    (tmp_path / "project_lexicon.yaml").write_text("node_vocabulary: []\n", encoding="utf-8")
    return PreflightAuditor(project_root=tmp_path)


def test_preflight_check_dataclass_attributes():
    check = PreflightCheck("goal_clarity", "PASS", "low", "OK", ["detail"])

    assert check.name == "goal_clarity"
    assert check.status == "PASS"
    assert check.severity == "low"
    assert check.details == ["detail"]


def test_preflight_result_dataclass_attributes():
    check = PreflightCheck("goal_clarity", "PASS", "low", "OK")
    result = PreflightResult("task-1", [check], "low")

    assert result.task_id == "task-1"
    assert result.checks == [check]
    assert result.severity == "low"
    assert result.to_dict()["checks"][0]["name"] == "goal_clarity"


def test_check_goal_clarity_all_fields_pass(tmp_path):
    check = _auditor(tmp_path).check_goal_clarity(_valid_task())

    assert check.status == "PASS"


def test_check_goal_clarity_missing_task_id_is_critical(tmp_path):
    task = _valid_task(task_id="")

    check = _auditor(tmp_path).check_goal_clarity(task)

    assert check.status == "FAIL"
    assert check.severity == "critical"


def test_check_goal_clarity_missing_description_is_high(tmp_path):
    task = _valid_task()
    task.pop("purpose")

    check = _auditor(tmp_path).check_goal_clarity(task)

    assert check.status == "FAIL"
    assert check.severity == "high"


def test_check_goal_clarity_missing_acceptance_criteria_warns_high(tmp_path):
    task = _valid_task(acceptance_criteria=[])

    check = _auditor(tmp_path).check_goal_clarity(task)

    assert check.status == "WARN"
    assert check.severity == "high"


def test_check_rollback_criteria_with_strategy_and_danger_passes(tmp_path):
    check = _auditor(tmp_path).check_rollback_criteria(_valid_task())

    assert check.status == "PASS"


def test_check_rollback_criteria_missing_for_deploy_is_critical(tmp_path):
    task = _valid_task(
        description="Deploy to production",
        rollback_strategy="",
    )

    check = _auditor(tmp_path).check_rollback_criteria(task)

    assert check.status == "FAIL"
    assert check.severity == "critical"
    assert "deploy_to_production" in check.details[-1]


def test_check_rollback_criteria_missing_for_normal_task_is_high(tmp_path):
    task = _valid_task(rollback_strategy="")

    check = _auditor(tmp_path).check_rollback_criteria(task)

    assert check.status == "FAIL"
    assert check.severity == "high"


def test_check_rollback_criteria_defaults_documentation_is_not_critical(tmp_path):
    task = _valid_task(
        description="Add defaults that include pypi_publish and major_version_bump.",
        rollback_strategy="",
    )

    check = _auditor(tmp_path).check_rollback_criteria(task)

    assert check.status == "FAIL"
    assert check.severity == "high"


def test_check_rollback_criteria_structured_operation_is_critical(tmp_path):
    task = _valid_task(operation="deploy_to_production", rollback_strategy="")

    check = _auditor(tmp_path).check_rollback_criteria(task)

    assert check.status == "FAIL"
    assert check.severity == "critical"


def test_check_rollback_criteria_missing_danger_signals_warns_medium(tmp_path):
    task = _valid_task(danger_signals=[])

    check = _auditor(tmp_path).check_rollback_criteria(task)

    assert check.status == "WARN"
    assert check.severity == "medium"


def test_check_context_completeness_missing_project_warns_high(tmp_path):
    task = _valid_task(project="")

    check = _auditor(tmp_path).check_context_completeness(task)

    assert check.status == "WARN"
    assert check.severity == "high"


def test_check_context_completeness_project_without_lexicon_warns_high(tmp_path):
    check = PreflightAuditor(project_root=tmp_path).check_context_completeness(_valid_task())

    assert check.status == "WARN"
    assert check.severity == "high"


def test_check_judgment_materials_with_bloom_level_passes(tmp_path):
    check = _auditor(tmp_path).check_judgment_materials(_valid_task())

    assert check.status == "PASS"


def test_check_judgment_materials_missing_bloom_level_warns_medium(tmp_path):
    check = _auditor(tmp_path).check_judgment_materials(_valid_task(bloom_level=""))

    assert check.status == "WARN"
    assert check.severity == "medium"


def test_classify_severity_critical_wins(tmp_path):
    checks = [
        PreflightCheck("a", "WARN", "medium", "warn"),
        PreflightCheck("b", "FAIL", "critical", "fail"),
    ]

    assert _auditor(tmp_path).classify_severity(checks) == "critical"


def test_classify_severity_high_only(tmp_path):
    checks = [PreflightCheck("a", "WARN", "high", "warn")]

    assert _auditor(tmp_path).classify_severity(checks) == "high"


def test_classify_severity_all_pass_is_low(tmp_path):
    checks = [PreflightCheck("a", "PASS", "critical", "ignored")]

    assert _auditor(tmp_path).classify_severity(checks) == "low"


def test_run_reads_task_yaml(tmp_path):
    task_path = _write_task(tmp_path, _valid_task())
    result = _auditor(tmp_path).run(task_path)

    assert result.task_id == "subtask_preflight"
    assert isinstance(result, PreflightResult)
    assert result.severity == "low"


def test_severity_at_or_above_critical_threshold():
    assert _severity_at_or_above("critical", "critical") is True


def test_severity_at_or_above_high_below_critical():
    assert _severity_at_or_above("high", "critical") is False


def test_critical_operations_include_cli_defaults(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    auditor = PreflightAuditor(project_root=tmp_path)

    assert "pypi_publish" in auditor.critical_operations()


def test_cli_preflight_success(tmp_path):
    task_path = _write_task(tmp_path, _valid_task())
    (tmp_path / "project_lexicon.yaml").write_text("node_vocabulary: []\n", encoding="utf-8")

    result = CliRunner().invoke(main, ["preflight", str(task_path), "--path", str(tmp_path)])

    assert result.exit_code == 0
    assert "Overall severity: low" in result.output


def test_cli_gungi_alias_success(tmp_path):
    task_path = _write_task(tmp_path, _valid_task())
    (tmp_path / "project_lexicon.yaml").write_text("node_vocabulary: []\n", encoding="utf-8")

    result = CliRunner().invoke(main, ["gungi", str(task_path), "--path", str(tmp_path)])

    assert result.exit_code == 0
    assert "[PASS] rollback_criteria" in result.output


def test_cli_preflight_critical_exits_one(tmp_path):
    task_path = _write_task(
        tmp_path,
        _valid_task(description="Deploy to production", rollback_strategy=""),
    )
    (tmp_path / "project_lexicon.yaml").write_text("node_vocabulary: []\n", encoding="utf-8")

    result = CliRunner().invoke(main, ["preflight", str(task_path), "--path", str(tmp_path)])

    assert result.exit_code == 1
    assert "HALT recommended" in result.output
