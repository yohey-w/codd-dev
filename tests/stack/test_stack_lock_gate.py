"""Stack lock ENFORCEMENT gate — Contract Kernel v2.77b (STEP 0 characterization).

v2.77a brought the framework-stack contract LIVE (intake only). v2.77b turns the
already-existing lock logic (``codd/stack/lock.py``) into a red/green GATE so a
stack-contract drift is RED — exercised *via the real pipeline / verify CLI
entry*, mirroring the v2.77a tests (``test_stack_intake_live.py``).

This is an ENFORCEMENT step: it ADDS a gate, so anti-false-green is the entire
point. The exit gates (v3_goal_contract_kernel.md §"v2.77b — Stack Lock
Enforcement"):
  1. stack lock DRIFT fixture is RED;
  2. valid lock fixture is GREEN;
  3. repair cannot silently refresh the lock to make the gate green (anti-gaming).

Plus the named "B. Live stack consumption gate" requirement "stack lock drift =
RED", and the behaviour-preserving guarantee: a project WITHOUT a ``stack:`` block
is byte-identical (no lock gate at all).

Anti-gaming locus (the crux): ``verify_lock(contract, build_lock(contract))`` is
ALWAYS ok by construction, so a drift can be masked by rewriting the lock. The
protection lives in the WRITE path — the enforcement gate WRITES a lock only when
one is ABSENT in a genuine first generation, and NEVER overwrites an existing
lock. A committed lock that drifts goes RED and is not refreshed.
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
    save_session,
)
from codd.stack.lock import (
    LOCK_DRIFT,
    LOCK_GENERATED,
    LOCK_MISSING,
    LOCK_OK,
    bootstrap_stack_lock,
    build_lock,
    dump_lock,
    enforce_stack_lock,
    stack_lock_path,
)
from codd.stack.command_plan import StackCommandSlotResult
from codd.stack.project import resolve_project_stack
from codd.stack.resolve import resolve_stack_from_declaration


def _passing_stack_executor(slot, project_root, *, timeout):  # noqa: ANN001
    """A v2.77c/d materialization executor that records the invoked slot and passes.

    These v2.77b tests assert LOCK behaviour, not whether the curated framework/addon
    commands actually run (CI has no node/npx). A passing executor isolates the lock
    gate from the command-execution gate (the same isolation technique the tests
    already use: stubbing stage bodies, and writing a matching lock).

    HONEST under v2.77d authenticity: for a TEST-kind slot (e.g. ``e2e_test``) it
    writes a REAL passing report to the slot's evidence path so the authenticity gate
    observes ≥1 test (a fake may fake process execution, but must NOT fake the
    classifier — it produces the artifact the real command would have). Non-test slots
    carry honest non-no-op argv from the curated profiles."""
    from codd.stack.command_authenticity import (
        StackCommandObservationKind,
        resolve_stack_command_observation_policy,
    )
    from codd.stack.command_plan import stack_command_evidence_path

    policy = resolve_stack_command_observation_policy(slot.slot_id)
    if (
        policy is not None
        and policy.kind is StackCommandObservationKind.TEST_REPORT
        and (slot.report_capture or "").strip().lower() == "stdout"
    ):
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
                                    "title": "smoke",
                                    "file": "tests/e2e/smoke.spec.ts",
                                    "tests": [
                                        {
                                            "title": "smoke",
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
    return StackCommandSlotResult(
        slot_id=slot.slot_id,
        owner=slot.owner,
        command_str=slot.command_str,
        spawned=True,
        returncode=0,
        timed_out=False,
    )

# Reuse the curated profiles already in codd/stack/profiles/ (the 42-test
# subsystem), exactly as the v2.77a intake tests do.
_VALID_STACK = {
    "language": "typescript",
    "frameworks": ["nextjs"],
    "addons": ["prisma", "playwright"],
}
# A DIFFERENT (still valid) declaration → a different ResolvedStackContract, so a
# different content_hash: this is what "drift" looks like against a pinned lock.
_DRIFTED_STACK = {
    "language": "typescript",
    "frameworks": ["nextjs"],
    "addons": ["prisma"],
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
    """Commit a stack lock pinned to ``declaration``'s resolved contract."""
    contract = resolve_stack_from_declaration(declaration)
    path = stack_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_lock(build_lock(contract)), encoding="utf-8")
    return path


class _StageStubPipeline(GreenfieldPipeline):
    """The REAL pipeline with every STAGE BODY replaced by a no-op marker.

    Drives the genuine ``run()`` entry — so the real stack intake AND the real
    v2.77b lock-enforcement gate (both in ``run()``, before the stage loop) are
    exercised — while each stage body trivially marks done. (Same technique as
    the v2.77a ``test_stack_intake_live.py``.)
    """

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


def _run_capturing(project: Path, *, resume: bool = False) -> tuple[object, list[str]]:
    lines: list[str] = []
    # Inject a passing v2.77c stack-command executor so the curated framework/addon
    # commands are not really run (CI has no node/npx) — the lock behaviour under test
    # is unaffected. A no-stack project never invokes it (hard branch on contract).
    result = _StageStubPipeline(
        echo=lines.append, stack_command_executor=_passing_stack_executor
    ).run(project, resume=resume)
    return result, lines


# ═══════════════════════════════════════════════════════════════════════════
# Exit gate 1 — stack lock DRIFT fixture is RED
# ═══════════════════════════════════════════════════════════════════════════

def test_greenfield_drift_is_red(tmp_path: Path) -> None:
    """A committed lock that no longer matches the resolved contract → RED."""
    project = _make_project(tmp_path, stack=_VALID_STACK)
    # Commit a lock pinned to a DIFFERENT (drifted) contract → the live contract
    # diverges from the pin.
    _write_lock_for(project, _DRIFTED_STACK)

    result, lines = _run_capturing(project)

    assert result.status == "failed", f"drift must be RED; got {result.status}"
    assert result.failed_stage == "stack_lock"
    assert "DRIFT" in (result.error or "")
    # The gate did NOT proceed as if the lock were fine (no false-green).
    assert any("stack lock gate" in line and "DRIFT" in line for line in lines)


def test_verify_drift_is_red(tmp_path: Path) -> None:
    """The verify CLI path reds (non-zero exit) on a drifted committed lock."""
    from codd.cli import _intake_stack_contract_for_verify

    project = _make_project(tmp_path, stack=_VALID_STACK)
    _write_lock_for(project, _DRIFTED_STACK)

    with pytest.raises(SystemExit) as excinfo:
        _intake_stack_contract_for_verify(project)
    assert excinfo.value.code != 0


def test_drift_unit_verdict(tmp_path: Path) -> None:
    """The enforcement primitive reports drift directly (both contexts RED)."""
    project = _make_project(tmp_path, stack=_VALID_STACK)
    _write_lock_for(project, _DRIFTED_STACK)
    contract = resolve_project_stack(project)

    # Drift is RED on the read-only gate AND on the creation-path bootstrap (which
    # refuses to overwrite an existing lock).
    assert enforce_stack_lock(contract, project).status == LOCK_DRIFT
    assert enforce_stack_lock(contract, project).red
    boot = bootstrap_stack_lock(contract, project)
    assert boot.status == LOCK_DRIFT and boot.red


# ═══════════════════════════════════════════════════════════════════════════
# Exit gate 2 — valid lock fixture is GREEN
# ═══════════════════════════════════════════════════════════════════════════

def test_greenfield_valid_lock_is_green(tmp_path: Path) -> None:
    """A committed lock that matches the resolved contract → no new failure."""
    project = _make_project(tmp_path, stack=_VALID_STACK)
    _write_lock_for(project, _VALID_STACK)  # pin matches the live contract

    result, lines = _run_capturing(project)

    assert result.status == "success", f"valid lock must be GREEN; {getattr(result,'error',None)}"
    assert any("stack lock gate" in line and "OK" in line for line in lines)
    # The lock verdict is recorded in the run trace (observable, like the hash).
    session = load_session(project)
    assert session["stack_contract"]["stack_lock_status"] == LOCK_OK


def test_verify_valid_lock_is_green(tmp_path: Path) -> None:
    """The verify CLI path does NOT raise when the committed lock matches."""
    from codd.cli import _intake_stack_contract_for_verify

    project = _make_project(tmp_path, stack=_VALID_STACK)
    _write_lock_for(project, _VALID_STACK)

    # Must not raise SystemExit. Inject a passing v2.77c executor (curated commands
    # are not runnable in CI) so this asserts the lock GREEN path, not command exec.
    _intake_stack_contract_for_verify(project, stack_command_executor=_passing_stack_executor)


def test_valid_unit_verdict(tmp_path: Path) -> None:
    project = _make_project(tmp_path, stack=_VALID_STACK)
    _write_lock_for(project, _VALID_STACK)
    contract = resolve_project_stack(project)

    gate = enforce_stack_lock(contract, project)
    assert gate.status == LOCK_OK and not gate.red


# ═══════════════════════════════════════════════════════════════════════════
# Missing lock — distinguish context (verify/resume = RED; first-gen = write once)
# ═══════════════════════════════════════════════════════════════════════════

def test_verify_missing_lock_is_red(tmp_path: Path) -> None:
    """A stack project with NO committed lock is unverifiable → RED on verify.

    Anti-false-green: you cannot claim the resolved contract matches a committed
    pin when there is no pin.
    """
    from codd.cli import _intake_stack_contract_for_verify

    project = _make_project(tmp_path, stack=_VALID_STACK)
    assert not stack_lock_path(project).exists()

    with pytest.raises(SystemExit) as excinfo:
        _intake_stack_contract_for_verify(project)
    assert excinfo.value.code != 0
    # The verify gate NEVER writes the lock (allow_generate=False) — so a missing
    # lock stays missing; it cannot be silently materialized into a green.
    assert not stack_lock_path(project).exists()


def test_greenfield_first_generation_writes_the_lock(tmp_path: Path) -> None:
    """A genuine first generation (no prior completed stage) WRITES the lock once."""
    project = _make_project(tmp_path, stack=_VALID_STACK)
    assert not stack_lock_path(project).exists()

    result, lines = _run_capturing(project)

    assert result.status == "success"
    # The lock was generated (not red) and now exists, pinned to the live contract.
    lock_path = stack_lock_path(project)
    assert lock_path.exists(), "first generation must write the lock"
    assert any("stack lock gate" in line and "generated" in line for line in lines)
    session = load_session(project)
    assert session["stack_contract"]["stack_lock_status"] == LOCK_GENERATED
    # The written lock is valid against the contract (a subsequent verify is GREEN).
    contract = resolve_project_stack(project)
    gate = enforce_stack_lock(contract, project)
    assert gate.status == LOCK_OK and not gate.red


def test_greenfield_resume_missing_lock_is_red(tmp_path: Path) -> None:
    """A RESUME (project already has completed stages) with a MISSING lock → RED.

    This is exactly the delete-and-regenerate attack surface: a project that was
    already built (so it SHOULD have a committed lock) suddenly has none. The gate
    fails closed (RED) instead of silently writing a fresh lock for the (possibly
    drifted) contract.
    """
    project = _make_project(tmp_path, stack=_VALID_STACK)
    # Seed a prior session with a completed stage → this is a RESUME, not first-gen.
    session = {
        "version": 1,
        "options": {},
        "stages": {name: {"status": "pending", "detail": ""} for name in STAGES},
        "result": {"status": "running", "failed_stage": None, "failed_unit": None, "error": None},
    }
    session["stages"]["init"]["status"] = "done"
    save_session(project, session)
    assert not stack_lock_path(project).exists()

    result, lines = _run_capturing(project, resume=True)

    assert result.status == "failed", "resume with missing lock must be RED"
    assert result.failed_stage == "stack_lock"
    assert "MISSING" in (result.error or "")
    # Fail closed: the gate did NOT write a lock on a resume.
    assert not stack_lock_path(project).exists()


def test_missing_unit_verdict(tmp_path: Path) -> None:
    project = _make_project(tmp_path, stack=_VALID_STACK)
    contract = resolve_project_stack(project)

    # The read-only gate → missing is RED, and it NEVER writes a lock.
    gate = enforce_stack_lock(contract, project)
    assert gate.status == LOCK_MISSING and gate.red
    assert not stack_lock_path(project).exists()


def test_bootstrap_writes_missing_lock_then_enforces_green(tmp_path: Path) -> None:
    """The creation-path bootstrap writes a missing lock once → GREEN; idempotent."""
    project = _make_project(tmp_path, stack=_VALID_STACK)
    contract = resolve_project_stack(project)
    assert not stack_lock_path(project).exists()

    gate = bootstrap_stack_lock(contract, project)
    assert gate.status == LOCK_GENERATED and not gate.red
    assert stack_lock_path(project).exists()

    # Re-bootstrapping an existing (valid) lock does NOT overwrite — it enforces.
    before = stack_lock_path(project).read_bytes()
    gate2 = bootstrap_stack_lock(contract, project)
    assert gate2.status == LOCK_OK and not gate2.red
    assert stack_lock_path(project).read_bytes() == before


# ═══════════════════════════════════════════════════════════════════════════
# Exit gate 3 — repair cannot SILENTLY refresh the lock to make the gate green
# ═══════════════════════════════════════════════════════════════════════════

def test_drift_lock_is_never_overwritten_by_the_gate(tmp_path: Path) -> None:
    """Neither the read-only gate NOR the creation-path bootstrap refreshes a drift.

    This is the anti-gaming crux. ``verify_lock(contract, build_lock(contract))``
    is always ok by construction, so IF anything refreshed a drifted lock it would
    turn drift-RED into green. The read-only gate writes NOTHING ever, and bootstrap
    writes only when the lock is ABSENT (exclusive create). A drift against an
    EXISTING lock stays RED, byte-for-byte unchanged, on BOTH functions.
    """
    project = _make_project(tmp_path, stack=_VALID_STACK)
    lock_path = _write_lock_for(project, _DRIFTED_STACK)  # committed, drifted pin
    before = lock_path.read_bytes()
    contract = resolve_project_stack(project)  # the (drifted-from) live contract

    # Read-only gate: drift stays RED, lock untouched.
    gate = enforce_stack_lock(contract, project)
    assert gate.status == LOCK_DRIFT and gate.red, "drift must NOT be refreshed to green"
    assert lock_path.read_bytes() == before, "the drifted lock was silently overwritten"

    # Bootstrap (the ONLY writer) also refuses: the lock EXISTS, so it does not
    # overwrite — it enforces, and the pre-existing drift stays RED.
    boot = bootstrap_stack_lock(contract, project)
    assert boot.status == LOCK_DRIFT and boot.red, "bootstrap must NOT refresh a drift"
    assert lock_path.read_bytes() == before, "bootstrap silently overwrote a drifted lock"


def test_delete_and_regenerate_attack_via_resume_is_red(tmp_path: Path) -> None:
    """End-to-end anti-gaming: drift → delete lock → resume must NOT auto-green.

    The full attack: a drift is introduced; an attacker DELETES the committed lock
    and re-runs greenfield (a resume) hoping the "missing lock" path writes a fresh
    lock matching the now-drifted contract → green. Because a resume is NOT a first
    generation, the gate fails closed (RED), so the attack cannot silence the drift.
    """
    project = _make_project(tmp_path, stack=_VALID_STACK)
    # Simulate an already-built project: prior session with completed stages, and a
    # committed lock — then the attacker DELETES the lock after a drift is introduced.
    session = {
        "version": 1,
        "options": {},
        "stages": {name: {"status": "done", "detail": ""} for name in STAGES},
        "result": {"status": "success", "failed_stage": None, "failed_unit": None, "error": None},
    }
    save_session(project, session)
    # (No lock on disk = the deleted lock.)
    assert not stack_lock_path(project).exists()

    result, _lines = _run_capturing(project, resume=True)

    # Fails closed — the attack does not produce a green.
    assert result.status == "failed"
    assert result.failed_stage == "stack_lock"
    assert not stack_lock_path(project).exists()


def test_rerun_without_resume_on_built_project_does_not_re_bootstrap(tmp_path: Path) -> None:
    """A re-run WITHOUT --resume on an already-built project is NOT a first gen.

    GPT-consult leak: "absence of session ≠ first generation". A run without
    --resume builds a FRESH in-memory session (all-pending). first_generation is
    therefore read from the ON-DISK session: an already-built project (persisted
    completed stages) with a DELETED lock must be RED — not silently re-bootstrapped
    to green for the (possibly drifted) contract.
    """
    project = _make_project(tmp_path, stack=_VALID_STACK)
    # Persist a session showing the project was already fully built, and DELETE the
    # lock (the attacker's move). Then run WITHOUT resume (fresh in-memory session).
    session = {
        "version": 1,
        "options": {},
        "stages": {name: {"status": "done", "detail": ""} for name in STAGES},
        "result": {"status": "success", "failed_stage": None, "failed_unit": None, "error": None},
    }
    save_session(project, session)
    assert not stack_lock_path(project).exists()

    result, _lines = _run_capturing(project, resume=False)  # NOTE: no --resume

    assert result.status == "failed", "re-run on a built project must not re-bootstrap"
    assert result.failed_stage == "stack_lock"
    assert not stack_lock_path(project).exists()


def test_corrupt_lock_is_red_not_silently_regenerated(tmp_path: Path) -> None:
    """A present-but-unparseable lock is RED (drift), never treated as missing.

    GPT-consult point #4: a parse error must NOT be classified as "missing" (which
    could invite regeneration). Both functions leave a broken lock untouched and RED.
    """
    project = _make_project(tmp_path, stack=_VALID_STACK)
    lock_path = stack_lock_path(project)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("\t- not: [valid: yaml", encoding="utf-8")  # malformed
    before = lock_path.read_bytes()
    contract = resolve_project_stack(project)

    gate = enforce_stack_lock(contract, project)
    assert gate.red and gate.status == LOCK_DRIFT  # NOT "missing"
    assert lock_path.read_bytes() == before

    # Bootstrap sees an EXISTING (broken) lock → does not overwrite, enforces RED.
    boot = bootstrap_stack_lock(contract, project)
    assert boot.red and boot.status == LOCK_DRIFT
    assert lock_path.read_bytes() == before


# ═══════════════════════════════════════════════════════════════════════════
# Behaviour-preserving — a project WITHOUT a stack block is byte-identical
# ═══════════════════════════════════════════════════════════════════════════

def test_no_stack_block_has_no_lock_gate_greenfield(tmp_path: Path) -> None:
    """No ``stack:`` block → no lock gate at all; run state is unchanged."""
    project = _make_project(tmp_path, stack=None)

    result, lines = _run_capturing(project)

    assert result.status == "success"
    # No lock gate trace line, no lock file written.
    assert not any("stack lock gate" in line for line in lines)
    assert not stack_lock_path(project).exists()
    session = load_session(project)
    assert "stack_contract" not in session
    assert session["result"]["status"] == "success"
    assert set(session["stages"]) == set(STAGES)


def test_no_stack_block_has_no_lock_gate_verify(tmp_path: Path) -> None:
    """No ``stack:`` block → verify path never enforces a lock (no exit, no write)."""
    from codd.cli import _intake_stack_contract_for_verify

    project = _make_project(tmp_path, stack=None)
    _intake_stack_contract_for_verify(project)  # must NOT raise
    assert not stack_lock_path(project).exists()


def test_lock_path_is_next_to_codd_yaml(tmp_path: Path) -> None:
    """The lock lives at ``<codd_dir>/stack.lock`` (next to codd.yaml)."""
    project = _make_project(tmp_path, stack=_VALID_STACK)
    assert stack_lock_path(project) == project / "codd" / "stack.lock"


# ═══════════════════════════════════════════════════════════════════════════
# Bypass closed — removing the stack: block while a committed lock remains = RED
# ═══════════════════════════════════════════════════════════════════════════

def test_removed_stack_block_with_committed_lock_is_red_greenfield(tmp_path: Path) -> None:
    """Drop the ``stack:`` block but keep the lock → RED (no silent un-governance)."""
    # Project declares NO stack, but a stack.lock is still committed (the
    # declaration was removed to dodge the gate).
    project = _make_project(tmp_path, stack=None)
    _write_lock_for(project, _VALID_STACK)
    assert stack_lock_path(project).exists()

    result, lines = _run_capturing(project)

    assert result.status == "failed", "a removed stack decl with a committed lock must be RED"
    assert result.failed_stage == "stack_lock"
    assert any("stack lock gate" in line for line in lines)


def test_removed_stack_block_with_committed_lock_is_red_verify(tmp_path: Path) -> None:
    from codd.cli import _intake_stack_contract_for_verify

    project = _make_project(tmp_path, stack=None)
    _write_lock_for(project, _VALID_STACK)

    with pytest.raises(SystemExit) as excinfo:
        _intake_stack_contract_for_verify(project)
    assert excinfo.value.code != 0


def test_no_stack_no_lock_is_byte_identical(tmp_path: Path) -> None:
    """A genuine non-stack project (no block, no lock) is unaffected — None verdict."""
    from codd.stack.lock import orphan_stack_lock

    project = _make_project(tmp_path, stack=None)
    assert not stack_lock_path(project).exists()
    # The orphan check returns None (no gate) — the byte-identical path.
    assert orphan_stack_lock(project) is None
