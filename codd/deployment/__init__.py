"""Deployment verification DAG primitives."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class RuntimeStateKind(Enum):
    DB_SCHEMA = "db_schema"
    DB_SEED = "db_seed"
    SERVER_RUNNING = "server_running"
    ENV_VAR_SET = "env_var_set"
    FILE_PRESENT = "file_present"


class VerificationKind(Enum):
    SMOKE = "smoke"
    HEALTH = "health"
    E2E = "e2e"
    LOAD = "load"


@dataclass
class DeploymentDocNode:
    path: str
    sections: list[str] = field(default_factory=list)
    deploy_target_ref: Optional[str] = None
    depends_on: list[str] = field(default_factory=list)


@dataclass
class RuntimeStateNode:
    identifier: str
    kind: RuntimeStateKind
    target: str
    expected_value: Any = None
    actual_check_command: Optional[str] = None
    capabilities_provided: list[str] = field(default_factory=list)


@dataclass
class VerificationTestNode:
    identifier: str
    kind: VerificationKind
    target: str
    verification_template_ref: str
    expected_outcome: Any = None
    # Explicit "this test verifies these runtime_state identifiers" declarations
    # parsed from sidecar YAML (``<test_path>.codd.yaml``). Used by
    # ``infer_deployment_edges`` to add EDGE_VERIFIED_BY edges without relying
    # on the legacy keyword-matching heuristic.
    verified_by: list[str] = field(default_factory=list)
    # Explicit (journey, axis_type, variant_id) coverage matrix parsed from
    # the same sidecar. Consumed by C9 ``environment_coverage`` so a test
    # asserts coverage without inlining the matrix in its source code.
    axis_matrix: list[dict[str, Any]] = field(default_factory=list)


EDGE_REQUIRES_DEPLOYMENT_STEP = "requires_deployment_step"
EDGE_EXECUTES_IN_ORDER = "executes_in_order"
EDGE_PRODUCES_STATE = "produces_state"
EDGE_VERIFIED_BY = "verified_by"


__all__ = [
    "DeploymentDocNode",
    "RuntimeStateKind",
    "RuntimeStateNode",
    "VerificationKind",
    "VerificationTestNode",
    "EDGE_REQUIRES_DEPLOYMENT_STEP",
    "EDGE_EXECUTES_IN_ORDER",
    "EDGE_PRODUCES_STATE",
    "EDGE_VERIFIED_BY",
]
