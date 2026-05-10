from __future__ import annotations

from datetime import date
from pathlib import Path

from codd.dag.checks.ci_health import CiHealthCheck
from codd.dag.checks.opt_out import OPT_OUT_STATUS, OptOutPolicy


def _run(
    project_root: Path,
    ci: dict | None,
    *,
    opt_outs: list[dict] | None = None,
    today: date | None = None,
):
    settings: dict = {}
    if ci is not None:
        settings["ci"] = ci
    if opt_outs is not None:
        settings["opt_outs"] = opt_outs
    policy = OptOutPolicy.from_config(settings)
    return CiHealthCheck(opt_out_policy=policy, today=today or date(2026, 5, 10)).run(
        project_root=project_root,
        settings=settings,
    )


def _write_workflow(project_root: Path, text: str) -> None:
    workflow_dir = project_root / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "ci.yml").write_text(text, encoding="utf-8")


def test_c8_red_when_provider_none_without_declaration(tmp_path: Path) -> None:
    result = _run(tmp_path, {"provider": "none"})

    assert result.status == "fail"
    assert result.severity == "red"
    assert result.block_deploy is True
    assert "opt_outs declaration" in result.message


def test_c8_red_when_provider_none_with_expired_declaration(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        {"provider": "none"},
        opt_outs=[
            {
                "check": "ci_health",
                "reason": "vendor migration",
                "expires_at": "2026-01-01",
            }
        ],
    )

    assert result.status == "fail"
    assert result.severity == "red"
    assert result.block_deploy is True
    assert "expired" in result.message


def test_c8_opt_out_when_provider_none_with_active_declaration(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        {"provider": "none"},
        opt_outs=[
            {
                "check": "ci_health",
                "reason": "vendor migration in progress",
                "expires_at": "2027-01-01",
                "approved_by": "owner",
            }
        ],
    )

    assert result.status == OPT_OUT_STATUS
    assert result.severity == "red"  # severity preserved
    assert result.block_deploy is False
    assert result.passed is False  # opt-out is NOT a green pass
    assert "vendor migration in progress" in result.message


def test_c8_red_when_workflow_missing(tmp_path: Path) -> None:
    result = _run(tmp_path, {"provider": "github_actions"})

    assert result.status == "fail"
    assert result.severity == "red"
    assert result.block_deploy is True
    assert result.findings[0].violation_type == "ci_workflow_missing"


def test_c8_red_when_ci_section_missing_entirely(tmp_path: Path) -> None:
    """Missing ci section must NOT silently pass — defaults apply, workflow lookup fails."""

    result = _run(tmp_path, ci=None)

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
