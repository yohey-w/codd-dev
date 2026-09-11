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
    runtime_obligations,
    runtime_smoke_enabled,
)
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
        violations: list[dict[str, Any]] = []
        violations.extend(
            _runtime_violations(criteria, declared_ids, config, resolved.runtime_severity)
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
# helpers
# ---------------------------------------------------------------------------


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
