from __future__ import annotations

from pathlib import Path

import yaml

from codd.lexicon_cli.inspector import LexiconInspector


def _lexicon_root(tmp_path: Path) -> Path:
    root = tmp_path / "lexicons"
    sample = root / "sample"
    sample.mkdir(parents=True)
    (sample / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "lexicon_name": "sample",
                "description": "Sample lexicon",
                "observation_dimensions": 2,
                "prompt_extension": "elicit_extend.md",
                "recommended_kinds": "recommended_kinds.yaml",
                "lexicon": "lexicon.yaml",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (sample / "lexicon.yaml").write_text(
        yaml.safe_dump(
            {
                "coverage_axes": [
                    {
                        "axis_type": "runtime_shape",
                        "variants": [
                            {"id": "shape_a", "attributes": {"source_literal": "shape literal"}},
                            "shape_b",
                        ],
                    },
                    {"axis_type": "deployment_target", "variants": [{"id": "target_a"}]},
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (sample / "recommended_kinds.yaml").write_text(
        yaml.safe_dump({"recommended_kinds": ["sample_gap"]}),
        encoding="utf-8",
    )
    (sample / "elicit_extend.md").write_text("Prompt", encoding="utf-8")
    return root


def test_text_inspector_matches_axis_type_in_root_requirements(tmp_path: Path) -> None:
    root = _lexicon_root(tmp_path)
    (tmp_path / "requirements.md").write_text("runtime_shape must be covered", encoding="utf-8")

    result = LexiconInspector(tmp_path, root).inspect("sample")

    assert result.axes[0].status == "covered_text_match"
    assert result.axes[0].hit_count == 1


def test_text_inspector_matches_variant_id(tmp_path: Path) -> None:
    root = _lexicon_root(tmp_path)
    (tmp_path / "DESIGN.md").write_text("Design covers shape_a", encoding="utf-8")

    result = LexiconInspector(tmp_path, root).inspect("sample")

    assert result.axes[0].status == "covered_text_match"


def test_text_inspector_matches_source_literal(tmp_path: Path) -> None:
    root = _lexicon_root(tmp_path)
    (tmp_path / "docs" / "design").mkdir(parents=True)
    (tmp_path / "docs" / "design" / "spec.md").write_text("Uses shape literal", encoding="utf-8")

    result = LexiconInspector(tmp_path, root).inspect("sample")

    assert result.axes[0].hit_count == 1
    assert result.axes[0].hits[0].path == "docs/design/spec.md"


def test_text_inspector_marks_unknown_without_hits(tmp_path: Path) -> None:
    root = _lexicon_root(tmp_path)
    (tmp_path / "requirements.md").write_text("No matching words", encoding="utf-8")

    result = LexiconInspector(tmp_path, root).inspect("sample")

    assert [axis.status for axis in result.axes] == ["unknown", "unknown"]


def test_text_inspector_scans_hidden_codd_documents(tmp_path: Path) -> None:
    root = _lexicon_root(tmp_path)
    (tmp_path / ".codd").mkdir()
    (tmp_path / ".codd" / "design.md").write_text("deployment_target", encoding="utf-8")

    result = LexiconInspector(tmp_path, root).inspect("sample")

    assert result.axes[1].status == "covered_text_match"


def test_text_inspector_scans_project_lexicon(tmp_path: Path) -> None:
    root = _lexicon_root(tmp_path)
    (tmp_path / "project_lexicon.yaml").write_text(
        yaml.safe_dump({"design_principles": ["target_a"]}),
        encoding="utf-8",
    )

    result = LexiconInspector(tmp_path, root).inspect("sample")

    assert result.axes[1].status == "covered_text_match"


def test_diff_result_counts_covered_axes(tmp_path: Path) -> None:
    root = _lexicon_root(tmp_path)
    (tmp_path / "requirements.md").write_text("runtime_shape", encoding="utf-8")

    result = LexiconInspector(tmp_path, root).inspect("sample")

    assert result.covered_count == 1
    assert result.total_count == 2


def test_manifest_axes_fallback_when_lexicon_yaml_missing(tmp_path: Path) -> None:
    root = tmp_path / "lexicons"
    sample = root / "sample"
    sample.mkdir(parents=True)
    (sample / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "lexicon_name": "sample",
                "coverage_axes": [{"axis_type": "manifest_axis", "variants": []}],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "requirements.md").write_text("manifest_axis", encoding="utf-8")

    result = LexiconInspector(tmp_path, root).inspect("sample")

    assert result.total_count == 1
    assert result.axes[0].status == "covered_text_match"


def test_hits_are_limited_but_count_all_matches(tmp_path: Path) -> None:
    root = _lexicon_root(tmp_path)
    (tmp_path / "requirements.md").write_text("\n".join(["runtime_shape"] * 12), encoding="utf-8")

    result = LexiconInspector(tmp_path, root).inspect("sample")

    assert result.axes[0].hit_count == 12
    assert len(result.axes[0].hits) == 10
