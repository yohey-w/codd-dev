"""Project-level wiring seam: resolve + enforce a project's declared stack.

This is the bridge the greenfield/verify pipeline calls. A project opts into the
framework layer by declaring a ``stack:`` block in its codd.yaml (design §1)::

    stack:
      language: typescript
      frameworks: [nextjs]
      addons: [prisma, playwright]

* :func:`resolve_project_stack` reads that block and returns the
  ResolvedStackContract — or ``None`` if the project declares no stack (the
  framework layer is OPT-IN; a project without a ``stack:`` block is completely
  unaffected, so Python/Go/plain-TS projects see no behaviour change).
* :func:`verify_project_stack` resolves then runs the obligation checkers,
  returning the gate result (or ``None`` for a no-stack project).

The pipeline call-site reds on ``result is not None and not result.passed``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .compose import ResolvedStackContract
from .obligations import ObligationResult, enforce_obligations
from .resolve import resolve_stack_from_declaration


def _read_stack_declaration(project_root: str | Path) -> Mapping[str, Any] | None:
    """Return the ``stack:`` block from the project's codd.yaml, or ``None``."""
    from codd.config import load_project_config

    try:
        config = load_project_config(Path(project_root))
    except (FileNotFoundError, ValueError):
        return None
    decl = config.get("stack")
    if not decl or not isinstance(decl, Mapping):
        return None
    return decl


def resolve_project_stack(project_root: str | Path) -> ResolvedStackContract | None:
    """Resolve a project's declared stack into a contract, or ``None`` if none.

    ``None`` means "this project does not use the framework layer" — never an
    error (the framework layer is opt-in).
    """
    decl = _read_stack_declaration(project_root)
    if decl is None:
        return None
    return resolve_stack_from_declaration(decl)


def verify_project_stack(
    project_root: str | Path, **checker_inputs: Any
) -> ObligationResult | None:
    """Resolve + enforce a project's stack obligations (the framework verify gate).

    Returns ``None`` for a project that declares no stack (no-op gate); otherwise
    the :class:`ObligationResult` (``result.passed`` is the gate verdict;
    ``checker_inputs`` such as ``report_data=`` / ``report_path=`` are forwarded
    to the checkers).
    """
    contract = resolve_project_stack(project_root)
    if contract is None:
        return None
    return enforce_obligations(contract, project_root=project_root, **checker_inputs)


class StackObligationGateError(RuntimeError):
    """The stack obligation-checker gate failed (Contract Kernel v2.77e).

    Raised by :func:`enforce_stack_obligation_gate` when the resolved stack
    contract's obligations are not all genuinely-enforced-and-satisfied
    (:attr:`ObligationResult.passed` is False): a blocking violation, an unenforced
    ERROR obligation (no registered checker), or a blocking fault (a checker that
    raised / returned None / returned a malformed value). The call-sites translate
    it to their context's RED — ``StageError`` in the greenfield pipeline (→
    ``_fail``), ``SystemExit`` non-zero on the verify CLI path — exactly like
    :class:`codd.stack.command_plan.StackCommandMaterializationError` and
    :class:`codd.stack.command_authenticity.StackCommandAuthenticityError`.
    """


def build_obligation_checker_inputs(
    contract: ResolvedStackContract,
    project_root: str | Path,
) -> dict[str, Any]:
    """Assemble the obligation checkers' runtime inputs from CURRENT-RUN evidence.

    Contract Kernel v2.77e. Most obligation checkers (e.g. the Next.js
    ``ignoreBuildErrors`` guard) read only the filesystem (``project_root``) and need
    no extra input — the gate always passes ``project_root`` to every checker. The
    one curated obligation that needs runtime evidence is the Playwright
    ``e2e_actually_executed`` check, which must see THIS run's e2e report.

    The evidence is consumed from the SAME source the v2.77c/d materialization +
    authenticity layer blessed — NOT re-derived or re-parsed with a divergent parser
    (GPT-5.5 Pro consult 2026-06-21: prefer the authenticity-normalized
    ``report_data`` to avoid parser drift / TOCTOU). For each TEST-report slot in the
    composed command plan, the slot's current-run report is parsed by the canonical
    ``runner_report`` adapter (the exact parser v2.77d uses) into normalized counts,
    which are shaped into the ``report_data`` the obligation checker reads. Because
    both layers use the canonical ``suites`` parser, a 0-test / fully-skipped e2e run
    reds in BOTH (defense in depth on ONE evidence source, never two parsers).

    Binding rule (anti-false-green): the report is passed ONLY when the current-run
    evidence was genuinely produced AND readable this run (a stale file the executor
    did not produce this run is NOT trusted → no report passed → a checker that needs
    it reds on "missing report", which is the correct RED). If the plan has more than
    one TEST-report slot the binding is AMBIGUOUS for a single ``report_data`` kwarg;
    the curated stack has exactly one (Playwright ``e2e_test``), so this is a clean
    single binding today — a future multi-e2e stack must disambiguate (left to the
    step that introduces it; until then a second TEST-report slot is surfaced as an
    error so it can never silently pick the wrong report).
    """
    from codd.stack.command_authenticity import (
        StackCommandObservationKind,
        observe_stack_command_report,
        resolve_stack_command_observation_policy,
    )
    from codd.stack.command_plan import stack_command_plan

    root = Path(project_root)
    plan = stack_command_plan(contract)  # conflict gate already passed upstream; pure projection

    test_report_obs: list[Any] = []
    for slot in plan.slots:
        policy = resolve_stack_command_observation_policy(
            slot.slot_id, contract_policies=contract.command_observation_policies
        )
        if policy is None or policy.kind is not StackCommandObservationKind.TEST_REPORT:
            continue
        if not (slot.report_capture or slot.report_path):
            continue
        obs = observe_stack_command_report(slot, root, policy)
        test_report_obs.append((slot, obs))

    if not test_report_obs:
        return {}
    if len(test_report_obs) > 1:
        slots = ", ".join(f"{s.slot_id}({s.owner})" for s, _ in test_report_obs)
        raise StackObligationGateError(
            "ambiguous obligation evidence: the resolved stack has more than one "
            f"TEST-report command slot ({slots}); a single report binding cannot pick "
            "one unambiguously (anti-false-green: never last-writer-wins across reports). "
            "A multi-e2e stack must declare per-obligation evidence binding."
        )

    slot, obs = test_report_obs[0]
    if not (obs.produced and obs.readable):
        # The current-run evidence was NOT produced/readable this run — do NOT pass a
        # stale or absent report. A checker that requires it then reds on "missing
        # report" (the correct RED); we never feed it untrusted evidence.
        return {}

    # Shape the canonical-normalized counts into the report_data the obligation checker
    # reads. ``expected`` = clean passes; ``unexpected``/``flaky`` = 0 so the checker's
    # "executed = expected+unexpected+flaky" equals the canonical clean-pass count: a
    # run with >=1 clean pass is GREEN; a fully-skipped/all-failed run (passed_cases==0)
    # is RED — agreeing with the canonical parser, no divergent stats read of the file.
    passed = int(obs.passed_cases or 0)
    report_data = {"stats": {"expected": passed, "unexpected": 0, "flaky": 0, "skipped": 0}}
    return {"report_data": report_data}


def enforce_stack_obligation_gate(
    contract: ResolvedStackContract | None,
    project_root: str | Path,
) -> ObligationResult | None:
    """The Contract Kernel v2.77e GATE: run the stack obligation checkers, RED on failure.

    Wired AFTER v2.77c materialization + v2.77d authenticity. Takes the ALREADY-RESOLVED
    contract (NO re-resolution from disk — avoids the TOCTOU hole where a stack file
    changes/deletes between materialization and this gate and the obligation gate then
    silently skips). For ``contract is None`` (a project that declared no stack) it is a
    no-op → ``None`` (byte-identical, no gate). Otherwise it invokes every obligation's
    registered checker with ``project_root`` plus the current-run evidence
    (:func:`build_obligation_checker_inputs`) and raises :class:`StackObligationGateError`
    unless :attr:`ObligationResult.passed` (every ERROR obligation genuinely enforced and
    satisfied; an unenforced ERROR obligation, a blocking violation, or a checker fault is
    RED). Returns the :class:`ObligationResult` on GREEN so the caller can record it.
    """
    if contract is None:
        return None
    checker_inputs = build_obligation_checker_inputs(contract, project_root)
    result = enforce_obligations(contract, project_root=Path(project_root), **checker_inputs)
    if not result.passed:
        reasons: list[str] = []
        for v in result.blocking_violations:
            reasons.append(f"[violation] {v.obligation.id}: {v.finding.detail}")
        for f in result.blocking_faults:
            reasons.append(f"[fault] {f.obligation.id}: {f.reason}")
        for o in result.unenforced:
            if o.severity == "error":
                reasons.append(
                    f"[unenforced] {o.id}: no registered checker for ref {o.checker!r} "
                    "(an ERROR obligation with no checker is unverifiable — RED)"
                )
        raise StackObligationGateError(
            f"stack obligation gate failed ({contract.stack_id}): "
            + "; ".join(reasons)
            + ". A framework/addon obligation was not genuinely enforced AND satisfied — "
            "anti-false-green: a missing/disabled/faulting checker or a violated obligation "
            "is RED, never a silent pass (Contract Kernel v2.77e)."
        )
    return result


def stack_contract_trace(contract: ResolvedStackContract) -> dict[str, Any]:
    """Run-trace fields proving a run consumed the resolved stack contract.

    Mirrors :meth:`codd.languages.contract.ResolvedLanguageContract.to_trace`: the
    minimal, deterministic facts a run record / trace records so the live-consumption
    of the framework-stack contract is observable (the v2.77a intake exit gate).
    ``stack_contract_hash`` is the deterministic :attr:`ResolvedStackContract.content_hash`;
    ``resolved_stack_id`` / ``resolved_stack_layers`` make "changing the profile changes
    the plan" visible in the record itself.
    """
    return {
        "resolved_stack_id": contract.stack_id,
        "stack_contract_hash": contract.content_hash,
        "resolved_stack_layers": [f"{ref.kind}:{ref.id}" for ref in contract.layers],
    }


def stack_contract_intake(project_root: str | Path) -> ResolvedStackContract | None:
    """Resolve a project's declared stack contract for LIVE consumption (intake only).

    This is the production seam the greenfield/verify pipeline calls EARLY in a run
    to bring the (previously dormant) framework-stack contract into the live run —
    *without* enforcing any obligation yet (enforcement is v2.77b-e). It is exactly
    :func:`resolve_project_stack`, named for the intake call-site so the
    "no non-test caller = 0 state" is eliminated by an unambiguous production caller.

    Contract:

    * No ``stack:`` block (the opt-in framework layer is unused) → ``None``. The
      vast majority of projects; behaviour is byte-identical, never an error.
    * A declared ``stack:`` block → the :class:`ResolvedStackContract` (its
      ``content_hash`` is the run-trace hash; see :func:`stack_contract_trace`).
    * A declared-but-BROKEN block (unknown language/framework/addon, malformed
      mapping) → the resolver RAISES (``ValueError`` / ``UnknownLanguageError`` /
      ``UnknownLayerError``). The intake NEVER swallows this: anti-false-green
      forbids a declared-but-unresolvable stack from silently proceeding as if no
      stack were declared. The pipeline call-site turns the raise into an honest
      stage/command failure.
    """
    return resolve_project_stack(project_root)
