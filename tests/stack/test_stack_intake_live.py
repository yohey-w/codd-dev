"""Live stack-contract intake — Contract Kernel v2.77a (STEP 0 characterization).

These tests prove the (formerly DORMANT) framework-stack contract is now
LIVE-CONSUMED by the greenfield pipeline and the verify CLI path — *via the real
pipeline entry*, not a unit call — without enforcing any obligation yet (intake
only; obligation enforcement is v2.77b-e).

Exit gates (v3_goal_contract_kernel.md §"v2.77a — Stack Contract Intake"):
  1. the stack contract hash appears in the run trace / record;
  2. changing the stack profile/declaration changes that hash (and thus the plan);
  3. a PRODUCTION caller (the pipeline / verify path) consumes the contract — the
     "no non-test caller = 0 state" is eliminated.

Anti-false-green: a declared-but-unresolvable ``stack:`` block is an HONEST
failure, never a silent skip (a declared-but-broken stack must NOT proceed as if
no stack were declared).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codd.greenfield.pipeline import (
    STAGES,
    GreenfieldPipeline,
    StageError,
    load_session,
)
from codd.stack.command_plan import StackCommandSlotResult


def _passing_stack_executor(slot, project_root, *, timeout):  # noqa: ANN001
    """A v2.77c materialization executor that records the invoked slot and passes.

    These v2.77a/b tests assert INTAKE + LOCK behaviour, not whether the curated
    framework/addon commands (next build / playwright / prisma) actually run — those
    are not runnable in CI. A passing executor isolates intake/lock from the new
    v2.77c command-execution gate, exactly as these tests already stub stage bodies
    to isolate intake from the stage gates (and as v2.77b's tests wrote a matching
    lock to isolate intake from the lock gate). It still proves the slots are INVOKED.
    """
    return StackCommandSlotResult(
        slot_id=slot.slot_id,
        owner=slot.owner,
        command_str=slot.command_str,
        spawned=True,
        returncode=0,
        timed_out=False,
    )


# A valid stack uses the curated Next.js/Prisma/Playwright profiles already in
# codd/stack/profiles/ (the 42-test subsystem) — language typescript so the
# language registry resolves it too.
_VALID_STACK = {
    "language": "typescript",
    "frameworks": ["nextjs"],
    "addons": ["prisma", "playwright"],
}
# A DIFFERENT (still valid) declaration: same language, fewer addons. Resolves to
# a different ResolvedStackContract, so a different content_hash (exit gate 2).
_VALID_STACK_VARIANT = {
    "language": "typescript",
    "frameworks": ["nextjs"],
    "addons": ["prisma"],
}
# A declared-but-BROKEN stack: an unknown framework id. resolve_stack_from_
# declaration RAISES — intake must surface this honestly, never silently skip.
_BROKEN_STACK = {
    "language": "typescript",
    "frameworks": ["this-framework-does-not-exist"],
}


def _make_project(tmp_path: Path, *, stack: dict | None) -> Path:
    """A pre-initialized CoDD project; optionally with a ``stack:`` block.

    All pipeline stages are stubbed (see :func:`_noop_pipeline`), so the project
    only needs a discoverable codd.yaml — the run reaches intake (which is before
    the stage loop) and then trivially completes.
    """
    project = tmp_path / "proj"
    codd_dir = project / "codd"
    codd_dir.mkdir(parents=True)
    config: dict = {"project": {"name": "proj", "language": "typescript"}}
    if stack is not None:
        config["stack"] = stack
    (codd_dir / "codd.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return project


class _StageStubPipeline(GreenfieldPipeline):
    """The REAL pipeline with every STAGE BODY replaced by a no-op marker.

    We drive the genuine ``GreenfieldPipeline.run()`` entry — so the real stack
    intake (which lives in ``run()``, BEFORE the stage loop), the real session
    record, and the real run trace are all exercised — while replacing each stage
    body with a trivial "mark done" so the run completes without any AI or real
    stage work. This isolates the INTAKE behaviour under test from every unrelated
    stage gate (VB SSOT, oracle, etc.). It does NOT stub ``run()`` or the intake.
    """

    def _stage_init(self, project_root, record, options):  # noqa: D401, ANN001
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


def _noop_pipeline(echo) -> GreenfieldPipeline:
    """A pipeline that runs the REAL ``run()`` entry (and real intake) to completion
    WITHOUT any AI or real stage work — see :class:`_StageStubPipeline`.

    Injects a passing v2.77c stack-command executor so the curated framework/addon
    commands are not really run (CI has no node/npx); the intake + lock behaviour
    under test is unaffected. For a NO-STACK project the executor is never invoked
    (the pipeline hard-branches on ``contract is not None``)."""
    return _StageStubPipeline(echo=echo, stack_command_executor=_passing_stack_executor)


def _run_capturing(project: Path) -> tuple[object, list[str]]:
    lines: list[str] = []
    result = _noop_pipeline(lines.append).run(project)
    return result, lines


# ── Exit gate 1 + 3: hash appears in the run trace + record via a PROD caller ──

def test_declared_stack_hash_appears_in_trace_and_record(tmp_path: Path) -> None:
    project = _make_project(tmp_path, stack=_VALID_STACK)

    result, lines = _run_capturing(project)

    # The pipeline (a PRODUCTION caller) consumed the contract — eliminating the
    # "no non-test caller = 0" dormant state.
    assert result.status == "success"
    trace_line = next(
        (line for line in lines if "stack contract intake" in line and "stack_contract_hash=" in line),
        None,
    )
    assert trace_line is not None, f"no stack-intake trace line emitted; got: {lines}"
    assert "typescript+nextjs+prisma+playwright" in trace_line

    # ...and it landed in the run record (the session checkpoint), with a real hash.
    session = load_session(project)
    assert session is not None
    record = session.get("stack_contract")
    assert record is not None, "stack contract not recorded in the run record"
    assert record["resolved_stack_id"] == "typescript+nextjs+prisma+playwright"
    assert record["stack_contract_hash"].startswith("sha256:")
    assert record["resolved_stack_layers"] == [
        "language:typescript",
        "framework:nextjs",
        "addon:prisma",
        "addon:playwright",
    ]


# ── Exit gate 2: changing the stack profile/declaration changes the hash/plan ──

def test_changing_stack_profile_changes_the_hash(tmp_path: Path) -> None:
    proj_full = _make_project(tmp_path / "a", stack=_VALID_STACK)
    proj_variant = _make_project(tmp_path / "b", stack=_VALID_STACK_VARIANT)

    _run_capturing(proj_full)
    _run_capturing(proj_variant)

    hash_full = load_session(proj_full)["stack_contract"]["stack_contract_hash"]
    hash_variant = load_session(proj_variant)["stack_contract"]["stack_contract_hash"]

    # Different declarations resolve to different contracts → different plan inputs.
    assert hash_full != hash_variant
    # And the recorded stack ids differ accordingly (the plan visibly changed).
    assert (
        load_session(proj_full)["stack_contract"]["resolved_stack_id"]
        != load_session(proj_variant)["stack_contract"]["resolved_stack_id"]
    )


# ── Behaviour-preserving: a project WITHOUT a stack block is unaffected ──

def test_no_stack_block_is_behaviour_preserving(tmp_path: Path) -> None:
    project = _make_project(tmp_path, stack=None)

    result, lines = _run_capturing(project)

    assert result.status == "success"
    # No stack hash anywhere in the trace.
    assert not any("stack_contract_hash=" in line for line in lines)
    # The intake emits a single, explicit opt-out trace line (NO-OP marker), but
    # adds NOTHING to the run record — byte-identical run state for non-stack
    # projects (the vast majority).
    session = load_session(project)
    assert session is not None
    assert "stack_contract" not in session

    # Cross-check: a run with NO stack block produces a session whose stage set
    # and result are exactly what a non-stack run always produced.
    assert session["result"]["status"] == "success"
    assert set(session["stages"]) == set(STAGES)


# ── Anti-false-green: a declared-but-broken stack is an HONEST error ──

def test_declared_but_broken_stack_is_honest_error_not_silent_skip(tmp_path: Path) -> None:
    project = _make_project(tmp_path, stack=_BROKEN_STACK)

    result, lines = _run_capturing(project)

    # The run FAILS honestly at intake — it does NOT silently proceed as if no
    # stack were declared (which would be a false-green).
    assert result.status == "failed"
    assert result.error is not None
    assert "stack contract intake failed" in result.error
    # The failure mentions the unresolved declaration — not swallowed.
    assert "this-framework-does-not-exist" in result.error or "unknown" in result.error.lower()
    # Crucially: NO success, and no stack contract recorded as if it resolved.
    session = load_session(project)
    assert session is not None
    assert session["result"]["status"] == "failed"
    assert "stack_contract" not in session


def test_malformed_stack_block_is_honest_error(tmp_path: Path) -> None:
    # A ``stack:`` block missing the required ``language`` key is malformed —
    # resolve_stack_from_declaration raises ValueError; intake must red honestly.
    project = _make_project(tmp_path, stack={"frameworks": ["nextjs"]})

    result, _lines = _run_capturing(project)

    assert result.status == "failed"
    assert result.error is not None
    assert "stack contract intake failed" in result.error


# ── Exit gate 1 + 3 on the VERIFY path: the verify CLI consumes the contract ──

def test_verify_path_emits_stack_hash_and_reds_on_broken_stack(tmp_path: Path) -> None:
    from codd.cli import _intake_stack_contract_for_verify
    from codd.stack.lock import build_lock, dump_lock, stack_lock_path
    from codd.stack.resolve import resolve_stack_from_declaration

    # Valid stack → the verify-path intake emits the hash to the trace. v2.77b adds
    # a stack-lock gate to this same path, so a verifiable stack project must have a
    # committed lock matching its contract (an unpinned stack is RED — see
    # test_stack_lock_gate.py); write the matching lock so this v2.77a-intake
    # assertion exercises the GREEN path.
    proj_ok = _make_project(tmp_path / "ok", stack=_VALID_STACK)
    _lock_path = stack_lock_path(proj_ok)
    _lock_path.parent.mkdir(parents=True, exist_ok=True)
    _lock_path.write_text(
        dump_lock(build_lock(resolve_stack_from_declaration(_VALID_STACK))), encoding="utf-8"
    )
    captured: list[str] = []
    import click

    # Capture click.echo output deterministically.
    orig_echo = click.echo
    try:
        click.echo = lambda *a, **k: captured.append(a[0] if a else "")
        # v2.77c adds a stack-command materialization gate to this same path; inject a
        # passing executor so the curated commands are not really run (CI has no node).
        _intake_stack_contract_for_verify(proj_ok, stack_command_executor=_passing_stack_executor)
    finally:
        click.echo = orig_echo
    assert any("stack contract intake" in line and "stack_contract_hash=" in line for line in captured)

    # No stack block → no-op, no exit, nothing emitted.
    proj_none = _make_project(tmp_path / "none", stack=None)
    _intake_stack_contract_for_verify(proj_none)  # must NOT raise

    # Declared-but-broken stack → honest non-zero exit (never a silent skip).
    proj_broken = _make_project(tmp_path / "broken", stack=_BROKEN_STACK)
    with pytest.raises(SystemExit) as excinfo:
        _intake_stack_contract_for_verify(proj_broken)
    assert excinfo.value.code != 0
