"""C7 user journey coherence check."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from codd.dag import DAG, Edge, Node
from codd.dag.checks import DagCheck, register_dag_check
from codd.dag.checks.deployment_completeness import DeploymentCompletenessCheck


@dataclass
class UserJourneyCoherenceResult:
    check_name: str = "user_journey_coherence"
    severity: str = "red"
    status: str = "pass"
    message: str = ""
    block_deploy: bool = True
    violations: list[dict[str, Any]] = field(default_factory=list)
    journey_reports: list[dict[str, Any]] = field(default_factory=list)
    passed: bool = True


@register_dag_check("user_journey_coherence")
class UserJourneyCoherenceCheck(DagCheck):
    """Validate declarative journey, expected value, evidence, and runtime coherence."""

    check_name = "user_journey_coherence"
    severity = "red"
    block_deploy = True

    def run(
        self,
        dag: DAG | None = None,
        project_root: str | Path | None = None,
        settings: dict[str, Any] | None = None,
        codd_config: dict[str, Any] | None = None,
    ) -> UserJourneyCoherenceResult:
        target_dag = dag if dag is not None else self.dag
        if target_dag is None:
            raise ValueError("dag is required for user_journey_coherence check")
        if project_root is not None:
            self.project_root = Path(project_root)
        if settings is not None:
            self.settings = settings
        if codd_config is not None:
            self.settings = codd_config

        journey_docs = [
            node
            for node in sorted(target_dag.nodes.values(), key=lambda item: item.id)
            if node.kind == "design_doc" and self._journey_entries(node)
        ]
        if not journey_docs:
            return UserJourneyCoherenceResult(
                severity="info",
                status="pass",
                message="No user_journeys declared, C7 SKIP",
                block_deploy=self.block_deploy,
            )

        journey_reports: list[dict[str, Any]] = []
        violations: list[dict[str, Any]] = []
        for design_doc in journey_docs:
            for index, journey in enumerate(self._journey_entries(design_doc)):
                report = self._check_journey(target_dag, design_doc, index, journey)
                journey_reports.append(report)
                violations.extend(report["violations"])

        red_count = sum(1 for violation in violations if violation.get("severity", "red") == "red")
        amber_count = sum(1 for violation in violations if violation.get("severity") == "amber")
        passed = red_count == 0
        severity = "red" if red_count else ("amber" if amber_count else "info")
        status = "pass" if passed else "fail"
        if violations:
            message = f"C7 user_journey_coherence found {red_count} red and {amber_count} amber violation(s)"
        else:
            message = "C7 user_journey_coherence PASS"

        return UserJourneyCoherenceResult(
            severity=severity,
            status=status,
            message=message,
            block_deploy=self.block_deploy,
            violations=violations,
            journey_reports=journey_reports,
            passed=passed,
        )

    def format_report(self, result: UserJourneyCoherenceResult | list[dict[str, Any]]) -> str:
        if isinstance(result, UserJourneyCoherenceResult):
            reports = result.journey_reports
        else:
            reports = result
        return json.dumps({"user_journey_coherence_report": reports}, ensure_ascii=False, indent=2, default=str)

    def _check_journey(self, dag: DAG, design_doc: Node, journey_index: int, journey: dict[str, Any]) -> dict[str, Any]:
        journey_name = str(journey.get("name") or f"journey_{journey_index + 1}")
        report: dict[str, Any] = {
            "user_journey": journey_name,
            "design_doc": design_doc.id,
            "violations": [],
            "remediation_hints": [],
        }

        lex_refs = self._lexicon_refs(journey)
        expected_nodes = self._expected_nodes(dag, lex_refs)
        for ref in lex_refs:
            if ref not in expected_nodes:
                report["violations"].append(
                    self._violation(
                        "missing_journey_lexicon",
                        journey_name,
                        design_doc.id,
                        lexicon_id=ref.removeprefix("lexicon:"),
                        required_by=f"design_doc.user_journeys[{journey_index}].expected_outcome_refs",
                        suggested_lexicon_entry={
                            "id": ref.removeprefix("lexicon:"),
                            "journey": journey_name,
                            "path": f"tests/e2e/{self._slug(journey_name)}.spec.ts",
                        },
                        human_review_required=self._human_review_required(design_doc, journey),
                    )
                )

        plan_tasks = self._plan_tasks_for_journey(dag, journey_name, lex_refs)
        if not plan_tasks:
            report["violations"].append(
                self._violation(
                    "no_plan_task_for_journey",
                    journey_name,
                    design_doc.id,
                    required_by=f"design_doc.user_journeys[{journey_index}]",
                    expected_outputs=self._expected_plan_outputs(journey_name, lex_refs),
                )
            )

        e2e_tests = self._e2e_tests_for_journey(dag, plan_tasks, expected_nodes)
        if plan_tasks and not e2e_tests:
            report["violations"].append(
                self._violation(
                    "no_e2e_test_for_journey",
                    journey_name,
                    design_doc.id,
                    required_by="plan_task.expected_outputs",
                    plan_tasks=[node.id for node in plan_tasks],
                )
            )
        elif e2e_tests and not self._any_test_in_post_deploy(dag, e2e_tests):
            report["violations"].append(
                self._violation(
                    "e2e_not_in_post_deploy",
                    journey_name,
                    design_doc.id,
                    required_by="deploy.post_deploy",
                    verification_tests=[node.id for node in e2e_tests],
                )
            )

        report["violations"].extend(
            self._runtime_constraint_violations(dag, design_doc, journey_name, journey_index)
        )
        report["violations"].extend(self._evidence_runtime_violations(dag, design_doc, journey_name))
        report["violations"].extend(
            self._browser_requirement_violations(dag, design_doc.id, journey_name, expected_nodes.values(), e2e_tests)
        )
        if not self._has_assertion_step(journey):
            report["violations"].append(
                self._violation(
                    "journey_step_no_assertion",
                    journey_name,
                    design_doc.id,
                    severity="amber",
                    required_by=f"design_doc.user_journeys[{journey_index}].steps",
                    remediation="Add an assertion step to the journey declaration.",
                )
            )

        report["remediation_hints"] = self._remediation_hints(report["violations"], design_doc)
        return report

    def _runtime_constraint_violations(
        self,
        dag: DAG,
        design_doc: Node,
        journey_name: str,
        journey_index: int,
    ) -> list[dict[str, Any]]:
        actual_caps, declared = self._runtime_capabilities(dag)
        if not declared:
            return []

        violations: list[dict[str, Any]] = []
        for index, constraint in enumerate(self._runtime_constraints(design_doc)):
            if constraint.get("required") is False:
                continue
            capability = constraint.get("capability")
            if not isinstance(capability, str) or not capability:
                continue
            if capability in actual_caps:
                continue
            violations.append(
                self._violation(
                    "unsatisfied_runtime_capability",
                    journey_name,
                    design_doc.id,
                    required_capability=capability,
                    required_by=f"design_doc.runtime_constraints[{index}]",
                    journey_required_by=f"design_doc.user_journeys[{journey_index}]",
                    rationale_from_design=constraint.get("rationale"),
                    actual_runtime_state=self._runtime_state_summary(actual_caps),
                    human_review_required=self._human_review_required(design_doc, constraint),
                )
            )
        return violations

    def _evidence_runtime_violations(self, dag: DAG, design_doc: Node, journey_name: str) -> list[dict[str, Any]]:
        requirements = self._capability_requirements()
        if not requirements:
            return []

        actual_caps, declared = self._runtime_capabilities(dag)
        if not declared:
            return []

        violations: list[dict[str, Any]] = []
        for impl_node in self._related_impl_nodes(dag, design_doc):
            for evidence in self._runtime_evidence(impl_node):
                capability_kind = evidence.get("capability_kind")
                if not isinstance(capability_kind, str):
                    continue
                required_caps = requirements.get(capability_kind, [])
                missing = [cap for cap in required_caps if cap not in actual_caps]
                if not missing:
                    continue
                for capability in missing:
                    violations.append(
                        self._violation(
                            "impl_evidence_runtime_mismatch",
                            journey_name,
                            design_doc.id,
                            capability_kind=capability_kind,
                            evidence=evidence.get("line_ref") or impl_node.id,
                            missing_runtime_capability=capability,
                            required_by=f"coherence.capability_requirements.{capability_kind}",
                        )
                    )
        return violations

    def _browser_requirement_violations(
        self,
        dag: DAG,
        design_doc_id: str,
        journey_name: str,
        expected_nodes: Iterable[Node],
        e2e_tests: list[Node],
    ) -> list[dict[str, Any]]:
        if not e2e_tests:
            return []

        violations: list[dict[str, Any]] = []
        for expected_node in expected_nodes:
            for index, requirement in enumerate(self._requirement_entries(expected_node)):
                capability = requirement.get("capability")
                if not isinstance(capability, str) or not capability:
                    continue
                if self._any_test_asserts(e2e_tests, capability):
                    continue
                violations.append(
                    self._violation(
                        "browser_expected_not_asserted",
                        journey_name,
                        design_doc_id,
                        expected_node=expected_node.id,
                        required_capability=capability,
                        required_by=f"{expected_node.id}.browser_requirements[{index}]",
                        rationale_from_design=requirement.get("rationale"),
                    )
                )
        return violations

    def _plan_tasks_for_journey(self, dag: DAG, journey_name: str, lex_refs: list[str]) -> list[Node]:
        matches: list[Node] = []
        expected_outputs = {f"design:{journey_name}", journey_name, *lex_refs}
        for node in sorted(dag.nodes.values(), key=lambda item: item.id):
            if node.kind != "plan_task":
                continue
            outputs = {str(output).strip() for output in self._as_list(node.attributes.get("expected_outputs"))}
            if outputs.intersection(expected_outputs):
                matches.append(node)
                continue
            if any(self._edge_marks_journey(edge, journey_name, lex_refs) for edge in self._edges_from(dag, node.id)):
                matches.append(node)
        return matches

    def _e2e_tests_for_journey(
        self,
        dag: DAG,
        plan_tasks: list[Node],
        expected_nodes: dict[str, Node],
    ) -> list[Node]:
        e2e_by_signal = self._e2e_tests_by_signal(dag)
        tests: list[Node] = []
        for task in plan_tasks:
            signals = {str(output).strip() for output in self._as_list(task.attributes.get("expected_outputs"))}
            for edge in self._edges_from(dag, task.id):
                signals.add(edge.to_id)
                target = dag.nodes.get(edge.to_id)
                if target is not None and target.kind == "expected":
                    signals.update(self._expected_test_signals(target))
                if target is not None and self._is_e2e_test(target):
                    tests.append(target)
            for output in list(signals):
                expected = expected_nodes.get(output)
                if expected is not None:
                    signals.update(self._expected_test_signals(expected))
            for signal in signals:
                tests.extend(e2e_by_signal.get(signal, []))
        return self._dedupe_nodes(tests)

    def _any_test_in_post_deploy(self, dag: DAG, tests: list[Node]) -> bool:
        deployment_docs = [node for node in dag.nodes.values() if node.kind == "deployment_doc"]
        if not deployment_docs:
            deployment_docs = [Node(id="deploy.post_deploy", kind="deployment_doc", attributes={})]
        check = DeploymentCompletenessCheck(dag, self.project_root, self.settings)
        for test_node in tests:
            if any(check._verification_test_in_deploy_flow(test_node, deployment_doc) for deployment_doc in deployment_docs):
                return True
        return False

    def _any_test_asserts(self, tests: list[Node], capability: str) -> bool:
        for test_node in tests:
            if capability in self._assertion_signals(test_node):
                return True
            source_text = self._verification_source_text(test_node)
            if source_text and capability in source_text:
                return True
        return False

    def _assertion_signals(self, node: Node) -> set[str]:
        signals: set[str] = set()
        for key in ("assertions", "asserted_capabilities", "browser_assertions", "expected_outcome"):
            signals.update(self._nested_strings(node.attributes.get(key)))
        return signals

    def _verification_source_text(self, node: Node) -> str:
        if self.project_root is None:
            return ""
        source = self._verification_source(node)
        if not source:
            return ""
        path = (Path(self.project_root) / source).resolve()
        root = Path(self.project_root).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            return ""
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")

    def _runtime_capabilities(self, dag: DAG) -> tuple[set[str], bool]:
        capabilities: set[str] = set()
        declared = False
        for node in dag.nodes.values():
            if node.kind != "runtime_state":
                continue
            attributes = node.attributes or {}
            if "capabilities_provided" not in attributes:
                continue
            declared = True
            capabilities.update(str(item) for item in self._as_list(attributes.get("capabilities_provided")) if item)
        return capabilities, declared

    def _capability_requirements(self) -> dict[str, list[str]]:
        coherence = self.settings.get("coherence", {}) if isinstance(self.settings, dict) else {}
        raw = coherence.get("capability_requirements") if isinstance(coherence, dict) else None
        if not isinstance(raw, dict):
            return {}

        requirements: dict[str, list[str]] = {}
        for capability_kind, value in raw.items():
            required = value.get("requires_runtime") if isinstance(value, dict) else value
            values = [str(item) for item in self._as_list(required) if item]
            if values:
                requirements[str(capability_kind)] = values
        return requirements

    def _related_impl_nodes(self, dag: DAG, design_doc: Node) -> list[Node]:
        nodes: list[Node] = []
        for edge in self._edges_from(dag, design_doc.id):
            target = dag.nodes.get(edge.to_id)
            if target is not None and target.kind == "impl_file":
                nodes.append(target)
        if nodes:
            return self._dedupe_nodes(nodes)
        return [node for node in sorted(dag.nodes.values(), key=lambda item: item.id) if node.kind == "impl_file"]

    def _expected_nodes(self, dag: DAG, lex_refs: list[str]) -> dict[str, Node]:
        nodes: dict[str, Node] = {}
        for ref in lex_refs:
            node = dag.nodes.get(ref)
            if node is not None and node.kind == "expected":
                nodes[ref] = node
        return nodes

    def _expected_test_signals(self, expected_node: Node) -> set[str]:
        signals: set[str] = set()
        for key in ("path", "file", "output", "artifact_path", "value"):
            value = expected_node.attributes.get(key)
            signals.update(str(item) for item in self._as_list(value) if item)
        return signals

    def _e2e_tests_by_signal(self, dag: DAG) -> dict[str, list[Node]]:
        by_signal: dict[str, list[Node]] = {}
        for node in dag.nodes.values():
            if not self._is_e2e_test(node):
                continue
            for signal in self._test_signals(node):
                by_signal.setdefault(signal, []).append(node)
        return by_signal

    def _is_e2e_test(self, node: Node) -> bool:
        if node.kind == "verification_test" and str(node.attributes.get("kind") or "").lower() == "e2e":
            return True
        path = str(node.path or node.id).lower()
        return node.kind == "test_file" and ("/e2e/" in path or path.startswith("e2e/"))

    def _test_signals(self, node: Node) -> set[str]:
        signals = {node.id}
        if node.path:
            signals.add(node.path)
        source = self._verification_source(node)
        if source:
            signals.add(source)
        return {signal for signal in signals if signal}

    def _verification_source(self, node: Node) -> str | None:
        outcome = node.attributes.get("expected_outcome")
        if isinstance(outcome, dict) and isinstance(outcome.get("source"), str):
            return outcome["source"]
        return node.path

    def _edge_marks_journey(self, edge: Edge, journey_name: str, lex_refs: list[str]) -> bool:
        attributes = edge.attributes or {}
        return attributes.get("journey") == journey_name or edge.to_id in lex_refs

    def _runtime_constraints(self, design_doc: Node) -> list[dict[str, Any]]:
        entries = design_doc.attributes.get("runtime_constraints", [])
        return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []

    def _journey_entries(self, design_doc: Node) -> list[dict[str, Any]]:
        entries = design_doc.attributes.get("user_journeys", [])
        return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []

    def _lexicon_refs(self, journey: dict[str, Any]) -> list[str]:
        refs: list[str] = []
        for ref in self._as_list(journey.get("expected_outcome_refs")):
            if isinstance(ref, str) and ref.strip().startswith("lexicon:"):
                refs.append(ref.strip())
        return self._dedupe_strings(refs)

    def _runtime_evidence(self, node: Node) -> list[dict[str, Any]]:
        entries = node.attributes.get("runtime_evidence", [])
        return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []

    def _requirement_entries(self, expected_node: Node) -> list[dict[str, Any]]:
        entries = expected_node.attributes.get("browser_requirements", [])
        return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []

    def _has_assertion_step(self, journey: dict[str, Any]) -> bool:
        for step in self._as_list(journey.get("steps")):
            if not isinstance(step, dict):
                continue
            action = str(step.get("action") or "").lower()
            if action.startswith("expect") or action.startswith("assert") or action.startswith("verify"):
                return True
            if any(str(key).lower().startswith(("expect", "assert", "verify")) for key in step):
                return True
        return False

    def _remediation_hints(self, violations: list[dict[str, Any]], design_doc: Node) -> list[str]:
        hints: list[str] = []
        for violation in violations:
            explicit = violation.get("remediation")
            if isinstance(explicit, str) and explicit:
                hints.append(explicit)
                continue
            violation_type = violation.get("type")
            if violation_type == "unsatisfied_runtime_capability":
                capability = str(violation.get("required_capability"))
                rationale = self._design_rationale(violation, design_doc)
                hints.append(f"Provide {capability} in deployment capability declarations.")
                if rationale:
                    hints.append(f"Or relax the design constraint with rationale: {rationale}")
            elif violation_type == "impl_evidence_runtime_mismatch":
                capability = str(violation.get("missing_runtime_capability"))
                hints.append(f"Provide {capability} or adjust coherence.capability_requirements.")
            elif violation_type == "missing_journey_lexicon":
                hints.append("Add the suggested expected entry to project_lexicon.yaml.")
            elif violation_type == "no_plan_task_for_journey":
                hints.append("Add a plan task expected output for this journey.")
            elif violation_type == "no_e2e_test_for_journey":
                hints.append("Add an E2E verification output for this journey plan task.")
            elif violation_type == "e2e_not_in_post_deploy":
                hints.append("Add the E2E verification command to post_deploy.")
            elif violation_type == "browser_expected_not_asserted":
                hints.append("Assert the declared browser requirement in the E2E verification.")
        return self._dedupe_strings(hints)

    def _design_rationale(self, violation: dict[str, Any], design_doc: Node) -> str:
        rationale = violation.get("rationale_from_design")
        if isinstance(rationale, str) and rationale:
            return rationale
        for constraint in self._runtime_constraints(design_doc):
            if constraint.get("capability") == violation.get("required_capability"):
                value = constraint.get("rationale")
                if isinstance(value, str):
                    return value
        return ""

    def _violation(
        self,
        violation_type: str,
        journey_name: str,
        design_doc: str,
        *,
        severity: str = "red",
        human_review_required: bool | None = None,
        **details: Any,
    ) -> dict[str, Any]:
        return {
            "type": violation_type,
            "severity": severity,
            "user_journey": journey_name,
            "design_doc": design_doc,
            **{key: value for key, value in details.items() if value is not None},
            "human_review_required": (
                self._human_review_required(*details.values()) if human_review_required is None else human_review_required
            ),
        }

    def _human_review_required(self, *values: Any) -> bool:
        text = " ".join(self._nested_strings(values)).lower()
        return any(
            marker in text
            for marker in (
                "legal",
                "compliance",
                "regulatory",
                "budget",
                "cost approval",
                "low confidence",
                "confidence: low",
                "confidence=low",
                "法務",
                "予算",
            )
        )

    def _runtime_state_summary(self, actual_caps: set[str]) -> str:
        return f"capabilities_provided=[{', '.join(sorted(actual_caps))}]"

    def _expected_plan_outputs(self, journey_name: str, lex_refs: list[str]) -> list[str]:
        return [f"design:{journey_name}", *lex_refs]

    def _nested_strings(self, value: Any) -> set[str]:
        strings: set[str] = set()
        if value is None:
            return strings
        if isinstance(value, str):
            strings.add(value)
            return strings
        if isinstance(value, dict):
            for key, item in value.items():
                strings.update(self._nested_strings(key))
                strings.update(self._nested_strings(item))
            return strings
        if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
            for item in value:
                strings.update(self._nested_strings(item))
            return strings
        strings.add(str(value))
        return strings

    @staticmethod
    def _edges_from(dag: DAG, from_id: str) -> list[Edge]:
        return [edge for edge in dag.edges if edge.from_id == from_id]

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
    def _dedupe_nodes(nodes: Iterable[Node]) -> list[Node]:
        result: list[Node] = []
        seen: set[str] = set()
        for node in nodes:
            if node.id in seen:
                continue
            seen.add(node.id)
            result.append(node)
        return result

    @staticmethod
    def _dedupe_strings(values: Iterable[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            if value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result

    @staticmethod
    def _slug(value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
        return slug or "journey"


def result_to_dict(result: UserJourneyCoherenceResult) -> dict[str, Any]:
    return asdict(result)
