from __future__ import annotations

from pathlib import Path

import yaml

from codd.dag.builder import build_dag
from codd.deployment import (
    EDGE_EXECUTES_IN_ORDER,
    EDGE_PRODUCES_STATE,
    EDGE_REQUIRES_DEPLOYMENT_STEP,
    EDGE_VERIFIED_BY,
    RuntimeStateKind,
    VerificationKind,
)
from codd.deployment.extractor import (
    extract_deployment_docs,
    extract_runtime_states,
    extract_verification_tests,
    infer_deployment_edges,
)


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _settings(**overrides):
    settings = {
        "design_doc_patterns": ["docs/design/*.md"],
        "impl_file_patterns": ["src/**/*.ts", "prisma/**/*.ts"],
        "test_file_patterns": ["tests/**/*.ts"],
        "plan_task_file": "docs/design/implementation_plan.md",
        "lexicon_file": "project_lexicon.yaml",
    }
    settings.update(overrides)
    return settings


def test_extract_deployment_md_sections(tmp_path):
    _write(tmp_path / "DEPLOYMENT.md", "# Deploy\n\n## Migrate\n\n## Seed Database\n")

    docs = extract_deployment_docs(tmp_path)

    assert docs[0].path == "DEPLOYMENT.md"
    assert docs[0].sections == ["migrate", "seed_database"]


def test_extract_deploy_md_alias(tmp_path):
    _write(tmp_path / "DEPLOY.md", "## Start Server\n")

    docs = extract_deployment_docs(tmp_path)

    assert [doc.path for doc in docs] == ["DEPLOY.md"]
    assert docs[0].sections == ["start_server"]


def test_extract_docs_deploy_glob(tmp_path):
    _write(tmp_path / "docs" / "deploy" / "staging.md", "## Smoke Test\n")
    _write(tmp_path / "docs" / "deploy" / "production.md", "## Build\n")

    docs = extract_deployment_docs(tmp_path)

    assert [doc.path for doc in docs] == ["docs/deploy/production.md", "docs/deploy/staging.md"]


def test_extract_frontmatter_depends_on_and_target(tmp_path):
    _write(
        tmp_path / "DEPLOYMENT.md",
        "---\ndepends_on:\n  - docs/design/api_design.md\ndeploy_target_ref: vps\n---\n## Seed\n",
    )

    doc = extract_deployment_docs(tmp_path)[0]

    assert doc.depends_on == ["docs/design/api_design.md"]
    assert doc.deploy_target_ref == "vps"


def test_extract_codd_dir_deploy_yaml(tmp_path):
    _write(tmp_path / "codd" / "codd.yaml", "project:\n  type: web\n")
    _write(
        tmp_path / "codd" / "deploy.yaml",
        yaml.safe_dump({"targets": {"vps": {"steps": [{"name": "migrate"}, {"name": "seed"}]}}}),
    )

    docs = extract_deployment_docs(tmp_path)

    assert docs[0].path == "codd/deploy.yaml"
    assert docs[0].deploy_target_ref == "vps"
    assert docs[0].sections == ["migrate", "seed"]


def test_codd_yaml_documents_override_default_markdown(tmp_path):
    _write(tmp_path / "DEPLOYMENT.md", "## Ignored\n")
    _write(tmp_path / "ops" / "release.md", "## Start\n")
    _write(
        tmp_path / "codd" / "codd.yaml",
        "project:\n  type: web\ndeployment:\n  documents:\n    - ops/release.md\n",
    )

    docs = extract_deployment_docs(tmp_path)

    assert [doc.path for doc in docs] == ["ops/release.md"]


def test_extract_deployment_docs_absent_returns_empty(tmp_path):
    assert extract_deployment_docs(tmp_path) == []


def test_runtime_state_from_migrate_section(tmp_path):
    states = extract_runtime_states(tmp_path, extract_deployment_docs_with_sections("migrate"), [])

    assert states[0].kind is RuntimeStateKind.DB_SCHEMA
    assert states[0].target == "database_schema"


def test_runtime_state_from_seed_section(tmp_path):
    states = extract_runtime_states(tmp_path, extract_deployment_docs_with_sections("seed"), [])

    assert states[0].kind is RuntimeStateKind.DB_SEED
    assert states[0].target == "seed_data"


def test_runtime_state_from_start_section(tmp_path):
    states = extract_runtime_states(tmp_path, extract_deployment_docs_with_sections("start_server"), [])

    assert states[0].kind is RuntimeStateKind.SERVER_RUNNING
    assert states[0].target == "server"


def test_runtime_state_from_design_acceptance_criteria(tmp_path):
    states = extract_runtime_states(
        tmp_path,
        [],
        [{"id": "docs/design/api.md", "acceptance_criteria": ["User can login", "ログインできる"]}],
    )

    assert states[0].identifier == "runtime:db_seed:users"
    assert states[0].kind is RuntimeStateKind.DB_SEED


def test_verification_tests_smoke_glob(tmp_path):
    _write(tmp_path / "tests" / "smoke" / "login.test.ts", "test('/api/auth/login')\n")

    tests = extract_verification_tests(tmp_path)

    assert tests[0].kind is VerificationKind.SMOKE
    assert tests[0].target == "/api/auth/login"


def test_verification_tests_e2e_glob(tmp_path):
    _write(tmp_path / "tests" / "e2e" / "login.spec.ts", "test('login')\n")

    tests = extract_verification_tests(tmp_path)

    assert tests[0].kind is VerificationKind.E2E
    assert tests[0].target == "login"


def test_verification_tests_e2e_spec_md(tmp_path):
    _write(tmp_path / "e2e-spec.md", "## Acceptance Criteria\n- User can login\n")

    tests = extract_verification_tests(tmp_path)

    assert tests[0].identifier == "verification:e2e:e2e-spec.md"
    assert tests[0].target == "acceptance_criteria"


def test_verification_template_ref_from_extension(tmp_path):
    _write(tmp_path / "tests" / "smoke" / "health.sh", "curl /health\n")
    _write(tmp_path / "tests" / "e2e" / "login.spec.ts", "test('login')\n")

    refs = {test.verification_template_ref for test in extract_verification_tests(tmp_path)}

    assert refs == {"curl", "playwright"}


def test_requires_deployment_step_from_design_acceptance_criteria(tmp_path):
    docs = extract_deployment_docs_with_sections("seed")
    edges = infer_deployment_edges(
        tmp_path,
        docs,
        [],
        [],
        [],
        [{"id": "docs/design/api.md", "acceptance_criteria": ["Run seed before login"]}],
    )

    assert ("docs/design/api.md", "DEPLOYMENT.md", EDGE_REQUIRES_DEPLOYMENT_STEP) in {
        (from_id, to_id, kind) for from_id, to_id, kind, _attributes in edges
    }


def test_executes_in_order_edges_include_order(tmp_path):
    docs = extract_deployment_docs_with_sections("migrate", "seed")
    edges = infer_deployment_edges(tmp_path, docs, [], [], ["prisma/migrations/001_init.ts", "prisma/seed.ts"])

    edge_lookup = {(from_id, to_id, kind): attributes for from_id, to_id, kind, attributes in edges}

    assert edge_lookup[("DEPLOYMENT.md", "prisma/migrations/001_init.ts", EDGE_EXECUTES_IN_ORDER)]["order"] == 1
    assert edge_lookup[("DEPLOYMENT.md", "prisma/seed.ts", EDGE_EXECUTES_IN_ORDER)]["order"] == 2


def test_produces_state_edges_from_impl_paths(tmp_path):
    states = extract_runtime_states(tmp_path, extract_deployment_docs_with_sections("migrate", "seed", "start"), [])
    edges = infer_deployment_edges(
        tmp_path,
        [],
        states,
        [],
        ["prisma/migrations/001_init.ts", "prisma/seed.ts", "src/server.ts"],
    )

    triples = {(from_id, to_id, kind) for from_id, to_id, kind, _attributes in edges}

    assert ("prisma/migrations/001_init.ts", "runtime:db_schema:database_schema", EDGE_PRODUCES_STATE) in triples
    assert ("prisma/seed.ts", "runtime:db_seed:seed_data", EDGE_PRODUCES_STATE) in triples
    assert ("src/server.ts", "runtime:server_running:server", EDGE_PRODUCES_STATE) in triples


def test_verified_by_edges_match_runtime_target(tmp_path):
    states = extract_runtime_states(
        tmp_path,
        [],
        [{"id": "docs/design/api.md", "acceptance_criteria": ["User can login"]}],
    )
    _write(tmp_path / "tests" / "smoke" / "login.test.ts", "test('/api/auth/login')\n")
    verification_tests = extract_verification_tests(tmp_path)

    edges = infer_deployment_edges(tmp_path, [], states, verification_tests, [])

    assert (states[0].identifier, verification_tests[0].identifier, EDGE_VERIFIED_BY) in {
        (from_id, to_id, kind) for from_id, to_id, kind, _attributes in edges
    }


def test_build_dag_includes_deployment_nodes_edges_and_keeps_existing_graph(tmp_path):
    _write(
        tmp_path / "docs" / "design" / "api.md",
        "---\nacceptance_criteria:\n  - Run seed before login\n---\n# API\nsrc/server.ts\n",
    )
    _write(tmp_path / "DEPLOYMENT.md", "## Seed\n\n## Start\n")
    _write(tmp_path / "prisma" / "seed.ts", "export async function seed() {}\n")
    _write(tmp_path / "src" / "server.ts", "export const server = true;\n")
    _write(tmp_path / "tests" / "smoke" / "login.test.ts", "test('/api/auth/login')\n")

    dag = build_dag(tmp_path, _settings())

    assert dag.nodes["docs/design/api.md"].kind == "design_doc"
    assert dag.nodes["src/server.ts"].kind == "impl_file"
    assert dag.nodes["DEPLOYMENT.md"].kind == "deployment_doc"
    assert dag.nodes["runtime:db_seed:users"].kind == "runtime_state"
    assert any(edge.kind == EDGE_REQUIRES_DEPLOYMENT_STEP for edge in dag.edges)
    assert any(edge.kind == EDGE_EXECUTES_IN_ORDER for edge in dag.edges)
    assert any(edge.kind == EDGE_PRODUCES_STATE for edge in dag.edges)
    assert any(edge.kind == EDGE_VERIFIED_BY for edge in dag.edges)


def extract_deployment_docs_with_sections(*sections: str):
    from codd.deployment import DeploymentDocNode

    return [DeploymentDocNode(path="DEPLOYMENT.md", sections=list(sections))]
