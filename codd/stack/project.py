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
