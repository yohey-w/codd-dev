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
