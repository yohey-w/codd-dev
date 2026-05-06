from __future__ import annotations

from pathlib import Path

import yaml

from codd.dag import Edge, Node
from codd.dag.builder import build_dag
from codd.dag.checks.user_journey_coherence import UserJourneyCoherenceCheck
from codd.dag.extractor import (
    extract_design_doc_journey_attrs,
    extract_design_doc_metadata,
    resolve_frontmatter_aliases,
)


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _doc_with_frontmatter(frontmatter: dict, body: str = "# Spec\n") -> str:
    return yaml.safe_dump(frontmatter, explicit_start=True, sort_keys=False) + "---\n" + body


def _settings(**overrides):
    settings = {
        "design_doc_patterns": ["docs/design/*.md"],
        "impl_file_patterns": ["src/**/*.py"],
        "test_file_patterns": ["tests/**/*.py"],
        "plan_task_file": "docs/design/implementation_plan.md",
        "lexicon_file": "project_lexicon.yaml",
    }
    settings.update(overrides)
    return settings


def _journey(**overrides) -> dict:
    journey = {
        "name": "startup_check",
        "criticality": "critical",
        "steps": [{"action": "expect_state", "value": "ready"}],
        "required_capabilities": [],
        "expected_outcome_refs": ["lexicon:e2e_startup_check"],
    }
    journey.update(overrides)
    return journey


def _constraint(**overrides) -> dict:
    constraint = {
        "capability": "scheduler_available",
        "required": True,
        "rationale": "The declared journey needs a scheduler.",
    }
    constraint.update(overrides)
    return constraint


def test_resolve_frontmatter_aliases_copies_alias_to_canonical_key():
    resolved = resolve_frontmatter_aliases(
        {"interaction_flows": [_journey()]},
        {"interaction_flows": "user_journeys"},
    )

    assert resolved["user_journeys"][0]["name"] == "startup_check"
    assert "interaction_flows" in resolved


def test_canonical_frontmatter_key_wins_over_alias():
    canonical = [_journey(name="canonical")]
    alias = [_journey(name="alias")]
    resolved = resolve_frontmatter_aliases(
        {"user_journeys": canonical, "interaction_flows": alias},
        {"interaction_flows": "user_journeys"},
    )

    assert resolved["user_journeys"] == canonical


def test_extract_attrs_accepts_alias_for_user_journeys():
    attrs = extract_design_doc_journey_attrs(
        {"interaction_flows": [_journey()]},
        frontmatter_alias={"interaction_flows": "user_journeys"},
    )

    assert attrs["user_journeys"][0]["name"] == "startup_check"


def test_extract_attrs_accepts_multiple_aliases_for_same_canonical():
    attrs = extract_design_doc_journey_attrs(
        {"journey_specs": [_journey(name="secondary")]},
        frontmatter_alias={
            "interaction_flows": "user_journeys",
            "journey_specs": "user_journeys",
        },
    )

    assert attrs["user_journeys"][0]["name"] == "secondary"


def test_extract_attrs_accepts_alias_for_runtime_constraints():
    attrs = extract_design_doc_journey_attrs(
        {"runtime_profiles": [_constraint()]},
        frontmatter_alias={"runtime_profiles": "runtime_constraints"},
    )

    assert attrs["runtime_constraints"][0]["capability"] == "scheduler_available"


def test_metadata_accepts_alias_for_expected_extraction(tmp_path: Path):
    payload = {"expected_nodes": [{"path_hint": "src/service.py"}], "expected_edges": []}
    doc = _write(
        tmp_path / "docs" / "design" / "spec.md",
        _doc_with_frontmatter({"expected_artifacts": payload}),
    )

    metadata = extract_design_doc_metadata(
        doc,
        frontmatter_alias={"expected_artifacts": "expected_extraction"},
    )

    assert metadata["attributes"]["expected_extraction"] == payload


def test_builder_uses_frontmatter_alias_from_settings(tmp_path: Path):
    _write(
        tmp_path / "docs" / "design" / "spec.md",
        _doc_with_frontmatter({"interaction_flows": [_journey(expected_outcome_refs=[])]}),
    )

    dag = build_dag(
        tmp_path,
        _settings(extraction={"frontmatter_alias": {"interaction_flows": "user_journeys"}}),
    )

    assert dag.nodes["docs/design/spec.md"].attributes["user_journeys"][0]["name"] == "startup_check"


def test_builder_uses_frontmatter_alias_from_codd_yaml(tmp_path: Path):
    _write(
        tmp_path / "codd" / "codd.yaml",
        yaml.safe_dump(
            {
                "project": {"type": "generic"},
                "dag": {"design_doc_patterns": ["docs/design/*.md"]},
                "extraction": {"frontmatter_alias": {"interaction_flows": "user_journeys"}},
            },
            sort_keys=False,
        ),
    )
    _write(
        tmp_path / "docs" / "design" / "spec.md",
        _doc_with_frontmatter({"interaction_flows": [_journey(expected_outcome_refs=[])]}),
    )

    dag = build_dag(tmp_path)

    assert dag.nodes["docs/design/spec.md"].attributes["user_journeys"][0]["name"] == "startup_check"


def test_alias_for_depends_on_creates_design_edge(tmp_path: Path):
    _write(tmp_path / "docs" / "design" / "base.md", "# Base\n")
    _write(
        tmp_path / "docs" / "design" / "feature.md",
        _doc_with_frontmatter({"prerequisites": ["base.md"]}),
    )

    dag = build_dag(
        tmp_path,
        _settings(extraction={"frontmatter_alias": {"prerequisites": "depends_on"}}),
    )

    assert any(
        edge.from_id == "docs/design/feature.md"
        and edge.to_id == "docs/design/base.md"
        and edge.kind == "depends_on"
        for edge in dag.edges
    )


def test_alias_written_design_doc_can_pass_c7(tmp_path: Path):
    _write(
        tmp_path / "docs" / "design" / "spec.md",
        _doc_with_frontmatter({"interaction_flows": [_journey()]}),
    )
    dag = build_dag(
        tmp_path,
        _settings(extraction={"frontmatter_alias": {"interaction_flows": "user_journeys"}}),
    )
    dag.add_node(
        Node(
            id="lexicon:e2e_startup_check",
            kind="expected",
            attributes={
                "id": "e2e_startup_check",
                "journey": "startup_check",
                "path": "tests/e2e/startup.py",
                "browser_requirements": [],
            },
        )
    )
    dag.add_node(
        Node(
            id="implementation_plan.md#E2E-STARTUP",
            kind="plan_task",
            attributes={"expected_outputs": ["lexicon:e2e_startup_check"]},
        )
    )
    dag.add_edge(
        Edge(
            from_id="implementation_plan.md#E2E-STARTUP",
            to_id="lexicon:e2e_startup_check",
            kind="produces",
            attributes={"journey": "startup_check"},
        )
    )
    dag.add_node(
        Node(
            id="verification:e2e:tests/e2e/startup.py",
            kind="verification_test",
            path="tests/e2e/startup.py",
            attributes={
                "kind": "e2e",
                "expected_outcome": {"source": "tests/e2e/startup.py"},
                "in_deploy_flow": True,
            },
        )
    )

    result = UserJourneyCoherenceCheck().run(dag, tmp_path, {})

    assert result.passed is True
