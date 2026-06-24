"""Tests for the opt-in ``artifact_contract`` DAG check.

Mirrors the dependency_freshness check style: synthetic, project-agnostic
artifacts; no project-specific paths or vocabulary. The check is dormant unless
``artifact_contract.enabled: true`` and defaults to an amber advisory.
"""

from __future__ import annotations

from pathlib import Path

from codd.dag.checks import get_registry
from codd.dag.checks.artifact_contract_check import (
    ArtifactContractCheck,
    ArtifactContractResult,
)


def _run(project_root: Path, config: dict) -> ArtifactContractResult:
    return ArtifactContractCheck(None, project_root, {}).run(codd_config=config)


def test_artifact_contract_registered():
    assert "artifact_contract" in get_registry()


def test_dormant_when_disabled(tmp_path):
    """Absent/disabled contract → skip, exit unaffected."""
    result = _run(tmp_path, {"artifact_contract": {"enabled": False}})
    assert isinstance(result, ArtifactContractResult)
    assert result.skipped is True
    assert result.status == "skip"
    assert result.passed is True


def test_dormant_when_no_stages(tmp_path):
    result = _run(tmp_path, {"artifact_contract": {"enabled": True, "stages": {}}})
    assert result.skipped is True


def test_pass_when_required_artifacts_present(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")
    config = {
        "artifact_contract": {
            "enabled": True,
            "stages": {"implement": ["source"]},
        }
    }
    result = _run(tmp_path, config)
    assert result.passed is True
    assert result.status == "pass"
    assert result.violations == []


def test_active_pass_exposes_checked_count_and_is_not_vacuous(tmp_path):
    # An active contract PASS reports how many stages it verified. checked_count is
    # non-zero (an active contract always declares >=1 stage), so the materiality
    # overlay does not flag it — and a hypothetical 0-stage report would be caught.
    from codd.dag.materiality import is_vacuous_pass

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")
    config = {
        "artifact_contract": {
            "enabled": True,
            "stages": {"implement": ["source"]},
        }
    }
    result = _run(tmp_path, config)
    assert result.checked_count == 1
    assert is_vacuous_pass(result) is False


def test_dormant_skip_is_not_vacuous(tmp_path):
    # The opt-out (disabled) path reports skip; a skip is never a vacuous pass, so
    # an unconfigured project is not flagged by the materiality overlay.
    from codd.dag.materiality import is_vacuous_pass

    result = _run(tmp_path, {"artifact_contract": {"enabled": False}})
    assert result.skipped is True
    assert result.checked_count == 0
    assert is_vacuous_pass(result) is False


def test_missing_artifact_is_amber_by_default(tmp_path):
    """Default severity is amber (advisory): deploy allowed, finding reported."""
    config = {
        "artifact_contract": {
            "enabled": True,
            "stages": {"implement": ["source"]},
        }
    }
    result = _run(tmp_path, config)
    assert result.severity == "amber"
    assert result.status == "warn"
    assert result.passed is True  # amber: advisory
    assert len(result.violations) == 1
    assert result.violations[0]["artifact"] == "source"
    assert result.violations[0]["stage"] == "implement"
    assert result.violations[0]["status"] == "missing"


def test_missing_artifact_red_hard_gates(tmp_path):
    config = {
        "artifact_contract": {
            "enabled": True,
            "severity": "red",
            "stages": {"implement": ["source"]},
        }
    }
    result = _run(tmp_path, config)
    assert result.severity == "red"
    assert result.status == "fail"
    assert result.passed is False
    assert len(result.violations) == 1


def test_severity_honoured_under_dag_section(tmp_path):
    config = {
        "artifact_contract": {"enabled": True, "stages": {"implement": ["source"]}},
        "dag": {"artifact_contract": {"severity": "red"}},
    }
    result = _run(tmp_path, config)
    assert result.severity == "red"


def test_runner_includes_artifact_contract(tmp_path):
    """The runner registers and runs the check via run_all_checks."""
    from codd.dag.runner import run_all_checks

    (tmp_path / "codd").mkdir()
    (tmp_path / "codd" / "codd.yaml").write_text(
        "project:\n  frameworks: []\n", encoding="utf-8"
    )
    results = run_all_checks(tmp_path)
    names = {getattr(r, "check_name", "") for r in results}
    assert "artifact_contract" in names
