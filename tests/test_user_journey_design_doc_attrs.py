from __future__ import annotations

import warnings
from pathlib import Path

import yaml

from codd.dag.builder import build_dag
from codd.dag.checks.node_completeness import NodeCompletenessCheck
from codd.dag.extractor import extract_design_doc_journey_attrs, extract_design_doc_metadata


REPO_ROOT = Path(__file__).resolve().parents[1]


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


def _doc_with_frontmatter(frontmatter: dict, body: str = "# Auth\n") -> str:
    return yaml.safe_dump(frontmatter, explicit_start=True, sort_keys=False) + "---\n" + body


def _runtime_constraint(**overrides) -> dict:
    constraint = {
        "capability": "tls_termination",
        "required": True,
        "rationale": "Session transport must satisfy declared auth behavior.",
    }
    constraint.update(overrides)
    return constraint


def _journey(**overrides) -> dict:
    journey = {
        "name": "login_to_dashboard",
        "criticality": "critical",
        "steps": [
            {"action": "navigate", "target": "/login"},
            {"action": "expect_url", "value": "/dashboard"},
        ],
        "required_capabilities": ["cookie_persistence"],
        "expected_outcome_refs": ["lexicon:e2e_login_journey"],
    }
    journey.update(overrides)
    return journey


def _write_project_with_expected(tmp_path: Path, journey: dict | None = None):
    _write(tmp_path / "src" / "login.ts", "export const login = true;\n")
    _write(
        tmp_path / "project_lexicon.yaml",
        yaml.safe_dump(
            {
                "required_artifacts": [
                    {
                        "id": "e2e_login_journey",
                        "title": "Login journey",
                        "scope": "auth",
                        "source": "ai_derived",
                        "path": "src/login.ts",
                    }
                ]
            },
            sort_keys=False,
        ),
    )
    _write(
        tmp_path / "docs" / "design" / "auth.md",
        _doc_with_frontmatter({"user_journeys": [journey or _journey()]}),
    )
    return build_dag(tmp_path, _settings())


def test_runtime_constraints_missing_defaults_to_empty_attributes(tmp_path):
    doc = _write(tmp_path / "docs" / "design" / "auth.md", "# Auth\n")

    metadata = extract_design_doc_metadata(doc)
    dag = build_dag(tmp_path, _settings())

    assert metadata["attributes"]["runtime_constraints"] == []
    assert dag.nodes["docs/design/auth.md"].attributes["runtime_constraints"] == []


def test_runtime_constraints_valid_entries_are_structured(tmp_path):
    constraints = [_runtime_constraint(source_section="Security")]
    doc = _write(
        tmp_path / "docs" / "design" / "auth.md",
        _doc_with_frontmatter({"runtime_constraints": constraints}),
    )

    attrs = extract_design_doc_metadata(doc)["attributes"]

    assert attrs["runtime_constraints"] == constraints


def test_runtime_constraints_missing_required_fields_warns_only():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        attrs = extract_design_doc_journey_attrs({"runtime_constraints": [{"capability": "tls_termination"}]})

    assert attrs["runtime_constraints"][0]["capability"] == "tls_termination"
    assert any("runtime_constraints[0] missing required field" in str(item.message) for item in caught)


def test_user_journeys_missing_defaults_to_empty_attributes(tmp_path):
    doc = _write(tmp_path / "docs" / "design" / "auth.md", "# Auth\n")

    metadata = extract_design_doc_metadata(doc)
    dag = build_dag(tmp_path, _settings())

    assert metadata["attributes"]["user_journeys"] == []
    assert dag.nodes["docs/design/auth.md"].attributes["user_journeys"] == []


def test_user_journeys_valid_entries_are_structured(tmp_path):
    journeys = [_journey()]
    doc = _write(
        tmp_path / "docs" / "design" / "auth.md",
        _doc_with_frontmatter({"user_journeys": journeys}),
    )

    attrs = extract_design_doc_metadata(doc)["attributes"]

    assert attrs["user_journeys"] == journeys


def test_user_journey_steps_are_preserved_in_order(tmp_path):
    steps = [
        {"action": "navigate", "target": "/login"},
        {"action": "form_submit", "fields": ["email", "password"]},
        {"action": "expect_url", "value": "/dashboard"},
    ]
    doc = _write(
        tmp_path / "docs" / "design" / "auth.md",
        _doc_with_frontmatter({"user_journeys": [_journey(steps=steps)]}),
    )

    attrs = extract_design_doc_metadata(doc)["attributes"]

    assert attrs["user_journeys"][0]["steps"] == steps


def test_expected_outcome_refs_lexicon_creates_expects_edge_to_expected_node(tmp_path):
    dag = _write_project_with_expected(tmp_path)

    assert dag.nodes["lexicon:e2e_login_journey"].kind == "expected"
    assert any(
        edge.from_id == "docs/design/auth.md"
        and edge.to_id == "lexicon:e2e_login_journey"
        and edge.kind == "expects"
        and edge.attributes["source"] == "expected_outcome_refs"
        for edge in dag.edges
    )
    assert NodeCompletenessCheck().run(dag, tmp_path).passed is True


def test_expected_outcome_refs_design_self_reference_is_graceful_skip(tmp_path):
    dag = _write_project_with_expected(tmp_path, _journey(expected_outcome_refs=["design:login_to_dashboard"]))

    assert not any(
        edge.from_id == "docs/design/auth.md" and edge.to_id == "docs/design/auth.md" for edge in dag.edges
    )


def test_expected_outcome_refs_unknown_prefix_warns_without_edge(tmp_path):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        dag = _write_project_with_expected(tmp_path, _journey(expected_outcome_refs=["artifact:e2e_login_journey"]))

    assert not any(edge.to_id == "artifact:e2e_login_journey" for edge in dag.edges)
    assert any("unknown prefix" in str(item.message) for item in caught)


def test_required_capabilities_are_preserved_on_design_doc_attributes(tmp_path):
    dag = _write_project_with_expected(tmp_path, _journey(required_capabilities=["tls_termination", "cookie_store"]))

    attributes = dag.nodes["docs/design/auth.md"].attributes

    assert attributes["user_journeys"][0]["required_capabilities"] == ["tls_termination", "cookie_store"]


def test_existing_design_doc_frontmatter_free_regression(tmp_path):
    _write(tmp_path / "docs" / "design" / "api.md", "# API\nBody\n")

    dag = build_dag(tmp_path, _settings())
    node = dag.nodes["docs/design/api.md"]

    assert node.kind == "design_doc"
    assert node.attributes["depends_on"] == []
    assert node.attributes["runtime_constraints"] == []
    assert node.attributes["user_journeys"] == []


def test_generality_gate_has_no_stack_or_provider_hardcodes():
    forbidden = ("NextAuth", "__Secure", "Vercel", "Cloudflare", "AWS", "Supabase", "osato")

    for relative in ("codd/dag/builder.py", "codd/dag/extractor.py"):
        content = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert all(token not in content for token in forbidden)


def test_builder_registers_attrs_on_design_doc_node(tmp_path):
    _write(
        tmp_path / "docs" / "design" / "auth.md",
        _doc_with_frontmatter(
            {
                "runtime_constraints": [_runtime_constraint()],
                "user_journeys": [_journey(expected_outcome_refs=[])],
            }
        ),
    )

    dag = build_dag(tmp_path, _settings())
    attributes = dag.nodes["docs/design/auth.md"].attributes

    assert attributes["runtime_constraints"][0]["capability"] == "tls_termination"
    assert attributes["user_journeys"][0]["name"] == "login_to_dashboard"


def test_user_journey_missing_required_fields_warns_only():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        attrs = extract_design_doc_journey_attrs({"user_journeys": [{"name": "login_to_dashboard"}]})

    assert attrs["user_journeys"][0]["name"] == "login_to_dashboard"
    assert attrs["user_journeys"][0]["steps"] == []
    assert attrs["user_journeys"][0]["required_capabilities"] == []
    assert attrs["user_journeys"][0]["expected_outcome_refs"] == []
    assert any("user_journeys[0] missing required field" in str(item.message) for item in caught)
