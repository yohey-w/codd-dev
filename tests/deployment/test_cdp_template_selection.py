from __future__ import annotations

from pathlib import Path

import yaml

from codd.dag.builder import build_dag
from codd.deployment import VerificationKind
from codd.deployment.extractor import extract_verification_tests


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _settings(**overrides):
    settings = {
        "design_doc_patterns": ["docs/design/*.md"],
        "impl_file_patterns": ["src/**/*.ts", "src/**/*.tsx"],
        "test_file_patterns": ["tests/**/*.ts"],
        "plan_task_file": "docs/design/implementation_plan.md",
        "lexicon_file": "project_lexicon.yaml",
    }
    settings.update(overrides)
    return settings


def _write_codd_config(project_root: Path, *, cdp_browser: bool) -> None:
    payload: dict = {"project": {"type": "web"}}
    if cdp_browser:
        payload["verification"] = {"templates": {"cdp_browser": {}}}
    _write(project_root / "codd" / "codd.yaml", yaml.safe_dump(payload, sort_keys=False))


def _write_design_doc(project_root: Path, name: str, journeys: list[dict] | None) -> None:
    frontmatter = {} if journeys is None else {"user_journeys": journeys}
    content = yaml.safe_dump(frontmatter, explicit_start=True, sort_keys=False) + "---\n# Design\n"
    _write(project_root / "docs" / "design" / name, content)


def _journey(name: str | None, target: str = "/login") -> dict:
    journey = {
        "criticality": "critical",
        "steps": [
            {"action": "navigate", "target": target},
            {"action": "expect_url", "target": target},
        ],
        "required_capabilities": [],
        "expected_outcome_refs": [],
    }
    if name is not None:
        journey["name"] = name
    return journey


def _cdp_tests(project_root: Path):
    return [
        test
        for test in extract_verification_tests(project_root)
        if test.verification_template_ref == "cdp_browser"
    ]


def test_without_cdp_browser_config_keeps_extension_template_selection(tmp_path):
    _write_codd_config(tmp_path, cdp_browser=False)
    _write(tmp_path / "tests" / "smoke" / "health.sh", "curl /health\n")
    _write(tmp_path / "tests" / "e2e" / "login.spec.ts", "test('login')\n")

    refs = {test.verification_template_ref for test in extract_verification_tests(tmp_path)}

    assert refs == {"curl", "playwright"}


def test_with_cdp_browser_config_keeps_existing_file_template_selection(tmp_path):
    _write_codd_config(tmp_path, cdp_browser=True)
    _write(tmp_path / "tests" / "smoke" / "health.sh", "curl /health\n")
    _write(tmp_path / "tests" / "e2e" / "login.spec.ts", "test('login')\n")

    tests = extract_verification_tests(tmp_path)
    refs_by_id = {test.identifier: test.verification_template_ref for test in tests}

    assert refs_by_id["verification:smoke:tests/smoke/health.sh"] == "curl"
    assert refs_by_id["verification:e2e:tests/e2e/login.spec.ts"] == "playwright"


def test_cdp_browser_config_without_design_docs_adds_no_journey_tests(tmp_path):
    _write_codd_config(tmp_path, cdp_browser=True)

    assert _cdp_tests(tmp_path) == []


def test_cdp_browser_config_without_user_journeys_adds_no_journey_tests(tmp_path):
    _write_codd_config(tmp_path, cdp_browser=True)
    _write_design_doc(tmp_path, "auth.md", None)

    assert _cdp_tests(tmp_path) == []


def test_user_journeys_without_cdp_browser_config_add_no_journey_tests(tmp_path):
    _write_codd_config(tmp_path, cdp_browser=False)
    _write_design_doc(tmp_path, "auth.md", [_journey("login_to_dashboard")])

    assert _cdp_tests(tmp_path) == []


def test_cdp_browser_config_with_user_journey_adds_synthetic_e2e_test(tmp_path):
    _write_codd_config(tmp_path, cdp_browser=True)
    _write_design_doc(tmp_path, "auth.md", [_journey("login_to_dashboard")])

    tests = _cdp_tests(tmp_path)

    assert len(tests) == 1
    assert tests[0].identifier == "verification:cdp_browser:login_to_dashboard"
    assert tests[0].kind is VerificationKind.E2E
    assert tests[0].target == "/login"
    assert tests[0].expected_outcome["source"] == "docs/design/auth.md"
    assert tests[0].expected_outcome["journey_name"] == "login_to_dashboard"


def test_empty_cdp_browser_mapping_counts_as_declared(tmp_path):
    _write_codd_config(tmp_path, cdp_browser=True)
    _write_design_doc(tmp_path, "auth.md", [_journey("checkout_flow", "/checkout")])

    tests = _cdp_tests(tmp_path)

    assert [test.identifier for test in tests] == ["verification:cdp_browser:checkout_flow"]


def test_multiple_user_journeys_create_multiple_cdp_browser_tests(tmp_path):
    _write_codd_config(tmp_path, cdp_browser=True)
    _write_design_doc(
        tmp_path,
        "auth.md",
        [_journey("login_to_dashboard"), _journey("reset_password", "/reset-password")],
    )

    identifiers = {test.identifier for test in _cdp_tests(tmp_path)}

    assert identifiers == {
        "verification:cdp_browser:login_to_dashboard",
        "verification:cdp_browser:reset_password",
    }


def test_duplicate_journey_names_keep_distinct_verification_tests(tmp_path):
    _write_codd_config(tmp_path, cdp_browser=True)
    _write_design_doc(tmp_path, "auth.md", [_journey("shared_flow")])
    _write_design_doc(tmp_path, "billing.md", [_journey("shared_flow", "/billing")])

    identifiers = [test.identifier for test in _cdp_tests(tmp_path)]

    assert len(identifiers) == 2
    assert "verification:cdp_browser:shared_flow" in identifiers
    assert any(identifier.startswith("verification:cdp_browser:shared_flow:") for identifier in identifiers)


def test_user_journey_without_name_is_skipped(tmp_path):
    _write_codd_config(tmp_path, cdp_browser=True)
    _write_design_doc(tmp_path, "auth.md", [_journey(None)])

    assert _cdp_tests(tmp_path) == []


def test_build_dag_includes_cdp_browser_verification_node_attributes(tmp_path):
    _write_codd_config(tmp_path, cdp_browser=True)
    _write_design_doc(tmp_path, "auth.md", [_journey("login_to_dashboard")])

    dag = build_dag(tmp_path, _settings())
    node = dag.nodes["verification:cdp_browser:login_to_dashboard"]

    assert node.kind == "verification_test"
    assert node.path == "docs/design/auth.md"
    assert node.attributes["kind"] == "e2e"
    assert node.attributes["template_ref"] == "cdp_browser"
    assert node.attributes["verification_template_ref"] == "cdp_browser"
    assert node.attributes["journey_name"] == "login_to_dashboard"


def test_build_dag_does_not_add_cdp_browser_node_without_config(tmp_path):
    _write_codd_config(tmp_path, cdp_browser=False)
    _write_design_doc(tmp_path, "auth.md", [_journey("login_to_dashboard")])

    dag = build_dag(tmp_path, _settings())

    assert "verification:cdp_browser:login_to_dashboard" not in dag.nodes
