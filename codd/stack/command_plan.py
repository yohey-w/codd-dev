"""Materialize a ResolvedStackContract's composed commands into the run's command plan.

Contract Kernel v2.77c (Stack Command Materialization). v2.77a brought the
framework-stack contract LIVE (intake hash → trace); v2.77b made the lock a
red/green gate. This step connects the *composed commands* (and, through them, the
stack obligations) to the verify/build/test command plan the run ACTUALLY executes
— so a declared ``framework_build`` / ``e2e_test`` / ``migration_check`` slot is
genuinely invoked, not silently ignored while the run greens on the language verify
alone (the false-green this step removes, GPT-5.5 Pro consult 2026-06-21).

Two responsibilities, kept PURE (this module imports neither pipeline nor CLI code;
it raises domain errors the call-sites translate):

1. **Conflict gate** — :func:`assert_stack_contract_clean` reds on ANY composition
   conflict (command collision / unproved replace / semantic weaken / exclusive /
   deny / any future kind) via :class:`StackContractConflictError`. The composer
   (``codd.stack.compose``) is the authority that lowers those into ``Conflict``
   entries — this module does NOT reimplement merge semantics, it gates on them.
   Defensive: it also reds on ``not is_clean`` / ``not strict_ok`` so an invalid
   contract state (conflicts cleared but a flag still false) can never sneak to
   green. "last-wins" is forbidden because the composer records a ``Conflict``
   instead of silently overwriting; this gate turns that record into a RED.

2. **Materialization** — :func:`stack_command_plan` builds a deterministic, ordered
   :class:`StackCommandPlan` from ``contract.commands`` + ``contract.command_owners``
   (NO framework-name literal — the plan is driven entirely by the resolved
   contract), and :func:`execute_stack_command_plan` actually invokes each slot by
   exit code. A non-zero / un-spawnable / timed-out slot is RED. This is exit-code
   pass/fail ONLY — proving the declared stack command slots were INVOKED. Proving
   those invoked commands are *meaningful* (no-op / ``"build": "true"`` / empty
   script / missing reporter / observed-no-tests) is v2.77d (command authenticity),
   a SEPARATE seam; the obligation-checker gate (``verify_project_stack``) is v2.77e.

A project with no ``stack:`` block resolves to ``contract is None`` and never
reaches this module — the call-sites hard-branch on ``contract is not None``, so
non-stack runs are byte-identical (no plan, no new trace keys, no execution).
"""

from __future__ import annotations

import os
import subprocess  # noqa: S404 — argv comes from the trusted resolved stack contract, shell=False.
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from codd.languages.profile import CommandSpec

from .compose import ResolvedStackContract

#: Default wall-clock cap for a single stack command slot. A slot that exceeds it is
#: RED (a timeout is never green), never a hang that blocks the gate forever. Mirrors
#: ``codd.languages.verify_executor.DEFAULT_VERIFY_TIMEOUT_SECONDS``.
DEFAULT_STACK_COMMAND_TIMEOUT_SECONDS = 1800

#: Layer-kind ordering for a deterministic plan: language slots first, then
#: framework, then addon (the design's composition order — §合成順序). Unknown
#: owner kinds sort last (stable, after the known kinds) so the plan is still
#: deterministic for a future layer kind.
_OWNER_KIND_ORDER = {"language": 0, "framework": 1, "addon": 2, "runtime": 3, "platform": 4}


class StackContractConflictError(RuntimeError):
    """A resolved stack contract carries a composition conflict — the gate reds.

    Raised by :func:`assert_stack_contract_clean`. The call-sites translate it to
    their context's RED (``StageError`` in the greenfield pipeline → ``_fail``;
    ``SystemExit`` non-zero on the verify CLI path).
    """


class StackCommandMaterializationError(RuntimeError):
    """A composed stack command slot failed when actually invoked (exit-code RED).

    Raised by :func:`execute_stack_command_plan` when a slot exits non-zero, cannot
    spawn, or times out. (Authenticity — whether an exit-0 slot was *meaningful* —
    is v2.77d, NOT raised here.)
    """


#: Harness-owned directory (under the project) where a stack command's CURRENT-RUN
#: report evidence is teed (for ``capture: stdout`` commands — ``npx playwright test
#: --reporter=json`` streams its report to stdout). A per-slot file under here is the
#: authenticity layer's "current-run evidence" (v2.77d): the executor writes THIS run's
#: stdout to it, the authenticity layer reads ONLY it, so a stale report from a prior
#: run can never be mistaken for this run's output (anti-false-green stale-report rule).
STACK_COMMAND_EVIDENCE_DIR = ".codd/stack-command-evidence"


def _owner_sort_key(owner: str, slot_id: str) -> tuple[int, str, str]:
    kind = owner.split(":", 1)[0] if owner else ""
    return (_OWNER_KIND_ORDER.get(kind, len(_OWNER_KIND_ORDER)), owner, slot_id)


def stack_command_evidence_path(slot: "StackCommandSlot", project_root: Path) -> Path:
    """The deterministic per-slot current-run report evidence file (``capture: stdout``).

    A stable path under :data:`STACK_COMMAND_EVIDENCE_DIR` keyed by the slot's owner +
    id, so the executor (which tees this run's stdout into it) and the authenticity
    layer (which parses it) agree on ONE location. Owner/slot are sanitized to a safe
    filename. Always inside ``project_root`` (the authenticity layer fails closed on an
    out-of-tree report).
    """
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in f"{slot.owner}__{slot.slot_id}")
    return project_root / STACK_COMMAND_EVIDENCE_DIR / f"{safe}.stdout"


def assert_stack_contract_clean(contract: ResolvedStackContract) -> None:
    """Conflict gate (v2.77c): red on ANY composition conflict / unclean state.

    Anti-false-green: a command collision, an unproved replace (a layer replacing
    another's command with different argv and no ``replace_with_proof`` — exactly a
    command conflict today), a semantic weaken (an addon lowering a framework
    obligation's severity), an exclusive conflict, a deny, or any future conflict
    kind makes the contract NOT clean → :class:`StackContractConflictError`. The
    composer already records these as ``Conflict`` entries (it refuses last-wins);
    this turns that record into a gate. Defensive triple-check (``conflicts`` /
    ``is_clean`` / ``strict_ok``) so a flag desync can never sneak to green.
    """
    if contract.conflicts or not contract.is_clean or not contract.strict_ok:
        reasons = (
            "; ".join(f"[{c.kind}] {c.detail}" for c in contract.conflicts)
            or "resolved stack contract is not in a clean composed state"
        )
        raise StackContractConflictError(
            f"stack composition conflict ({contract.stack_id}): {reasons}. "
            "A command collision / unproved replace / weakened obligation / exclusive "
            "conflict is RED — the composer does NOT silently last-wins-merge a "
            "collision, and the run will not materialize a command plan from a "
            "conflicted contract. Resolve the conflict in the stack profiles (or "
            "declare an explicit replace_with_proof once that mechanism exists)."
        )


@dataclass(frozen=True)
class StackCommandSlot:
    """One materialized command slot in the run's plan, with its owning namespace.

    ``slot_id`` is the command id (e.g. ``typecheck`` / ``framework_build`` /
    ``e2e_test``); ``owner`` is its composed owner (``language:typescript`` /
    ``framework:nextjs`` / ``addon:playwright``) — the namespace ownership that makes
    a green in one slot never imply another. ``argv`` is the canonical (shell-free)
    command. ``cwd``/``env`` may carry literal layout placeholders (not substituted
    here — Phase-1 contract).
    """

    slot_id: str
    owner: str
    argv: tuple[str, ...]
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    requires_materialized_deps: bool = False
    report_path: str | None = None
    report_adapter: str | None = None
    report_capture: str | None = None

    @property
    def command_str(self) -> str:
        return " ".join(self.argv)

    def to_record(self) -> dict[str, object]:
        rec: dict[str, object] = {
            "slot": self.slot_id,
            "owner": self.owner,
            "argv": list(self.argv),
        }
        if self.cwd:
            rec["cwd"] = self.cwd
        return rec


@dataclass(frozen=True)
class StackCommandPlan:
    """The deterministic, contract-driven plan of composed stack command slots.

    Built ENTIRELY from ``contract.commands`` + ``contract.command_owners`` (no
    framework literal): changing the stack profile/declaration changes the resolved
    commands and thus this plan. ``content_hash`` ties the plan to the contract it
    came from (observability — recorded in the run trace next to the stack hash).
    """

    stack_id: str
    content_hash: str
    slots: tuple[StackCommandSlot, ...]

    @property
    def command_ids(self) -> tuple[str, ...]:
        return tuple(s.slot_id for s in self.slots)

    def to_record(self) -> dict[str, object]:
        return {
            "stack_id": self.stack_id,
            "stack_contract_hash": self.content_hash,
            "command_slots": [s.to_record() for s in self.slots],
        }


def _slot_from_command(slot_id: str, owner: str, spec: CommandSpec) -> StackCommandSlot:
    report = spec.report
    return StackCommandSlot(
        slot_id=slot_id,
        owner=owner,
        argv=tuple(spec.argv),
        cwd=spec.cwd,
        env={str(k): str(v) for k, v in spec.env.items()},
        requires_materialized_deps=bool(spec.requires_materialized_deps),
        report_path=(report.path if report else None),
        report_adapter=(report.adapter if report else None),
        report_capture=(report.capture if report else None),
    )


def stack_command_plan(contract: ResolvedStackContract) -> StackCommandPlan:
    """Materialize the composed stack commands into a deterministic command plan.

    Runs the conflict gate FIRST (:func:`assert_stack_contract_clean`) — a plan is
    NEVER built from a conflicted contract. Then maps every composed command to a
    :class:`StackCommandSlot` carrying its owning namespace, ordered deterministically
    by owner kind (language → framework → addon) then owner then slot id. No
    framework-name literal: the plan is a pure projection of the resolved contract.
    """
    assert_stack_contract_clean(contract)
    owners = contract.command_owners
    slots = [
        _slot_from_command(slot_id, owners.get(slot_id, ""), spec)
        for slot_id, spec in contract.commands.items()
    ]
    slots.sort(key=lambda s: _owner_sort_key(s.owner, s.slot_id))
    return StackCommandPlan(
        stack_id=contract.stack_id,
        content_hash=contract.content_hash,
        slots=tuple(slots),
    )


@dataclass(frozen=True)
class StackCommandSlotResult:
    """The exit-code outcome of invoking one stack command slot.

    ``spawned`` is False when the tool could not be executed at all (missing
    binary / spawn error); ``timed_out`` True when it exceeded the cap. ``ok`` is the
    anti-false-green verdict: it requires the slot to have SPAWNED, NOT timed out,
    and exited 0. (It says nothing about whether the command was *meaningful* — that
    is v2.77d.)
    """

    slot_id: str
    owner: str
    command_str: str
    spawned: bool
    returncode: int | None
    timed_out: bool
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.spawned and not self.timed_out and self.returncode == 0


@dataclass(frozen=True)
class StackCommandPlanResult:
    """Aggregate result of executing a whole :class:`StackCommandPlan`."""

    results: tuple[StackCommandSlotResult, ...]

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    @property
    def failed(self) -> tuple[StackCommandSlotResult, ...]:
        return tuple(r for r in self.results if not r.ok)

    @property
    def executed_slot_ids(self) -> tuple[str, ...]:
        return tuple(r.slot_id for r in self.results)


class StackCommandExecutor(Protocol):
    """Seam that invokes one stack command slot and reports its exit-code outcome.

    The default (:func:`default_stack_command_executor`) shells out via
    ``subprocess.run`` (trusted argv, ``shell=False``). Tests inject a recording /
    sentinel-writing executor so they can prove the declared slots are actually
    invoked WITHOUT needing real Next.js / Playwright (GPT-consult: "use a fake
    fixture command that writes a sentinel or a mocked executor that records called
    command ids").
    """

    def __call__(
        self, slot: StackCommandSlot, project_root: Path, *, timeout: float
    ) -> StackCommandSlotResult: ...


def default_stack_command_executor(
    slot: StackCommandSlot, project_root: Path, *, timeout: float
) -> StackCommandSlotResult:
    """Invoke a slot via ``subprocess.run`` (exit-code only; trusted argv, shell=False).

    Mirrors :func:`codd.languages.verify_executor.execute_verify_plan`'s spawn
    handling: a missing binary / spawn error → ``spawned=False`` (RED); a timeout →
    ``timed_out=True`` (RED); otherwise the real exit code.

    It performs the stale-report / capture transport the authenticity layer (v2.77d)
    depends on — NOT report parsing/observation (that stays in
    :mod:`codd.stack.command_authenticity`):

    * BEFORE running, for a ``capture: stdout`` slot, unlink any stale current-run
      evidence file so a leftover green report from a prior run can never be read as
      this run's output (the canonical stale-report false-green).
    * AFTER running, tee THIS run's stdout into that evidence file so the authenticity
      layer parses the current run's report. This is capture TRANSPORT only — the
      parser/observation lives outside the executor.
    """
    cwd = (project_root / slot.cwd) if slot.cwd else project_root
    env = os.environ.copy()
    env.update(slot.env)

    # Stale-report prevention: for a stdout-captured report, remove the prior run's
    # evidence BEFORE spawning (mirror verify_executor step b).
    evidence_path: Path | None = None
    if (slot.report_capture or "").strip().lower() == "stdout":
        evidence_path = stack_command_evidence_path(slot, project_root)
        try:
            evidence_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # An unremovable stale evidence file is an observability hazard — but the
            # exit-code executor does not classify; we leave it and let the authenticity
            # layer fail-closed when it parses (it reads only what we write next).
            pass

    try:
        completed = subprocess.run(  # noqa: S603 — trusted argv from the resolved contract, shell=False.
            list(slot.argv),
            shell=False,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, OSError) as exc:
        return StackCommandSlotResult(
            slot_id=slot.slot_id,
            owner=slot.owner,
            command_str=slot.command_str,
            spawned=False,
            returncode=None,
            timed_out=False,
            detail=f"stack command slot could not spawn ({slot.command_str!r}): {exc}",
        )
    except subprocess.TimeoutExpired:
        return StackCommandSlotResult(
            slot_id=slot.slot_id,
            owner=slot.owner,
            command_str=slot.command_str,
            spawned=True,
            returncode=None,
            timed_out=True,
            detail=f"stack command slot timed out after {timeout}s ({slot.command_str!r})",
        )

    # Capture transport: persist THIS run's stdout to the evidence file (mirror
    # verify_executor step d) so the authenticity layer observes the current report.
    if evidence_path is not None:
        try:
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            evidence_path.write_text(completed.stdout or "", encoding="utf-8")
        except OSError:
            # Could not persist — the authenticity layer will see a missing report and
            # fail-closed (REPORT_MISSING). The executor still reports the exit code.
            pass

    return StackCommandSlotResult(
        slot_id=slot.slot_id,
        owner=slot.owner,
        command_str=slot.command_str,
        spawned=True,
        returncode=completed.returncode,
        timed_out=False,
        detail=("" if completed.returncode == 0 else f"exit {completed.returncode}"),
    )


def execute_stack_command_plan(
    plan: StackCommandPlan,
    project_root: Path,
    *,
    executor: StackCommandExecutor | None = None,
    timeout: float | None = None,
) -> StackCommandPlanResult:
    """Invoke every slot in ``plan`` (exit-code pass/fail) and aggregate the result.

    This is the materialization that makes "stack obligations affect actual
    commands": the composed ``framework_build`` / ``e2e_test`` / ``migration_check``
    slots are actually run, so a slot that would fail is no longer silently skipped
    while the run greens on the language verify alone. ``executor`` is the injectable
    seam (default: real subprocess). Exit-code ONLY — authenticity is v2.77d.
    """
    run_executor = executor if executor is not None else default_stack_command_executor
    run_timeout = timeout if timeout is not None else DEFAULT_STACK_COMMAND_TIMEOUT_SECONDS
    results = tuple(
        run_executor(slot, project_root, timeout=run_timeout) for slot in plan.slots
    )
    return StackCommandPlanResult(results=results)


def materialize_stack_command_plan(
    contract: ResolvedStackContract,
    project_root: Path,
    *,
    executor: StackCommandExecutor | None = None,
    timeout: float | None = None,
) -> tuple[StackCommandPlan, StackCommandPlanResult]:
    """Conflict-gate → build the plan → execute it, raising the domain RED on failure.

    The single entry the pipeline / verify call-sites use:

    * the conflict gate (:func:`assert_stack_contract_clean`, inside
      :func:`stack_command_plan`) raises :class:`StackContractConflictError` on any
      conflict;
    * executing the plan raises :class:`StackCommandMaterializationError` if any slot
      is not ``ok`` (non-zero / un-spawnable / timed out).

    Returns ``(plan, result)`` on success so the caller can record the materialized
    plan (and which slots executed) in the run trace.
    """
    plan = stack_command_plan(contract)  # conflict gate runs here
    result = execute_stack_command_plan(
        plan, project_root, executor=executor, timeout=timeout
    )
    if not result.ok:
        detail = "; ".join(
            f"{r.slot_id} ({r.owner}): {r.detail or 'failed'}" for r in result.failed
        )
        raise StackCommandMaterializationError(
            f"stack command materialization failed ({plan.stack_id}): {detail}. "
            "A composed stack command slot was invoked and did NOT pass (exit-code) — "
            "the declared framework/addon command is part of the run's command plan "
            "and must run green (anti-false-green: a failing framework_build/e2e_test "
            "is not silently skipped while the language verify greens alone)."
        )

    # AUTHENTICITY (Contract Kernel v2.77d): exit 0 is necessary but NOT sufficient.
    # Each slot must prove it did its job for its KIND — a no-op / observed-no-tests /
    # missing-or-unreadable-report / observed-failure command is RED even on exit 0.
    # Lazy import to avoid a module-load cycle (command_authenticity imports this
    # module for the slot/result types). Raises StackCommandAuthenticityError on RED.
    from codd.stack.command_authenticity import assert_stack_commands_authentic

    assert_stack_commands_authentic(
        plan,
        result,
        project_root,
        contract_policies=contract.command_observation_policies,
    )
    return plan, result
