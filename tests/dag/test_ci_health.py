from __future__ import annotations

from pathlib import Path

from codd.dag.checks.ci_health import CiHealthCheck


def _run(project_root: Path, ci: dict):
    return CiHealthCheck().run(project_root=project_root, settings={"ci": ci})


def _write_workflow(project_root: Path, text: str) -> None:
    workflow_dir = project_root / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "ci.yml").write_text(text, encoding="utf-8")


def test_c8_skip_when_provider_none(tmp_path: Path) -> None:
    result = _run(tmp_path, {"provider": "none"})

    assert result.status == "skip"
    assert result.passed is True


def test_c8_red_when_workflow_missing(tmp_path: Path) -> None:
    result = _run(tmp_path, {"provider": "github_actions"})

    assert result.status == "fail"
    assert result.severity == "red"
    assert result.block_deploy is True
    assert result.findings[0].violation_type == "ci_workflow_missing"


def test_c8_amber_when_trigger_incomplete(tmp_path: Path) -> None:
    _write_workflow(tmp_path, "name: ci\non: [push]\njobs:\n  test:\n    steps:\n      - run: pytest\n")

    result = _run(tmp_path, {"provider": "github_actions"})

    assert result.status == "warn"
    assert result.severity == "amber"
    assert result.findings[0].violation_type == "ci_trigger_incomplete"


def test_c8_pass_when_workflow_correct(tmp_path: Path) -> None:
    _write_workflow(
        tmp_path,
        "name: ci\non: [push, pull_request]\njobs:\n  test:\n    steps:\n      - run: pytest\n",
    )

    result = _run(tmp_path, {"provider": "github_actions"})

    assert result.status == "pass"
    assert result.passed is True
    assert result.findings == []


def test_c8_amber_when_verification_not_in_workflow(tmp_path: Path) -> None:
    _write_workflow(
        tmp_path,
        "name: ci\non: [push, pull_request]\njobs:\n  test:\n    steps:\n      - run: pytest tests/unit\n",
    )
    (tmp_path / "deploy.yaml").write_text("post_deploy: pytest tests/e2e\n", encoding="utf-8")

    result = _run(tmp_path, {"provider": "github_actions"})

    assert result.status == "warn"
    assert result.severity == "amber"
    assert result.findings[0].violation_type == "ci_verification_not_in_workflow"
