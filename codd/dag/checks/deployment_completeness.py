"""C6 deployment chain completeness check."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from codd.dag import DAG, Edge, Node
from codd.dag.checks import register_dag_check
from codd.deployment import (
    EDGE_EXECUTES_IN_ORDER,
    EDGE_PRODUCES_STATE,
    EDGE_REQUIRES_DEPLOYMENT_STEP,
    EDGE_VERIFIED_BY,
)


DEPLOYMENT_STEPS = ("migrate", "seed", "build", "start")
STEP_STATE_KIND = {
    "migrate": "db_schema",
    "seed": "db_seed",
    "build": "file_present",
    "start": "server_running",
}


@dataclass
class DeploymentChainViolation:
    design_doc: str
    chain_status: str
    broken_at: str
    expected_chain: list[str]
    remediation: str


@dataclass
class DeploymentCompletenessResult:
    check_name: str = "deployment_completeness"
    severity: str = "red"
    block_deploy: bool = True
    violations: list[DeploymentChainViolation] = field(default_factory=list)
    passed: bool = True


@register_dag_check("deployment_completeness")
class DeploymentCompletenessCheck:
    """
    Verify the C6 deploy chain:

    design_doc -> deployment_doc -> impl_file -> runtime_state -> verification_test
    and that the verification test is part of the deploy flow.
    """

    severity = "red"
    block_deploy = True

    def __init__(
        self,
        dag: DAG | None = None,
        project_root: str | Path | None = None,
        settings: dict[str, Any] | None = None,
    ) -> None:
        self.dag = dag
        self.project_root = Path(project_root) if project_root is not None else None
        self.settings = settings or {}
        self._project_post_deploy_hooks: list[str] | None = None

    def run(
        self,
        dag: DAG | None = None,
        project_root: str | Path | None = None,
        codd_config: dict[str, Any] | None = None,
    ) -> DeploymentCompletenessResult:
        target_dag = dag or self.dag
        if target_dag is None:
            raise ValueError("dag is required for deployment_completeness check")

        if project_root is not None:
            self.project_root = Path(project_root)
        if codd_config is not None:
            self.settings = codd_config

        if not self._has_deployment_signal(target_dag):
            return DeploymentCompletenessResult()

        violations: list[DeploymentChainViolation] = []
        for node in sorted(target_dag.nodes.values(), key=lambda item: item.id):
            if node.kind != "design_doc":
                continue
            violations.extend(self._check_design_doc(target_dag, node))

        return DeploymentCompletenessResult(
            violations=violations,
            passed=not violations,
        )

    def _check_design_doc(self, dag: DAG, design_doc_node: Node) -> list[DeploymentChainViolation]:
        violations: list[DeploymentChainViolation] = []
        requires_edges = self._edges_from(dag, design_doc_node.id, EDGE_REQUIRES_DEPLOYMENT_STEP)

        for requires_edge in requires_edges:
            deployment_doc = dag.nodes.get(requires_edge.to_id)
            expected_steps = self._required_steps_for_edge(design_doc_node, deployment_doc, requires_edge)
            if deployment_doc is None or deployment_doc.kind != "deployment_doc":
                violations.append(
                    self._violation(
                        design_doc_node.id,
                        "missing_deployment_doc",
                        design_doc_node.id,
                        requires_edge.to_id,
                        expected_steps=expected_steps,
                    )
                )
                continue

            if not expected_steps:
                expected_steps = self._deployment_doc_steps(deployment_doc)

            for step in expected_steps:
                violation = self._check_step_chain(dag, design_doc_node, deployment_doc, step)
                if violation is not None:
                    violations.append(violation)

        return violations

    def _check_step_chain(
        self,
        dag: DAG,
        design_doc: Node,
        deployment_doc: Node,
        step: str,
    ) -> DeploymentChainViolation | None:
        if not self._deployment_doc_has_step(deployment_doc, step):
            return self._violation(
                design_doc.id,
                "missing_step_in_deployment_doc",
                design_doc.id,
                deployment_doc.id,
                step=step,
            )

        executes_edges = self._matching_executes_edges(dag, deployment_doc.id, step)
        impl_edge = next(
            (edge for edge in executes_edges if self._valid_node_kind(dag, edge.to_id, "impl_file")),
            None,
        )
        if impl_edge is None:
            return self._violation(
                design_doc.id,
                "missing_impl_for_step",
                design_doc.id,
                deployment_doc.id,
                step=step,
                impl_id=executes_edges[0].to_id if executes_edges else self._expected_impl_for_step(step),
            )

        produces_edges = self._matching_produces_edges(dag, impl_edge.to_id, step)
        state_edge = next(
            (edge for edge in produces_edges if self._valid_node_kind(dag, edge.to_id, "runtime_state")),
            None,
        )
        if state_edge is None:
            return self._violation(
                design_doc.id,
                "state_not_produced",
                design_doc.id,
                deployment_doc.id,
                step=step,
                impl_id=impl_edge.to_id,
                state_id=produces_edges[0].to_id if produces_edges else self._expected_state_for_step(step),
            )

        verified_edges = self._edges_from(dag, state_edge.to_id, EDGE_VERIFIED_BY)
        verification_edge = next(
            (edge for edge in verified_edges if self._valid_node_kind(dag, edge.to_id, "verification_test")),
            None,
        )
        if verification_edge is None:
            return self._violation(
                design_doc.id,
                "no_verification_test",
                design_doc.id,
                deployment_doc.id,
                step=step,
                impl_id=impl_edge.to_id,
                state_id=state_edge.to_id,
                verification_id=(
                    verified_edges[0].to_id
                    if verified_edges
                    else self._expected_verification_for_state(state_edge.to_id)
                ),
            )

        verification_node = dag.nodes[verification_edge.to_id]
        if not self._verification_test_in_deploy_flow(verification_node, deployment_doc):
            return self._violation(
                design_doc.id,
                "verification_test_not_in_deploy_flow",
                design_doc.id,
                deployment_doc.id,
                step=step,
                impl_id=impl_edge.to_id,
                state_id=state_edge.to_id,
                verification_id=verification_edge.to_id,
            )

        return None

    def _generate_remediation(self, broken_at: str, context: dict[str, Any]) -> str:
        deployment_doc = str(context.get("deployment_doc") or "DEPLOYMENT.md")
        step = str(context.get("step") or "deployment step")
        impl_file = str(context.get("impl_id") or self._expected_impl_for_step(step))
        state_id = str(context.get("state_id") or self._expected_state_for_step(step))
        verification_id = str(context.get("verification_id") or self._expected_verification_for_state(state_id))

        messages = {
            "missing_deployment_doc": (
                f"Add {deployment_doc} or deploy.yaml and connect it with requires_deployment_step."
            ),
            "missing_step_in_deployment_doc": f"Add the {step} step to {deployment_doc}.",
            "missing_impl_for_step": f"Add {impl_file} and ensure the deploy artifact includes it.",
            "state_not_produced": f"Ensure {impl_file} runs during deploy and produces {state_id}.",
            "no_verification_test": f"Add a smoke or e2e verification test for {state_id}.",
            "verification_test_not_in_deploy_flow": f"Add {verification_id} to deploy.yaml post_deploy.",
        }
        return messages.get(broken_at, "Complete the deployment verification chain.")

    def format_report(self, violations: list[DeploymentChainViolation] | DeploymentCompletenessResult) -> str:
        if isinstance(violations, DeploymentCompletenessResult):
            violation_items = violations.violations
        else:
            violation_items = violations
        return json.dumps(
            {"incomplete_chain_report": [asdict(violation) for violation in violation_items]},
            ensure_ascii=False,
            indent=2,
        )

    def _violation(
        self,
        design_doc: str,
        broken_at: str,
        design_id: str,
        deployment_id: str,
        *,
        expected_steps: list[str] | None = None,
        step: str | None = None,
        impl_id: str | None = None,
        state_id: str | None = None,
        verification_id: str | None = None,
    ) -> DeploymentChainViolation:
        context = {
            "deployment_doc": deployment_id,
            "step": step or (expected_steps[0] if expected_steps else None),
            "impl_id": impl_id,
            "state_id": state_id,
            "verification_id": verification_id,
        }
        return DeploymentChainViolation(
            design_doc=design_doc,
            chain_status="INCOMPLETE",
            broken_at=broken_at,
            expected_chain=self._expected_chain(
                broken_at,
                design_id,
                deployment_id,
                expected_steps=expected_steps,
                step=step,
                impl_id=impl_id,
                state_id=state_id,
                verification_id=verification_id,
            ),
            remediation=self._generate_remediation(broken_at, context),
        )

    def _expected_chain(
        self,
        broken_at: str,
        design_id: str,
        deployment_id: str,
        *,
        expected_steps: list[str] | None = None,
        step: str | None = None,
        impl_id: str | None = None,
        state_id: str | None = None,
        verification_id: str | None = None,
    ) -> list[str]:
        step_label = step or ", ".join(expected_steps or ["deployment_step"])
        impl_label = impl_id or self._expected_impl_for_step(step_label)
        state_label = state_id or self._expected_state_for_step(step_label)
        verification_label = verification_id or self._expected_verification_for_state(state_label)

        return [
            self._chain_line(f"{design_id} -> {deployment_id}", broken_at == "missing_deployment_doc"),
            self._chain_line(f"{deployment_id} -> {step_label} step", broken_at == "missing_step_in_deployment_doc"),
            self._chain_line(f"{step_label} step -> {impl_label}", broken_at == "missing_impl_for_step"),
            self._chain_line(f"{impl_label} -> {state_label}", broken_at == "state_not_produced"),
            self._chain_line(f"{state_label} -> {verification_label}", broken_at == "no_verification_test"),
            self._chain_line(
                f"{verification_label} -> deploy.yaml post_deploy",
                broken_at == "verification_test_not_in_deploy_flow",
            ),
        ]

    @staticmethod
    def _chain_line(text: str, broken: bool) -> str:
        return f"{text} [{'missing' if broken else 'ok'}]"

    @staticmethod
    def _has_deployment_signal(dag: DAG) -> bool:
        if any(node.kind == "deployment_doc" for node in dag.nodes.values()):
            return True
        return any(
            edge.kind
            in {
                EDGE_REQUIRES_DEPLOYMENT_STEP,
                EDGE_EXECUTES_IN_ORDER,
                EDGE_PRODUCES_STATE,
                EDGE_VERIFIED_BY,
            }
            for edge in dag.edges
        )

    def _required_steps_for_edge(
        self,
        design_doc: Node,
        deployment_doc: Node | None,
        edge: Edge,
    ) -> list[str]:
        steps: list[str] = []
        edge_attributes = edge.attributes or {}
        for key in ("required_steps", "steps", "keywords"):
            steps.extend(self._coerce_steps(self._as_list(edge_attributes.get(key))))

        if not steps:
            steps.extend(self._steps_from_design_doc(design_doc))
        if not steps and deployment_doc is not None:
            steps.extend(self._deployment_doc_steps(deployment_doc))
        return self._dedupe(steps)

    def _steps_from_design_doc(self, design_doc: Node) -> list[str]:
        attributes = design_doc.attributes or {}
        frontmatter = attributes.get("frontmatter") if isinstance(attributes.get("frontmatter"), dict) else {}
        text = "\n".join(
            str(value)
            for value in (
                attributes.get("acceptance_criteria"),
                attributes.get("criteria"),
                frontmatter.get("acceptance_criteria"),
                frontmatter.get("criteria"),
            )
            if value
        )
        return self._coerce_steps(_deployment_keywords(text))

    def _deployment_doc_steps(self, deployment_doc: Node) -> list[str]:
        attributes = deployment_doc.attributes or {}
        raw_steps: list[Any] = []
        raw_steps.extend(self._as_list(attributes.get("sections")))
        raw_steps.extend(self._as_list(attributes.get("steps")))
        return self._coerce_steps(raw_steps)

    def _deployment_doc_has_step(self, deployment_doc: Node, step: str) -> bool:
        return any(self._same_step(step, candidate) for candidate in self._deployment_doc_steps(deployment_doc))

    def _matching_executes_edges(self, dag: DAG, deployment_doc_id: str, step: str) -> list[Edge]:
        return [
            edge
            for edge in self._edges_from(dag, deployment_doc_id, EDGE_EXECUTES_IN_ORDER)
            if self._edge_matches_step(edge, step)
        ]

    def _matching_produces_edges(self, dag: DAG, impl_id: str, step: str) -> list[Edge]:
        produces_edges = self._edges_from(dag, impl_id, EDGE_PRODUCES_STATE)
        matching = [edge for edge in produces_edges if self._state_matches_step(dag, edge.to_id, step)]
        return matching or produces_edges

    def _edge_matches_step(self, edge: Edge, step: str) -> bool:
        section = (edge.attributes or {}).get("section")
        if section and self._same_step(step, str(section)):
            return True
        return self._path_matches_step(edge.to_id, step)

    def _state_matches_step(self, dag: DAG, state_id: str, step: str) -> bool:
        expected_kind = STEP_STATE_KIND.get(self._normalize_step(step))
        if expected_kind is None:
            return True

        node = dag.nodes.get(state_id)
        if node is not None:
            state_kind = str((node.attributes or {}).get("kind") or state_id).lower()
            return expected_kind in state_kind
        return expected_kind in state_id.lower()

    def _verification_test_in_deploy_flow(self, verification_node: Node, deployment_doc: Node) -> bool:
        attributes = verification_node.attributes or {}
        for key in ("in_deploy_flow", "deploy_flow", "post_deploy"):
            if attributes.get(key) is True:
                return True

        hooks = []
        hooks.extend(self._hooks_from_mapping(deployment_doc.attributes or {}))
        hooks.extend(self._project_hooks())
        if not hooks:
            return False

        signals = self._verification_signals(verification_node)
        for hook in hooks:
            normalized_hook = hook.lower()
            if any(signal and signal in normalized_hook for signal in signals):
                return True
            kind = str(attributes.get("kind") or "").lower()
            template = str(attributes.get("verification_template_ref") or "").lower()
            if kind == "smoke" and ("smoke" in normalized_hook or "test:smoke" in normalized_hook):
                return True
            if kind == "e2e" and ("e2e" in normalized_hook or "playwright" in normalized_hook):
                return True
            if template == "curl" and "curl" in normalized_hook:
                return True
        return False

    @staticmethod
    def _verification_signals(verification_node: Node) -> set[str]:
        attributes = verification_node.attributes or {}
        expected_outcome = attributes.get("expected_outcome")
        source = expected_outcome.get("source") if isinstance(expected_outcome, dict) else None
        raw_signals = {
            verification_node.id,
            verification_node.path,
            source,
            attributes.get("target"),
            attributes.get("verification_template_ref"),
        }

        signals: set[str] = set()
        for value in raw_signals:
            if not value:
                continue
            text = str(value).lower()
            signals.add(text)
            path = Path(text)
            signals.add(path.name)
            signals.add(path.stem)
        return {signal for signal in signals if len(signal) >= 3}

    def _project_hooks(self) -> list[str]:
        if self._project_post_deploy_hooks is not None:
            return self._project_post_deploy_hooks

        hooks = self._hooks_from_mapping(self.settings)
        root = self.project_root
        if root is not None:
            for path in self._deploy_yaml_candidates(Path(root)):
                if not path.is_file():
                    continue
                payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if isinstance(payload, dict):
                    hooks.extend(self._hooks_from_mapping(payload))

        self._project_post_deploy_hooks = self._dedupe(hooks)
        return self._project_post_deploy_hooks

    @staticmethod
    def _deploy_yaml_candidates(project_root: Path) -> list[Path]:
        return [
            project_root / "deploy.yaml",
            project_root / ".codd" / "deploy.yaml",
            project_root / "codd" / "deploy.yaml",
        ]

    def _hooks_from_mapping(self, mapping: dict[str, Any]) -> list[str]:
        hooks: list[str] = []
        for key in ("post_deploy", "post_deploy_steps", "post_deploy_hooks", "verification", "verifications"):
            hooks.extend(self._coerce_hook_texts(mapping.get(key)))

        targets = mapping.get("targets")
        if isinstance(targets, dict):
            for target_config in targets.values():
                if isinstance(target_config, dict):
                    hooks.extend(self._hooks_from_mapping(target_config))
        return hooks

    def _coerce_hook_texts(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple)):
            hooks: list[str] = []
            for item in value:
                hooks.extend(self._coerce_hook_texts(item))
            return hooks
        if isinstance(value, dict):
            hooks: list[str] = []
            for key in ("command", "verification", "name", "test", "script"):
                hooks.extend(self._coerce_hook_texts(value.get(key)))
            return hooks
        return [str(value)]

    @staticmethod
    def _edges_from(dag: DAG, from_id: str, kind: str) -> list[Edge]:
        return [edge for edge in dag.edges if edge.from_id == from_id and edge.kind == kind]

    @staticmethod
    def _valid_node_kind(dag: DAG, node_id: str, expected_kind: str) -> bool:
        node = dag.nodes.get(node_id)
        return node is not None and node.kind == expected_kind

    @classmethod
    def _coerce_steps(cls, values: Iterable[Any]) -> list[str]:
        steps: list[str] = []
        for value in values:
            step = cls._normalize_step(str(value))
            if step in DEPLOYMENT_STEPS:
                steps.append(step)
        return cls._dedupe(steps)

    @staticmethod
    def _normalize_step(value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
        if "migrate" in normalized or "migration" in normalized:
            return "migrate"
        if "seed" in normalized:
            return "seed"
        if "build" in normalized:
            return "build"
        if "start" in normalized or "server" in normalized or normalized in {"up", "run"}:
            return "start"
        return normalized

    @classmethod
    def _same_step(cls, left: str, right: str) -> bool:
        return cls._normalize_step(left) == cls._normalize_step(right)

    @staticmethod
    def _path_matches_step(path_text: str, step: str) -> bool:
        path = path_text.lower()
        name = Path(path).name
        normalized_step = DeploymentCompletenessCheck._normalize_step(step)
        if normalized_step == "migrate":
            return "migration" in path or "migrate" in path or "schema.prisma" in path
        if normalized_step == "seed":
            return "seed" in name or "/seed" in path
        if normalized_step == "build":
            return name in {"dockerfile", "package.json"} or "dockerfile" in path
        if normalized_step == "start":
            return name in {"main.ts", "main.js", "server.ts", "server.js", "app.ts", "app.js", "index.ts", "index.js"}
        return normalized_step in path

    @staticmethod
    def _expected_impl_for_step(step: str) -> str:
        normalized_step = DeploymentCompletenessCheck._normalize_step(step)
        return {
            "migrate": "prisma/migrations",
            "seed": "prisma/seed.ts",
            "build": "Dockerfile",
            "start": "src/server.ts",
        }.get(normalized_step, f"{normalized_step}_impl")

    @staticmethod
    def _expected_state_for_step(step: str) -> str:
        normalized_step = DeploymentCompletenessCheck._normalize_step(step)
        return {
            "migrate": "runtime:db_schema:database_schema",
            "seed": "runtime:db_seed:seed_data",
            "build": "runtime:file_present:build_artifact",
            "start": "runtime:server_running:server",
        }.get(normalized_step, f"runtime:state:{normalized_step}")

    @staticmethod
    def _expected_verification_for_state(state_id: str) -> str:
        if "seed" in state_id or "user" in state_id:
            return "verification:smoke:login"
        if "server" in state_id:
            return "verification:smoke:health"
        return "verification:smoke:deployment"

    @staticmethod
    def _as_list(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
        return [value]

    @staticmethod
    def _dedupe(values: Iterable[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not value or value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result


def _deployment_keywords(text: str) -> list[str]:
    normalized = text.lower()
    keywords: list[str] = []
    if "migrate" in normalized or "migration" in normalized:
        keywords.append("migrate")
    if "seed" in normalized:
        keywords.append("seed")
    if "build" in normalized:
        keywords.append("build")
    if "start" in normalized or "server" in normalized:
        keywords.append("start")
    return keywords
