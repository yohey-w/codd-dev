from __future__ import annotations

import json

from click.testing import CliRunner

import codd.dag.checks.deployment_completeness as deployment_module
from codd.cli import main
from codd.dag import DAG, Edge, Node
from codd.dag.builder import build_dag
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


def test_no_deployment_doc_or_edges_skips_not_vacuous_pass(tmp_path):
    # No deployment_doc and no deploy edges = the C6 chain is not declared. The
    # check must SKIP (verified nothing on purpose), not emit a clean PASS that a
    # verify summary cannot distinguish from a real verification (false-green).
    dag = DAG()
    dag.add_node(Node(id="docs/design/api.md", kind="design_doc"))

    result = _run(dag, tmp_path)

    assert result.passed is True
    assert result.violations == []
    assert result.status == "skip"
    assert result.skipped is True
    assert result.checked_count == 0


def test_complete_chain_reports_checked_count(tmp_path):
    # A real verification walks the declared chain — checked_count is non-zero so
    # the pass is materially distinct from the vacuous (skip) case above.
    result = _run(_complete_seed_dag(), tmp_path)

    assert result.passed is True
    assert result.skipped is False
    assert result.status == "pass"
    assert result.checked_count == 1


def test_downstream_only_edges_skip_with_zero_checked(tmp_path):
    downstream_kinds = (
        EDGE_EXECUTES_IN_ORDER,
        EDGE_PRODUCES_STATE,
        EDGE_VERIFIED_BY,
    )
    for edge_kind in downstream_kinds:
        dag = DAG()
        dag.add_node(Node(id="docs/design/job.md", kind="design_doc"))
        dag.add_node(Node(id="downstream:from", kind="impl_file"))
        dag.add_node(Node(id="downstream:to", kind="runtime_state"))
        dag.add_edge(Edge(from_id="downstream:from", to_id="downstream:to", kind=edge_kind))

        result = _run(dag, tmp_path)

        assert result.status == "skip", edge_kind
        assert result.skipped is True, edge_kind
        assert result.checked_count == 0, edge_kind
        assert "1 downstream deploy edge(s) observed" in result.message, edge_kind


def test_deployment_doc_without_declared_root_skips_with_zero_checked(tmp_path):
    dag = DAG()
    dag.add_node(Node(id="docs/design/job.md", kind="design_doc"))
    dag.add_node(
        Node(
            id="DEPLOYMENT.md",
            kind="deployment_doc",
            attributes={"sections": ["seed"]},
        )
    )

    result = _run(dag, tmp_path)

    assert result.status == "skip"
    assert result.skipped is True
    assert result.checked_count == 0
    assert "1 deployment_doc node(s)" in result.message


def test_checked_count_is_declared_requirement_roots_not_design_docs(tmp_path):
    dag = _complete_seed_dag()
    dag.add_node(
        Node(
            id="SECOND_DEPLOYMENT.md",
            kind="deployment_doc",
            path="SECOND_DEPLOYMENT.md",
            attributes={"sections": ["seed"], "post_deploy": ["npm run test:smoke"]},
        )
    )
    dag.add_edge(
        Edge(
            from_id="docs/design/api.md",
            to_id="SECOND_DEPLOYMENT.md",
            kind=EDGE_REQUIRES_DEPLOYMENT_STEP,
            attributes={"keywords": ["seed"]},
        )
    )
    dag.add_edge(
        Edge(
            from_id="SECOND_DEPLOYMENT.md",
            to_id="prisma/seed.ts",
            kind=EDGE_EXECUTES_IN_ORDER,
            attributes={"order": 1, "section": "seed"},
        )
    )

    result = _run(dag, tmp_path)

    assert result.passed is True
    assert result.checked_count == 2


def test_two_declared_roots_count_both_and_keep_one_broken_chain_red(tmp_path):
    dag = _complete_seed_dag()
    dag.add_node(
        Node(
            id="SECOND_DEPLOYMENT.md",
            kind="deployment_doc",
            attributes={"sections": ["seed"], "post_deploy": ["npm run test:smoke"]},
        )
    )
    dag.add_edge(
        Edge(
            from_id="docs/design/api.md",
            to_id="SECOND_DEPLOYMENT.md",
            kind=EDGE_REQUIRES_DEPLOYMENT_STEP,
            attributes={"keywords": ["seed"]},
        )
    )

    result = _run(dag, tmp_path)

    assert result.status == "fail"
    assert result.checked_count == 2
    assert [violation.design_doc for violation in result.violations] == [
        "docs/design/api.md"
    ]
    assert [violation.broken_at for violation in result.violations] == [
        "missing_impl_for_step"
    ]


def test_declared_requirement_without_expected_steps_fails_not_vacuous_pass(tmp_path):
    dag = DAG()
    dag.add_node(Node(id="docs/design/job.md", kind="design_doc"))
    dag.add_node(
        Node(
            id="DEPLOYMENT.md",
            kind="deployment_doc",
            attributes={"sections": []},
        )
    )
    dag.add_edge(
        Edge(
            from_id="docs/design/job.md",
            to_id="DEPLOYMENT.md",
            kind=EDGE_REQUIRES_DEPLOYMENT_STEP,
            attributes={"source": "deployment_frontmatter"},
        )
    )

    result = _run(dag, tmp_path)

    assert result.status == "fail"
    assert result.checked_count == 1
    assert result.violations[0].broken_at == "missing_step_in_deployment_doc"
    assert "None" not in result.violations[0].remediation
    assert "deployment_step" in result.violations[0].remediation


def test_builder_can_generate_declared_requirement_without_expected_steps(tmp_path):
    design_dir = tmp_path / "docs" / "design"
    design_dir.mkdir(parents=True)
    (design_dir / "job.md").write_text("# Generic job\n", encoding="utf-8")
    (tmp_path / "DEPLOYMENT.md").write_text(
        "---\ndepends_on:\n  - docs/design/job.md\n---\n# Deployment\n",
        encoding="utf-8",
    )

    dag = build_dag(
        tmp_path,
        {
            "design_doc_patterns": ["docs/design/*.md"],
            "impl_file_patterns": [],
            "test_file_patterns": [],
        },
    )
    declared_roots = [
        edge
        for edge in dag.edges
        if edge.kind == EDGE_REQUIRES_DEPLOYMENT_STEP
    ]

    assert len(declared_roots) == 1
    assert declared_roots[0].from_id == "docs/design/job.md"
    assert declared_roots[0].to_id == "DEPLOYMENT.md"
    result = _run(dag, tmp_path)
    assert result.status == "fail"
    assert result.checked_count == 1
    assert result.violations[0].broken_at == "missing_step_in_deployment_doc"


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

    result = _run(dag, tmp_path)

    assert result.status == "fail"
    assert result.checked_count == 1
    assert result.violations[0].broken_at == "missing_deployment_doc"


def test_missing_deployment_doc_when_target_is_wrong_kind(tmp_path):
    dag = _complete_seed_dag()
    dag.nodes["DEPLOYMENT.md"].kind = "impl_file"

    result = _run(dag, tmp_path)

    assert result.status == "fail"
    assert result.checked_count == 1
    assert result.violations[0].broken_at == "missing_deployment_doc"


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


def test_deploy_yaml_symlink_escaping_root_is_not_credited(tmp_path):
    # RED-before-GREEN: ``_project_hooks`` read the fixed-name ``deploy.yaml``
    # candidate via ``is_file()`` / ``read_text()`` after ``Path.resolve()``
    # followed the symlink off-root, so an in-root ``deploy.yaml`` symlink whose
    # target escapes the project tree credited its off-root post_deploy hook into
    # the C6 chain (path-escape false-green: the verification looked deploy-wired).
    import os

    project_root = tmp_path / "project"
    project_root.mkdir()
    # An off-root deploy.yaml whose post_deploy WOULD credit the smoke test.
    outside = tmp_path / "outside_deploy.yaml"
    outside.write_text(
        "targets:\n  vps:\n    post_deploy:\n      - npm run test:smoke\n",
        encoding="utf-8",
    )
    link = project_root / "deploy.yaml"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        import pytest

        pytest.skip("symlinks not supported on this platform")

    dag = _complete_seed_dag(deploy_flow=False)

    result = _run(dag, project_root)

    # Old behavior: off-root hook read => passed=True (false-green). Now: the
    # escaping candidate is dropped, the verification is NOT deploy-wired.
    assert result.passed is False
    assert len(result.violations) == 1
    assert result.violations[0].broken_at == "verification_test_not_in_deploy_flow"


def test_deploy_yaml_in_root_symlink_still_credited(tmp_path):
    # Anti-false-red: an in-root ``deploy.yaml`` symlink whose target ALSO stays
    # inside the project root keeps crediting its post_deploy hook (in-root ->
    # in-root is a valid layout, not an escape).
    import os

    project_root = tmp_path / "project"
    project_root.mkdir()
    real = project_root / "config" / "real_deploy.yaml"
    real.parent.mkdir(parents=True)
    real.write_text(
        "targets:\n  vps:\n    post_deploy:\n      - npm run test:smoke\n",
        encoding="utf-8",
    )
    link = project_root / "deploy.yaml"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        import pytest

        pytest.skip("symlinks not supported on this platform")

    dag = _complete_seed_dag(deploy_flow=False)

    result = _run(dag, project_root)

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
    # An empty project declares no deploy chain, so the check SKIPs (verified
    # nothing) instead of rendering a clean PASS over zero design docs.
    result = CliRunner().invoke(
        main,
        ["dag", "verify", "--project-path", str(tmp_path), "--check", "deployment_completeness"],
    )

    assert result.exit_code == 0
    # The skip is emitted with severity="info" (not the dataclass default "red")
    # so severity-keyed roll-ups never count a "verified nothing" skip as a
    # covered red check. The CLI therefore labels the SKIP [info].
    assert "SKIP  deployment_completeness [info]" in result.output


def test_design_acceptance_criteria_can_supply_required_steps(tmp_path):
    dag = _complete_seed_dag()
    dag.edges[0].attributes = {}
    dag.nodes["docs/design/api.md"].attributes = {
        "frontmatter": {"acceptance_criteria": ["login must run seed during deploy"]}
    }

    result = _run(dag, tmp_path)

    assert result.passed is True
