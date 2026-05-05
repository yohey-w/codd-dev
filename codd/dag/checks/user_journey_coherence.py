"""C7 user journey coherence check scaffold."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from codd.dag import DAG
from codd.dag.checks import CheckResult, DagCheck, register_dag_check


@register_dag_check("user_journey_coherence")
class UserJourneyCoherenceCheck(DagCheck):
    """Pass-through scaffold for future journey coherence validation."""

    check_name = "user_journey_coherence"
    severity = "red"
    block_deploy = True

    def run(
        self,
        dag: DAG | None = None,
        project_root: str | Path | None = None,
        settings: dict[str, Any] | None = None,
    ) -> CheckResult:
        target_dag = dag if dag is not None else self.dag
        if target_dag is None:
            raise ValueError("dag is required for user_journey_coherence check")
        if project_root is not None:
            self.project_root = Path(project_root)
        if settings is not None:
            self.settings = settings

        journey_docs = [
            node
            for node in target_dag.nodes.values()
            if node.kind == "design_doc" and node.attributes.get("user_journeys")
        ]
        if not journey_docs:
            return CheckResult(
                check_name=self.check_name,
                severity="info",
                status="pass",
                message="No user_journeys declared, C7 SKIP",
                block_deploy=self.block_deploy,
            )
        return CheckResult(
            check_name=self.check_name,
            severity=self.severity,
            status="pass",
            message="C7 skeleton (cmd_393e not yet implemented)",
            block_deploy=self.block_deploy,
        )
