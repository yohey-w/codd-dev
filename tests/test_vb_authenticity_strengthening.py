"""Tests for the marker-authenticity STRENGTHENING (tautology + distribution).

* Self-comparison tautology detection (`assert x == x`) is added to the constant-
  assertion classification WITHOUT loosening any accept criterion — and without
  false-REDing the legitimate NaN idiom (`x != x`) or table-driven / artifact-
  observing tests.
* Marker distribution is REPORTED (visibility), never enforced as a cap.
* The verify-stage repair prompt carries the VB contract when a test file is
  among the failing artifacts.
"""

from __future__ import annotations

from pathlib import Path

from codd.config import load_project_config
from codd.project_types import resolve_layout_profile
from codd.vb_marker_authenticity import (
    _python_direct_assertion_evidence as _ev,
    build_authenticity_report,
)
from codd.verifiable_behavior_audit import (
    build_vb_coverage_audit,
    summarize_marker_distribution,
)


def _setup(tmp_path: Path, vb_rows: str, test_body: str, *, extra_files=None):
    project = tmp_path / "proj"
    (project / "codd").mkdir(parents=True)
    (project / "codd" / "codd.yaml").write_text(
        "project:\n  name: demo\n  language: python\nscan:\n  test_dirs: [tests/]\n",
        encoding="utf-8",
    )
    doc = project / "docs" / "test" / "test_strategy.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(
        "# Test Strategy\n\n| VB | Description | Test |\n| --- | --- | --- |\n" + vb_rows,
        encoding="utf-8",
    )
    test_file = project / "tests" / "test_x.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(test_body, encoding="utf-8")
    for rel, content in (extra_files or {}).items():
        p = project / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    config = load_project_config(project)
    profile = resolve_layout_profile(
        language="python",
        project_name="demo",
        source_dirs=["src"],
        test_dirs=["tests"],
        config=config,
        project_root=project,
    )
    return project, config, profile


# ---------------------------------------------------------------------------
# Tautology detection — unit + integration
# ---------------------------------------------------------------------------


def test_tautology_direct_is_rejected_unit():
    assert _ev("r = f()\nassert r == r").reason == "tautology_direct"
    assert _ev("assert compute(1) == compute(1)").reason == "tautology_direct"
    assert _ev("v = f()\nassert v <= v").reason == "tautology_direct"
    # xUnit self-equality.
    assert _ev("self.assertEqual(sorted(v), sorted(v))").reason == "tautology_direct"


def test_legitimate_assertions_not_flagged_unit():
    # A real comparison to an independent expected value.
    assert _ev("r = f()\nassert r == 3").ok is True
    # The NaN idiom (vacuously-false self-comparison) must NOT be flagged.
    assert _ev("x = f()\nassert x != x").ok is True
    # Order self-comparison that would FAIL is not our concern (not a false-green).
    assert _ev("x = f()\nassert x < y").ok is True


def test_tautology_reds_the_authenticity_gate(tmp_path):
    project, config, profile = _setup(
        tmp_path,
        "| VB-01 | compute adds | t |\n",
        (
            "\ndef compute(a, b):\n    return a + b\n\n"
            "# codd: covers vb=VB-01\n"
            "def test_bad():\n    x = compute(1, 2)\n    assert x == x\n"
        ),
    )
    report = build_authenticity_report(
        project, config=config, profile=profile, strict_observability=True
    )
    assert not report.passed
    assert any(v.kind == "no_assertion" and "itself" in v.message.lower() for v in report.violations)


# ---------------------------------------------------------------------------
# Anti-false-red: legitimate table-driven + artifact-observing tests PASS
# ---------------------------------------------------------------------------


def test_table_driven_test_covering_multiple_vbs_passes(tmp_path):
    project, config, profile = _setup(
        tmp_path,
        "| VB-01 | one | t |\n| VB-02 | two | t |\n| VB-03 | three | t |\n",
        (
            "\nimport pytest\n\ndef compute(a, b):\n    return a + b\n\n"
            "# codd: covers vb=VB-01\n"
            "# codd: covers vb=VB-02\n"
            "# codd: covers vb=VB-03\n"
            "@pytest.mark.parametrize('a,b,expected', [(1, 2, 3), (2, 2, 4), (0, 0, 0)])\n"
            "def test_compute(a, b, expected):\n    result = compute(a, b)\n    assert result == expected\n"
        ),
    )
    auth = build_authenticity_report(
        project, config=config, profile=profile, strict_observability=True
    )
    assert auth.passed, [v.message for v in auth.violations]
    cov = build_vb_coverage_audit(project, config=config)
    assert not cov.uncovered_rows  # all three VBs covered by the one table-driven test


def test_artifact_observing_test_passes(tmp_path):
    # A governance/artifact test that READS a produced file and asserts on its
    # content references the read value → direct evidence → not a false-RED.
    project, config, profile = _setup(
        tmp_path,
        "| VB-01 | no forbidden import in app | t |\n",
        (
            "\nfrom pathlib import Path\n\n"
            "# codd: covers vb=VB-01\n"
            "def test_no_forbidden_import():\n"
            "    source = Path('src/app.py').read_text()\n"
            "    assert 'import forbidden' not in source\n"
        ),
        extra_files={"src/app.py": "x = 1\n"},
    )
    auth = build_authenticity_report(
        project, config=config, profile=profile, strict_observability=True
    )
    assert auth.passed, [v.message for v in auth.violations]


# ---------------------------------------------------------------------------
# Marker distribution — visibility, not a cap
# ---------------------------------------------------------------------------


def test_summarize_marker_distribution(tmp_path):
    project, config, _ = _setup(
        tmp_path,
        "| VB-01 | one | t |\n| VB-02 | two | t |\n",
        (
            "\ndef compute(a, b):\n    return a + b\n\n"
            "# codd: covers vb=VB-01\n"
            "def test_one():\n    r = compute(1, 2)\n    assert r == 3\n\n"
            "# codd: covers vb=VB-02\n"
            "def test_two():\n    r = compute(2, 2)\n    assert r == 4\n"
        ),
    )
    dist = summarize_marker_distribution(build_vb_coverage_audit(project, config=config))
    # One test file carrying two covers markers.
    assert dist == {"tests/test_x.py": 2}


# ---------------------------------------------------------------------------
# Verify-stage repair prompt carries the VB contract for a test failure (Task 7)
# ---------------------------------------------------------------------------


def _repair_project(tmp_path: Path) -> tuple[Path, dict]:
    project = tmp_path / "proj"
    (project / "codd").mkdir(parents=True)
    (project / "codd" / "codd.yaml").write_text(
        "project:\n  name: demo\n  language: python\nscan:\n  test_dirs: [tests/]\n",
        encoding="utf-8",
    )
    doc = project / "docs" / "test" / "test_strategy.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(
        "# Test Strategy\n\n| VB | Description | Test |\n| --- | --- | --- |\n"
        "| VB-01 | compute adds | t |\n",
        encoding="utf-8",
    )
    return project, load_project_config(project)


def test_fix_prompt_includes_vb_contract_for_test_failure(tmp_path):
    from codd.fixer import FailureInfo, _build_fix_prompt

    project, config = _repair_project(tmp_path)
    failures = [
        FailureInfo(
            source="local",
            category="test",
            summary="assertion failed",
            log="tests/test_x.py::test_one FAILED",
            failed_files=["tests/test_x.py"],
        )
    ]
    prompt = _build_fix_prompt(project, failures, "", config)
    assert "Verifiable-behavior contract" in prompt
    assert "CLOSED ID LIST" in prompt
    assert "VB-01" in prompt


def test_fix_prompt_omits_vb_contract_for_source_only_failure(tmp_path):
    from codd.fixer import FailureInfo, _build_fix_prompt

    project, config = _repair_project(tmp_path)
    failures = [
        FailureInfo(
            source="local",
            category="build",
            summary="type error",
            log="src/app.py:3 error",
            failed_files=["src/app.py"],
        )
    ]
    prompt = _build_fix_prompt(project, failures, "", config)
    assert "Verifiable-behavior contract" not in prompt
