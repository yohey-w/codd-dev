from __future__ import annotations

import json

from click.testing import CliRunner

import codd.dag.checks.deployment_completeness as deployment_module
from codd.cli import main
from codd.dag import DAG, Edge, Node
from codd.dag.checks import get_registry
from codd.dag.checks.deployment_completeness import (
    DeploymentChainViolation,
    DeploymentCompletenessCheck,
)
from codd.deployment import (
    EDGE_EXECUTES_IN_ORDER,
    EDGE_PRODUCES_STATE,
    EDGE_REQUIRES_DEPLOYMENT_STEP,
    EDGE_VERIFIED_BY,
)


def _complete_seed_dag(*, deploy_flow: bool = True) -> DAG:
    dag = DAG()
    dag.add_node(Node(id="docs/design/api.md", kind="design_doc", path="docs/design/api.md"))
    dag.add_node(
        Node(
            id="DEPLOYMENT.md",
            kind="deployment_doc",
            path="DEPLOYMENT.md",
            attributes={"sections": ["seed"], "post_deploy": ["npm run test:smoke"] if deploy_flow else []},
        )
    )
    dag.add_node(Node(id="prisma/seed.ts", kind="impl_file", path="prisma/seed.ts"))
    dag.add_node(
        Node(
            id="runtime:db_seed:seed_data",
            kind="runtime_state",
            attributes={"kind": "db_seed", "target": "seed_data"},
        )
    )
    dag.add_node(
        Node(
            id="verification:smoke:tests/smoke/login.test.ts",
            kind="verification_test",
            path="tests/smoke/login.test.ts",
            attributes={
                "kind": "smoke",
                "target": "login",
                "verification_template_ref": "playwright",
                "expected_outcome": {"source": "tests/smoke/login.test.ts"},
            },
        )
    )
    dag.add_edge(
        Edge(
            from_id="docs/design/api.md",
            to_id="DEPLOYMENT.md",
            kind=EDGE_REQUIRES_DEPLOYMENT_STEP,
            attributes={"keywords": ["seed"]},
        )
    )
    dag.add_edge(
        Edge(
            from_id="DEPLOYMENT.md",
            to_id="prisma/seed.ts",
            kind=EDGE_EXECUTES_IN_ORDER,
            attributes={"order": 1, "section": "seed"},
        )
    )
    dag.add_edge(Edge(from_id="prisma/seed.ts", to_id="runtime:db_seed:seed_data", kind=EDGE_PRODUCES_STATE))
    dag.add_edge(
        Edge(
            from_id="runtime:db_seed:seed_data",
            to_id="verification:smoke:tests/smoke/login.test.ts",
            kind=EDGE_VERIFIED_BY,
        )
    )
    return dag


def _run(dag: DAG, tmp_path):
    return DeploymentCompletenessCheck().run(dag, tmp_path, {})


def _single_violation(dag: DAG, tmp_path) -> DeploymentChainViolation:
    result = _run(dag, tmp_path)
    assert result.passed is False
    assert len(result.violations) == 1
    return result.violations[0]


def test_deployment_completeness_registered():
    assert deployment_module.DeploymentCompletenessCheck is get_registry()["deployment_completeness"]


def test_no_deployment_doc_or_edges_is_backward_compatible(tmp_path):
    dag = DAG()
    dag.add_node(Node(id="docs/design/api.md", kind="design_doc"))

    result = _run(dag, tmp_path)

    assert result.passed is True
    assert result.violations == []


def test_complete_chain_passes(tmp_path):
    result = _run(_complete_seed_dag(), tmp_path)

    assert result.passed is True
    assert result.violations == []


def test_missing_deployment_doc_detected(tmp_path):
    dag = DAG()
    dag.add_node(Node(id="docs/design/api.md", kind="design_doc"))
    dag.add_edge(
        Edge(
            from_id="docs/design/api.md",
            to_id="DEPLOYMENT.md",
            kind=EDGE_REQUIRES_DEPLOYMENT_STEP,
            attributes={"keywords": ["seed"]},
        )
    )

    violation = _single_violation(dag, tmp_path)

    assert violation.broken_at == "missing_deployment_doc"


def test_missing_deployment_doc_when_target_is_wrong_kind(tmp_path):
    dag = _complete_seed_dag()
    dag.nodes["DEPLOYMENT.md"].kind = "impl_file"

    violation = _single_violation(dag, tmp_path)

    assert violation.broken_at == "missing_deployment_doc"


def test_missing_step_from_edge_keywords_detected(tmp_path):
    dag = _complete_seed_dag()
    dag.nodes["DEPLOYMENT.md"].attributes["sections"] = ["migrate"]

    violation = _single_violation(dag, tmp_path)

    assert violation.broken_at == "missing_step_in_deployment_doc"
    assert "seed step" in violation.expected_chain[1]


def test_missing_step_from_required_steps_attribute_detected(tmp_path):
    dag = _complete_seed_dag()
    dag.nodes["DEPLOYMENT.md"].attributes["sections"] = ["migrate"]
    dag.edges[0].attributes = {"required_steps": ["seed"]}

    violation = _single_violation(dag, tmp_path)

    assert violation.broken_at == "missing_step_in_deployment_doc"


def test_missing_impl_when_execute_edge_absent(tmp_path):
    dag = _complete_seed_dag()
    dag.edges = [edge for edge in dag.edges if edge.kind != EDGE_EXECUTES_IN_ORDER]

    violation = _single_violation(dag, tmp_path)

    assert violation.broken_at == "missing_impl_for_step"
    assert "prisma/seed.ts" in violation.remediation


def test_missing_impl_when_execute_target_is_orphan(tmp_path):
    dag = _complete_seed_dag()
    del dag.nodes["prisma/seed.ts"]

    violation = _single_violation(dag, tmp_path)

    assert violation.broken_at == "missing_impl_for_step"


def test_missing_impl_when_execute_target_wrong_kind(tmp_path):
    dag = _complete_seed_dag()
    dag.nodes["prisma/seed.ts"].kind = "deployment_doc"

    violation = _single_violation(dag, tmp_path)

    assert violation.broken_at == "missing_impl_for_step"


def test_state_not_produced_when_produces_edge_absent(tmp_path):
    dag = _complete_seed_dag()
    dag.edges = [edge for edge in dag.edges if edge.kind != EDGE_PRODUCES_STATE]

    violation = _single_violation(dag, tmp_path)

    assert violation.broken_at == "state_not_produced"


def test_state_not_produced_when_runtime_state_orphan(tmp_path):
    dag = _complete_seed_dag()
    del dag.nodes["runtime:db_seed:seed_data"]

    violation = _single_violation(dag, tmp_path)

    assert violation.broken_at == "state_not_produced"


def test_no_verification_test_when_verified_edge_absent(tmp_path):
    dag = _complete_seed_dag()
    dag.edges = [edge for edge in dag.edges if edge.kind != EDGE_VERIFIED_BY]

    violation = _single_violation(dag, tmp_path)

    assert violation.broken_at == "no_verification_test"


def test_no_verification_test_when_target_orphan(tmp_path):
    dag = _complete_seed_dag()
    del dag.nodes["verification:smoke:tests/smoke/login.test.ts"]

    violation = _single_violation(dag, tmp_path)

    assert violation.broken_at == "no_verification_test"


def test_verification_not_in_deploy_flow_when_no_post_deploy(tmp_path):
    dag = _complete_seed_dag(deploy_flow=False)

    violation = _single_violation(dag, tmp_path)

    assert violation.broken_at == "verification_test_not_in_deploy_flow"


def test_verification_in_deploy_flow_by_doc_attribute(tmp_path):
    dag = _complete_seed_dag(deploy_flow=False)
    dag.nodes["DEPLOYMENT.md"].attributes["post_deploy_hooks"] = ["npx playwright test tests/smoke/login.test.ts"]

    result = _run(dag, tmp_path)

    assert result.passed is True


def test_verification_in_deploy_flow_by_test_attribute(tmp_path):
    dag = _complete_seed_dag(deploy_flow=False)
    dag.nodes["verification:smoke:tests/smoke/login.test.ts"].attributes["in_deploy_flow"] = True

    result = _run(dag, tmp_path)

    assert result.passed is True


def test_verification_in_deploy_flow_by_deploy_yaml(tmp_path):
    (tmp_path / "deploy.yaml").write_text(
        "targets:\n  vps:\n    post_deploy:\n      - npm run test:smoke\n",
        encoding="utf-8",
    )
    dag = _complete_seed_dag(deploy_flow=False)

    result = _run(dag, tmp_path)

    assert result.passed is True


def test_format_report_outputs_incomplete_chain_report_json(tmp_path):
    dag = _complete_seed_dag(deploy_flow=False)
    result = _run(dag, tmp_path)

    payload = json.loads(DeploymentCompletenessCheck().format_report(result.violations))

    assert payload["incomplete_chain_report"][0]["broken_at"] == "verification_test_not_in_deploy_flow"


def test_format_report_accepts_result_object(tmp_path):
    dag = _complete_seed_dag(deploy_flow=False)
    result = _run(dag, tmp_path)

    payload = json.loads(DeploymentCompletenessCheck().format_report(result))

    assert payload["incomplete_chain_report"][0]["chain_status"] == "INCOMPLETE"


def test_remediation_hint_for_missing_impl_mentions_artifact(tmp_path):
    dag = _complete_seed_dag()
    dag.edges = [edge for edge in dag.edges if edge.kind != EDGE_EXECUTES_IN_ORDER]

    violation = _single_violation(dag, tmp_path)

    assert violation.remediation == "Add prisma/seed.ts and ensure the deploy artifact includes it."


def test_expected_chain_marks_broken_stage(tmp_path):
    dag = _complete_seed_dag()
    dag.edges = [edge for edge in dag.edges if edge.kind != EDGE_PRODUCES_STATE]

    violation = _single_violation(dag, tmp_path)

    assert "runtime:db_seed:seed_data [missing]" in violation.expected_chain[3]


def test_dag_verify_cli_runs_deployment_completeness_check(tmp_path):
    result = CliRunner().invoke(
        main,
        ["dag", "verify", "--project-path", str(tmp_path), "--check", "deployment_completeness"],
    )

    assert result.exit_code == 0
    assert "PASS  deployment_completeness [red]" in result.output


def test_design_acceptance_criteria_can_supply_required_steps(tmp_path):
    dag = _complete_seed_dag()
    dag.edges[0].attributes = {}
    dag.nodes["docs/design/api.md"].attributes = {
        "frontmatter": {"acceptance_criteria": ["login must run seed during deploy"]}
    }

    result = _run(dag, tmp_path)

    assert result.passed is True
