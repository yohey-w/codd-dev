"""DAG check: enforce the project's opt-in Artifact Contract.

Pipeline stages have historically judged completion by *the process finished*
rather than *the required artifacts were produced*. The Artifact Contract
(``codd/artifact_contract.py`` + the shipped artifact catalog) closes that gap
by letting a project declare, per stage, the catalog artifacts that stage MUST
produce, then verifying them deterministically.

This check surfaces that gate inside ``codd dag verify``. It is **opt-in**:
dormant unless ``artifact_contract.enabled: true`` is set in ``codd.yaml``.
When dormant it reports ``skip`` (exit code unaffected), exactly mirroring how
``dependency_freshness`` stays an amber advisory so existing projects keep
passing unchanged. When active and an artifact is missing or invalid, the
finding is reported at the configured severity:

    artifact_contract:
      enabled: true
      severity: red      # opt in to hard-gating; default is amber advisory
      stages:
        design: [design_spec, lexicon]
        implement: [source, test_suite]

Projects pinning ``dag.enabled_checks`` must add ``artifact_contract`` to that
list; ``codd dag verify`` already prints the registered-but-unselected notice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from codd.artifact_contract import (
    CatalogError,
    load_catalog,
    load_contract,
    verify_contract,
)
from codd.dag.checks import DagCheck, register_dag_check


SETTINGS_KEY = "artifact_contract"


@dataclass
class ArtifactContractResult:
    check_name: str = "artifact_contract"
    severity: str = "info"
    status: str = "pass"
    message: str = ""
    block_deploy: bool = False
    violations: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    passed: bool = True
    skipped: bool = False


@register_dag_check("artifact_contract")
class ArtifactContractCheck(DagCheck):
    check_name = "artifact_contract"
    severity = "amber"
    block_deploy = False

    def run(
        self,
        dag: Any | None = None,
        project_root: str | Path | None = None,
        settings: dict[str, Any] | None = None,
        codd_config: dict[str, Any] | None = None,
    ) -> ArtifactContractResult:
        root = Path(project_root).resolve() if project_root is not None else self.project_root
        active_settings = settings if settings is not None else self.settings
        if root is None:
            raise ValueError("project_root is required")

        full_config = codd_config if codd_config is not None else active_settings
        contract = load_contract(full_config)
        if not contract.is_active:
            return ArtifactContractResult(
                status="skip",
                skipped=True,
                message="artifact_contract disabled (opt-in; enable via artifact_contract.enabled)",
            )

        try:
            catalog = load_catalog()
        except CatalogError as exc:  # shipped catalog is broken — surface loudly
            return ArtifactContractResult(
                severity="red",
                status="fail",
                passed=False,
                message=f"artifact_contract catalog error: {exc}",
            )

        report = verify_contract(catalog, contract, root)

        violations: list[dict[str, Any]] = []
        for stage_report in report.stages:
            for check in stage_report.failures:
                violations.append(
                    {
                        "stage": stage_report.stage,
                        "artifact": check.artifact_id,
                        "status": check.status,
                        "detail": check.detail,
                    }
                )

        if not violations:
            stages_checked = len(report.stages)
            return ArtifactContractResult(
                severity="info",
                status="pass",
                message=f"artifact_contract PASS ({stages_checked} stage(s) checked)",
            )

        severity = _resolve_severity(full_config)
        return ArtifactContractResult(
            severity=severity,
            status="fail" if severity == "red" else "warn",
            passed=severity != "red",
            message=(
                f"artifact_contract found {len(violations)} unmet required artifact(s) "
                f"across {len(report.stages)} stage(s)"
            ),
            violations=violations,
        )


def _resolve_severity(codd_config: Mapping[str, Any] | None) -> str:
    """Resolve severity from the ``artifact_contract`` section (default amber).

    Mirrors ``dependency_freshness``: honoured both at the top level and under
    a ``dag:`` section.
    """

    merged: dict[str, Any] = {}
    if isinstance(codd_config, Mapping):
        for container in (codd_config, codd_config.get("dag")):
            if isinstance(container, Mapping):
                section = container.get(SETTINGS_KEY)
                if isinstance(section, Mapping):
                    merged.update(section)
    severity = str(merged.get("severity") or "amber").strip().lower()
    return severity if severity in {"amber", "red"} else "amber"
