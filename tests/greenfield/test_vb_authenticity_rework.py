"""Tests for the bounded implement-stage marker-AUTHENTICITY rework loop.

The coverage gate already re-drives the owning test tasks with gap feedback; the
authenticity gate used to fail-closed immediately on the first non-credible
marker (the ExprCalc Python greenfield dogfood stalled here with 24 markers).
:func:`codd.greenfield.pipeline._drive_vb_authenticity_rework` mirrors the
coverage loop's semantics: re-drive with the verbatim findings + the closed VB
contract, bounded, with an oscillation guard and a VB-table tampering guard, and
re-judge with the UNCHANGED deterministic gate each round.

The gate here is REAL (``build_authenticity_report`` / ``build_vb_coverage_audit``
run against files on disk); only the ``rerun`` callback is stubbed — it rewrites
the test file to simulate the AI's next attempt, so convergence/abort behavior is
deterministic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.config import load_project_config
from codd.greenfield.pipeline import (
    StageError,
    _drive_vb_authenticity_rework,
    _vb_rework_max_rounds,
    _build_authenticity_rework_feedback,
)
from codd.project_types import resolve_layout_profile


_VB_DOC = """# Test Strategy

| VB | Description | Test |
| --- | --- | --- |
| VB-01 | compute adds two numbers | t1 |
| VB-02 | compute of equal args | t2 |
"""

# Initial test file: VB-01 covered credibly; the second marker is an ORPHAN
# (`AC-99`), so authenticity fails (orphan) AND VB-02 is uncovered.
_INITIAL = """
def compute(a, b):
    return a + b

# codd: covers vb=VB-01
def test_one():
    result = compute(1, 2)
    assert result == 3

# codd: covers vb=AC-99
def test_two():
    result = compute(2, 2)
    assert result == 4
"""

# Fixed test file: the orphan is corrected to VB-02, still a real assertion.
_FIXED = """
def compute(a, b):
    return a + b

# codd: covers vb=VB-01
def test_one():
    result = compute(1, 2)
    assert result == 3

# codd: covers vb=VB-02
def test_two():
    result = compute(2, 2)
    assert result == 4
"""


def _setup(tmp_path: Path, initial: str = _INITIAL) -> tuple[Path, dict, object]:
    project = tmp_path / "proj"
    (project / "codd").mkdir(parents=True)
    (project / "codd" / "codd.yaml").write_text(
        "project:\n  name: demo\n  language: python\nscan:\n  test_dirs: [tests/]\n",
        encoding="utf-8",
    )
    doc = project / "docs" / "test" / "test_strategy.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(_VB_DOC, encoding="utf-8")
    test_file = project / "tests" / "test_x.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(initial, encoding="utf-8")
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


def _make_rerun(project: Path, states: list[str]):
    """A stub ``rerun(feedback, scope, rows)`` that applies the next file state.

    Each invocation writes ``states.pop(0)`` to the test file (simulating the AI's
    next attempt); once exhausted it leaves the file unchanged. Records the number
    of calls and the feedback it saw.
    """
    calls: list[str] = []
    test_file = project / "tests" / "test_x.py"

    def _rerun(feedback, scope=None, rows=None):
        calls.append(feedback)
        if states:
            test_file.write_text(states.pop(0), encoding="utf-8")

    _rerun.calls = calls  # type: ignore[attr-defined]
    return _rerun


def _noop_scope_resolver(_docs):
    return None


# ---------------------------------------------------------------------------
# Config accessor
# ---------------------------------------------------------------------------


def test_vb_rework_max_rounds_default_and_overrides():
    assert _vb_rework_max_rounds(None) == 2
    assert _vb_rework_max_rounds({}) == 2
    assert _vb_rework_max_rounds({"greenfield": {"vb_rework": {"max_rounds": 0}}}) == 0
    assert _vb_rework_max_rounds({"greenfield": {"vb_rework": {"max_rounds": 5}}}) == 5
    # Malformed values fall back to the default.
    assert _vb_rework_max_rounds({"greenfield": {"vb_rework": {"max_rounds": -1}}}) == 2
    assert _vb_rework_max_rounds({"greenfield": {"vb_rework": "nope"}}) == 2


# ---------------------------------------------------------------------------
# Convergence
# ---------------------------------------------------------------------------


def test_rework_converges_when_ai_fixes_the_orphan(tmp_path):
    project, config, profile = _setup(tmp_path)
    rerun = _make_rerun(project, [_FIXED])
    # Must NOT raise: round 1 fixes the orphan, authenticity + coverage pass.
    _drive_vb_authenticity_rework(
        project,
        config=config,
        authenticity_profile=profile,
        echo=lambda _m: None,
        rerun=rerun,
        rerun_oracle=None,
        scope_resolver=_noop_scope_resolver,
        max_rounds=2,
    )
    assert len(rerun.calls) == 1  # converged in one round


def test_rework_feedback_carries_findings_and_contract(tmp_path):
    project, config, profile = _setup(tmp_path)
    captured: list[str] = []

    def _rerun(feedback, scope=None, rows=None):
        captured.append(feedback)
        # fix it so the loop terminates cleanly after capturing the feedback.
        (project / "tests" / "test_x.py").write_text(_FIXED, encoding="utf-8")

    _drive_vb_authenticity_rework(
        project,
        config=config,
        authenticity_profile=profile,
        echo=lambda _m: None,
        rerun=_rerun,
        rerun_oracle=None,
        scope_resolver=_noop_scope_resolver,
        max_rounds=2,
    )
    fb = captured[0]
    assert "REJECTED" in fb
    assert "AC-99" in fb  # the verbatim finding
    assert "CLOSED ID LIST" in fb  # the VB contract is re-projected
    assert "do NOT edit the VB registry" in fb  # anti-tampering instruction


# ---------------------------------------------------------------------------
# Budget exhaustion (fail-closed)
# ---------------------------------------------------------------------------


def test_rework_budget_exhaustion_is_red(tmp_path):
    # Three orphan markers; the AI fixes ONE per round. With max_rounds=2 the
    # count strictly shrinks each round (3 -> 2 -> 1) so the oscillation guard
    # never fires, but the budget runs out with 1 finding left -> RED.
    three_orphans = """
def compute(a, b):
    return a + b

# codd: covers vb=AC-97
def test_a():
    result = compute(1, 1)
    assert result == 2

# codd: covers vb=AC-98
def test_b():
    result = compute(2, 1)
    assert result == 3

# codd: covers vb=AC-99
def test_c():
    result = compute(3, 1)
    assert result == 4
"""
    fix_one = """
def compute(a, b):
    return a + b

# codd: covers vb=VB-01
def test_a():
    result = compute(1, 1)
    assert result == 2

# codd: covers vb=AC-98
def test_b():
    result = compute(2, 1)
    assert result == 3

# codd: covers vb=AC-99
def test_c():
    result = compute(3, 1)
    assert result == 4
"""
    fix_two = """
def compute(a, b):
    return a + b

# codd: covers vb=VB-01
def test_a():
    result = compute(1, 1)
    assert result == 2

# codd: covers vb=VB-02
def test_b():
    result = compute(2, 1)
    assert result == 3

# codd: covers vb=AC-99
def test_c():
    result = compute(3, 1)
    assert result == 4
"""
    project, config, profile = _setup(tmp_path, initial=three_orphans)
    rerun = _make_rerun(project, [fix_one, fix_two])
    with pytest.raises(StageError, match="marker-authenticity gate failed"):
        _drive_vb_authenticity_rework(
            project,
            config=config,
            authenticity_profile=profile,
            echo=lambda _m: None,
            rerun=rerun,
            rerun_oracle=None,
            scope_resolver=_noop_scope_resolver,
            max_rounds=2,
        )
    assert len(rerun.calls) == 2  # used the whole budget


# ---------------------------------------------------------------------------
# Oscillation guard (no progress -> abort before budget)
# ---------------------------------------------------------------------------


def test_rework_oscillation_guard_aborts(tmp_path):
    # The AI never changes anything -> finding count does not shrink -> abort in
    # round 1 even though max_rounds=5 (no wasted AI calls).
    project, config, profile = _setup(tmp_path)
    rerun = _make_rerun(project, [])  # no state changes
    with pytest.raises(StageError, match="marker-authenticity gate failed"):
        _drive_vb_authenticity_rework(
            project,
            config=config,
            authenticity_profile=profile,
            echo=lambda _m: None,
            rerun=rerun,
            rerun_oracle=None,
            scope_resolver=_noop_scope_resolver,
            max_rounds=5,
        )
    assert len(rerun.calls) == 1  # aborted after the first no-progress round


# ---------------------------------------------------------------------------
# Tampering guard (editing the VB registry during rework -> RED)
# ---------------------------------------------------------------------------


def test_rework_vb_table_tampering_is_red(tmp_path):
    project, config, profile = _setup(tmp_path)

    def _tamper_rerun(feedback, scope=None, rows=None):
        # "Legalize" an invented marker by ADDING a new VB-* row to the registry
        # during implement rework (exactly the dogfood escape: mint VB-99 so a
        # previously-orphan marker becomes declared). Only VB-* first-column ids
        # are parsed as declarations, so this DOES change the declared set.
        doc = project / "docs" / "test" / "test_strategy.md"
        doc.write_text(
            doc.read_text(encoding="utf-8") + "| VB-99 | sneaky | t3 |\n",
            encoding="utf-8",
        )

    with pytest.raises(StageError, match="VB-id set was MODIFIED during authenticity rework"):
        _drive_vb_authenticity_rework(
            project,
            config=config,
            authenticity_profile=profile,
            echo=lambda _m: None,
            rerun=_tamper_rerun,
            rerun_oracle=None,
            scope_resolver=_noop_scope_resolver,
            max_rounds=2,
        )


# ---------------------------------------------------------------------------
# rerun_oracle re-asserted after each round
# ---------------------------------------------------------------------------


def test_rework_reasserts_oracle_after_rerun(tmp_path):
    project, config, profile = _setup(tmp_path)
    rerun = _make_rerun(project, [_FIXED])
    oracle_calls: list[int] = []
    _drive_vb_authenticity_rework(
        project,
        config=config,
        authenticity_profile=profile,
        echo=lambda _m: None,
        rerun=rerun,
        rerun_oracle=lambda: oracle_calls.append(1),
        scope_resolver=_noop_scope_resolver,
        max_rounds=2,
    )
    assert oracle_calls == [1]  # oracle re-asserted once, after the single round
