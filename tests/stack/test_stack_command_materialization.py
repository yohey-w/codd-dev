"""Stack command materialization — Contract Kernel v2.77c (STEP 0 characterization).

v2.77a brought the framework-stack contract LIVE (intake hash → trace); v2.77b made
the lock a red/green gate. v2.77c connects the *composed commands* (and thus the
stack obligations) to the run's ACTUAL verify/build/test command plan — exercised
*via the real pipeline / verify CLI entry*, mirroring ``test_stack_intake_live.py``
and ``test_stack_lock_gate.py``.

This is an ENFORCEMENT + MATERIALIZATION step, so anti-false-green is the whole
point. Exit gates (v3_goal_contract_kernel.md §"v2.77c — Stack Command
Materialization"):
  1. command collision fixture is RED;
  2. unproved replace fixture is RED;
  3. valid merge fixture is GREEN;
  4. stack obligations affect actual commands (the composed slots are the commands
     the run actually invokes).

Plus the named requirements: "last-wins merge FORBIDDEN" (a collision is RED, never
silently last-wins) and "exclusive_select / deny / replace_with_proof semantics →
pipeline gate" (the gate reds on ANY conflict kind). And the behaviour-preserving
guarantee: a project WITHOUT a ``stack:`` block is byte-identical (no conflict gate,
no plan, no execution, no new trace keys).

Scope note (kept in lane): this step is exit-code-only. Command AUTHENTICITY
(no-op / ``"build":"true"`` / observed-no-tests → RED) is v2.77d; the
obligation-checker gate (``verify_project_stack``) is v2.77e — NOT exercised here.

A recording / sentinel executor is injected so the curated framework/addon commands
(next build / playwright / prisma) are not really run (CI has no node/npx) while we
still PROVE the declared slots are the ones the run invokes (GPT-5.5 Pro consult
2026-06-21: "use a mocked executor that records called command ids").
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from codd.greenfield.pipeline import (
    STAGES,
    GreenfieldPipeline,
    load_session,
)
from codd.languages.registry import default_registry as LANG
from codd.stack.command_plan import (
    StackCommandMaterializationError,
    StackCommandSlot,
    StackCommandSlotResult,
    StackContractConflictError,
    assert_stack_contract_clean,
    execute_stack_command_plan,
    materialize_stack_command_plan,
    stack_command_plan,
)
from codd.stack.compose import Conflict, compose
from codd.stack.lock import build_lock, dump_lock, stack_lock_path
from codd.stack.profile import AddonProfile, CommandSpec, FrameworkProfile, LayerIdentity, Obligation
from codd.stack.registry import default_addon_registry
from codd.stack.resolve import resolve_stack_from_declaration

# The curated Next.js/Prisma/Playwright profiles (the 42-test subsystem), exactly as
# the v2.77a/b tests use. This composes CLEAN — the valid-merge fixture.
_VALID_STACK = {
    "language": "typescript",
    "frameworks": ["nextjs"],
    "addons": ["prisma", "playwright"],
}


# ── recording executor (proves the composed slots are the invoked commands) ──

def _write_passing_playwright_report(slot: StackCommandSlot, project_root: Path) -> None:
    """Write a REAL parseable passing Playwright JSON report to the slot's evidence path.

    An HONEST fake: a fake executor may avoid spawning Playwright, but it must NOT fake
    classification — it must produce the report artifact the real command would have, so
    the SAME authenticity path (adapter parse → test-count observation) runs in tests as
    in production (v2.77d). Only for a stdout-captured TEST slot (e.g. ``e2e_test``).
    """
    from codd.stack.command_authenticity import StackCommandObservationKind, resolve_stack_command_observation_policy
    from codd.stack.command_plan import stack_command_evidence_path

    policy = resolve_stack_command_observation_policy(slot.slot_id)
    if policy is None or policy.kind is not StackCommandObservationKind.TEST_REPORT:
        return
    if (slot.report_capture or "").strip().lower() != "stdout":
        return
    path = stack_command_evidence_path(slot, project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "suites": [
                    {
                        "title": "e2e",
                        "specs": [
                            {
                                "title": "home page renders",
                                "file": "tests/e2e/home.spec.ts",
                                "tests": [
                                    {
                                        "title": "home page renders",
                                        "status": "expected",
                                        "results": [{"status": "passed"}],
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _write_failing_playwright_report(slot: StackCommandSlot, project_root: Path) -> None:
    """Write a REAL parseable FAILING Playwright JSON report (seeded-mutation analogue)."""
    from codd.stack.command_authenticity import StackCommandObservationKind, resolve_stack_command_observation_policy
    from codd.stack.command_plan import stack_command_evidence_path

    policy = resolve_stack_command_observation_policy(slot.slot_id)
    if policy is None or policy.kind is not StackCommandObservationKind.TEST_REPORT:
        return
    if (slot.report_capture or "").strip().lower() != "stdout":
        return
    path = stack_command_evidence_path(slot, project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "suites": [
                    {
                        "title": "e2e",
                        "specs": [
                            {
                                "title": "home page renders",
                                "file": "tests/e2e/home.spec.ts",
                                "tests": [
                                    {
                                        "title": "home page renders",
                                        "status": "unexpected",
                                        "results": [{"status": "failed"}],
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


class _RecordingExecutor:
    """Records every slot it is asked to invoke and passes (exit-code 0), HONESTLY.

    Used to PROVE exit gate 4 (the composed stack command slots are the commands the
    run actually invokes) without running real Next.js/Playwright. ``calls`` holds the
    invoked ``(slot_id, owner, argv)`` in invocation order.

    HONEST under v2.77d authenticity: for a TEST-kind slot (``e2e_test``) it writes a
    REAL passing report to the slot's evidence path (it does not fake the classifier —
    the authenticity layer parses that report and observes ≥1 passed test). The non-test
    slots (``framework_build``/``generate``/``typecheck``) carry honest, non-no-op argv
    from the curated profiles, so they pass the no-op gate on real data.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []

    def __call__(self, slot: StackCommandSlot, project_root: Path, *, timeout: float):
        self.calls.append((slot.slot_id, slot.owner, slot.argv))
        _write_passing_playwright_report(slot, project_root)
        return StackCommandSlotResult(
            slot_id=slot.slot_id,
            owner=slot.owner,
            command_str=slot.command_str,
            spawned=True,
            returncode=0,
            timed_out=False,
        )


def _failing_executor(slot: StackCommandSlot, project_root: Path, *, timeout: float):
    """An executor where a specific framework slot exits non-zero (a real build fail)."""
    rc = 1 if slot.slot_id == "framework_build" else 0
    return StackCommandSlotResult(
        slot_id=slot.slot_id,
        owner=slot.owner,
        command_str=slot.command_str,
        spawned=True,
        returncode=rc,
        timed_out=False,
        detail=("" if rc == 0 else "exit 1"),
    )


# ── colliding-profile registry injection (drive the REAL pipeline to a conflict) ──

@pytest.fixture
def colliding_addon(monkeypatch: pytest.MonkeyPatch):
    """Register a temporary addon that REDECLARES nextjs' ``framework_build`` slot
    with DIFFERENT argv → a command collision (an unproved replace) in the resolved
    contract, so a ``stack:`` declaration naming it drives the REAL pipeline/verify
    path to the conflict gate. Cleaned up by monkeypatch (registry cache restored)."""
    bad = AddonProfile(
        identity=LayerIdentity(id="collidertool", kind="addon"),
        capability="build",
        commands={"framework_build": CommandSpec(id="framework_build", argv=("rogue", "build"))},
    )
    profiles = dict(default_addon_registry._ensure_loaded())  # force-load, copy
    profiles["collidertool"] = bad
    monkeypatch.setattr(default_addon_registry, "_profiles", profiles)
    return bad


_COLLIDING_STACK = {
    "language": "typescript",
    "frameworks": ["nextjs"],          # owns framework_build = npx next build
    "addons": ["collidertool"],        # redeclares framework_build = rogue build → COLLISION
}


def _make_project(tmp_path: Path, *, stack: dict | None) -> Path:
    """A pre-initialized CoDD project; optionally with a ``stack:`` block."""
    project = tmp_path / "proj"
    codd_dir = project / "codd"
    codd_dir.mkdir(parents=True)
    config: dict = {"project": {"name": "proj", "language": "typescript"}}
    if stack is not None:
        config["stack"] = stack
    (codd_dir / "codd.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return project


def _write_lock_for(project: Path, declaration: dict) -> Path:
    """Commit a stack lock pinned to ``declaration``'s resolved contract (so the
    v2.77b lock gate is GREEN and we exercise the v2.77c gate beyond it)."""
    contract = resolve_stack_from_declaration(declaration)
    path = stack_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_lock(build_lock(contract)), encoding="utf-8")
    return path


class _StageStubPipeline(GreenfieldPipeline):
    """The REAL pipeline with every STAGE BODY replaced by a no-op marker (same
    technique as the v2.77a/b tests) — so the real intake + lock + v2.77c
    materialization gate (all in ``run()``, before the stage loop) are exercised."""

    def _stage_init(self, project_root, record, options):  # noqa: ANN001
        record["status"] = "done"

    def _stage_elicit(self, project_root, record, options):  # noqa: ANN001
        record["status"] = "done"

    def _stage_plan(self, project_root, record, options):  # noqa: ANN001
        record["status"] = "done"

    def _stage_generate(self, project_root, record, options):  # noqa: ANN001
        record["status"] = "done"

    def _stage_implement(self, project_root, record, options):  # noqa: ANN001
        record["status"] = "done"

    def _stage_verify(self, project_root, record, options):  # noqa: ANN001
        record["status"] = "done"

    def _stage_ci_scaffold(self, project_root, record, options):  # noqa: ANN001
        record["status"] = "done"

    def _stage_propagate(self, project_root, record, options):  # noqa: ANN001
        record["status"] = "done"

    def _stage_check(self, project_root, record, options):  # noqa: ANN001
        record["status"] = "done"


def _run(project: Path, *, executor=None, resume: bool = False) -> tuple[object, list[str]]:
    lines: list[str] = []
    result = _StageStubPipeline(echo=lines.append, stack_command_executor=executor).run(
        project, resume=resume
    )
    return result, lines


# ═══════════════════════════════════════════════════════════════════════════
# Exit gate 1 — command collision fixture is RED (last-wins FORBIDDEN)
# ═══════════════════════════════════════════════════════════════════════════

def test_greenfield_command_collision_is_red(tmp_path: Path, colliding_addon) -> None:
    """A stack whose layers collide on a command slot (different argv) → RED.

    This realizes "command collision → RED" and "last-wins merge FORBIDDEN": the
    composer records a Conflict instead of silently keeping one argv, and the v2.77c
    gate turns that into a hard failure. A recording executor is injected to PROVE no
    command is executed once the contract is conflicted (the gate reds BEFORE exec).
    """
    project = _make_project(tmp_path, stack=_COLLIDING_STACK)
    _write_lock_for(project, _COLLIDING_STACK)  # lock GREEN so we reach the v2.77c gate
    rec = _RecordingExecutor()

    result, lines = _run(project, executor=rec)

    assert result.status == "failed", f"a command collision must be RED; got {result.status}"
    assert result.failed_stage == "stack_commands"
    assert "conflict" in (result.error or "").lower()
    assert "framework_build" in (result.error or "")
    # last-wins was NOT silently applied: the gate reds and NOTHING was executed.
    assert rec.calls == [], "a conflicted contract must not materialize/execute commands"
    assert any("stack command materialization" in line for line in lines)


def test_verify_command_collision_is_red(tmp_path: Path, colliding_addon) -> None:
    """The verify CLI path reds (non-zero exit) on a command collision."""
    from codd.cli import _intake_stack_contract_for_verify

    project = _make_project(tmp_path, stack=_COLLIDING_STACK)
    _write_lock_for(project, _COLLIDING_STACK)
    rec = _RecordingExecutor()

    with pytest.raises(SystemExit) as excinfo:
        _intake_stack_contract_for_verify(project, stack_command_executor=rec)
    assert excinfo.value.code != 0
    assert rec.calls == []  # no execution from a conflicted contract


# ═══════════════════════════════════════════════════════════════════════════
# Exit gate 2 — unproved replace is RED (== command collision today)
# ═══════════════════════════════════════════════════════════════════════════

def test_unproved_replace_is_red_unit(colliding_addon) -> None:
    """An "unproved replace" (a layer replacing another's command with different argv
    and no replace_with_proof) IS a command collision in the current model → the
    conflict gate reds. (The executable-proof GREEN path is a later sub-step.)"""
    contract = resolve_stack_from_declaration(_COLLIDING_STACK)
    assert any(c.kind == "command" for c in contract.conflicts)
    with pytest.raises(StackContractConflictError):
        assert_stack_contract_clean(contract)
    with pytest.raises(StackContractConflictError):
        stack_command_plan(contract)  # the plan is NEVER built from a conflicted contract


def test_gate_reds_on_exclusive_and_semantic_conflict_kinds() -> None:
    """The gate reds on ANY conflict kind — exclusive / deny / semantic, not only
    command. (Proves the gate is exhaustive; it does NOT claim the composer already
    emits an exclusive conflict from the curated registries — GPT-consult framing.)"""
    ts = LANG.resolve("typescript")
    base = compose(ts)  # a clean contract

    # A hand-constructed EXCLUSIVE conflict → the gate must red.
    from dataclasses import replace

    conflicted_exclusive = replace(
        base, conflicts=(Conflict(kind="exclusive", detail="two primary roles on one path"),)
    )
    with pytest.raises(StackContractConflictError):
        assert_stack_contract_clean(conflicted_exclusive)

    # And a SEMANTIC weaken conflict, composed for real from profiles.
    strong = FrameworkProfile(
        identity=LayerIdentity(id="sfw", kind="framework"),
        obligations=(Obligation(id="must_hold", severity="error"),),
    )
    weak = AddonProfile(
        identity=LayerIdentity(id="wad", kind="addon"),
        obligations=(Obligation(id="must_hold", severity="warn"),),
    )
    semantic = compose(ts, [strong], [weak])
    assert any(c.kind == "semantic" for c in semantic.conflicts)
    with pytest.raises(StackContractConflictError):
        assert_stack_contract_clean(semantic)


def test_gate_reds_on_flag_desync_even_without_conflicts() -> None:
    """Defensive: ``strict_ok``/``is_clean`` False with an EMPTY conflicts tuple is an
    invalid state and must be RED, never silently GREEN (anti-false-green)."""
    ts = LANG.resolve("typescript")
    from dataclasses import replace

    # A subclass that lies: no conflicts, but strict_ok=False.
    class _Lying(type(compose(ts))):  # type: ignore[misc]
        @property
        def strict_ok(self) -> bool:
            return False

    base = compose(ts)
    lying = _Lying(**{f.name: getattr(base, f.name) for f in base.__dataclass_fields__.values()})
    assert lying.conflicts == ()
    with pytest.raises(StackContractConflictError):
        assert_stack_contract_clean(lying)


# ═══════════════════════════════════════════════════════════════════════════
# Exit gate 3 — valid merge fixture is GREEN
# ═══════════════════════════════════════════════════════════════════════════

def test_greenfield_valid_merge_is_green_and_records_plan(tmp_path: Path) -> None:
    """A clean curated stack → GREEN, and the materialized command plan is recorded."""
    project = _make_project(tmp_path, stack=_VALID_STACK)
    _write_lock_for(project, _VALID_STACK)
    rec = _RecordingExecutor()

    result, lines = _run(project, executor=rec)

    assert result.status == "success", f"a clean valid merge must be GREEN; {getattr(result,'error',None)}"
    assert any("stack command materialization" in line and "slot(s) invoked" in line for line in lines)
    # The materialized plan landed in the run record (observable, like the hash/lock).
    session = load_session(project)
    plan_rec = session["stack_contract"]["stack_command_plan"]
    assert plan_rec["stack_id"] == "typescript+nextjs+prisma+playwright"
    assert plan_rec["stack_contract_hash"].startswith("sha256:")


def test_replace_with_proof_identical_argv_merge_is_green() -> None:
    """A layer re-declaring a slot with IDENTICAL argv is a harmless merge → GREEN
    (no conflict). This is the GREEN counterpart to the unproved-replace RED: today's
    realization of "a replace is allowed only when it does not actually change the
    command" — the executable-proof GREEN path for a DIFFERENT argv is a later step."""
    ts = LANG.resolve("typescript")
    same = FrameworkProfile(
        identity=LayerIdentity(id="echofw", kind="framework"),
        commands={"typecheck": ts.commands["typecheck"]},  # SAME argv as the language
    )
    contract = compose(ts, [same])
    assert contract.is_clean
    assert_stack_contract_clean(contract)  # must NOT raise
    plan = stack_command_plan(contract)  # plan builds fine
    assert "typecheck" in plan.command_ids


# ═══════════════════════════════════════════════════════════════════════════
# Exit gate 4 — stack obligations affect ACTUAL commands
# ═══════════════════════════════════════════════════════════════════════════

def test_composed_slots_are_the_invoked_commands(tmp_path: Path) -> None:
    """The composed VERIFICATION command slots are the commands the run INVOKES.

    This is the crux of v2.77c: a declared ``framework_build``/``e2e_test`` slot is
    genuinely part of the run's command plan (not silently ignored while the language
    verify greens alone). The recording executor proves each was invoked, WITH its owning
    namespace.

    Contract Kernel v2.77g — the plan materializes ONLY verification slots. The prisma
    ``generate`` (codegen) / ``migrate_deploy`` (mutating) slots are KNOWN
    non-verification: they are EXCLUDED from the plan (a verify run must not run codegen
    or apply migrations). The earlier version of this test asserted ``generate`` was
    invoked — that asserted the over-execution BUG (a non-verification slot running during
    verify, the sibling of the ``dev``/``start`` server hang); it is corrected here to
    assert the right behavior: verification slots run, non-verification slots do not.
    """
    project = _make_project(tmp_path, stack=_VALID_STACK)
    _write_lock_for(project, _VALID_STACK)
    rec = _RecordingExecutor()

    result, _lines = _run(project, executor=rec)
    assert result.status == "success"

    invoked = {slot_id: owner for slot_id, owner, _argv in rec.calls}
    # The framework/addon VERIFICATION slots the run previously ignored are now invoked,
    # owned:
    assert invoked.get("framework_build") == "framework:nextjs"
    assert invoked.get("e2e_test") == "addon:playwright"
    # ...alongside the language-owned slots (namespace ownership preserved).
    assert invoked.get("typecheck") == "language:typescript"
    assert invoked.get("verify") == "language:typescript"
    # NON-verification slots are NOT executed by a verify run (codegen / mutating
    # migration / dev+start servers): silently running them is the over-execution bug.
    assert "generate" not in invoked, "prisma generate (codegen) is not a verify-time slot"
    assert "migrate_deploy" not in invoked, "a mutating migration must not run during verify"
    assert "dev" not in invoked and "start" not in invoked, "server slots must not run"
    # The exact next-build argv was the one materialized from the contract.
    fb_argv = next(argv for sid, _o, argv in rec.calls if sid == "framework_build")
    assert fb_argv == ("npx", "next", "build")

    # The executed slot ids are recorded in the run trace (observable).
    session = load_session(project)
    executed = session["stack_contract"]["stack_commands_executed"]
    assert {"framework_build", "e2e_test", "verify", "typecheck"} == set(executed)


def test_a_failing_stack_command_slot_is_red(tmp_path: Path) -> None:
    """The false-green this step closes: a declared framework_build that FAILS makes
    the run RED — it is NOT silently skipped while the language verify greens alone."""
    project = _make_project(tmp_path, stack=_VALID_STACK)
    _write_lock_for(project, _VALID_STACK)

    result, lines = _run(project, executor=_failing_executor)

    assert result.status == "failed", "a failing framework_build slot must be RED"
    assert result.failed_stage == "stack_commands"
    assert "framework_build" in (result.error or "")
    assert "materialization failed" in (result.error or "")


def test_verify_failing_stack_command_slot_is_red(tmp_path: Path) -> None:
    """The verify CLI path reds when a composed stack command slot fails (exit-code)."""
    from codd.cli import _intake_stack_contract_for_verify

    project = _make_project(tmp_path, stack=_VALID_STACK)
    _write_lock_for(project, _VALID_STACK)

    with pytest.raises(SystemExit) as excinfo:
        _intake_stack_contract_for_verify(project, stack_command_executor=_failing_executor)
    assert excinfo.value.code != 0


def test_changing_stack_changes_the_materialized_plan() -> None:
    """Contract-driven (no framework literal): a different stack → a different plan."""
    full = stack_command_plan(resolve_stack_from_declaration(_VALID_STACK))
    fewer = stack_command_plan(
        resolve_stack_from_declaration(
            {"language": "typescript", "frameworks": ["nextjs"]}  # no addons
        )
    )
    assert "e2e_test" in full.command_ids  # playwright addon contributed it
    assert "e2e_test" not in fewer.command_ids
    assert full.content_hash != fewer.content_hash


def test_execute_plan_aggregates_exit_codes(tmp_path: Path) -> None:
    """``execute_stack_command_plan`` ok iff every slot exited 0; failed slots listed."""
    contract = resolve_stack_from_declaration(_VALID_STACK)
    plan = stack_command_plan(contract)

    ok_res = execute_stack_command_plan(plan, tmp_path, executor=_RecordingExecutor())
    assert ok_res.ok and not ok_res.failed
    assert set(ok_res.executed_slot_ids) == set(plan.command_ids)

    bad_res = execute_stack_command_plan(plan, tmp_path, executor=_failing_executor)
    assert not bad_res.ok
    assert {r.slot_id for r in bad_res.failed} == {"framework_build"}


def test_materialize_raises_on_failing_slot(tmp_path: Path) -> None:
    """The single materialize entry raises the domain RED on a failing slot."""
    contract = resolve_stack_from_declaration(_VALID_STACK)
    with pytest.raises(StackCommandMaterializationError):
        materialize_stack_command_plan(contract, tmp_path, executor=_failing_executor)


# ═══════════════════════════════════════════════════════════════════════════
# Behaviour-preserving — a project WITHOUT a stack block is byte-identical
# ═══════════════════════════════════════════════════════════════════════════

def test_no_stack_block_has_no_materialization_greenfield(tmp_path: Path) -> None:
    """No ``stack:`` block → no conflict gate, no plan, no execution, no new trace key."""
    project = _make_project(tmp_path, stack=None)
    rec = _RecordingExecutor()

    result, lines = _run(project, executor=rec)

    assert result.status == "success"
    # No materialization trace line, no executor call, no stack_contract record at all.
    assert not any("stack command materialization" in line for line in lines)
    assert rec.calls == []
    session = load_session(project)
    assert "stack_contract" not in session
    assert session["result"]["status"] == "success"
    assert set(session["stages"]) == set(STAGES)


def test_no_stack_block_has_no_materialization_verify(tmp_path: Path) -> None:
    """No ``stack:`` block → verify path never materializes/executes (no exit)."""
    from codd.cli import _intake_stack_contract_for_verify

    project = _make_project(tmp_path, stack=None)
    rec = _RecordingExecutor()
    _intake_stack_contract_for_verify(project, stack_command_executor=rec)  # must NOT raise
    assert rec.calls == []


# ═══════════════════════════════════════════════════════════════════════════
# Contract Kernel v2.77g — verification-slots-only filtering + placeholder
# substitution (the two REAL bugs the live Next.js dogfood found).
# ═══════════════════════════════════════════════════════════════════════════

_NEXTJS_PLAYWRIGHT = {
    "language": "typescript",
    "frameworks": ["nextjs"],
    "addons": ["playwright"],
}


def test_plan_excludes_server_and_convenience_slots(tmp_path: Path) -> None:
    """BUG 1: the plan materializes ONLY verification slots — the ``dev`` / ``start``
    SERVER slots (which never exit → would hang the gate) and the ``generate`` /
    ``migrate`` convenience slots are NOT in the plan, so they are never spawned."""
    contract = resolve_stack_from_declaration(_VALID_STACK)
    # The composed contract still DECLARES all the slots...
    assert {"dev", "start", "generate", "migrate_deploy"} <= set(contract.commands)
    # ...but the PLAN (what gets executed) excludes the non-verification ones.
    plan = stack_command_plan(contract)
    ids = set(plan.command_ids)
    assert "dev" not in ids and "start" not in ids, "server slots would hang the gate"
    assert "generate" not in ids, "codegen is not a verify-time slot"
    assert "migrate_deploy" not in ids, "a mutating migration must not run during verify"
    # The genuine verification slots ARE present (none silently dropped).
    assert {"typecheck", "verify", "framework_build", "e2e_test"} <= ids


def test_framework_build_is_a_verification_slot(tmp_path: Path) -> None:
    """ANTI-FALSE-GREEN: ``framework_build`` (the production build the
    ignoreBuildErrors obligation is enforced against) MUST be executed — dropping it
    would let a stack green without proving the build gate ran."""
    from codd.stack.compose import VERIFICATION_SLOTS

    assert "framework_build" in VERIFICATION_SLOTS
    assert "build" in VERIFICATION_SLOTS
    plan = stack_command_plan(resolve_stack_from_declaration(_VALID_STACK))
    assert "framework_build" in plan.command_ids


def test_unknown_slot_is_red_at_plan_build() -> None:
    """THREE-STATE classification: a slot id in NEITHER the verification set NOR the
    known non-verification set is RED at plan build (the harness never guesses — a
    silently-dropped unknown slot could be a real check = false-green)."""
    from codd.languages.registry import default_registry as _LANG
    from codd.stack.command_plan import StackCommandClassificationError
    from codd.stack.compose import compose
    from codd.stack.profile import CommandSpec as _CS
    from codd.stack.profile import FrameworkProfile as _FP
    from codd.stack.profile import LayerIdentity as _LI

    ts = _LANG.resolve("typescript")
    weird = _FP(
        identity=_LI(id="weirdfw", kind="framework"),
        commands={"frobnicate": _CS(id="frobnicate", argv=("npx", "frob"))},
    )
    contract = compose(ts, [weird])
    with pytest.raises(StackCommandClassificationError) as exc:
        stack_command_plan(contract)
    assert "frobnicate" in str(exc.value)


def test_layout_placeholders_substituted_at_plan_build() -> None:
    """BUG 2: a slot's ``{module_root}`` / ``{test_root}`` / ``{report}`` are resolved at
    plan build, so the executor spawns in a real dir and writes to a real path."""
    plan = stack_command_plan(resolve_stack_from_declaration(_NEXTJS_PLAYWRIGHT))
    typecheck = next(s for s in plan.slots if s.slot_id == "typecheck")
    assert typecheck.cwd == ".", "{module_root} must be resolved, not literal"
    verify = next(s for s in plan.slots if s.slot_id == "verify")
    assert verify.cwd == "."
    assert "{test_root}" not in verify.argv and "tests" in verify.argv
    # No slot carries a leftover {placeholder} in cwd/argv/env.
    for slot in plan.slots:
        assert "{" not in (slot.cwd or ""), f"{slot.slot_id} cwd unresolved: {slot.cwd!r}"
        for arg in slot.argv:
            # A leftover layout placeholder is a bug; JSON-ish braces in argv are NOT
            # (the guard only matches the known layout tokens).
            for tok in ("{module_root}", "{repo_root}", "{manifest_root}", "{test_root}", "{report}"):
                assert tok not in arg, f"{slot.slot_id} argv unresolved: {arg!r}"


def test_report_placeholder_aligns_with_report_path() -> None:
    """ANTI-FALSE-GREEN: ``--outputFile={report}`` must resolve to EXACTLY the slot's
    ``report_path`` (the path the authenticity/obligation reader reads) — else the writer
    and reader disagree (false-RED, or a stale-file false-green)."""
    plan = stack_command_plan(resolve_stack_from_declaration(_NEXTJS_PLAYWRIGHT))
    verify = next(s for s in plan.slots if s.slot_id == "verify")
    assert verify.report_path == ".codd/verify/vitest.json"
    # The argv writes to the SAME path the reader reads.
    assert f"--outputFile={verify.report_path}" in verify.argv


def test_unsubstituted_placeholder_refuses_to_spawn() -> None:
    """ANTI-FALSE-GREEN (v2.75 cwd-bug class): a slot still carrying a literal
    ``{module_root}`` / ``{test_root}`` does NOT spawn in a literal dir — it is RED
    (spawned=False), never a silent pass or a spawn in a wrong dir."""
    from codd.stack.command_plan import default_stack_command_executor

    bad = StackCommandSlot(
        slot_id="verify",
        owner="language:typescript",
        argv=("npx", "vitest", "run", "{test_root}"),
        cwd="{module_root}",
    )
    res = default_stack_command_executor(bad, Path("/tmp"), timeout=5)
    assert res.spawned is False
    assert "unsubstituted" in res.detail.lower()


def test_json_argv_is_not_a_false_unsubstituted_placeholder(tmp_path: Path) -> None:
    """A legitimate argv carrying JSON braces (``printf '%s' '{"k":1}'``) is NOT flagged
    as an unsubstituted placeholder — the guard matches only the KNOWN layout tokens."""
    from codd.stack.command_plan import default_stack_command_executor

    slot = StackCommandSlot(
        slot_id="framework_build",
        owner="framework:nextjs",
        argv=("printf", "%s", '{"built": true, "mode": "production"}'),
        cwd=".",
    )
    res = default_stack_command_executor(slot, tmp_path, timeout=10)
    assert res.spawned is True, f"JSON argv wrongly refused: {res.detail}"
    assert res.returncode == 0


def test_ambiguous_test_root_leaves_placeholder_for_red(tmp_path: Path) -> None:
    """``{test_root}`` is resolved only when EXACTLY one test root is declared; zero or
    multiple make it ambiguous → the placeholder is left literal so the executor reds
    (never a silent guess).

    REGRESSION (found while dogfooding ``codd verify`` on a generated TypeScript
    project): asserting the ``test_root`` PROPERTY alone is not sufficient proof of
    the docstring's "the placeholder is left literal so the executor reds" claim.
    ``_substitute_stack_placeholders`` used to replace ``{test_root}`` with that
    empty string UNCONDITIONALLY — ``"run {test_root}".replace("{test_root}", "")``
    silently ERASES the literal placeholder text (no ``{test_root}`` substring
    survives; the argv element just becomes an empty string) rather than leaving it
    for the unsubstituted-placeholder guard to catch, so the guard never actually
    fired for an ambiguous test root even though this test already asserted the
    ambiguous property value. Prove the FULL chain instead: property → substituted
    argv → guard detection → refused spawn.
    """
    from codd.stack.command_plan import (
        _substitute_stack_placeholders,
        _unsubstituted_placeholders,
        default_stack_command_executor,
    )
    from codd.stack.compose import StackLayout

    assert StackLayout(test_roots=()).test_root == ""  # zero → ambiguous
    assert StackLayout(test_roots=("a", "b")).test_root == ""  # multiple → ambiguous
    assert StackLayout(test_roots=("tests",)).test_root == "tests"  # exactly one → ok

    ambiguous_layout = StackLayout(module_root=".", repo_root=".", manifest_root=".", test_roots=())
    argv = tuple(
        _substitute_stack_placeholders(a, ambiguous_layout, report_path=".codd/verify/vitest.json")
        for a in ("npx", "vitest", "run", "{test_root}", "--outputFile={report}")
    )
    assert "{test_root}" in argv, "an ambiguous test root must survive substitution, never be erased"

    slot = StackCommandSlot(
        slot_id="verify", owner="language:typescript", argv=argv, report_path=".codd/verify/vitest.json"
    )
    assert _unsubstituted_placeholders(slot), "the guard must detect the surviving {test_root} placeholder"

    res = default_stack_command_executor(slot, tmp_path, timeout=5)
    assert res.spawned is False, "an ambiguous {test_root} must refuse to spawn, never guess"
    assert "unsubstituted" in res.detail.lower()


def test_build_precedes_e2e_in_the_plan() -> None:
    """ORDERING: ``framework_build`` must come BEFORE ``e2e_test`` (an e2e drives a built
    app). The owner-kind sort (language → framework → addon) yields this for the curated
    stack; pin it so a future reorder cannot silently run e2e before the build."""
    plan = stack_command_plan(resolve_stack_from_declaration(_NEXTJS_PLAYWRIGHT))
    ids = list(plan.command_ids)
    assert ids.index("framework_build") < ids.index("e2e_test")
