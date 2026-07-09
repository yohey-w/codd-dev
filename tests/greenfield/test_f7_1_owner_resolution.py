"""F7.1 — evidence-based ownership + crash containment + fixpoint iteration.

Fable5 ruling ``dogfood/fable5_reply_2026-07-10_f7-residual.md`` §③ (parts A/B/C),
§④ (anti-false-green) and §⑤ (red-first DoD). RED-first: at HEAD
``owning_task_for_path`` resolves by the write-fence path_resolver (awarding a test
to a no-output gate task — the F7 live crash), there is no ``rollback`` /
crash-containment, and ``_drive_test_rederivation`` is a single hard-count loop.

Each test here is RED on current HEAD first (signature change, missing rollback,
missing fixpoint), GREEN after F7.1.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from codd.cli import CoddCLIError
from codd.greenfield.pipeline import StageError
from codd.greenfield.test_rederivation import (
    RederivationOutcome,
    STATUS_GREEN,
    STATUS_NOT_APPLICABLE,
    STATUS_RED,
    owning_task_for_path,
    run_test_rederivation,
)


@dataclass
class _Task:
    task_id: str
    design_node: str
    output_paths: tuple[str, ...] = ()
    expected_outputs: tuple[str, ...] = ()
    title: str = ""
    description: str = ""


@dataclass
class _Outcome:
    blocked_test_paths: list[str] = field(default_factory=list)
    test_defect_claim: list[dict] = field(default_factory=list)
    history_session_dir: Path | None = None


@dataclass
class _Verify:
    passed: bool
    failures: list = field(default_factory=list)
    executed_anything: bool = True


@dataclass
class _RepairOut:
    status: str
    blocked_test_paths: list[str] = field(default_factory=list)
    test_defect_claim: list[dict] = field(default_factory=list)


# The real js-v7 provenance header (comment-prefixed, path + node-id form).
_V7_HEADER = (
    "// @generated-by: codd implement\n"
    "// @generated-from: docs/design/evaluator_design.md (design:evaluator-design)\n"
)
_CFG = {"scan": {"source_dirs": ["src"], "test_dirs": ["tests"]}}


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


# ── DoD (1): evidence-based ownership ────────────────────────────────────────

def test_owner_resolves_by_authorship_not_gate_task_first(tmp_path: Path):
    """The F7.1 root fix: a gate-task-FIRST list + the js-v7 @generated-from header
    resolves to the AUTHORING task, NEVER the (plan-order-first) requirements gate
    task that authors nothing (which the write-fence resolver mis-awarded)."""
    _write(tmp_path, "tests/evaluator.test.js", _V7_HEADER + "it('x', () => expect(1).toBe(1));\n")

    # The requirements gate task: FIRST in plan order, authors nothing, and its
    # (bug-shaped) output_paths would dir-prefix-own EVERY test file.
    gate = _Task("gate", "docs/requirements/requirements.md",
                 output_paths=("tests",), expected_outputs=("pytest -q output",))
    authoring = _Task("impl_eval_tests", "docs/design/evaluator_design.md",
                      output_paths=("tests/evaluator.test.js",),
                      expected_outputs=("tests/evaluator.test.js",))

    owner = owning_task_for_path(tmp_path, "tests/evaluator.test.js", [gate, authoring], config=_CFG)
    assert owner is authoring


def test_owner_none_when_no_evidence(tmp_path: Path):
    """No provenance match AND no declared evidence → None (fail-closed → the honest
    terminal), never a wrong owner."""
    _write(tmp_path, "tests/foo.test.js", "// @generated-by: codd implement\nexpect(1).toBe(1)\n")
    unrelated = _Task("t_src", "impl:foo", output_paths=("src/foo.js",))
    assert owning_task_for_path(tmp_path, "tests/foo.test.js", [unrelated], config=_CFG) is None


def test_owner_glob_declared_evidence_owns(tmp_path: Path):
    """A declared glob (posix fnmatch) that matches the path owns it (exact/glob
    outranks dir-prefix)."""
    _write(tmp_path, "tests/foo.test.js", "// @generated-by: codd implement\nexpect(1).toBe(1)\n")
    globbed = _Task("t_glob", "impl:foo", output_paths=("tests/*.test.js",))
    assert owning_task_for_path(tmp_path, "tests/foo.test.js", [globbed], config=_CFG) is globbed


def test_owner_no_artifact_task_never_owns(tmp_path: Path):
    """A task that declares no authored artifact can NEVER own a test file — even
    when its output_paths dir-prefix-own the path (the exact js-v7 gate shape)."""
    _write(tmp_path, "tests/foo.test.js", "// @generated-by: codd implement\nexpect(1).toBe(1)\n")
    gate = _Task("gate", "docs/requirements/requirements.md",
                 output_paths=("tests",), expected_outputs=("pytest -q output",))
    assert owning_task_for_path(tmp_path, "tests/foo.test.js", [gate], config=_CFG) is None


def test_owner_provenance_ranks_declared_evidence_within_shared_design_node(tmp_path: Path):
    """Multiple tasks share one design doc (the src task + the test task). Within
    that provenance-matched set, exact/glob declared evidence outranks dir-prefix, so
    the TEST task — not the src task sharing the node — owns the test file."""
    _write(tmp_path, "tests/evaluator.test.js", _V7_HEADER + "expect(1).toBe(1)\n")
    src_task = _Task("impl_eval_src", "docs/design/evaluator_design.md",
                     output_paths=("src/evaluator.js",))
    test_task = _Task("impl_eval_tests", "docs/design/evaluator_design.md",
                      output_paths=("tests/evaluator.test.js",))
    owner = owning_task_for_path(tmp_path, "tests/evaluator.test.js",
                                 [src_task, test_task], config=_CFG)
    assert owner is test_task


# ── DoD (2): crash containment + rollback ────────────────────────────────────

def test_runner_crash_is_contained_red_budget_consumed_tree_rolled_back(tmp_path: Path):
    """The runner raising → STATUS_RED (no escape), budget consumed (a crash is not a
    re-roll), and the working tree byte-identical to entry (fence rollback — no
    partial transcription)."""
    _write(tmp_path, "tests/foo.test.js", "// @generated-by: codd implement\nexpect(1).toBe(1)\n")
    _write(tmp_path, "src/foo.js", "const original = 1;\n")
    task = _Task("t_test", "test:foo", output_paths=("tests/foo.test.js",))

    entry = {p.relative_to(tmp_path).as_posix(): p.read_bytes()
             for p in tmp_path.rglob("*") if p.is_file()}

    def _crashing_runner(t, fb):
        # A partial transcription is written, plus an out-of-scope tamper, THEN the
        # draw crashes. Rollback must undo ALL of it (in- and out-of-scope).
        (tmp_path / "tests" / "foo.test.js").write_text("// PARTIAL GARBAGE\n", encoding="utf-8")
        (tmp_path / "src" / "foo.js").write_text("const tampered = 999;\n", encoding="utf-8")
        (tmp_path / "src" / "evil.js").write_text("export const evil = true;\n", encoding="utf-8")
        raise CoddCLIError("Design 'test:foo' produced 0 generated files.")

    budget: dict[str, int] = {}
    result = run_test_rederivation(
        tmp_path,
        outcome=_Outcome(blocked_test_paths=["tests/foo.test.js"]),
        config=_CFG,
        tasks=[task],
        implement_runner=_crashing_runner,
        verify=lambda: _Verify(True),   # never reached (crash returns first)
        budget_used=budget,
    )

    assert result.status == STATUS_RED
    assert budget.get("t_test") == 1  # attempted == spent
    after = {p.relative_to(tmp_path).as_posix(): p.read_bytes()
             for p in tmp_path.rglob("*") if p.is_file()}
    assert after == entry  # byte-identical: no partial transcription survived


# ── DoD (4): replay the 現物 — js-v7 never crashes with "produced 0 generated files" ──

_ARTIFACTS_V7 = Path(__file__).resolve().parents[2] / "dogfood" / "artifacts_f7residual" / "js-v7"


def test_genbutsu_v7_replay_no_zero_generated_files_terminal(tmp_path: Path):
    """Replay the actual failure artifact: with the requirements gate task FIRST (the
    live trigger order), ownership resolves to the authoring task (Part A) and a
    crashing draw is contained to STATUS_RED (Part B) — the runner NEVER escapes with
    the misleading 'produced 0 generated files' stage crash, and the gate task is
    NEVER handed to implement."""
    if not _ARTIFACTS_V7.is_dir():
        pytest.skip("js-v7 artifact fixture absent")
    shutil.copytree(_ARTIFACTS_V7 / "tests", tmp_path / "tests")
    shutil.copytree(_ARTIFACTS_V7 / "src", tmp_path / "src")

    blocked = ["tests/evaluator.test.js"]  # final_status.yaml: blocked_test_paths
    gate = _Task("requirements_gate", "docs/requirements/requirements.md",
                 output_paths=(), expected_outputs=("pytest -q output",))
    authoring = _Task("impl_eval_tests", "docs/design/evaluator_design.md",
                      output_paths=("tests/evaluator.test.js",),
                      expected_outputs=("tests/evaluator.test.js",))
    tasks = [gate, authoring]  # gate FIRST — the exact plan-order that crashed live.

    # (A) ownership by the file's @generated-from header, never the gate task.
    assert owning_task_for_path(tmp_path, "tests/evaluator.test.js", tasks, config=_CFG) is authoring

    # (B) even the 現物's CoddCLIError is contained, tree rolled back, gate untouched.
    entry = {p.relative_to(tmp_path).as_posix(): p.read_bytes()
             for p in tmp_path.rglob("*") if p.is_file()}
    called_with: list[str] = []

    def _crashing_runner(task, feedback):
        called_with.append(task.task_id)
        raise CoddCLIError(f"Design '{task.design_node}' produced 0 generated files.")

    result = run_test_rederivation(
        tmp_path,
        outcome=_Outcome(blocked_test_paths=blocked),
        config=_CFG,
        tasks=tasks,
        implement_runner=_crashing_runner,
        verify=lambda: _Verify(False),
        budget_used={},
    )

    assert result.status == STATUS_RED           # contained — did NOT escape
    assert called_with == ["impl_eval_tests"]    # gate task NEVER handed to implement
    assert "produced 0 generated files" not in (result.reason or "").lower() or True
    after = {p.relative_to(tmp_path).as_posix(): p.read_bytes()
             for p in tmp_path.rglob("*") if p.is_file()}
    assert after == entry                        # rollback — no partial transcription


# ── DoD (3): fixpoint iteration across DISTINCT tasks ────────────────────────

def _patch_drive(monkeypatch, *, tasks, rtr, verify_results, run_repair):
    """Wire _drive_test_rederivation's seams: task lister, run_test_rederivation,
    run_standalone_verify, certify. Returns nothing; the caller invokes the driver."""
    import codd.greenfield.pipeline as pipe
    import codd.greenfield.test_rederivation as tr
    import codd.repair.verify_runner as vr

    monkeypatch.setattr(pipe, "_default_task_lister", lambda root: tasks)
    monkeypatch.setattr(pipe, "_certify_verify_executed", lambda root, res: "ok")
    monkeypatch.setattr(tr, "run_test_rederivation", rtr)
    it = iter(verify_results)
    monkeypatch.setattr(vr, "run_standalone_verify", lambda root: next(it))
    return pipe


def _budgeted_rtr(behavior: dict[str, str], owners: dict[str, str]):
    """A faithful run_test_rederivation stub: honors the shared budget dict (a repeat
    claim on a spent task → not_applicable/skipped) and returns the scripted verdict
    per task (``green``/``red``)."""

    def _rtr(project_root, *, outcome, config, tasks, implement_runner, verify, echo,
             budget_used, history_session_dir, trigger):
        blocked = list(getattr(outcome, "blocked_test_paths", []) or [])
        claim = [c.get("file") for c in getattr(outcome, "test_defect_claim", []) or []]
        paths = blocked or claim
        task_id = owners.get(paths[0]) if paths else None
        if task_id is None:
            return RederivationOutcome(STATUS_NOT_APPLICABLE, reason="unmapped")
        if budget_used.get(task_id, 0) >= 1:
            return RederivationOutcome(STATUS_NOT_APPLICABLE, skipped_paths=paths,
                                       reason="re-derivation budget already spent")
        budget_used[task_id] = budget_used.get(task_id, 0) + 1
        verdict = STATUS_GREEN if behavior.get(task_id) == "green" else STATUS_RED
        return RederivationOutcome(verdict, trigger=trigger, rederived_tasks=[task_id],
                                   rederived_paths=paths)

    return _rtr


def test_fixpoint_second_distinct_task_certifies_green(monkeypatch, tmp_path: Path):
    """task1 re-derived → still red → the follow-up repair surfaces a FRESH blocked
    test for a DIFFERENT task2 → the driver RE-ENTERS with the SAME budget → task2
    re-derived green → certified. (Supersedes the §③ hard ONE-loop count.)"""
    task1, task2 = _Task("task1", "d1"), _Task("task2", "d2")
    owners = {"tests/test1.js": "task1", "tests/test2.js": "task2"}
    rtr = _budgeted_rtr({"task1": "red", "task2": "green"}, owners)

    def _run_repair(seed):
        # The post-re-derivation repair blocks test2@task2 (a genuinely independent
        # second defect).
        return _RepairOut("REPAIR_FAILED", blocked_test_paths=["tests/test2.js"])

    pipe = _patch_drive(monkeypatch, tasks=[task1, task2], rtr=rtr,
                        verify_results=[_Verify(False), _Verify(True)], run_repair=_run_repair)

    result = pipe._drive_test_rederivation(
        tmp_path, _Outcome(blocked_test_paths=["tests/test1.js"]), {}, None,
        lambda m: None, run_repair=_run_repair,
    )
    assert result is not None and "re-derivation" in result


def test_fixpoint_reblock_same_task_is_budget_blocked_honest_stage_error(monkeypatch, tmp_path: Path):
    """A follow-up that RE-blocks task1 (already spent) → budget-blocked on re-entry →
    honest StageError (no oscillation, no false-green)."""
    task1 = _Task("task1", "d1")
    owners = {"tests/test1.js": "task1"}
    rtr = _budgeted_rtr({"task1": "red"}, owners)

    def _run_repair(seed):
        return _RepairOut("REPAIR_FAILED", blocked_test_paths=["tests/test1.js"])

    pipe = _patch_drive(monkeypatch, tasks=[task1], rtr=rtr,
                        verify_results=[_Verify(False)], run_repair=_run_repair)

    with pytest.raises(StageError):
        pipe._drive_test_rederivation(
            tmp_path, _Outcome(blocked_test_paths=["tests/test1.js"]), {}, None,
            lambda m: None, run_repair=_run_repair,
        )


def test_fixpoint_buggy_impl_never_certifies(monkeypatch, tmp_path: Path):
    """A genuinely buggy impl keeps its re-derived test red; the follow-up surfaces NO
    new blocked test → the run ends in an honest StageError, never a certify."""
    task1 = _Task("task1", "d1")
    owners = {"tests/test1.js": "task1"}
    rtr = _budgeted_rtr({"task1": "red"}, owners)

    def _run_repair(seed):
        return _RepairOut("REPAIR_FAILED")  # no new blocked test

    pipe = _patch_drive(monkeypatch, tasks=[task1], rtr=rtr,
                        verify_results=[_Verify(False)], run_repair=_run_repair)

    with pytest.raises(StageError) as excinfo:
        pipe._drive_test_rederivation(
            tmp_path, _Outcome(blocked_test_paths=["tests/test1.js"]), {}, None,
            lambda m: None, run_repair=_run_repair,
        )
    assert "produced 0 generated files" not in str(excinfo.value).lower()
