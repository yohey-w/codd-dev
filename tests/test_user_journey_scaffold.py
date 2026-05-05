from __future__ import annotations

from pathlib import Path

import yaml
from click.testing import CliRunner

from codd.cli import main
from codd.config import load_project_config
from codd.dag import DAG, Node
from codd.dag.builder import build_dag
from codd.dag.checks import get_registry
from codd.dag.checks.user_journey_coherence import UserJourneyCoherenceCheck
from codd.dag.extractor import extract_design_doc_metadata


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


def _write_lexicon_project(tmp_path: Path, artifact: dict):
    _write(tmp_path / "src" / "login.ts", "export const login = true;\n")
    _write(
        tmp_path / "project_lexicon.yaml",
        yaml.safe_dump({"required_artifacts": [artifact]}, sort_keys=False),
    )
    dag = build_dag(tmp_path, _settings())
    artifact_id = str(artifact["id"])
    node_id = artifact_id if artifact_id.startswith("lexicon:") else f"lexicon:{artifact_id}"
    return dag.nodes[node_id].attributes


def _artifact(**overrides) -> dict:
    artifact = {
        "id": "e2e_login_journey",
        "title": "Login journey",
        "scope": "auth",
        "source": "ai_derived",
        "path": "src/login.ts",
    }
    artifact.update(overrides)
    return artifact


def test_design_doc_frontmatter_without_user_journey_keys_keeps_existing_metadata(tmp_path):
    doc = _write(tmp_path / "docs" / "design" / "api.md", "---\ndepends_on:\n  - system.md\n---\n# API\n")

    metadata = extract_design_doc_metadata(doc)
    dag = build_dag(tmp_path, _settings())
    attributes = dag.nodes["docs/design/api.md"].attributes

    assert metadata["depends_on"] == ["system.md"]
    assert metadata["attributes"]["runtime_constraints"] == []
    assert metadata["attributes"]["user_journeys"] == []
    assert attributes["runtime_constraints"] == []
    assert attributes["user_journeys"] == []


def test_runtime_constraints_frontmatter_is_passed_through_to_design_doc_attributes(tmp_path):
    constraints = [{"capability": "tls_termination", "required": True, "rationale": "auth session transport"}]
    doc = _write(
        tmp_path / "docs" / "design" / "auth.md",
        yaml.safe_dump({"runtime_constraints": constraints}, explicit_start=True) + "---\n# Auth\n",
    )

    metadata = extract_design_doc_metadata(doc)
    dag = build_dag(tmp_path, _settings())

    assert metadata["attributes"]["runtime_constraints"] == constraints
    assert dag.nodes["docs/design/auth.md"].attributes["runtime_constraints"] == constraints


def test_user_journeys_frontmatter_is_passed_through_to_design_doc_attributes(tmp_path):
    journeys = [
        {
            "name": "login_to_dashboard",
            "criticality": "critical",
            "steps": [{"action": "navigate", "target": "/login"}],
            "required_capabilities": ["browser_cookie_persistence"],
            "expected_outcome_refs": [],
        }
    ]
    doc = _write(
        tmp_path / "docs" / "design" / "auth.md",
        yaml.safe_dump({"user_journeys": journeys}, explicit_start=True) + "---\n# Auth\n",
    )

    metadata = extract_design_doc_metadata(doc)
    dag = build_dag(tmp_path, _settings())

    assert metadata["attributes"]["user_journeys"] == journeys
    assert dag.nodes["docs/design/auth.md"].attributes["user_journeys"] == journeys


def test_lexicon_journey_attribute_is_passed_through(tmp_path):
    attributes = _write_lexicon_project(tmp_path, _artifact(journey="login_to_dashboard"))

    assert attributes["journey"] == "login_to_dashboard"


def test_lexicon_browser_requirements_are_passed_through(tmp_path):
    requirements = [{"capability": "cookie_set", "value": True}]
    attributes = _write_lexicon_project(tmp_path, _artifact(browser_requirements=requirements))

    assert attributes["browser_requirements"] == requirements


def test_lexicon_runtime_requirements_are_passed_through(tmp_path):
    requirements = [{"capability": "tls_termination", "required": True}]
    attributes = _write_lexicon_project(tmp_path, _artifact(runtime_requirements=requirements))

    assert attributes["runtime_requirements"] == requirements


def test_lexicon_unknown_keys_are_ignored_by_expected_node_attributes(tmp_path):
    attributes = _write_lexicon_project(tmp_path, _artifact(unmodeled_vendor_key={"keep": False}))

    assert "unmodeled_vendor_key" not in attributes


def test_user_journey_coherence_check_is_registered():
    assert get_registry()["user_journey_coherence"] is UserJourneyCoherenceCheck


def test_user_journey_coherence_without_declared_journeys_returns_skip_pass():
    dag = DAG()
    dag.add_node(Node(id="docs/design/auth.md", kind="design_doc", attributes={}))

    result = UserJourneyCoherenceCheck().run(dag)

    assert result.passed is True
    assert result.status == "pass"
    assert "SKIP" in result.message


def test_user_journey_coherence_with_declared_journeys_without_chain_reports_failure():
    dag = DAG()
    dag.add_node(
        Node(
            id="docs/design/auth.md",
            kind="design_doc",
            attributes={"user_journeys": [{"name": "login_to_dashboard"}]},
        )
    )

    result = UserJourneyCoherenceCheck().run(dag)

    assert result.passed is False
    assert "no_plan_task_for_journey" in {violation["type"] for violation in result.violations}


def test_codd_yaml_coherence_capability_patterns_are_loaded_as_dict(tmp_path):
    _write(
        tmp_path / "codd" / "codd.yaml",
        yaml.safe_dump(
            {
                "coherence": {
                    "capability_patterns": {
                        "cookie_security_secure_attribute": {
                            "matches": [{"regex": "__Secure-", "languages": ["typescript"]}]
                        }
                    }
                }
            },
            sort_keys=False,
        ),
    )

    config = load_project_config(tmp_path)

    patterns = config["coherence"]["capability_patterns"]
    assert isinstance(patterns, dict)
    assert patterns["cookie_security_secure_attribute"]["matches"][0]["regex"] == "__Secure-"


def test_codd_yaml_coherence_capability_patterns_default_to_empty_dict(tmp_path):
    _write(tmp_path / "codd" / "codd.yaml", "project:\n  name: demo\n")

    config = load_project_config(tmp_path)

    assert config["coherence"]["capability_patterns"] == {}


def test_dag_verify_user_journey_coherence_check_runs_gracefully(tmp_path):
    result = CliRunner().invoke(
        main,
        ["dag", "verify", "--project-path", str(tmp_path), "--check", "user_journey_coherence"],
    )

    assert result.exit_code == 0
    assert "PASS  user_journey_coherence" in result.output


def test_default_dag_verify_keeps_existing_checks_and_adds_user_journey_coherence(tmp_path):
    check_names = (
        "node_completeness",
        "edge_validity",
        "depends_on_consistency",
        "task_completion",
        "transitive_closure",
        "deployment_completeness",
        "user_journey_coherence",
    )
    args = ["dag", "verify", "--project-path", str(tmp_path)]
    for check_name in check_names:
        args.extend(["--check", check_name])

    result = CliRunner().invoke(main, args)

    assert result.exit_code == 0
    for check_name in check_names:
        assert check_name in result.output
