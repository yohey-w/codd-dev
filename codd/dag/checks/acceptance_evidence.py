"""DAG check: acceptance_evidence — every acceptance criterion carries evidence.

The gate for the invariant documented in :mod:`codd.acceptance_evidence`:

    For every acceptance criterion AC there is evidence E such that
    (a) E is machine-executed, (b) E runs through the shipped path, (c) E
    references AC's named parameters, and (d) E is bound to the content of the
    implementation it verified.

Stage 1 of the check implements (a)'s **demotion** half: an acceptance
criterion that declares a RUNTIME obligation, in a project whose runtime stage
is switched off, is RED — never a silent skip. The remaining halves land in the
following stages and are listed in ``docs/design/acceptance-evidence-invariant.md``.

Generality: the check reads the project's own declarations (requirement tables,
``operation_flow``, ``runtime_smoke``) and carries no project, framework or
language literal. It is dormant for a project that declares no acceptance
criteria — there is nothing to certify — and every severity is configurable, so
a project may downgrade a class to amber without losing the finding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from codd.acceptance_evidence import (
    AcceptanceCriterion,
    acceptance_settings,
    load_acceptance_criteria,
    resolve_test_targets,
    runtime_obligations,
    runtime_smoke_enabled,
    scan_requirement_anchors,
)
from codd.acceptance_record import implementation_digest, load_ledger
from codd.dag.checks import DagCheck, register_dag_check

CHECK_NAME = "acceptance_evidence"


@dataclass
class AcceptanceEvidenceResult:
    check_name: str = CHECK_NAME
    severity: str = "red"
    status: str = "pass"
    message: str = ""
    block_deploy: bool = False
    passed: bool = True
    skipped: bool = False
    # Acceptance criteria actually evaluated. 0 with status="skip" means the
    # project declares none (dormant), which the materiality overlay must not
    # read as a verified clean run.
    checked_count: int = 0
    violations: list[dict[str, Any]] = field(default_factory=list)


@register_dag_check(CHECK_NAME)
class AcceptanceEvidenceCheck(DagCheck):
    """Reconcile declared acceptance criteria against executable evidence."""

    check_name = CHECK_NAME
    severity = "red"
    block_deploy = False

    def run(
        self,
        dag: Any | None = None,
        project_root: str | Path | None = None,
        settings: dict[str, Any] | None = None,
        codd_config: dict[str, Any] | None = None,
    ) -> AcceptanceEvidenceResult:
        target_dag = dag if dag is not None else self.dag
        if project_root is not None:
            self.project_root = Path(project_root)
        if settings is not None:
            self.settings = settings
        root = (self.project_root or Path.cwd()).resolve()

        # Acceptance criteria, operation_flow and runtime_smoke all live at the
        # TOP level of codd.yaml, so prefer the full config the runner passes in
        # over the merged ``dag:`` section.
        config = codd_config if codd_config is not None else dict(self.settings or {})
        resolved = acceptance_settings(config)
        if not resolved.enabled:
            return AcceptanceEvidenceResult(
                status="skip",
                skipped=True,
                message="acceptance_evidence: disabled in codd.yaml",
            )

        criteria = load_acceptance_criteria(root, config)
        if not criteria:
            return AcceptanceEvidenceResult(
                status="skip",
                skipped=True,
                message=(
                    "acceptance_evidence: no acceptance criteria declared in the project's "
                    "requirement documents — nothing to certify."
                ),
            )

        declared_ids = _declared_operation_ids(config, target_dag)
        test_paths = _nodes_of_kind(target_dag, {"test_file"})
        impl_paths = _nodes_of_kind(target_dag, {"impl_file", "common"})
        anchors = scan_requirement_anchors(
            root,
            sorted(test_paths | impl_paths),
            (criterion.req_id for criterion in criteria),
        )

        violations: list[dict[str, Any]] = []
        violations.extend(
            _runtime_violations(criteria, declared_ids, config, resolved.runtime_severity)
        )
        violations.extend(
            _binding_violations(
                root,
                criteria,
                config=config,
                settings=resolved,
                test_paths=test_paths,
                anchors=anchors,
                # Only an EXECUTABLE runtime obligation counts as a binding. When
                # the runtime stage is off, the criterion really is proved by
                # nothing, and the two findings ask for two different remedies
                # (turn the stage on / write a test) — not one defect twice.
                runtime_bound_ids=frozenset(
                    criterion.req_id
                    for criterion, _targets in runtime_obligations(criteria, declared_ids)
                )
                if runtime_smoke_enabled(config)
                else frozenset(),
            )
        )

        return _finalize(violations, checked_count=len(criteria), max_findings=resolved.max_findings)


# ---------------------------------------------------------------------------
# (a) runtime obligations must be executable
# ---------------------------------------------------------------------------


def _runtime_violations(
    criteria: Iterable[AcceptanceCriterion],
    declared_ids: frozenset[str],
    config: Mapping[str, Any],
    severity: str,
) -> list[dict[str, Any]]:
    """Runtime-evidence obligations in a project whose runtime stage cannot run.

    This is the *demotion* hole: ``codd verify`` runs Step 8 only when the
    project's ``runtime_smoke`` section is enabled, and an absent section made
    the step vanish without a word. A criterion whose evidence is "the running
    system does X" then had NO evidence while the run still reported green.
    """

    if runtime_smoke_enabled(config):
        return []
    violations: list[dict[str, Any]] = []
    for criterion, targets in runtime_obligations(criteria, declared_ids):
        violations.append(
            {
                "type": "runtime_evidence_not_executable",
                "severity": severity,
                "req_id": criterion.req_id,
                "source": criterion.source,
                "targets": list(targets),
                "message": (
                    f"[acceptance_evidence] Acceptance criterion `{criterion.req_id}` "
                    f"({criterion.source}) declares runtime evidence "
                    f"({', '.join(targets)}) but `runtime_smoke` is not enabled in "
                    "codd.yaml, so that evidence is never executed — the stage is "
                    "skipped silently and the criterion reads as accepted on no "
                    "evidence. Enable `runtime_smoke` (and run `codd verify --runtime`), "
                    "declare different evidence in the criterion's `verified_by` column, "
                    "or downgrade with `acceptance_evidence.runtime_severity: amber`."
                ),
            }
        )
    return violations


# ---------------------------------------------------------------------------
# (a) wiring + (d) freshness: every criterion bound to machine-checked evidence
# ---------------------------------------------------------------------------


def _binding_violations(
    root: Path,
    criteria: list[AcceptanceCriterion],
    *,
    config: Mapping[str, Any],
    settings: Any,
    test_paths: set[str],
    anchors: Mapping[str, set[str]],
    runtime_bound_ids: frozenset[str],
) -> list[dict[str, Any]]:
    """Every acceptance criterion must reach evidence a machine can re-run.

    The four binding paths, in the order a project typically acquires them:

    1. an explicit ``verified_by: test:<name>`` that RESOLVES to a test file;
    2. a VB registry row referenced from the criterion's own row, covered by a
       ``codd: covers vb=`` marker (the generated-project path);
    3. a test file that writes the requirement id as a token (the brownfield
       path — no document surgery required);
    4. an explicit ``verified_by: manual:<owner>`` with a ledger record that is
       still fresh against the implementation it accepted.

    A criterion reaching none of them is ``unbound_acceptance``: it is written
    down, agreed with a customer, and connected to nothing. That state used to
    be invisible — and, for a project with no VB table at all, was announced as
    a one-line notice and passed.
    """

    violations: list[dict[str, Any]] = []
    registry_exists, covered_by_req = _vb_bindings(
        root, config, (criterion.req_id for criterion in criteria)
    )
    ledger = load_ledger(root)

    if not registry_exists and _require_vb_table(config):
        violations.append(
            {
                "type": "vb_registry_missing",
                "severity": settings.unbound_severity,
                "req_id": "(project)",
                "message": (
                    f"[acceptance_evidence] This project declares {len(criteria)} acceptance "
                    "criterion/criteria but owns NO verifiable-behavior registry "
                    "(`docs/test/test_strategy.md`), so not one of them is reconciled against "
                    "a test marker. Run `codd acceptance sync` to derive the registry from the "
                    "acceptance criteria, or set `test_coverage.require_vb_table: false` to "
                    "declare that this project verifies its criteria some other way."
                ),
            }
        )

    for criterion in criteria:
        bound: list[str] = []

        for ref in criterion.evidence_of("test"):
            resolved_tests = resolve_test_targets(ref.target, test_paths)
            if resolved_tests:
                bound.extend(resolved_tests)
            else:
                violations.append(
                    {
                        "type": "unresolved_evidence",
                        "severity": settings.unbound_severity,
                        "req_id": criterion.req_id,
                        "source": criterion.source,
                        "message": (
                            f"[acceptance_evidence] Acceptance criterion `{criterion.req_id}` "
                            f"({criterion.source}) declares `verified_by: test:{ref.target}`, but "
                            "no test file matches that name. A pointer to a test that does not "
                            "exist is worse than no pointer: it reads as evidence."
                        ),
                    }
                )

        bound.extend(sorted(covered_by_req.get(criterion.req_id, set())))
        bound.extend(sorted(anchors.get(criterion.req_id, set()) & test_paths))

        manual_refs = criterion.evidence_of("manual")
        if manual_refs:
            violations.extend(
                _manual_violations(root, criterion, manual_refs, ledger, anchors, settings)
            )
            record = ledger.get(criterion.req_id)
            if record is not None and record.status == "pass":
                current = implementation_digest(root, sorted(anchors.get(criterion.req_id, set())))
                if not record.is_stale(current):
                    bound.append(f"manual:{record.by}")

        if bound:
            continue
        if criterion.req_id in runtime_bound_ids:
            # An executable runtime obligation IS machine-checked evidence.
            continue
        violations.append(
            {
                "type": "unbound_acceptance",
                "severity": settings.unbound_severity,
                "req_id": criterion.req_id,
                "source": criterion.source,
                "message": (
                    f"[acceptance_evidence] Acceptance criterion `{criterion.req_id}` "
                    f"({criterion.source}) is bound to NO machine-checked evidence: no "
                    "`verified_by:` declaration, no verifiable-behavior row referenced from "
                    "its own row, and no test file that names the requirement id. Add a "
                    "`verified_by: test:<name> | runtime:<case> | manual:<owner>` column entry, "
                    "or write `" + criterion.req_id + "` into the test that proves it."
                ),
            }
        )
    return violations


def _manual_violations(
    root: Path,
    criterion: AcceptanceCriterion,
    manual_refs: tuple[Any, ...],
    ledger: Mapping[str, Any],
    anchors: Mapping[str, set[str]],
    settings: Any,
) -> list[dict[str, Any]]:
    """(d): a manual verdict expires when the implementation it accepted changes."""

    violations: list[dict[str, Any]] = []
    owners = ", ".join(ref.target for ref in manual_refs)
    record = ledger.get(criterion.req_id)
    if record is None or record.status != "pass":
        violations.append(
            {
                "type": "manual_evidence_missing",
                "severity": settings.unbound_severity,
                "req_id": criterion.req_id,
                "source": criterion.source,
                "message": (
                    f"[acceptance_evidence] Acceptance criterion `{criterion.req_id}` declares "
                    f"manual evidence (owner: {owners}) but the acceptance ledger holds no "
                    f"passing record for it. Record the verdict with "
                    f"`codd acceptance record {criterion.req_id} pass --by <owner>` so it is "
                    "bound to the implementation it accepted."
                ),
            }
        )
        return violations

    current = implementation_digest(root, sorted(anchors.get(criterion.req_id, set())))
    if record.is_stale(current):
        violations.append(
            {
                "type": "stale_manual_evidence",
                "severity": settings.freshness_severity,
                "req_id": criterion.req_id,
                "source": criterion.source,
                "message": (
                    f"[acceptance_evidence] The manual acceptance of `{criterion.req_id}` "
                    f"(by {record.by} at {record.recorded_at}) was bound to a different "
                    "implementation than the one on disk now, so the verdict is about code "
                    "that no longer exists. Re-check the criterion and re-record it with "
                    f"`codd acceptance record {criterion.req_id} pass --by <owner>`."
                ),
            }
        )
    elif criterion.critical:
        violations.append(
            {
                "type": "critical_manual_only",
                "severity": "amber",
                "req_id": criterion.req_id,
                "source": criterion.source,
                "message": (
                    f"[acceptance_evidence] `{criterion.req_id}` is marked critical but its only "
                    "evidence is a manual record. A criterion that must not regress should have "
                    "evidence that re-runs itself; the manual record proves one moment, not the "
                    "next release."
                ),
            }
        )
    return violations


def _vb_bindings(
    root: Path,
    config: Mapping[str, Any],
    req_ids: Iterable[str],
) -> tuple[bool, dict[str, set[str]]]:
    """(registry_exists, requirement id -> covering test paths via the VB registry).

    Two links, both already part of CoDD's vocabulary:

    * a VB id *referenced* from a later column of another document's row — that
      row's first cell is the requirement/AC id, which is exactly the
      requirement→behaviour link, and is the shape generated test docs are
      already told to write;
    * a declared VB row that names the requirement id in its own later columns —
      the shape ``codd acceptance sync`` writes when it derives the registry from
      the acceptance criteria.

    Only a COVERED behaviour binds: a declared-but-uncovered VB row is the VB
    audit's own finding, and treating it as acceptance evidence would launder an
    uncovered behaviour into an accepted one.
    """

    from codd.acceptance_evidence import requirement_anchor_pattern
    from codd.verifiable_behavior_audit import (
        build_vb_coverage_audit,
        discover_vb_documents,
        parse_vb_references,
    )

    try:
        report = build_vb_coverage_audit(root, config=dict(config))
    except (OSError, ValueError):
        return False, {}
    if not report.rows:
        return False, {}

    covered_tests: dict[str, list[str]] = {
        row.vb_id.casefold(): list(row.matched_tests)
        for row in report.rows
        if row.coverage_status == "covered"
    }
    bindings: dict[str, set[str]] = {}
    pattern = requirement_anchor_pattern(req_ids)

    # Link 2: the declared row names the requirement id.
    if pattern is not None:
        for row in report.rows:
            matched = covered_tests.get(row.vb_id.casefold())
            if not matched:
                continue
            for match in pattern.finditer(f"{row.description} {row.declared_scenarios}"):
                bindings.setdefault(match.group("id"), set()).update(matched)

    # Link 1: another document's row references the canonical id.
    for doc_path in discover_vb_documents(root, config=dict(config)):
        try:
            text = doc_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for reference in parse_vb_references(text, source_doc=doc_path.name):
            matched = covered_tests.get(reference.vb_id.casefold())
            if matched and reference.row_id:
                bindings.setdefault(reference.row_id, set()).update(matched)
    return True, bindings


def _require_vb_table(config: Mapping[str, Any]) -> bool:
    """Shared with the implement-time gate — one switch, two enforcement points."""

    from codd.verifiable_behavior_audit import require_vb_table

    return require_vb_table(dict(config))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _nodes_of_kind(dag: Any, kinds: set[str]) -> set[str]:
    """Relative paths of DAG nodes of the given kinds (node ids ARE the paths)."""

    nodes = getattr(dag, "nodes", {}) if dag is not None else {}
    return {node.id for node in nodes.values() if getattr(node, "kind", "") in kinds}


def _declared_operation_ids(config: Mapping[str, Any], dag: Any) -> frozenset[str]:
    """Operation ids declared in codd.yaml or in any design-doc frontmatter.

    Mirrors ``cli._operation_flows_from_project`` without importing the CLI: the
    DAG already carries every design document's parsed frontmatter, so the doc
    side needs no second filesystem walk.
    """

    from codd.requirements_meta import operation_flow_operations

    payloads: list[Any] = []
    if isinstance(config, Mapping) and isinstance(config.get("operation_flow"), Mapping):
        payloads.append(config["operation_flow"])
    for node in getattr(dag, "nodes", {}).values() if dag is not None else ():
        attributes = getattr(node, "attributes", None)
        if not isinstance(attributes, Mapping):
            continue
        frontmatter = attributes.get("frontmatter")
        if not isinstance(frontmatter, Mapping):
            continue
        for container in (frontmatter, frontmatter.get("codd")):
            if isinstance(container, Mapping) and isinstance(container.get("operation_flow"), Mapping):
                payloads.append(container["operation_flow"])

    ids: set[str] = set()
    for payload in payloads:
        for operation in operation_flow_operations(payload):
            raw = operation.get("id")
            if isinstance(raw, str) and raw.strip():
                ids.add(raw.strip().lower())
    return frozenset(ids)


def _finalize(
    violations: list[dict[str, Any]],
    *,
    checked_count: int,
    max_findings: int,
) -> AcceptanceEvidenceResult:
    """Build the result, choosing severity/status from the violations present."""

    reds = [item for item in violations if item.get("severity") == "red"]
    ambers = [item for item in violations if item.get("severity") != "red"]
    # Reds first, so a cap never hides a hard failure behind advisories.
    ordered = reds + ambers
    shown = ordered[:max_findings]
    truncated = ordered[max_findings:]
    if truncated:
        # The truncated findings still name every criterion they concern: a
        # silent "...and 20 more" is exactly the kind of invisible skip this
        # check exists to abolish.
        remaining = ", ".join(
            f"{item.get('req_id', '?')}({item.get('type', '?')})" for item in truncated
        )
        shown = list(shown) + [
            {
                "type": "findings_truncated",
                "severity": "amber",
                "message": (
                    f"[acceptance_evidence] ...and {len(truncated)} more finding(s), "
                    f"in full: {remaining}. Raise `acceptance_evidence.max_findings` in "
                    "codd.yaml to expand them."
                ),
            }
        ]

    if reds:
        message = (
            f"acceptance_evidence: {len(reds)} acceptance criterion violation(s) "
            f"({len(ambers)} advisory) across {checked_count} declared criteria"
        )
        return AcceptanceEvidenceResult(
            severity="red",
            status="fail",
            passed=False,
            message=message,
            checked_count=checked_count,
            violations=shown,
        )
    if ambers:
        return AcceptanceEvidenceResult(
            severity="amber",
            status="warn",
            passed=True,
            message=(
                f"acceptance_evidence: {len(ambers)} advisory finding(s) across "
                f"{checked_count} declared acceptance criteria"
            ),
            checked_count=checked_count,
            violations=shown,
        )
    return AcceptanceEvidenceResult(
        severity="amber",
        status="pass",
        passed=True,
        message=(
            f"acceptance_evidence: {checked_count} acceptance criterion/criteria bound to "
            "executable evidence — OK"
        ),
        checked_count=checked_count,
    )
