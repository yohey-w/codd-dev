"""Check freshness of doc-to-doc ``depends_on`` edges via the reconciliation ledger.

``depends_on_consistency`` compares typed literal values carried by the
propagation output; it can only see what propagation happened to compare.
This check asks the orthogonal *state* question: for every doc->doc
``depends_on`` edge, has the downstream document been reconciled with the
upstream document's **latest** commit? Reconciliation acknowledgements are
written by ``codd propagate --commit`` (including HITL "no update needed"
judgements) into ``reconciliation_ledger.json``.

Default behaviour is an **amber advisory** so existing projects keep passing
``codd dag verify`` unchanged (exit code is unaffected). A project can opt in
to hard-gating with::

    dependency_freshness:
      severity: red

in ``codd.yaml`` (also honoured under the ``dag:`` section).

The **ledger is the primary mechanism**. When no ledger entry exists for an
edge the check falls back to a commit-recency heuristic: best-effort
onboarding *triage*, not proof of freshness. The basic form flags edges whose
upstream was last committed after the downstream. Because a commit touching
*both* documents carries no ordering signal between them (a bulk touch is
indistinguishable from a genuine co-update), a joint tip commit gets a second
look: if the upstream also changed *on its own* after the downstream's
previous change, the edge is still flagged. Repeated bulk touches can still
mask drift -- only an acknowledged ledger baseline (``codd propagate
--verify`` followed by ``--commit``) closes that hole, which is why the
missing-baseline state is always reported explicitly; the check never
silently passes on an empty baseline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from codd.dag.checks import DagCheck, register_dag_check
from codd.reconciliation_ledger import (
    commit_history_for_path,
    edge_key,
    last_commit_for_path,
    last_commit_timestamp_for_path,
    ledger_path,
    load_ledger,
)


SETTINGS_KEY = "dependency_freshness"
_DOC_NODE_KINDS = {"design_doc", "common"}
# ``kind="common"`` is overloaded: design docs opt in via frontmatter, but
# implementation/test files matched by ``common_node_patterns`` also get
# ``kind="common"`` (for the transitive-closure exemption). Only markdown
# nodes are documents; ``.md`` is the codebase-wide doc discriminator and the
# only extension the reconciliation ledger writer acknowledges, so non-md
# common nodes can never be reconciled and must stay out of this check.
_DOC_SUFFIX = ".md"


@dataclass
class DependencyFreshnessResult:
    check_name: str = "dependency_freshness"
    severity: str = "info"
    status: str = "pass"
    message: str = ""
    block_deploy: bool = False
    violations: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    passed: bool = True
    skipped: bool = False
    edges_checked: int = 0
    # Mirror of ``edges_checked`` under the name the materiality overlay reads.
    # The overlay flags ``checked_count==0 + pass`` as a vacuous pass; exposing it
    # here (defaulting to 0, so the existing no-input skip paths are covered too)
    # lets the overlay reason about this check consistently with the others.
    checked_count: int = 0


@register_dag_check("dependency_freshness")
class DependencyFreshnessCheck(DagCheck):
    check_name = "dependency_freshness"
    severity = "amber"
    block_deploy = False

    def run(
        self,
        dag: Any | None = None,
        project_root: str | Path | None = None,
        settings: dict[str, Any] | None = None,
        codd_config: dict[str, Any] | None = None,
    ) -> DependencyFreshnessResult:
        target_dag = dag if dag is not None else self.dag
        root = Path(project_root).resolve() if project_root is not None else self.project_root
        active_settings = settings if settings is not None else self.settings
        if target_dag is None or root is None:
            raise ValueError("dag and project_root are required")

        options = _resolve_options(active_settings, codd_config)
        if not options["enabled"]:
            return DependencyFreshnessResult(
                status="skip",
                skipped=True,
                message="dependency_freshness disabled via config",
            )

        edges = doc_to_doc_edges(target_dag)
        if not edges:
            return DependencyFreshnessResult(
                status="skip",
                skipped=True,
                message="no doc-to-doc depends_on edges found; dependency_freshness skipped",
            )

        if not _git_history_available(root, edges):
            return DependencyFreshnessResult(
                status="skip",
                skipped=True,
                message="git history unavailable; dependency_freshness skipped",
            )

        ledger = load_ledger(root)
        warnings: list[str] = []
        ledger_edges: Mapping[str, Any] = (ledger or {}).get("edges", {})

        violations: list[dict[str, Any]] = []
        checked = 0
        history_cache: dict[str, list[tuple[str, int]]] = {}
        for downstream, upstream in edges:
            current_upstream = last_commit_for_path(root, upstream)
            if current_upstream is None:
                # Upstream has no committed history; nothing to be stale against.
                continue
            checked += 1
            entry = ledger_edges.get(edge_key(downstream, upstream))
            if isinstance(entry, Mapping):
                recorded = str(entry.get("upstream_commit") or "")
                if recorded and recorded != current_upstream:
                    violations.append(
                        {
                            "kind": "unacked_upstream_change",
                            "downstream": downstream,
                            "upstream": upstream,
                            "detail": (
                                f"{upstream} changed (now {current_upstream[:12]}) after the last "
                                f"acknowledged reconciliation ({recorded[:12]}) of {downstream}. "
                                "Run `codd propagate --verify` and re-acknowledge via `--commit`."
                            ),
                        }
                    )
                continue
            # No ledger entry for this edge: commit-recency fallback
            # (best-effort onboarding triage; the ledger is the primary mechanism).
            upstream_ts = last_commit_timestamp_for_path(root, upstream)
            downstream_ts = last_commit_timestamp_for_path(root, downstream)
            if upstream_ts is None or downstream_ts is None:
                continue
            if upstream_ts > downstream_ts:
                violations.append(
                    {
                        "kind": "never_reconciled",
                        "downstream": downstream,
                        "upstream": upstream,
                        "detail": (
                            f"{upstream} was last committed after {downstream} and no reconciliation "
                            "has ever been acknowledged for this depends_on edge. Run "
                            "`codd propagate --verify` and acknowledge via `--commit`."
                        ),
                    }
                )
            elif upstream_ts == downstream_ts:
                # Equal recency usually means a joint tip commit. A commit
                # touching both files carries no ordering signal between them,
                # so disambiguate via exclusive history.
                joint_violation = _joint_tip_violation(root, downstream, upstream, history_cache)
                if joint_violation is not None:
                    violations.append(joint_violation)

        if checked == 0:
            # Doc->doc edge(s) existed, but none had a comparable (committed)
            # upstream history to be stale against → this check examined nothing.
            # Returning a green ``status='pass'`` here (the old fall-through, with a
            # ledger present it carried no warning either) was a vacuous false-green
            # the materiality overlay could not catch — the result exposed no
            # ``checked_count``. A no-material run is a SKIP (deploy still allowed),
            # not a clean pass. The missing-baseline warning is intentionally not
            # raised: with zero comparable edges there is nothing to acknowledge.
            return DependencyFreshnessResult(
                severity="info",
                status="skip",
                skipped=True,
                message=(
                    "dependency_freshness SKIP: doc-to-doc depends_on edge(s) found but "
                    "none had a comparable committed upstream to check freshness against"
                ),
                passed=True,
                block_deploy=False,
                edges_checked=0,
                checked_count=0,
            )

        if ledger is None:
            warnings.append(
                f"reconciliation ledger not found at {ledger_path(root)} — baseline not created: "
                f"none of the {checked} doc-to-doc depends_on edge(s) has an acknowledged "
                "reconciliation. Falling back to the commit-recency heuristic (best-effort "
                "onboarding triage — it cannot prove freshness, and repeated commits touching "
                "both documents can mask upstream drift). Establish the baseline via "
                "`codd propagate --verify` followed by `codd propagate --commit`."
            )

        if violations:
            severity = options["severity"]
            return DependencyFreshnessResult(
                severity=severity,
                status="fail" if severity == "red" else "warn",
                message=(
                    f"dependency_freshness found {len(violations)} stale doc-to-doc "
                    f"depends_on edge(s) out of {checked} checked"
                ),
                violations=violations,
                warnings=warnings,
                passed=severity != "red",
                edges_checked=checked,
                checked_count=checked,
            )

        # No violations. If advisory warnings were collected (currently the
        # missing-baseline note when the reconciliation ledger is absent), surface
        # them as amber/warn — the CLI only renders WARN (and counts the finding)
        # when severity == "amber". Returning info/pass here hid those findings
        # behind a green PASS row (a false-green). Deploy stays allowed either way
        # (passed=True, block_deploy=False); with no warnings it is a clean
        # info/pass (unchanged). Mirrors resource_flow_coherence's round-1 #2 fix.
        if warnings:
            return DependencyFreshnessResult(
                severity="amber",
                status="warn",
                message=(
                    f"dependency_freshness found {len(warnings)} advisory warning(s) "
                    f"({checked} doc-to-doc depends_on edge(s) checked, no violations)"
                ),
                warnings=warnings,
                passed=True,
                block_deploy=False,
                edges_checked=checked,
                checked_count=checked,
            )

        return DependencyFreshnessResult(
            severity="info",
            status="pass",
            message=f"dependency_freshness PASS ({checked} doc-to-doc depends_on edge(s) checked)",
            warnings=warnings,
            edges_checked=checked,
            checked_count=checked,
        )


def _resolve_options(
    settings: Mapping[str, Any] | None,
    codd_config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Resolve enabled/severity from dag settings or full codd config.

    Precedence: explicit dag settings (``dag.dependency_freshness``) override
    the top-level ``dependency_freshness`` mapping in codd.yaml/defaults.
    """

    merged: dict[str, Any] = {}
    for source in (codd_config, settings):
        if not isinstance(source, Mapping):
            continue
        for container in (source, source.get("dag")):
            if not isinstance(container, Mapping):
                continue
            section = container.get(SETTINGS_KEY)
            if isinstance(section, Mapping):
                merged.update(section)

    severity = str(merged.get("severity") or "amber").strip().lower()
    if severity not in {"amber", "red"}:
        severity = "amber"
    return {"enabled": bool(merged.get("enabled", True)), "severity": severity}


def _joint_tip_violation(
    project_root: Path,
    downstream: str,
    upstream: str,
    history_cache: dict[str, list[tuple[str, int]]],
) -> dict[str, Any] | None:
    """Disambiguate an edge whose endpoints share the same tip commit.

    A commit that touches both documents (bulk formatting, mass regeneration,
    one-shot feature commit) is indistinguishable -- by git metadata alone --
    from a genuine co-update, so it must not be taken as proof of freshness.
    Flag the edge when the upstream also changed **on its own** (a commit
    touching the upstream but not the downstream) after the downstream's
    previous change: that exclusive drift was never followed by any downstream
    change other than the ambiguous joint tip.

    Conservative by construction: returns ``None`` whenever the tips differ,
    either history is empty, the upstream has no exclusive commits, or the
    downstream has no history before the shared tip. Known limitation
    (documented): repeated joint commits older than the tip still mask drift;
    only the reconciliation ledger closes that hole.
    """

    upstream_history = _cached_history(project_root, upstream, history_cache)
    downstream_history = _cached_history(project_root, downstream, history_cache)
    if not upstream_history or not downstream_history:
        return None
    tip = upstream_history[0][0]
    if downstream_history[0][0] != tip:
        return None
    downstream_commits = {commit for commit, _ in downstream_history}
    upstream_exclusive = next(
        ((commit, ts) for commit, ts in upstream_history if commit not in downstream_commits),
        None,
    )
    downstream_previous = next(
        ((commit, ts) for commit, ts in downstream_history if commit != tip),
        None,
    )
    if upstream_exclusive is None or downstream_previous is None:
        return None
    if upstream_exclusive[1] <= downstream_previous[1]:
        return None
    return {
        "kind": "never_reconciled",
        "downstream": downstream,
        "upstream": upstream,
        "joint_tip_commit": tip,
        "detail": (
            f"{upstream} and {downstream} were last touched together by commit {tip[:12]}, "
            f"but {upstream} also changed on its own ({upstream_exclusive[0][:12]}) after "
            f"{downstream}'s previous change ({downstream_previous[0][:12]}), and no "
            "reconciliation has ever been acknowledged for this depends_on edge. A commit "
            "touching both documents does not prove they were reconciled. Run "
            "`codd propagate --verify` and acknowledge via `--commit`."
        ),
    }


def _cached_history(
    project_root: Path,
    rel_path: str,
    cache: dict[str, list[tuple[str, int]]],
) -> list[tuple[str, int]]:
    if rel_path not in cache:
        cache[rel_path] = commit_history_for_path(project_root, rel_path)
    return cache[rel_path]


def doc_to_doc_edges(dag: Any) -> list[tuple[str, str]]:
    """Return (downstream_path, upstream_path) pairs for doc->doc depends_on edges.

    Public helper: the single source of truth for "which document depends_on
    which document" used both by this freshness check and by
    ``codd propagate --baseline`` (so the baseline-ack set is exactly the set
    this check would later evaluate). Source->doc edges are deliberately
    excluded — only ``.md`` document endpoints qualify (see :func:`_is_doc_node`).
    """

    nodes = getattr(dag, "nodes", {}) or {}
    pairs: list[tuple[str, str]] = []
    for edge in getattr(dag, "edges", []) or []:
        if getattr(edge, "kind", None) != "depends_on":
            continue
        from_id = str(getattr(edge, "from_id", ""))
        to_id = str(getattr(edge, "to_id", ""))
        from_node = nodes.get(from_id)
        to_node = nodes.get(to_id)
        if from_node is None or to_node is None:
            continue
        downstream = str(getattr(from_node, "path", None) or from_id)
        upstream = str(getattr(to_node, "path", None) or to_id)
        if not _is_doc_node(from_node, downstream):
            continue
        if not _is_doc_node(to_node, upstream):
            continue
        pairs.append((downstream, upstream))
    return sorted(set(pairs))


# Backward-compatible alias: the original private name is kept so any existing
# import (internal or external) keeps resolving to the same implementation.
_doc_to_doc_edges = doc_to_doc_edges


def _is_doc_node(node: Any, path: str) -> bool:
    """True when the node is a design *document* (not a common code file).

    ``design_doc`` nodes are documents by construction. ``common`` nodes are
    documents only when their path is markdown: ``common_node_patterns`` also
    assigns ``kind="common"`` to source/test files, which share the kind
    string but are code, not docs.
    """

    kind = getattr(node, "kind", None)
    if kind == "design_doc":
        return True
    return kind == "common" and path.endswith(_DOC_SUFFIX)


def _git_history_available(project_root: Path, edges: list[tuple[str, str]]) -> bool:
    """True when at least one edge endpoint has resolvable git history."""

    seen: set[str] = set()
    for downstream, upstream in edges:
        for path in (downstream, upstream):
            if path in seen:
                continue
            seen.add(path)
            if last_commit_for_path(project_root, path) is not None:
                return True
    return False
