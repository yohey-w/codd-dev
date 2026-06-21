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
