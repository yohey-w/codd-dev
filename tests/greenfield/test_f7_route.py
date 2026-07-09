"""F7 — impl-blind test re-derivation: greenfield ROUTE DoD (fenced tests-scoped
rerun, anti-false-green invariants, budget, provenance, 現物 replay). Fable5 ruling
``dogfood/fable5_reply_2026-07-10_js-repair-direction.md`` §③–§⑤ (DoD 4-6, 8).

Red-first: ``codd.greenfield.test_rederivation`` does not exist at HEAD.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from codd.greenfield.test_rederivation import (
    REDERIVATION_FEEDBACK,
    STATUS_GREEN,
    STATUS_RED,
    run_test_rederivation,
)


@dataclass
class _Outcome:
    """Minimal stand-in for RepairLoopOutcome carrying the F7 routing fields."""

    blocked_test_paths: list[str] = field(default_factory=list)
    test_defect_claim: list[dict] = field(default_factory=list)
    history_session_dir: Path | None = None


@dataclass
class _Task:
    task_id: str
    design_node: str
    output_paths: tuple[str, ...] = ()
    expected_outputs: tuple[str, ...] = ()
    title: str = ""
    description: str = ""


@dataclass
class _Verify:
    passed: bool


_HEADER = "// @generated-by: codd implement\n"


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


# ── DoD (4a/4b): fenced tests-scoped rerun of ONLY owning tasks; impl-blind prompt ──

def test_route_reruns_only_owning_task_with_impl_blind_prompt(tmp_path: Path):
    _write(tmp_path, "tests/foo.test.js", _HEADER + "expect(x).toBe(SENTINEL_RECEIVED)\n")
    _write(tmp_path, "src/foo.js", "const SENTINEL_SRC_BODY = 4;\n")

    test_task = _Task("t_test", "test:foo", output_paths=("tests/foo.test.js",))
    src_task = _Task("t_src", "impl:foo", output_paths=("src/foo.js",))

    calls: list[tuple[str, str]] = []

    def _runner(task, feedback):
        calls.append((task.task_id, feedback))

    outcome = _Outcome(blocked_test_paths=["tests/foo.test.js"])
    result = run_test_rederivation(
        tmp_path,
        outcome=outcome,
        config={"scan": {"test_dirs": ["tests/"]}},
        tasks=[test_task, src_task],
        implement_runner=_runner,
        verify=lambda: _Verify(True),
    )

    # ONLY the owning test task re-run — never the src task.
    assert [c[0] for c in calls] == ["t_test"]
    assert result.rederived_tasks == ["t_test"]
    # The built prompt is the canonical impl-blind feedback — NO verify output, NO
    # expected/received VALUES, NO SUT src body.
    prompt = calls[0][1]
    assert prompt == REDERIVATION_FEEDBACK
    assert "SENTINEL_RECEIVED" not in prompt
    assert "SENTINEL_SRC_BODY" not in prompt
    assert result.status == STATUS_GREEN


def test_route_second_trigger_same_task_is_budget_blocked(tmp_path: Path):
    _write(tmp_path, "tests/foo.test.js", _HEADER + "expect(x).toBe(1)\n")
    task = _Task("t_test", "test:foo", output_paths=("tests/foo.test.js",))
    calls: list[str] = []

    def _runner(t, fb):
        calls.append(t.task_id)

    budget: dict[str, int] = {}
    outcome = _Outcome(blocked_test_paths=["tests/foo.test.js"])
    common = dict(
        config={},
        tasks=[task],
        implement_runner=_runner,
        verify=lambda: _Verify(False),
        budget_used=budget,
    )
    first = run_test_rederivation(tmp_path, outcome=outcome, **common)
    second = run_test_rederivation(tmp_path, outcome=outcome, **common)

    assert first.ran is True
    assert calls == ["t_test"]  # the SECOND call did NOT re-run (no oscillation)
    assert second.ran is False
    assert "budget" in second.reason.lower()


def test_route_never_rederives_header_less_human_test(tmp_path: Path):
    # No codd generation header → human-authored → NEVER re-derived.
    _write(tmp_path, "tests/foo.test.js", "expect(x).toBe(1)\n")
    task = _Task("t_test", "test:foo", output_paths=("tests/foo.test.js",))
    calls: list[str] = []

    result = run_test_rederivation(
        tmp_path,
        outcome=_Outcome(blocked_test_paths=["tests/foo.test.js"]),
        config={},
        tasks=[task],
        implement_runner=lambda t, fb: calls.append(t.task_id),
        verify=lambda: _Verify(True),
    )
    assert calls == []
    assert result.ran is False
    assert "tests/foo.test.js" in result.skipped_paths


# ── DoD (5): out-of-scope write during re-derivation → reverted by the fence ──

def test_route_write_fence_reverts_out_of_scope_write(tmp_path: Path):
    _write(tmp_path, "tests/foo.test.js", _HEADER + "expect(x).toBe(1)\n")
    _write(tmp_path, "src/foo.js", "const original = 1;\n")
    task = _Task("t_test", "test:foo", output_paths=("tests/foo.test.js",))

    def _runner(t, fb):
        # A model that (wrongly) edits src + creates an out-of-scope file during a
        # tests-scoped re-derivation. Both are OUT of the test fence.
        (tmp_path / "src" / "foo.js").write_text("const tampered = 999;\n", encoding="utf-8")
        (tmp_path / "src" / "evil.js").write_text("export const evil = true;\n", encoding="utf-8")
        # An IN-scope test edit that must persist.
        (tmp_path / "tests" / "foo.test.js").write_text(_HEADER + "expect(x).toBe(2)\n", encoding="utf-8")

    run_test_rederivation(
        tmp_path,
        outcome=_Outcome(blocked_test_paths=["tests/foo.test.js"]),
        config={"scan": {"test_dirs": ["tests/"]}},
        tasks=[task],
        implement_runner=_runner,
        verify=lambda: _Verify(True),
    )

    # Out-of-scope create removed; out-of-scope modify restored; in-scope edit kept.
    assert not (tmp_path / "src" / "evil.js").exists()
    assert (tmp_path / "src" / "foo.js").read_text() == "const original = 1;\n"
    assert "toBe(2)" in (tmp_path / "tests" / "foo.test.js").read_text()


# ── DoD (6): regenerated test dropping a covers marker → implement gate red ──

def test_route_implement_gate_reds_before_verify(tmp_path: Path):
    _write(tmp_path, "tests/foo.test.js", _HEADER + "expect(x).toBe(1)\n")
    task = _Task("t_test", "test:foo", output_paths=("tests/foo.test.js",))
    verify_calls: list[int] = []

    def _gate():
        raise RuntimeError("1 declared verifiable behavior became uncovered (dropped covers marker)")

    def _verify():
        verify_calls.append(1)
        return _Verify(True)

    result = run_test_rederivation(
        tmp_path,
        outcome=_Outcome(blocked_test_paths=["tests/foo.test.js"]),
        config={},
        tasks=[task],
        implement_runner=lambda t, fb: None,
        verify=_verify,
        implement_gate=_gate,
    )

    assert result.status == STATUS_RED
    assert "uncovered" in result.reason
    # The implement-side gate runs BEFORE verify — a red gate never reaches verify.
    assert verify_calls == []


# ── DoD (8): REPLAY THE 現物 — js-v3 / js-v5 fixtures drive trigger→re-derivation→green ──

_ARTIFACTS = Path(__file__).resolve().parents[2] / "dogfood" / "artifacts_jsrepair"


@pytest.mark.parametrize(
    "run_dir, defective",
    [
        ("js-v5", ["tests/evaluator.test.js", "tests/tokenizer.test.js"]),
        ("js-v3", ["tests/tokenizer.test.js"]),
    ],
)
def test_genbutsu_replay_drives_rederivation_to_green(tmp_path: Path, run_dir: str, defective: list[str]):
    """The ACTUAL failing artifacts: a mocked engine emitting the claim drives
    trigger → re-derivation → green end-to-end (fenced, budgeted, provenance-checked)."""
    src_art = _ARTIFACTS / run_dir
    if not src_art.is_dir():
        pytest.skip(f"artifact fixture {run_dir} absent")
    shutil.copytree(src_art / "tests", tmp_path / "tests")
    shutil.copytree(src_art / "src", tmp_path / "src")

    # The task that OWNS the test suite (its declared output path is ``tests``, per
    # the artifacts' ``@output-paths: tests`` header).
    task = _Task("write_tests", "test:suite", output_paths=("tests",))

    old_bytes = {p: (tmp_path / p).read_bytes() for p in defective}

    def _mock_engine_rederive(t, feedback):
        # The "re-derivation" (implement's test authorship): re-transcribe the test
        # from the design. We simulate a CORRECTED transcription that no longer
        # carries the tautology/wrong-constant. (Header preserved so provenance holds.)
        for rel in defective:
            (tmp_path / rel).write_text(
                _HEADER + "// re-derived strictly from the design\nit('ok', () => expect(1).toBe(1));\n",
                encoding="utf-8",
            )

    session = tmp_path / ".codd" / "repair_history" / "sess1"
    # T2: the mocked engine emitted a legal test_defect_claim for the defect.
    outcome = _Outcome(
        test_defect_claim=[{"file": p, "assertion": "unsatisfiable", "reason": "tautology"} for p in defective],
        history_session_dir=session,
    )

    result = run_test_rederivation(
        tmp_path,
        outcome=outcome,
        config={"scan": {"test_dirs": ["tests/"]}},
        tasks=[task],
        implement_runner=_mock_engine_rederive,
        verify=lambda: _Verify(True),   # fresh verify GREEN after the design-true rewrite
        history_session_dir=session,
        trigger="T2",
        # The mocked engine's re-derivation preserves the VB contract by construction;
        # the real implement-side gate is exercised in isolation by DoD (6) above.
        implement_gate=lambda: None,
    )

    assert result.status == STATUS_GREEN
    assert result.rederived_tasks == ["write_tests"]
    assert sorted(result.rederived_paths) == sorted(defective)
    # The defective transcriptions were actually rewritten.
    for p in defective:
        assert (tmp_path / p).read_bytes() != old_bytes[p]
    # The event was recorded (paths, tasks, old/new hashes).
    assert (session / "test_rederivation.yaml").exists()
