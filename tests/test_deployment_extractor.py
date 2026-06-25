from __future__ import annotations

from pathlib import Path

import pytest
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


def test_e2e_symlink_escaping_root_is_not_synthesized_or_read(tmp_path):
    # Path-escape leak: an e2e spec that is a symlink to a file OUTSIDE the
    # project root must not become a verification node, and the out-of-root
    # target must not be read. Previously _glob_paths resolved the symlink to
    # its out-of-root target, so a verification:e2e:/abs/outside.spec.ts node
    # was synthesized and the external file was read by _verification_target.
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    target = outside / "outside.spec.ts"
    target.write_text("test('/secret/admin/panel')\n", encoding="utf-8")

    e2e_dir = project / "tests" / "e2e"
    e2e_dir.mkdir(parents=True)
    link = e2e_dir / "spoof.spec.ts"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform")

    tests = extract_verification_tests(project)

    resolved_target = target.resolve().as_posix()
    for test in tests:
        assert resolved_target not in test.identifier
        assert test.expected_outcome.get("source") != resolved_target
        # The out-of-root URL must never be lifted as a target (would mean read).
        assert test.target != "/secret/admin/panel"
    assert all("outside.spec.ts" not in test.identifier for test in tests)


def test_in_root_e2e_spec_is_unchanged_by_symlink_jail(tmp_path):
    # Regression: a normal in-root spec (even when it physically lives behind an
    # in-root symlink) still resolves inside the root and stays discoverable.
    _write(tmp_path / "tests" / "e2e" / "login.spec.ts", "test('login')\n")

    tests = extract_verification_tests(tmp_path)

    assert [t.identifier for t in tests] == ["verification:e2e:tests/e2e/login.spec.ts"]
    assert tests[0].target == "login"


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


# ═══════════════════════════════════════════════════════════
# #3 — e2e routing by ProjectCapabilities.e2e_modality (cli → vitest)
# ═══════════════════════════════════════════════════════════


def _cli_config():
    return {"required_artifacts": {"project_type": "cli"}}


def _web_config():
    return {"required_artifacts": {"project_type": "web"}}


def test_cli_modality_routes_ts_e2e_to_vitest(tmp_path):
    _write(tmp_path / "tests" / "e2e" / "convert.test.ts", "test('cli converts')\n")

    tests = extract_verification_tests(tmp_path, _cli_config())

    assert len(tests) == 1
    assert tests[0].verification_template_ref == "vitest"


def test_browser_modality_routes_ts_e2e_to_playwright(tmp_path):
    _write(tmp_path / "tests" / "e2e" / "login.spec.ts", "test('login')\n")

    tests = extract_verification_tests(tmp_path, _web_config())

    assert tests[0].verification_template_ref == "playwright"


def test_cli_modality_discovers_test_ts_glob(tmp_path):
    # CLI e2e files are *.test.ts (not Playwright's *.spec.ts) and must be found.
    _write(tmp_path / "tests" / "e2e" / "cli.test.ts", "test('x')\n")

    tests = extract_verification_tests(tmp_path, _cli_config())

    assert any(t.verification_template_ref == "vitest" for t in tests)


def test_cli_modality_discovers_e2e_ts_glob(tmp_path):
    # ``*.e2e.ts`` is the explicit e2e convention codex emits unprompted; it must
    # be discovered as an E2E verification node (and routed to vitest for cli),
    # else the verify stage never RUNS the generated e2e test.
    _write(tmp_path / "tests" / "e2e" / "tempconv_conversion.e2e.ts", "test('x')\n")

    tests = extract_verification_tests(tmp_path, _cli_config())

    assert len(tests) == 1
    assert tests[0].verification_template_ref == "vitest"


def test_no_declared_type_keeps_legacy_playwright_routing(tmp_path):
    # Backward compatibility: no project_type configured → legacy extension
    # routing (.ts → playwright), so existing builds are unaffected.
    _write(tmp_path / "tests" / "e2e" / "login.spec.ts", "test('login')\n")

    tests = extract_verification_tests(tmp_path)  # no config

    assert tests[0].verification_template_ref == "playwright"


def test_sh_e2e_still_routes_to_curl_under_cli(tmp_path):
    _write(tmp_path / "tests" / "smoke" / "health.sh", "curl /health\n")

    tests = extract_verification_tests(tmp_path, _cli_config())

    assert tests[0].verification_template_ref == "curl"


def test_deployment_e2e_modality_override_wins(tmp_path):
    _write(tmp_path / "tests" / "e2e" / "x.test.ts", "test('x')\n")
    config = {"deployment": {"e2e_modality": "cli"}}

    tests = extract_verification_tests(tmp_path, config)

    assert tests[0].verification_template_ref == "vitest"


def test_vitest_template_is_registered():
    from codd.deployment.providers import VERIFICATION_TEMPLATES
    import codd.deployment.providers.verification  # noqa: F401

    assert "vitest" in VERIFICATION_TEMPLATES


# ═══════════════════════════════════════════════════════════
# B — Python HTTP e2e: discovery + routing (.py → pytest_http);
#     TS/JS modality routing and .sh → curl UNCHANGED.
# ═══════════════════════════════════════════════════════════


def test_verification_template_ref_python_e2e_routes_to_pytest_http():
    from codd.deployment.extractor import _verification_template_ref

    assert _verification_template_ref(Path("tests/e2e/test_items.py")) == "pytest_http"


def test_verification_template_ref_ts_modality_routing_unchanged():
    from codd.deployment.extractor import _verification_template_ref

    # Adding the .py branch must not move TS/JS modality routing or .sh → curl.
    assert _verification_template_ref(Path("x.spec.ts"), e2e_modality="browser") == "playwright"
    assert _verification_template_ref(Path("x.spec.ts"), e2e_modality="cli") == "vitest"
    assert _verification_template_ref(Path("x.test.ts")) == "playwright"  # legacy default
    assert _verification_template_ref(Path("health.sh")) == "curl"
    assert _verification_template_ref(Path("notes.md")) == "document"


def test_python_e2e_test_is_discovered_and_routed(tmp_path):
    # A generated Python e2e test (pytest convention) must be DISCOVERED by
    # extract_verification_tests and routed to pytest_http — else verify never
    # RUNS it (silent drop). Uses a Python project config.
    _write(tmp_path / "tests" / "e2e" / "test_items.py", "def test_x():\n    assert True\n")

    config = {"project": {"language": "python"}, "required_artifacts": {"project_type": "web"}}
    tests = extract_verification_tests(tmp_path, config)

    assert len(tests) == 1
    assert tests[0].kind is VerificationKind.E2E
    assert tests[0].verification_template_ref == "pytest_http"


def test_python_e2e_underscore_test_suffix_discovered(tmp_path):
    # ``*_test.py`` is the other pytest convention; it must also be discovered.
    _write(tmp_path / "tests" / "e2e" / "items_test.py", "def test_x():\n    assert True\n")

    tests = extract_verification_tests(tmp_path, {"project": {"language": "python"}})

    assert any(t.verification_template_ref == "pytest_http" for t in tests)


def test_python_e2e_init_and_helpers_not_discovered(tmp_path):
    # Bare ``tests/e2e/*.py`` is intentionally NOT a discovery pattern, so an
    # __init__.py / helper module is not wrongly treated as an e2e test.
    _write(tmp_path / "tests" / "e2e" / "__init__.py", "")
    _write(tmp_path / "tests" / "e2e" / "helpers.py", "def login():\n    pass\n")

    tests = extract_verification_tests(tmp_path, {"project": {"language": "python"}})

    assert tests == []


# ═══════════════════════════════════════════════════════════
# #5 — db_seed:users injection gated by project capability/modality
# ═══════════════════════════════════════════════════════════


def test_db_seed_not_injected_for_cli_modality(tmp_path):
    states = extract_runtime_states(
        tmp_path,
        [],
        [{"id": "docs/design/cmd.md", "acceptance_criteria": ["User runs the converter"]}],
        _cli_config(),
    )

    assert all(state.identifier != "runtime:db_seed:users" for state in states)


def test_db_seed_injected_for_web_modality(tmp_path):
    states = extract_runtime_states(
        tmp_path,
        [],
        [{"id": "docs/design/api.md", "acceptance_criteria": ["User can login"]}],
        _web_config(),
    )

    assert any(state.identifier == "runtime:db_seed:users" for state in states)


def test_db_seed_injected_when_no_type_declared_legacy(tmp_path):
    # Backward compatibility: no declared type → legacy injection preserved.
    states = extract_runtime_states(
        tmp_path,
        [],
        [{"id": "docs/design/api.md", "acceptance_criteria": ["User can login"]}],
    )

    assert any(state.identifier == "runtime:db_seed:users" for state in states)


def test_db_seed_explicit_override_suppresses_for_web(tmp_path):
    config = {
        "required_artifacts": {"project_type": "web"},
        "deployment": {"infer_db_seed_from_criteria": False},
    }
    states = extract_runtime_states(
        tmp_path,
        [],
        [{"id": "docs/design/api.md", "acceptance_criteria": ["User can login"]}],
        config,
    )

    assert all(state.identifier != "runtime:db_seed:users" for state in states)


def test_db_seed_explicit_override_enables_for_cli(tmp_path):
    config = {
        "required_artifacts": {"project_type": "cli"},
        "deployment": {"infer_db_seed_from_criteria": True},
    }
    states = extract_runtime_states(
        tmp_path,
        [],
        [{"id": "docs/design/cmd.md", "acceptance_criteria": ["User runs the converter"]}],
        config,
    )

    assert any(state.identifier == "runtime:db_seed:users" for state in states)


def test_db_seed_not_injected_for_detected_cpp_lib(tmp_path):
    """PRECISION: a C++ library (detected ``cpp_embedded``, no declared type) whose
    criteria mention ``user`` must NOT get the web/DB ``db_seed:users`` heuristic.

    The injection is a WEB heuristic; a typed non-web project detected by its build
    markers (here ``CMakeLists.txt``) is gated out generically — not a per-OSS case.
    """
    (tmp_path / "CMakeLists.txt").write_text("project(demo)\n", encoding="utf-8")
    states = extract_runtime_states(
        tmp_path,
        [],
        [{"id": "docs/design/api.md", "acceptance_criteria": ["The user runs the tool"]}],
    )

    assert all(state.identifier != "runtime:db_seed:users" for state in states)


def test_db_seed_not_injected_for_detected_java_lib(tmp_path):
    """PRECISION: a Java library (detected ``java`` via pom.xml) is gated out too."""
    (tmp_path / "pom.xml").write_text("<project />\n", encoding="utf-8")
    states = extract_runtime_states(
        tmp_path,
        [],
        [{"id": "docs/design/api.md", "acceptance_criteria": ["A user can login"]}],
    )

    assert all(state.identifier != "runtime:db_seed:users" for state in states)


def test_db_seed_still_injected_for_generic_untyped_project(tmp_path):
    """No build markers → ``generic`` → legacy injection preserved (no over-gating)."""
    states = extract_runtime_states(
        tmp_path,
        [],
        [{"id": "docs/design/api.md", "acceptance_criteria": ["User can login"]}],
    )

    assert any(state.identifier == "runtime:db_seed:users" for state in states)
