"""The oracle value-types relocated to the leaf are the SAME objects the gate exports.

Step 2 of the Contract Kernel oracle dispatch moved ``ImplementOracleFinding`` /
``ImplementOracleResult`` / ``OracleScopeError`` + the ``EVIDENCE_*`` constants to
the leaf :mod:`codd.implement_oracle_types`, with :mod:`codd.implement_oracle`
re-importing + re-exporting them. The invariant this guards: IDENTITY is preserved,
so every existing ``from codd.implement_oracle import ImplementOracleResult`` (and
the pipeline's ``isinstance``/``except OracleScopeError``) keeps working against the
SAME class object — a behaviour-preserving move, not a copy.
"""

from __future__ import annotations

import codd.implement_oracle as gate
import codd.implement_oracle_types as leaf


def test_value_objects_are_the_same_object_in_gate_and_leaf() -> None:
    assert gate.ImplementOracleResult is leaf.ImplementOracleResult
    assert gate.ImplementOracleFinding is leaf.ImplementOracleFinding
    assert gate.OracleScopeError is leaf.OracleScopeError


def test_evidence_constants_are_the_same_object() -> None:
    assert gate.EVIDENCE_CATEGORIES is leaf.EVIDENCE_CATEGORIES
    for name in (
        "EVIDENCE_MISSING_SYMBOL",
        "EVIDENCE_MODULE_RESOLUTION",
        "EVIDENCE_TEST_NOT_COLLECTED",
        "EVIDENCE_ENVIRONMENT_BUILD",
        "EVIDENCE_BOUNDARY_VIOLATION",
        "EVIDENCE_OTHER",
    ):
        assert getattr(gate, name) == getattr(leaf, name)
    # The category tuple is exactly the six design categories.
    assert leaf.EVIDENCE_CATEGORIES == (
        leaf.EVIDENCE_MISSING_SYMBOL,
        leaf.EVIDENCE_MODULE_RESOLUTION,
        leaf.EVIDENCE_TEST_NOT_COLLECTED,
        leaf.EVIDENCE_ENVIRONMENT_BUILD,
        leaf.EVIDENCE_BOUNDARY_VIOLATION,
        leaf.EVIDENCE_OTHER,
    )


def test_oracle_scope_error_is_runtime_error_subclass() -> None:
    assert issubclass(leaf.OracleScopeError, RuntimeError)


def test_feedback_message_uses_relocated_cap_and_lists_findings() -> None:
    # _FEEDBACK_FINDING_CAP moved with the result class; feedback_message still
    # references it (and the gate re-exports it for its other callers).
    assert gate._FEEDBACK_FINDING_CAP == leaf._FEEDBACK_FINDING_CAP == 12
    findings = [
        leaf.ImplementOracleFinding(
            category=leaf.EVIDENCE_MISSING_SYMBOL, code=f"TS{2300 + i}", message=f"m{i}", path=f"f{i}.ts"
        )
        for i in range(leaf._FEEDBACK_FINDING_CAP + 3)
    ]
    result = leaf.ImplementOracleResult(
        passed=False, executed=True, command="tsc --noEmit", findings=findings
    )
    msg = result.feedback_message()
    assert "TS2300" in msg  # first finding shown
    # bounded to the cap, with an "... and N more" tail
    assert "and 3 more diagnostic(s)." in msg


def test_category_counts() -> None:
    result = leaf.ImplementOracleResult(
        passed=False,
        executed=True,
        command="x",
        findings=[
            leaf.ImplementOracleFinding(category=leaf.EVIDENCE_MISSING_SYMBOL, code="a", message="m"),
            leaf.ImplementOracleFinding(category=leaf.EVIDENCE_MISSING_SYMBOL, code="b", message="m"),
            leaf.ImplementOracleFinding(category=leaf.EVIDENCE_MODULE_RESOLUTION, code="c", message="m"),
        ],
    )
    assert result.category_counts() == {
        leaf.EVIDENCE_MISSING_SYMBOL: 2,
        leaf.EVIDENCE_MODULE_RESOLUTION: 1,
    }
