"""Tests for ``dag.lexicon_file`` honoring in one-to-many relation detection.

Regression for a dormant false-green: ``detect_one_to_many_relations`` only read
the root-level ``project_lexicon.yaml``/``lexicon.yaml`` and ignored the
configured ``dag.lexicon_file``. The builder treats ``lexicon_file`` as the
canonical lexicon, so a project that points it elsewhere had its one-to-many
relations silently missed — ``cardinality_coverage`` reported
``SKIP checked_count=0`` instead of surfacing the relation.

The detector now accepts ``settings`` and resolves ``lexicon_file`` through the
shared :func:`codd.path_safety.require_project_path` jail, reading it *before* the
legacy filenames. Both ``cardinality_coverage`` and ``ui_coherence`` callers
thread their config through.

Path safety is **fail-closed** for a *configured* ``lexicon_file``: a non-empty
value that escapes the project root (``../`` traversal, out-of-root absolute, or
an in-root symlink whose target leaves the tree) raises
:class:`codd.path_safety.PathEscapeError` instead of being silently skipped (the
old silent-skip was a false-green — the check "passed" on a lexicon it never
read). The callers catch the error and emit an honest red/error finding. An
*empty / unset* ``lexicon_file`` and the legacy default filenames merely being
absent remain legitimate skips (never fail-closed) — the anti-false-red guard.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from codd.dag import DAG, Node
from codd.dag.checks._one_to_many_detection import detect_one_to_many_relations
from codd.dag.checks.cardinality_coverage import CardinalityCoverageCheck
from codd.dag.checks.ui_coherence import UiCoherenceCheck
from codd.path_safety import PathEscapeError


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


# --- Fail-closed: a configured lexicon_file escaping the root RAISES (was silent) ---
#
# RED-before-GREEN: previously these asserted ``relations == []`` (silent-skip =>
# the check went dormant and PASSED on configured metadata it never read = a
# false-green). The fix makes a configured out-of-root lexicon_file fail-closed
# via PathEscapeError so the caller can surface an honest red.


def test_traversal_lexicon_file_outside_root_raises(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    # A lexicon sitting OUTSIDE the project root, reachable via a traversal path.
    outside = tmp_path / "secret_lexicon.yaml"
    _write_lexicon(outside, _MANY_TO_ONE_LEXICON)
    settings = {"lexicon_file": "../secret_lexicon.yaml"}

    # Old behavior: returned [] (silent-skip). Now: fail-closed.
    with pytest.raises(PathEscapeError):
        detect_one_to_many_relations(None, project_root, settings=settings)


def test_absolute_lexicon_file_outside_root_raises(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "secret_lexicon.yaml"
    _write_lexicon(outside, _MANY_TO_ONE_LEXICON)
    settings = {"lexicon_file": str(outside)}  # absolute, outside the jail

    with pytest.raises(PathEscapeError):
        detect_one_to_many_relations(None, project_root, settings=settings)


def test_symlink_lexicon_file_escaping_root_raises(tmp_path: Path) -> None:
    # An in-root lexicon_file name that is a symlink whose target escapes the
    # project tree: resolve-and-confine must follow the link and fail-closed.
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "secret_lexicon.yaml"
    _write_lexicon(outside, _MANY_TO_ONE_LEXICON)
    link = project_root / "lexicon_link.yaml"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    settings = {"lexicon_file": "lexicon_link.yaml"}

    with pytest.raises(PathEscapeError):
        detect_one_to_many_relations(None, project_root, settings=settings)


# --- Caller-level: escape => honest red/error finding (not crash, not silent-pass) ---


def _design_doc_node() -> Node:
    # A policy=all design doc so that, absent the escape, the check would have a
    # real shape to reason about (proves the red comes from the escape itself).
    return Node(
        id="docs/design/orders.md",
        kind="design_doc",
        attributes={
            "aggregation_policies": [
                {
                    "field_id": "line_items",
                    "cardinality": "1:N",
                    "cardinality_assertion": {
                        "policy": "all",
                        "member_signals": ["line_item:A_visible"],
                    },
                }
            ],
        },
    )


def test_cardinality_coverage_escaped_lexicon_is_red(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    _write_lexicon(tmp_path / "secret_lexicon.yaml", _MANY_TO_ONE_LEXICON)
    settings = {"lexicon_file": "../secret_lexicon.yaml"}

    dag = DAG()
    dag.add_node(_design_doc_node())

    result = CardinalityCoverageCheck(dag, project_root, settings).run()

    # Old behavior: relation silently missed => SKIP, checked_count=0 (false-green).
    assert result.skipped is False
    assert result.status == "fail"
    assert result.severity == "red"
    assert result.block_deploy is True
    assert any(
        w.get("type") == "cardinality_lexicon_file_out_of_root" for w in result.warnings
    ), result.warnings


def test_ui_coherence_escaped_lexicon_is_red(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    _write_lexicon(tmp_path / "secret_lexicon.yaml", _MANY_TO_ONE_LEXICON)
    settings = {"lexicon_file": "../secret_lexicon.yaml"}

    dag = DAG()
    dag.add_node(_design_doc_node())

    result = UiCoherenceCheck(dag, project_root, settings).run()

    assert result.skipped is False
    assert result.status == "fail"
    assert result.severity == "red"
    assert result.block_deploy is True
    assert any(
        v.get("type") == "ui_coherence_lexicon_file_out_of_root" for v in result.violations
    ), result.violations


# --- Anti-false-red: in-root config / unset / legacy absence stay unchanged ---


def test_unset_lexicon_file_does_not_raise(tmp_path: Path) -> None:
    # No lexicon_file at all (and no legacy file present): legitimate skip, never
    # fail-closed. Must NOT raise and must return [].
    relations = detect_one_to_many_relations(None, tmp_path, settings={})
    assert relations == []
    relations_none = detect_one_to_many_relations(None, tmp_path, settings=None)
    assert relations_none == []


def test_empty_string_lexicon_file_does_not_raise(tmp_path: Path) -> None:
    # An explicitly empty lexicon_file is treated as unset (no-config skip).
    relations = detect_one_to_many_relations(None, tmp_path, settings={"lexicon_file": "  "})
    assert relations == []


def test_legacy_filename_absence_is_not_fail_closed(tmp_path: Path) -> None:
    # lexicon_file unset and the legacy default filenames simply don't exist:
    # legitimate absence, NOT an escape. Detector returns [] without raising,
    # and cardinality_coverage SKIPs (not red).
    dag = DAG()
    dag.add_node(_design_doc_node())
    result = CardinalityCoverageCheck(dag, tmp_path, {}).run()
    assert result.skipped is True
    assert result.status == "skip"
    assert result.severity != "red"


def test_in_root_configured_lexicon_unaffected(tmp_path: Path) -> None:
    # A normal in-root configured lexicon_file is still read (regression guard:
    # the fail-closed change must not break the happy path).
    _write_lexicon(tmp_path / "docs" / "custom_lexicon.yaml", _MANY_TO_ONE_LEXICON)
    settings = {"lexicon_file": "docs/custom_lexicon.yaml"}
    relations = detect_one_to_many_relations(None, tmp_path, settings=settings)
    assert any(
        r["parent"] == "order" and r["child"] == "line_item" for r in relations
    ), relations


# --- Legacy-default symlink escape: a per-file symlink at the LEGACY default
# filename whose target leaves the tree must be dropped, not read as evidence.
#
# RED-before-GREEN: ``_relations_from_project_lexicon`` appends the legacy
# ``project_lexicon.yaml`` / ``lexicon.yaml`` filenames and read them via
# ``is_file()`` / ``read_text()`` after ``Path.resolve()`` followed the symlink
# off-root — so an in-root ``lexicon.yaml`` symlink pointing at an off-root file
# leaked its one-to-many relations into the gate (path-escape false-green). The
# configured ``lexicon_file`` was already jailed (require_project_path); only the
# legacy default fell through. The fix re-confines every candidate before reading
# while keeping legacy *absence* a legitimate skip (not fail-closed).


def test_legacy_default_symlink_escaping_root_is_dropped(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    # An off-root lexicon with a one-to-many relation, reachable only via an
    # in-root symlink at the LEGACY default filename (no lexicon_file configured).
    outside = tmp_path / "secret_lexicon.yaml"
    _write_lexicon(outside, _MANY_TO_ONE_LEXICON)
    link = project_root / "lexicon.yaml"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")

    # No settings => legacy defaults are the only source. Old behavior: the
    # symlink resolved off-root and its relation was read (false-green). Now: the
    # escaping candidate is dropped, so no relation is reported. Legacy default is
    # an absence-style candidate, so this is a silent drop, NOT a raise.
    relations = detect_one_to_many_relations(None, project_root, settings={})
    assert relations == [], relations


def test_legacy_default_in_root_symlink_still_read(tmp_path: Path) -> None:
    # Anti-false-red: an in-root ``lexicon.yaml`` symlink whose target ALSO stays
    # inside the project root keeps being read (in-root -> in-root is valid).
    project_root = tmp_path / "project"
    project_root.mkdir()
    real = project_root / "docs" / "real_lexicon.yaml"
    _write_lexicon(real, _MANY_TO_ONE_LEXICON)
    link = project_root / "lexicon.yaml"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")

    relations = detect_one_to_many_relations(None, project_root, settings={})
    assert any(
        r["parent"] == "order" and r["child"] == "line_item" for r in relations
    ), relations
