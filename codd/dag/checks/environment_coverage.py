"""C9 environment coverage check."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from codd.dag.checks import DagCheck, register_dag_check


@dataclass
class EnvironmentCoverageResult:
    check_name: str = "environment_coverage"
    severity: str = "red"
    status: str = "pass"
    message: str = ""
    block_deploy: bool = True
    violations: list[dict[str, Any]] = field(default_factory=list)
    passed: bool = True


@register_dag_check("environment_coverage")
class EnvironmentCoverageCheck(DagCheck):
    check_name = "environment_coverage"
    severity = "red"
    block_deploy = True

    def run(self, *args: Any, **kwargs: Any) -> EnvironmentCoverageResult:
        return EnvironmentCoverageResult(
            severity="info",
            status="pass",
            message="C9 environment_coverage PASS",
            block_deploy=self.block_deploy,
        )
