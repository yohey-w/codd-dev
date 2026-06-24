"""Tests for ``dag.lexicon_file`` honoring in one-to-many relation detection.

Regression for a dormant false-green: ``detect_one_to_many_relations`` only read
the root-level ``project_lexicon.yaml``/``lexicon.yaml`` and ignored the
configured ``dag.lexicon_file``. The builder treats ``lexicon_file`` as the
canonical lexicon, so a project that points it elsewhere had its one-to-many
relations silently missed — ``cardinality_coverage`` reported
``SKIP checked_count=0`` instead of surfacing the relation.

The detector now accepts ``settings`` and resolves ``lexicon_file`` with the same
root-jail semantics the builder uses (a path escaping the project root is never
read), including it *before* the legacy filenames. Both ``cardinality_coverage``
and ``ui_coherence`` callers thread their config through.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from codd.dag import DAG, Node
from codd.dag.checks._one_to_many_detection import detect_one_to_many_relations
from codd.dag.checks.cardinality_coverage import CardinalityCoverageCheck


# A many-to-one declaration the schema-light detector recognizes from a lexicon
# entry description ("many-to-one with order" => order -> line_item).
_MANY_TO_ONE_LEXICON = {
    "terms": {
        "line_item": {
            "description": "An order line. many-to-one with order.",
        },
    },
}


def _write_lexicon(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")


# --- Core fix: a custom dag.lexicon_file is read (was ignored => dormant) ---


def test_custom_lexicon_file_relations_detected(tmp_path: Path) -> None:
    # lexicon lives at a non-default path declared via settings["lexicon_file"].
    _write_lexicon(tmp_path / "docs" / "custom_lexicon.yaml", _MANY_TO_ONE_LEXICON)
    settings = {"lexicon_file": "docs/custom_lexicon.yaml"}

    relations = detect_one_to_many_relations(
        None, tmp_path, settings=settings
    )

    # Old behavior: [] (only root project_lexicon.yaml / lexicon.yaml were read).
    assert any(
        r["parent"] == "order" and r["child"] == "line_item" for r in relations
    ), relations


def test_custom_lexicon_file_makes_cardinality_coverage_fail(tmp_path: Path) -> None:
    # End-to-end: the custom lexicon supplies the 1:N relation; a design doc
    # declares policy=all with a member that no test asserts => red FAIL.
    # Old behavior: the relation was missed => SKIP, checked_count=0 (false-green).
    _write_lexicon(tmp_path / "docs" / "custom_lexicon.yaml", _MANY_TO_ONE_LEXICON)
    settings = {"lexicon_file": "docs/custom_lexicon.yaml"}

    dag = DAG()
    dag.add_node(
        Node(
            id="docs/design/orders.md",
            kind="design_doc",
            attributes={
                "aggregation_policies": [
                    {
                        "field_id": "line_items",
                        "cardinality": "1:N",
                        "cardinality_assertion": {
                            "policy": "all",
                            "member_signals": [
                                "line_item:A_visible",
                                "line_item:B_visible",
                            ],
                        },
                    }
                ],
            },
        )
    )
    dag.add_node(
        Node(
            id="tests/e2e/orders.test.ts",
            kind="test_file",
            attributes={"assertions": ["line_item:A_visible"]},
        )
    )

    result = CardinalityCoverageCheck(dag, tmp_path, settings).run()

    assert result.skipped is False
    assert result.status == "fail"
    assert result.severity == "red"
    assert result.block_deploy is True
    assert result.checked_count >= 1


# --- Regression: the legacy root path keeps working unchanged ---


def test_root_project_lexicon_still_detected(tmp_path: Path) -> None:
    # No lexicon_file configured; the root project_lexicon.yaml is still read.
    _write_lexicon(tmp_path / "project_lexicon.yaml", _MANY_TO_ONE_LEXICON)

    relations = detect_one_to_many_relations(None, tmp_path)

    assert any(
        r["parent"] == "order" and r["child"] == "line_item" for r in relations
    ), relations


def test_root_project_lexicon_detected_with_settings(tmp_path: Path) -> None:
    # lexicon_file points at the default name; the root file is still the source.
    _write_lexicon(tmp_path / "project_lexicon.yaml", _MANY_TO_ONE_LEXICON)
    settings = {"lexicon_file": "project_lexicon.yaml"}

    relations = detect_one_to_many_relations(None, tmp_path, settings=settings)

    assert any(
        r["parent"] == "order" and r["child"] == "line_item" for r in relations
    ), relations


# --- Root-jail: a lexicon_file escaping the project root is never read ---


def test_lexicon_file_outside_root_is_not_read(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    # A lexicon sitting OUTSIDE the project root, reachable via a traversal path.
    outside = tmp_path / "secret_lexicon.yaml"
    _write_lexicon(outside, _MANY_TO_ONE_LEXICON)
    settings = {"lexicon_file": "../secret_lexicon.yaml"}

    relations = detect_one_to_many_relations(
        None, project_root, settings=settings
    )

    # Path escapes the project root => not read => no relations leaked.
    assert relations == []


def test_absolute_lexicon_file_outside_root_is_not_read(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "secret_lexicon.yaml"
    _write_lexicon(outside, _MANY_TO_ONE_LEXICON)
    settings = {"lexicon_file": str(outside)}  # absolute, outside the jail

    relations = detect_one_to_many_relations(
        None, project_root, settings=settings
    )

    assert relations == []
