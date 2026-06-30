"""Unresolved internal-import residue DAG check (amber, deploy-allowed).

Surfaces *unresolved internal-import residue*: import specifiers that can only
refer to in-tree code — relative imports (Python ``.b`` / ``..pkg``; JS ``./x`` /
``../x``), configured first-party alias-prefixed specifiers, and C++ quote-form
local includes — that resolved to NO in-tree DAG node. Such a specifier is a
discovery gap and a false-green risk (a missing source file or an under-scoped
source set). This is the first-class, ``codd dag verify``-visible promotion of
the builder's ``_warn_unresolved_residue`` advisory, which still warns for
backward compat. The residue is computed once, in the single place import
resolution happens (``codd.dag.builder._add_import_edges``), and attached to the
DAG as ``import_residue_report``; the advisory and the check consume that SAME
measurement, so they can never drift. (``codd dag verify`` rebuilds the DAG
in-memory, so the check reads the builder's exact object — no reload, no
recompute, no persisted dag.json field.)

Severity is fixed ``amber`` and ``block_deploy`` is fixed ``False`` — surfacing
only. Promoting residue to a red / deploy-blocking gate (e.g. via a "> N
unresolved fails" tolerance) is a NEW gate and therefore owner-gated; it is
intentionally NOT done here (see the ``block_deploy`` note in
:class:`UnresolvedImportResidueResult`).

Generality: the check carries no ``language ==`` / framework literal. Whether a
specifier is "internal-looking" is decided by the DATA-driven, specifier-SHAPE
predicate ``_is_internal_looking_specifier`` (relative / first-party alias / C++
quote shapes) that the builder already uses when resolving import edges — so the
check inherits that language-agnosticism for free and merely echoes the residue.
Supporting another language is a config/alias change, never a core edit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codd.dag.checks import register_dag_check


@dataclass
class UnresolvedImportResidueResult:
    check_name: str = "unresolved_import_residue"
    severity: str = "amber"
    findings: list[str] = field(default_factory=list)
    message: str = ""
    passed: bool = True
    # ``block_deploy`` is fixed ``False``: unresolved internal-import residue is
    # advisory (amber), never a deploy gate. Promoting it to red (e.g. a "> N
    # unresolved fails" threshold) introduces a NEW gate, which is owner-gated and
    # deliberately out of scope here.
    block_deploy: bool = False
    # ``status``/``skipped``/``checked_count`` mirror ``transitive_closure`` /
    # ``source_completeness`` and close the vacuous-pass hole: when no
    # internal-looking import exists (or the DAG never went through the import-edge
    # pass) the check examined nothing → SKIP (``checked_count`` 0), never a
    # vacuous green PASS. ``checked_count`` is the number of internal-looking
    # specifiers actually examined (resolved + unresolved).
    status: str = "pass"
    skipped: bool = False
    checked_count: int = 0


@register_dag_check("unresolved_import_residue")
class UnresolvedImportResidueCheck:
    """Report internal-looking import specifiers that resolved to no in-tree node.

    The builder's ``_add_import_edges`` resolves every import edge; an
    INTERNAL-looking specifier (relative, first-party alias, or C++ quote
    include) that resolves to nothing is collected as *residue* and attached to
    the DAG (``import_residue_report``). This check reads that single shared
    measurement:

    - **no internal-looking import examined** (count 0, or the DAG was not built
      through the import-edge pass) → SKIP (examined nothing; not a vacuous PASS);
    - **residue non-empty** → amber WARN finding (deploy still allowed), listing
      the unresolved specifiers (full list in ``findings``; a bounded sample in
      the message);
    - **otherwise** → PASS (every internal-looking import resolved in-tree).

    Discrimination is by the DATA-driven internal-looking rule the builder uses,
    never a ``language ==`` branch.
    """

    def __init__(self, dag=None, project_root: Path | None = None, settings: dict[str, Any] | None = None):
        self.dag = dag
        self.project_root = Path(project_root) if project_root is not None else None
        self.settings = settings or {}

    def run(
        self,
        dag=None,
        project_root: Path | None = None,
        settings: dict[str, Any] | None = None,
    ) -> UnresolvedImportResidueResult:
        dag = dag if dag is not None else self.dag

        # The measurement is attached by the builder's import-edge pass. Duck-typed
        # read (no ImportResidueReport import) keeps the check decoupled from
        # builder internals and free of any import cost/cycle.
        report = getattr(dag, "import_residue_report", None) if dag is not None else None
        if report is None:
            # DAG was not produced by the import-edge pass → nothing examined →
            # SKIP, never a vacuous green PASS.
            return UnresolvedImportResidueResult(status="skip", skipped=True, checked_count=0)

        internal_count = int(getattr(report, "internal_import_count", 0) or 0)
        residue = list(getattr(report, "residue", []) or [])

        if internal_count == 0:
            # No internal-looking import specifier anywhere → examined nothing →
            # SKIP (not a vacuous PASS), exactly like source_completeness on an
            # empty source set.
            return UnresolvedImportResidueResult(status="skip", skipped=True, checked_count=0)

        if residue:
            ordered = sorted(residue)
            examples = ", ".join(ordered[:5])
            message = (
                f"{len(ordered)} internal-looking import specifier(s) did not resolve to any "
                f"in-tree node (unresolved residue) out of {internal_count} examined — likely "
                f"missing source files or an under-scoped source set (e.g. {examples})."
            )
            return UnresolvedImportResidueResult(
                findings=ordered,
                message=message,
                passed=True,
                status="warn",
                skipped=False,
                checked_count=internal_count,
            )

        # Every internal-looking import resolved to an in-tree node.
        return UnresolvedImportResidueResult(
            passed=True,
            status="pass",
            skipped=False,
            checked_count=internal_count,
        )
