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


# --- verification hook label schema -----------------------------------------


def test_c8_labeled_verification_with_command_is_matched_by_command(tmp_path: Path) -> None:
    """``verification:`` holding a hook name next to ``command:`` is a label;
    only the command must appear in the CI workflow."""

    _write_workflow(
        tmp_path,
        "name: ci\non: [push, pull_request]\njobs:\n  test:\n    steps:\n      - run: pytest tests/e2e\n",
    )
    (tmp_path / "deploy.yaml").write_text(
        "targets:\n"
        "  primary:\n"
        "    post_deploy:\n"
        "      - verification: gate_name\n"
        "        command: pytest tests/e2e\n"
        "        required: true\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, {"provider": "github_actions"})

    assert result.status == "pass"
    assert result.findings == []


def test_c8_labeled_endpoint_verification_is_not_collected_as_command(tmp_path: Path) -> None:
    """An endpoint verification (``verification:`` + ``url:``) declares no
    command, so it imposes no CI command obligation."""

    _write_workflow(
        tmp_path,
        "name: ci\non: [push, pull_request]\njobs:\n  test:\n    steps:\n      - run: pytest tests/unit\n",
    )
    (tmp_path / "deploy.yaml").write_text(
        "targets:\n"
        "  primary:\n"
        "    post_deploy:\n"
        "      - verification: health\n"
        "        url: https://example.invalid/api/health\n"
        "        expected_status: 200\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, {"provider": "github_actions"})

    assert result.status == "pass"
    assert result.findings == []


# --- root-jail: out-of-root workflow_glob ----------------------------------


def test_c8_out_of_root_glob_does_not_crash_and_reds(tmp_path: Path) -> None:
    """An absolute ``ci.workflow_glob`` pointing outside the project root must
    NOT crash (historically ``ValueError: ... not in the subpath ...``). It is
    surfaced as a structured red ``ci_workflow_out_of_root`` finding instead."""

    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside" / ".github" / "workflows"
    outside.mkdir(parents=True)
    (outside / "ci.yml").write_text(
        "name: ci\non: [push, pull_request]\njobs:\n  test:\n    steps:\n      - run: pytest\n",
        encoding="utf-8",
    )
    abs_glob = str(outside / "*.yml")

    result = _run(project_root, {"provider": "github_actions", "workflow_glob": abs_glob})

    assert result.status == "fail"
    assert result.severity == "red"
    assert result.block_deploy is True
    assert result.passed is False
    assert result.findings[0].violation_type == "ci_workflow_out_of_root"
    assert any(str(outside) in detail for detail in result.findings[0].details)
    # The out-of-root workflow file must not be read into workflow_files.
    assert result.workflow_files == []


def test_c8_out_of_root_glob_serializes_in_json_mode(tmp_path: Path) -> None:
    """JSON mode must emit valid JSON for the out-of-root case (no non-JSON
    error leaking from a propagated ``ValueError``)."""

    import json

    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside" / ".github" / "workflows"
    outside.mkdir(parents=True)
    (outside / "ci.yml").write_text("name: ci\non: [push]\n", encoding="utf-8")
    abs_glob = str(outside / "*.yml")

    result = _run(project_root, {"provider": "github_actions", "workflow_glob": abs_glob})
    report = CiHealthCheck().format_report(result)

    payload = json.loads(report)  # raises if not valid JSON
    finding = payload["ci_health_report"]["findings"][0]
    assert finding["violation_type"] == "ci_workflow_out_of_root"
    assert finding["block_deploy"] is True


def test_c8_in_root_absolute_glob_unchanged(tmp_path: Path) -> None:
    """An absolute glob that resolves INSIDE the project root keeps working
    exactly as before (regression guard for the root-jail change)."""

    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "ci.yml").write_text(
        "name: ci\non: [push, pull_request]\njobs:\n  test:\n    steps:\n      - run: pytest\n",
        encoding="utf-8",
    )
    abs_glob = str(workflow_dir / "*.yml")

    result = _run(tmp_path, {"provider": "github_actions", "workflow_glob": abs_glob})

    assert result.status == "pass"
    assert result.passed is True
    assert result.findings == []
    assert result.workflow_files == [".github/workflows/ci.yml"]


def test_c8_bare_verification_string_is_still_a_command(tmp_path: Path) -> None:
    """Without a sibling spec key, a string ``verification:`` value keeps its
    historical meaning: it is the verification command itself."""

    _write_workflow(
        tmp_path,
        "name: ci\non: [push, pull_request]\njobs:\n  test:\n    steps:\n      - run: pytest tests/unit\n",
    )
    (tmp_path / "deploy.yaml").write_text(
        "targets:\n"
        "  primary:\n"
        "    post_deploy:\n"
        "      - verification: pytest tests/e2e\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, {"provider": "github_actions"})

    assert result.status == "warn"
    assert result.findings[0].violation_type == "ci_verification_not_in_workflow"
    assert result.findings[0].details == ["pytest tests/e2e"]
