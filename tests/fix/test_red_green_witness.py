"""Unit tests for the Stage-4 red-green witness (anti-false-green).

The witness (design section 4.7 / Phase 4) runs AFTER the deterministic gate
is green: it proves the patched tests actually DETECT this run's change by
restoring the impl to its pre-run baseline and re-running the tests — they MUST
go red. A green there means the tests do not exercise the change (semantic
false green), so the run must NOT report a verified success and must roll back.

All dependencies are injected fakes (TestRunner / CheckRunner / ai_invoke):
no real AI, no network, no live test command, no project/framework literals in
the witness core. Concrete file names appear only in fixtures (allowed).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from codd.fix.design_updater import DesignUpdate
from codd.fix.impl_propagation import run_impl_propagation
from codd.fixer import FailureInfo

# A sentinel the "patched" implementation contains and the baseline does not.
PATCHED_MARKER = "NEW_BEHAVIOR_MARKER"

IMPL_REL = "src/feature.py"
TEST_REL = "tests/test_feature.py"


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _make_baseline(tmp_path: Path) -> None:
    """A pre-existing impl file WITHOUT the new behaviour, and no test yet."""
    impl = tmp_path / IMPL_REL
    impl.parent.mkdir(parents=True, exist_ok=True)
    impl.write_text("def feature():\n    return 'old'\n", encoding="utf-8")


def _applied_update() -> list[tuple[str, DesignUpdate]]:
    """One applied design update (only its ``.diff`` feeds the patch prompt)."""
    return [
        (
            "docs/design/feature.md",
            DesignUpdate(
                target_path=Path("docs/design/feature.md"),
                original_content="# Feature\n",
                proposed_content="# Feature\n\nNow returns new.\n",
                diff="+Now returns new.",
                changed=True,
            ),
        )
    ]


def _patch_ai(impl_body: str, test_body: str) -> Callable[[str], str]:
    """A one-shot fake AI emitting an impl block + a test block.

    The deterministic gate must pass after this single attempt, so the bodies
    are whatever the gate fakes accept; the witness logic is what we exercise.
    """

    payload = (
        f"```python {IMPL_REL}\n{impl_body}\n```\n\n"
        f"```python {TEST_REL}\n{test_body}\n```\n"
    )

    def invoke(_prompt: str) -> str:
        return payload

    return invoke


def _no_red_checks(_root: Path) -> list[object]:
    """CheckRunner: the DAG gate is always green (no red findings)."""
    return []


def _tests_detecting_marker(root: Path) -> list[FailureInfo] | None:
    """TestRunner modelling tests that DO detect the change.

    Models a real new_feature: the behaviour test only exists once the patch
    creates it. So this is red ONLY when the patched test file is present AND
    the impl lacks the marker (the witness's baseline-restore step). It is green
    at preflight (no test yet) and green on the fully patched tree (impl has the
    marker) — the well-behaved case the witness rewards.
    """
    test_present = (root / TEST_REL).is_file()
    impl = root / IMPL_REL
    impl_text = impl.read_text(encoding="utf-8") if impl.is_file() else ""
    if test_present and PATCHED_MARKER not in impl_text:
        return [
            FailureInfo(
                source="local",
                category="test",
                summary="feature does not return new value",
                log="AssertionError: expected new behaviour",
            )
        ]
    return []


def _tests_passing_always(_root: Path) -> list[FailureInfo] | None:
    """TestRunner modelling a superficial test that passes on the baseline too.

    Always green regardless of impl content — the patched test does NOT detect
    the change, so the witness MUST reject it.
    """
    return []


def _common_kwargs(tmp_path: Path) -> dict[str, object]:
    return dict(
        phenomenon_text="動画レッスンに本文を表示したい",
        applied=_applied_update(),
        config={},
        check_runner=_no_red_checks,
        baseline_red_checks=set(),
        impl_paths=[IMPL_REL],
        test_paths=[TEST_REL],
        max_attempts=1,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_witness_rejects_tests_that_pass_on_baseline(tmp_path: Path) -> None:
    """Design 6.1 test 6: a test that passes on the baseline impl is rejected.

    The deterministic gate goes green (tests pass on the patched impl), but the
    witness restores the baseline impl and the tests STILL pass → they do not
    detect the change → verified must be False, the run rolled back, and the
    impl file restored to its baseline content.
    """
    _make_baseline(tmp_path)
    ai = _patch_ai(
        impl_body=f"def feature():\n    # {PATCHED_MARKER}\n    return 'new'\n",
        test_body="def test_feature():\n    assert True\n",
    )

    result = run_impl_propagation(
        tmp_path,
        ai_invoke=ai,
        test_runner=_tests_passing_always,
        intent="new_feature",
        **_common_kwargs(tmp_path),
    )

    assert result.verified is False
    assert result.witness_applicable is True
    assert result.witness_passed is False
    assert result.rolled_back is True
    assert "false green" in result.witness_reason
    # Targeted rollback restored the baseline impl and removed the created test.
    assert (tmp_path / IMPL_REL).read_text(encoding="utf-8") == (
        "def feature():\n    return 'old'\n"
    )
    assert not (tmp_path / TEST_REL).exists()


def test_witness_accepts_tests_that_fail_on_baseline(tmp_path: Path) -> None:
    """The good path: patched tests fail on baseline impl, pass on patched.

    verified is True, the witness passed, no rollback, and the patched files
    (impl with marker + new test) are left in place.
    """
    _make_baseline(tmp_path)
    ai = _patch_ai(
        impl_body=f"def feature():\n    # {PATCHED_MARKER}\n    return 'new'\n",
        test_body="def test_feature():\n    assert feature() == 'new'\n",
    )

    result = run_impl_propagation(
        tmp_path,
        ai_invoke=ai,
        test_runner=_tests_detecting_marker,
        intent="new_feature",
        **_common_kwargs(tmp_path),
    )

    assert result.verified is True
    assert result.witness_applicable is True
    assert result.witness_passed is True
    assert result.rolled_back is False
    assert PATCHED_MARKER in (tmp_path / IMPL_REL).read_text(encoding="utf-8")
    assert (tmp_path / TEST_REL).exists()
    assert sorted(result.written_paths) == sorted([IMPL_REL, TEST_REL])


def test_witness_not_applicable_for_improvement_intent(tmp_path: Path) -> None:
    """intent='improvement' → no witness; normal success even if tests are weak.

    Uses the always-passing test runner (which the witness WOULD reject) to
    prove the witness is genuinely skipped for non behaviour-changing intents.
    """
    _make_baseline(tmp_path)
    ai = _patch_ai(
        impl_body=f"def feature():\n    # {PATCHED_MARKER}\n    return 'new'\n",
        test_body="def test_feature():\n    assert True\n",
    )

    result = run_impl_propagation(
        tmp_path,
        ai_invoke=ai,
        test_runner=_tests_passing_always,
        intent="improvement",
        **_common_kwargs(tmp_path),
    )

    assert result.verified is True
    assert result.witness_applicable is False
    assert result.witness_passed is True  # vacuously — skipped
    assert result.rolled_back is False
    assert "not applicable" in result.witness_reason


def test_witness_fails_when_no_test_changed_for_new_feature(tmp_path: Path) -> None:
    """A behaviour change (new_feature) that wrote NO test file fails the witness.

    The AI emits only an impl block; the gate passes, but with no created/changed
    test there is nothing that could detect the change → witness fails, the run
    is not verified, and the impl is rolled back.
    """
    _make_baseline(tmp_path)

    def impl_only_ai(_prompt: str) -> str:
        return (
            f"```python {IMPL_REL}\n"
            f"def feature():\n    # {PATCHED_MARKER}\n    return 'new'\n```\n"
        )

    result = run_impl_propagation(
        tmp_path,
        ai_invoke=impl_only_ai,
        test_runner=_tests_detecting_marker,
        intent="new_feature",
        **_common_kwargs(tmp_path),
    )

    assert result.verified is False
    assert result.witness_applicable is True
    assert result.witness_passed is False
    assert "no behaviour-witness test" in result.witness_reason
    assert result.rolled_back is True
    # Impl restored to baseline.
    assert (tmp_path / IMPL_REL).read_text(encoding="utf-8") == (
        "def feature():\n    return 'old'\n"
    )


def test_witness_fails_when_test_command_unavailable(tmp_path: Path) -> None:
    """An unavailable test command makes the witness inconclusive → FAIL.

    The DAG check gate alone is green (so the run reaches the witness), but the
    test runner is unavailable at witness time. Refuse to claim a verified fix
    that cannot be witnessed.
    """
    _make_baseline(tmp_path)
    ai = _patch_ai(
        impl_body=f"def feature():\n    # {PATCHED_MARKER}\n    return 'new'\n",
        test_body="def test_feature():\n    assert feature() == 'new'\n",
    )

    calls = {"n": 0}

    def flaky_tests(root: Path) -> list[FailureInfo] | None:
        # Green for the preflight + the gate runs; unavailable once the witness
        # restores the baseline and re-runs (the 3rd call onward).
        calls["n"] += 1
        if calls["n"] >= 3:
            raise RuntimeError("test command vanished")
        return []

    kwargs = _common_kwargs(tmp_path)

    result = run_impl_propagation(
        tmp_path,
        ai_invoke=ai,
        test_runner=flaky_tests,
        intent="bugfix",
        **kwargs,
    )

    assert result.verified is False
    assert result.witness_applicable is True
    assert result.witness_passed is False
    assert "inconclusive" in result.witness_reason
    assert result.rolled_back is True
