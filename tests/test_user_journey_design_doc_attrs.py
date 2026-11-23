from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest
import yaml

from codd.dag import DAG, Node
from codd.dag.builder import _add_design_doc_expected_outcome_edges, build_dag
from codd.dag.checks.node_completeness import NodeCompletenessCheck
from codd.dag.checks.user_journey_coherence import UserJourneyCoherenceCheck
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


def _write_project_with_frontmatter(
    tmp_path: Path,
    frontmatter: dict,
    *,
    artifacts: list[dict] | None = None,
):
    _write(tmp_path / "src" / "login.ts", "export const login = true;\n")
    if artifacts is not None:
        _write(
            tmp_path / "project_lexicon.yaml",
            yaml.safe_dump({"required_artifacts": artifacts}, sort_keys=False),
        )
    _write(
        tmp_path / "docs" / "design" / "auth.md",
        _doc_with_frontmatter(frontmatter),
    )
    return build_dag(tmp_path, _settings())


def _expected_artifact(artifact_id: str = "e2e_login_journey") -> dict:
    return {
        "id": artifact_id,
        "title": "Completed workflow",
        "scope": "generic",
        "source": "declared",
        "path": "src/login.ts",
    }


def _write_project_with_expected(tmp_path: Path, journey: dict | None = None):
    return _write_project_with_frontmatter(
        tmp_path,
        {"user_journeys": [journey or _journey()]},
        artifacts=[_expected_artifact()],
    )


def _expected_ref_edges(dag: DAG) -> list:
    return [
        edge
        for edge in dag.edges
        if edge.from_id == "docs/design/auth.md"
        and edge.kind == "expects"
        and (edge.attributes or {}).get("source") == "expected_outcome_refs"
    ]


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


def test_canonical_user_journey_creates_serialized_expected_outcome_edge(tmp_path):
    dag = _write_project_with_frontmatter(
        tmp_path,
        {"codd": {"user_journeys": [_journey()]}},
        artifacts=[_expected_artifact()],
    )

    edges = _expected_ref_edges(dag)
    assert [(edge.to_id, edge.attributes) for edge in edges] == [
        (
            "lexicon:e2e_login_journey",
            {
                "source": "expected_outcome_refs",
                "ref": "lexicon:e2e_login_journey",
                "journey": "login_to_dashboard",
            },
        )
    ]
    saved = json.loads((tmp_path / ".codd" / "dag.json").read_text(encoding="utf-8"))
    assert [
        edge
        for edge in saved["edges"]
        if edge["from_id"] == "docs/design/auth.md"
        and edge["to_id"] == "lexicon:e2e_login_journey"
        and edge["kind"] == "expects"
        and edge.get("attributes", {}).get("source") == "expected_outcome_refs"
    ] == [
        {
            "from_id": "docs/design/auth.md",
            "to_id": "lexicon:e2e_login_journey",
            "kind": "expects",
            "attributes": {
                "source": "expected_outcome_refs",
                "ref": "lexicon:e2e_login_journey",
                "journey": "login_to_dashboard",
            },
        }
    ]


@pytest.mark.parametrize(
    "attributes",
    [
        {"user_journeys": [_journey()]},
        {"frontmatter": {"user_journeys": [_journey()]}},
    ],
    ids=["direct-attributes", "raw-top-level-frontmatter"],
)
def test_expected_outcome_edges_accept_existing_builder_attribute_shapes(attributes):
    dag = DAG()
    dag.add_node(Node(id="docs/design/auth.md", kind="design_doc", attributes=attributes))
    dag.add_node(Node(id="lexicon:e2e_login_journey", kind="expected"))

    _add_design_doc_expected_outcome_edges(dag, {"docs/design/auth.md": {"attributes": {}}})

    assert len(_expected_ref_edges(dag)) == 1


def test_expected_outcome_edge_dedup_preserves_distinct_refs_and_journeys(tmp_path):
    login = _journey(
        expected_outcome_refs=[
            "lexicon:e2e_login_journey",
            "lexicon:e2e_login_journey",
            "lexicon:e2e_export_journey",
        ]
    )
    audit = _journey(name="audit_completion", expected_outcome_refs=["lexicon:e2e_login_journey"])
    dag = _write_project_with_frontmatter(
        tmp_path,
        {
            "user_journeys": [login],
            "codd": {"user_journeys": [login, audit]},
        },
        artifacts=[_expected_artifact(), _expected_artifact("e2e_export_journey")],
    )

    assert {
        (edge.to_id, edge.attributes["journey"])
        for edge in _expected_ref_edges(dag)
    } == {
        ("lexicon:e2e_login_journey", "login_to_dashboard"),
        ("lexicon:e2e_export_journey", "login_to_dashboard"),
        ("lexicon:e2e_login_journey", "audit_completion"),
    }
    assert len(_expected_ref_edges(dag)) == 3


def test_same_named_journeys_in_top_level_and_canonical_keep_distinct_refs(tmp_path):
    top_level = _journey(expected_outcome_refs=["lexicon:e2e_login_journey"])
    canonical = _journey(expected_outcome_refs=["lexicon:e2e_export_journey"])
    dag = _write_project_with_frontmatter(
        tmp_path,
        {
            "user_journeys": [top_level],
            "codd": {"user_journeys": [canonical]},
        },
        artifacts=[_expected_artifact(), _expected_artifact("e2e_export_journey")],
    )

    assert {
        (edge.to_id, edge.attributes["journey"])
        for edge in _expected_ref_edges(dag)
    } == {
        ("lexicon:e2e_login_journey", "login_to_dashboard"),
        ("lexicon:e2e_export_journey", "login_to_dashboard"),
    }
    assert len(_expected_ref_edges(dag)) == 2


def test_canonical_missing_expected_catalog_keeps_c7_red_without_substitute_node(tmp_path):
    with pytest.warns(UserWarning, match="missing lexicon node"):
        dag = _write_project_with_frontmatter(
            tmp_path,
            {"codd": {"user_journeys": [_journey()]}},
        )

    assert "lexicon:e2e_login_journey" not in dag.nodes
    assert _expected_ref_edges(dag) == []
    result = UserJourneyCoherenceCheck().run(dag, tmp_path, {})
    assert result.passed is False
    assert "missing_journey_lexicon" in {item["type"] for item in result.violations}


def test_canonical_expected_ref_to_wrong_kind_warns_without_edge():
    attributes = {"frontmatter": {"codd": {"user_journeys": [_journey()]}}}
    dag = DAG()
    dag.add_node(Node(id="docs/design/auth.md", kind="design_doc", attributes=attributes))
    dag.add_node(Node(id="lexicon:e2e_login_journey", kind="impl_file"))

    with pytest.warns(UserWarning, match="missing lexicon node"):
        _add_design_doc_expected_outcome_edges(dag, {"docs/design/auth.md": {"attributes": {}}})

    assert _expected_ref_edges(dag) == []


def test_canonical_unknown_prefix_warns_without_edge(tmp_path):
    with pytest.warns(UserWarning, match="unknown prefix"):
        dag = _write_project_with_frontmatter(
            tmp_path,
            {"codd": {"user_journeys": [_journey(expected_outcome_refs=["artifact:result"])]}},
            artifacts=[_expected_artifact()],
        )

    assert _expected_ref_edges(dag) == []


@pytest.mark.parametrize("journeys", [None, "not-a-list", ["not-a-mapping"]])
def test_canonical_invalid_journey_shapes_do_not_create_edges(tmp_path, journeys):
    dag = _write_project_with_frontmatter(
        tmp_path,
        {"codd": {"user_journeys": journeys}},
        artifacts=[_expected_artifact()],
    )

    assert _expected_ref_edges(dag) == []


def test_canonical_design_self_reference_adds_no_edge_or_cycle(tmp_path):
    dag = _write_project_with_frontmatter(
        tmp_path,
        {
            "codd": {
                "user_journeys": [
                    _journey(expected_outcome_refs=["design:login_to_dashboard"]),
                ]
            }
        },
        artifacts=[_expected_artifact()],
    )

    assert not any(
        edge.from_id == "docs/design/auth.md" and edge.to_id == "docs/design/auth.md" for edge in dag.edges
    )
    assert dag.detect_cycles() == []


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


def test_display_presentation_and_aggregation_attrs_are_preserved(tmp_path):
    frontmatter = {
        "user_journeys": [_journey(expected_outcome_refs=[])],
        "display_fields": [
            {
                "field_id": "record.summary_value",
                "cardinality": "0..N",
                "expected_aggregation_signals": ["record_summary_many_source_display"],
            }
        ],
        "presentation_specs": [
            {
                "field_id": "record.published_at",
                "format": "YYYY-MM-DD HH:mm",
                "timezone": "Etc/UTC",
                "locale": "en-US",
            }
        ],
        "aggregation_policies": [
            {
                "field_id": "record.summary_value",
                "cardinality_when_many": {"policy": "average"},
                "test_data_variants": {"required_cardinality": ["0", "1", "N"]},
            }
        ],
    }
    _write(tmp_path / "docs" / "design" / "auth.md", _doc_with_frontmatter(frontmatter))
    dag = build_dag(tmp_path, _settings())

    attributes = dag.nodes["docs/design/auth.md"].attributes

    assert attributes["display_fields"][0]["field_id"] == "record.summary_value"
    assert attributes["display_fields"][0]["expected_aggregation_signals"] == ["record_summary_many_source_display"]
    assert attributes["display_fields"][0]["lexicon_refs"] == []
    assert attributes["display_fields"][0]["evidence_signals"] == []
    assert attributes["presentation_specs"][0]["format"] == "YYYY-MM-DD HH:mm"
    assert attributes["presentation_specs"][0]["expected_presentation_signals"] == []
    assert attributes["aggregation_policies"][0]["cardinality_when_many"] == {"policy": "average"}
    assert attributes["aggregation_policies"][0]["test_data_variants"] == {"required_cardinality": ["0", "1", "N"]}


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
