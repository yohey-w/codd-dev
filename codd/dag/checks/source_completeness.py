"""Source completeness DAG check (amber, deploy-allowed).

Surfaces a *source completeness gap*: more source files exist on disk than the
DAG has source nodes for — i.e. files of a configured source suffix that fall
outside the configured source scope and are therefore silently inert (a
discovery gap and a false-green risk). This is the first-class,
``codd dag verify``-visible promotion of the builder's
``_warn_source_completeness`` advisory, which still warns for backward compat.
The two share the pure :func:`codd.dag.builder.compute_source_completeness`, so
the advisory and the check can never drift.

Severity is fixed ``amber`` and ``block_deploy`` is fixed ``False`` — surfacing
only. Promoting the gap to a red / deploy-blocking gate (e.g. via a tolerance
threshold) is a NEW gate and therefore owner-gated; it is intentionally NOT done
here (see the ``block_deploy`` note in :class:`SourceCompletenessResult`).

Generality: the check carries no ``language ==`` / framework literal. The set of
"source" suffixes is DATA (``implementation_suffixes`` / ``test_suffixes`` from
settings, resolved by :func:`source_suffixes_from_settings`); a DAG node counts
as a source node purely by its ``path`` suffix being in that set — exactly the
data-driven discrimination ``transitive_closure`` uses for node KIND. Supporting
another language is a config/suffix change, never a core edit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codd.dag.checks import register_dag_check


@dataclass
class SourceCompletenessResult:
    check_name: str = "source_completeness"
    severity: str = "amber"
    findings: list[str] = field(default_factory=list)
    message: str = ""
    passed: bool = True
    # ``block_deploy`` is fixed ``False``: a source-scope gap is advisory (amber),
    # never a deploy gate. Promoting it to red (e.g. a "> N missing files fails"
    # threshold) introduces a NEW gate, which is owner-gated and deliberately out
    # of scope here.
    block_deploy: bool = False
    # ``status``/``skipped``/``checked_count`` mirror ``transitive_closure`` and
    # close the vacuous-pass hole: when no source file exists on disk the check
    # examined nothing → SKIP (``checked_count`` 0), never a vacuous green PASS.
    # ``checked_count`` is the number of on-disk source files actually examined.
    status: str = "pass"
    skipped: bool = False
    checked_count: int = 0
    # Raw measurement, carried for JSON consumers / debugging.
    on_disk: int = 0
    node_count: int = 0


@register_dag_check("source_completeness")
class SourceCompletenessCheck:
    """Report on-disk source files that are outside the DAG's source scope.

    A *source node* is any DAG node whose ``path`` suffix is in the resolved
    source-suffix set (``implementation_suffixes`` ∪ ``test_suffixes``). The
    check compares the count of such nodes against the on-disk files of the same
    suffixes:

    - **no source file on disk** → SKIP (examined nothing; not a vacuous PASS);
    - **on-disk source files outnumber source nodes and some are uncovered** →
      amber WARN finding (deploy still allowed), listing up to 10 missing files;
    - **otherwise** → PASS (``checked_count`` = on-disk source files examined).

    Discrimination is by the DATA suffix set, never a ``language ==`` branch.
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
    ) -> SourceCompletenessResult:
        # Lazy import keeps registration (module import time) free of any
        # builder dependency; the builder is already imported by the runner by
        # the time ``run`` executes, so there is no import cost or cycle.
        from codd.dag.builder import compute_source_completeness, source_suffixes_from_settings

        dag = dag if dag is not None else self.dag
        root = Path(project_root) if project_root is not None else self.project_root
        if root is None:
            root = Path.cwd()
        effective_settings = settings if settings is not None else self.settings

        source_node_paths = self._source_node_paths(dag, root, effective_settings, source_suffixes_from_settings)
        report = compute_source_completeness(root, effective_settings, source_node_paths)

        if report.on_disk == 0:
            # No source file on disk → nothing to compare → SKIP, not a vacuous
            # green PASS.
            return SourceCompletenessResult(
                status="skip",
                skipped=True,
                checked_count=0,
                on_disk=0,
                node_count=report.node_count,
            )

        if report.on_disk > report.node_count and report.missing:
            examples = ", ".join(report.missing[:5])
            message = (
                f"{report.on_disk} source file(s) on disk but only {report.node_count} "
                f"source node(s) in the DAG — {len(report.missing)} file(s) are outside the "
                f"configured source scope and will be inert (e.g. {examples}). Consider "
                f"widening source_dirs / impl_file_patterns."
            )
            return SourceCompletenessResult(
                findings=list(report.missing),
                message=message,
                passed=True,
                status="warn",
                skipped=False,
                checked_count=report.on_disk,
                on_disk=report.on_disk,
                node_count=report.node_count,
            )

        # Every on-disk source file is covered by a DAG source node.
        return SourceCompletenessResult(
            passed=True,
            status="pass",
            skipped=False,
            checked_count=report.on_disk,
            on_disk=report.on_disk,
            node_count=report.node_count,
        )

    def _source_node_paths(
        self,
        dag,
        project_root: Path,
        settings: dict[str, Any],
        source_suffixes_from_settings,
    ) -> set[Path]:
        """Resolved file paths of the DAG's source nodes (suffix-driven).

        A node is a source node iff it has a ``path`` whose suffix is in the
        DATA source-suffix set — the same data-driven rule the pure function and
        the builder advisory use. Nodes without a path (e.g. ``expected`` /
        ``runtime_state``) and non-source artifacts (e.g. ``design_doc`` ``.md``)
        are excluded by their suffix, with no per-language literal.
        """
        if dag is None:
            return set()
        source_suffixes = source_suffixes_from_settings(settings)
        paths: set[Path] = set()
        for node in dag.nodes.values():
            node_path = getattr(node, "path", None)
            if not node_path:
                continue
            if Path(node_path).suffix in source_suffixes:
                paths.add((project_root / node_path).resolve())
        return paths
