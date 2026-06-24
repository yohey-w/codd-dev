from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codd.dag import DAG, Node
from codd.dag.checks import get_registry
from codd.dag.checks.environment_coverage import EnvironmentCoverageCheck
from codd.dag.coverage_axes import (
    CoverageAxis,
    CoverageVariant,
    extract_coverage_axes_from_design_doc,
    extract_coverage_axes_from_lexicon,
)
from codd.dag.extractor import extract_design_doc_metadata


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _doc(frontmatter: dict) -> str:
    return yaml.safe_dump(frontmatter, explicit_start=True, sort_keys=False) + "---\n# Spec\n"


def test_coverage_variant_serializes_round_trip():
    variant = CoverageVariant(
        id="small_case",
        label="Small Case",
        attributes={"size": 1},
        criticality="critical",
    )

    loaded = CoverageVariant.from_dict(variant.to_dict())

    assert loaded == variant


def test_coverage_axis_serializes_round_trip():
    axis = CoverageAxis(
        axis_type="runtime_shape",
        rationale="Required by the operating profile.",
        variants=[CoverageVariant(id="shape_a", label="Shape A", criticality="high")],
        source="lexicon",
        owner_section="project_lexicon.yaml",
    )

    loaded = CoverageAxis.from_dict(axis.to_dict())

    assert loaded == axis


def test_variant_string_entry_becomes_unclear_variant():
    variant = CoverageVariant.from_dict("shape_a")

    assert variant.id == "shape_a"
    assert variant.label == "shape_a"
    assert variant.criticality is None


def test_variant_dict_without_label_uses_id():
    variant = CoverageVariant.from_dict({"id": "shape_a", "criticality": "info"})

    assert variant.label == "shape_a"


def test_invalid_variant_without_id_raises():
    with pytest.raises(ValueError, match="missing id"):
        CoverageVariant.from_dict({"label": "No id"})


def test_extract_coverage_axes_from_lexicon_reads_entries(tmp_path: Path):
    path = _write(
        tmp_path / "project_lexicon.yaml",
        yaml.safe_dump(
            {
                "coverage_axes": [
                    {
                        "axis_type": "runtime_shape",
                        "rationale": "Two shapes are required.",
                        "variants": [{"id": "shape_a", "label": "Shape A", "criticality": "critical"}],
                    }
                ]
            },
            sort_keys=False,
        ),
    )

    axes = extract_coverage_axes_from_lexicon(path)

    assert axes[0].axis_type == "runtime_shape"
    assert axes[0].source == "lexicon"
    assert axes[0].owner_section == "project_lexicon.yaml"
    assert axes[0].variants[0].criticality == "critical"


def test_extract_coverage_axes_from_missing_lexicon_returns_empty(tmp_path: Path):
    assert extract_coverage_axes_from_lexicon(tmp_path / "project_lexicon.yaml") == []


def test_extract_coverage_axes_from_lexicon_ignores_non_mapping(tmp_path: Path):
    path = _write(tmp_path / "project_lexicon.yaml", "- not-a-map\n")

    assert extract_coverage_axes_from_lexicon(path) == []


def test_design_doc_metadata_passthroughs_coverage_axes(tmp_path: Path):
    doc = _write(
        tmp_path / "docs" / "design" / "spec.md",
        _doc({"coverage_axes": [{"axis_type": "runtime_shape", "variants": ["shape_a"]}]}),
    )

    metadata = extract_design_doc_metadata(doc)

    assert metadata["attributes"]["coverage_axes"][0]["axis_type"] == "runtime_shape"


def test_extract_coverage_axes_from_design_doc_node():
    node = Node(
        id="docs/design/spec.md",
        kind="design_doc",
        attributes={"coverage_axes": [{"axis_type": "runtime_shape", "variants": ["shape_a"]}]},
    )

    axes = extract_coverage_axes_from_design_doc(node)

    assert axes[0].source == "design_doc"
    assert axes[0].owner_section == "docs/design/spec.md"
    assert axes[0].variants[0].id == "shape_a"


def test_extract_coverage_axes_from_design_doc_reads_frontmatter_codd():
    # Axes authored at the canonical frontmatter.codd position must be read.
    # Before the central metadata helper, only attributes["coverage_axes"] was
    # read, so a codd-nested axis was dropped → C9 dormant (false-green).
    node = Node(
        id="docs/design/spec.md",
        kind="design_doc",
        attributes={
            "coverage_axes": [],
            "frontmatter": {
                "codd": {"coverage_axes": [{"axis_type": "runtime_shape", "variants": ["shape_a"]}]}
            },
        },
    )

    axes = extract_coverage_axes_from_design_doc(node)

    assert len(axes) == 1
    assert axes[0].axis_type == "runtime_shape"
    assert axes[0].source == "design_doc"


def test_extract_coverage_axes_does_not_double_count_lifted_axis():
    # Extractor lifts a top-level frontmatter axis into BOTH attributes and the
    # raw frontmatter copy; it must be read once, not twice.
    axis_entry = {"axis_type": "runtime_shape", "variants": ["shape_a"]}
    node = Node(
        id="docs/design/spec.md",
        kind="design_doc",
        attributes={
            "coverage_axes": [axis_entry],
            "frontmatter": {"coverage_axes": [axis_entry]},
        },
    )

    axes = extract_coverage_axes_from_design_doc(node)

    assert len(axes) == 1


def test_extract_coverage_axes_unions_top_level_and_codd():
    node = Node(
        id="docs/design/spec.md",
        kind="design_doc",
        attributes={
            "coverage_axes": [{"axis_type": "axis_top", "variants": ["v1"]}],
            "frontmatter": {
                "coverage_axes": [{"axis_type": "axis_top", "variants": ["v1"]}],  # raw dup
                "codd": {"coverage_axes": [{"axis_type": "axis_nested", "variants": ["v2"]}]},
            },
        },
    )

    axes = extract_coverage_axes_from_design_doc(node)

    assert {axis.axis_type for axis in axes} == {"axis_top", "axis_nested"}


def test_design_doc_axis_can_override_owner_section():
    node = Node(
        id="docs/design/spec.md",
        kind="design_doc",
        attributes={
            "coverage_axes": [
                {
                    "axis_type": "runtime_shape",
                    "owner_section": "local-section",
                    "variants": [{"id": "shape_a"}],
                }
            ]
        },
    )

    axes = extract_coverage_axes_from_design_doc(node)

    assert axes[0].owner_section == "local-section"


def test_invalid_coverage_axis_warns_and_is_ignored(tmp_path: Path):
    path = _write(
        tmp_path / "project_lexicon.yaml",
        yaml.safe_dump({"coverage_axes": [{"rationale": "missing type", "variants": []}]}),
    )

    with pytest.warns(UserWarning, match="missing axis_type"):
        axes = extract_coverage_axes_from_lexicon(path)

    assert axes == []


def test_environment_coverage_skeleton_is_registered():
    assert get_registry()["environment_coverage"] is EnvironmentCoverageCheck


def test_environment_coverage_skeleton_passes():
    result = EnvironmentCoverageCheck().run(DAG())

    assert result.passed is True
    assert result.block_deploy is True


# --- journey scope (opt-in axis/variant -> journey applicability) ----------


def test_journey_scope_undeclared_keeps_serialization_stable():
    variant = CoverageVariant(id="shape_a", label="Shape A", criticality="high")
    axis = CoverageAxis(
        axis_type="runtime_shape",
        rationale="r",
        variants=[variant],
        source="lexicon",
    )

    assert "journey_scope" not in variant.to_dict()
    assert "journey_scope" not in axis.to_dict()


def test_journey_scope_list_shorthand_is_include_list():
    variant = CoverageVariant.from_dict({"id": "shape_a", "journey_scope": ["flow_one", "flow_*"]})

    assert variant.journey_scope is not None
    assert variant.journey_scope.applies_to("flow_one") is True
    assert variant.journey_scope.applies_to("flow_two") is True
    assert variant.journey_scope.applies_to("other") is False


def test_journey_scope_empty_include_means_no_journeys():
    variant = CoverageVariant.from_dict({"id": "shape_a", "journey_scope": {"include": []}})

    assert variant.journey_scope is not None
    assert variant.journey_scope.applies_to("anything") is False


def test_journey_scope_exclude_only_keeps_other_journeys():
    variant = CoverageVariant.from_dict({"id": "shape_a", "journey_scope": {"exclude": ["flow_one"]}})

    assert variant.journey_scope is not None
    assert variant.journey_scope.applies_to("flow_one") is False
    assert variant.journey_scope.applies_to("flow_two") is True


def test_journey_scope_exclude_wins_over_include():
    variant = CoverageVariant.from_dict(
        {"id": "shape_a", "journey_scope": {"include": ["flow_*"], "exclude": ["flow_two"]}}
    )

    assert variant.journey_scope is not None
    assert variant.journey_scope.applies_to("flow_one") is True
    assert variant.journey_scope.applies_to("flow_two") is False


def test_journey_scope_round_trips_through_to_dict():
    variant = CoverageVariant.from_dict(
        {"id": "shape_a", "journey_scope": {"include": ["flow_*"], "exclude": ["flow_two"]}}
    )

    loaded = CoverageVariant.from_dict(variant.to_dict())

    assert loaded == variant


def test_axis_journey_scope_round_trips_through_to_dict():
    axis = CoverageAxis.from_dict(
        {
            "axis_type": "runtime_shape",
            "journey_scope": {"exclude": ["flow_two"]},
            "variants": [{"id": "shape_a"}],
        },
        default_source="lexicon",
    )

    loaded = CoverageAxis.from_dict(axis.to_dict())

    assert loaded == axis
    assert loaded.journey_scope is not None
    assert loaded.journey_scope.applies_to("flow_two") is False


def test_invalid_journey_scope_warns_and_keeps_variant():
    with pytest.warns(UserWarning, match="journey_scope ignored"):
        variant = CoverageVariant.from_dict({"id": "shape_a", "journey_scope": 42})

    assert variant.id == "shape_a"
    assert variant.journey_scope is None


def test_journey_scope_for_variant_overrides_axis_default():
    from codd.dag.coverage_axes import JourneyScope

    axis_scope = JourneyScope(include=["axis_flow"])
    variant_scope = JourneyScope(include=["variant_flow"])
    scoped_variant = CoverageVariant(id="a", label="A", journey_scope=variant_scope)
    plain_variant = CoverageVariant(id="b", label="B")
    axis = CoverageAxis(
        axis_type="runtime_shape",
        rationale="r",
        variants=[scoped_variant, plain_variant],
        source="lexicon",
        journey_scope=axis_scope,
    )

    assert axis.journey_scope_for(scoped_variant) is variant_scope
    assert axis.journey_scope_for(plain_variant) is axis_scope
    assert CoverageAxis(
        axis_type="x", rationale="r", variants=[plain_variant], source="lexicon"
    ).journey_scope_for(plain_variant) is None
