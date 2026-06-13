"""Regression tests for the verifiable-behavior (VB) coverage gate.

These encode a live greenfield dogfood failure (the "kakeibo" run): generated
tests carried lowercase, zero-padded, prefix-less markers (``vb=08``) and lived
under ``src/tests/`` while the design docs declared ``VB-08`` and the project
configured ``scan.test_dirs: [tests/]``. The gate reported 45 of 45 behaviors
uncovered (a false-RED) for three independent reasons, each guarded here:

* H-A — id-format mismatch: ``VB-08`` was compared literally to marker ``08``.
* H-B — scan scope: ``src/tests/`` was never scanned (only ``tests/`` was).
* multi-token lines: ``# codd: covers vb=22 vb=23 vb=24`` recovered only ``22``.
"""

from __future__ import annotations

from pathlib import Path

from codd.verifiable_behavior_audit import (
    build_vb_coverage_audit,
    run_implement_coverage_gate,
    _normalize_vb_id,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Canonical id normalization (H-A)
# ---------------------------------------------------------------------------


def test_normalize_zero_padded_and_prefixless_ids_converge():
    # Declared `VB-08` and any marker spelling of the same number map to one key.
    assert _normalize_vb_id("VB-08") == _normalize_vb_id("08") == "8"
    assert _normalize_vb_id("vb=8") == "8"  # value as it might be spelled raw
    assert _normalize_vb_id("VB-8") == "8"
    assert _normalize_vb_id("VB-08") == _normalize_vb_id("8")


def test_normalize_does_not_collide_distinct_numbers():
    # The critical non-collision: VB-1 must NOT equal VB-11 after normalization.
    assert _normalize_vb_id("VB-1") == "1"
    assert _normalize_vb_id("VB-11") == "11"
    assert _normalize_vb_id("VB-1") != _normalize_vb_id("VB-11")
    assert _normalize_vb_id("VB-01") != _normalize_vb_id("VB-10")


def test_normalize_preserves_non_numeric_schemes():
    # Non-numeric ids (e.g. VB-AUTH-1, VB-add) keep their alphanumeric remainder.
    assert _normalize_vb_id("VB-AUTH-1") == _normalize_vb_id("AUTH-1") == "auth-1"
    assert _normalize_vb_id("VB-add") == _normalize_vb_id("add") == "add"
    assert _normalize_vb_id("VB-AUTH-1") != _normalize_vb_id("VB-AUTH-2")


def test_normalize_leading_vb_is_token_scoped_not_substring():
    # A body that itself begins with `vb` must normalize symmetrically: the
    # declared id and a bare marker value reconcile (no asymmetric over-strip).
    assert _normalize_vb_id("VB-vbx") == _normalize_vb_id("vbx") == "vbx"


# ---------------------------------------------------------------------------
# End-to-end gate: the kakeibo case (H-A + H-B + multi-token together)
# ---------------------------------------------------------------------------


def test_gate_passes_for_kakeibo_style_markers(tmp_path):
    project = tmp_path
    _write(
        project / "docs" / "test" / "acceptance_criteria.md",
        "| ID | Description |\n"
        "| --- | --- |\n"
        "| VB-08 | date prefilled with today |\n"
        "| VB-22 | delete removes record |\n"
        "| VB-23 | delete returns 302 |\n"
        "| VB-24 | delete redirect Location is / |\n",
    )
    # Tests live under src/tests/ (NOT the configured tests/), with prefix-less,
    # zero-padded, multi-token markers — exactly the dogfood shape.
    _write(
        project / "src" / "tests" / "test_delete.py",
        "# codd: covers vb=22 vb=23 vb=24\n"
        "def test_delete():\n    assert True\n",
    )
    _write(
        project / "src" / "tests" / "test_register_html.py",
        "# codd: covers vb=08\n"
        "def test_date_prefill():\n    assert True\n",
    )
    config = {"scan": {"test_dirs": ["tests/"], "source_dirs": ["src/"]}}

    report = build_vb_coverage_audit(project, config=config)
    assert report.summary["vb_count"] == 4
    assert report.summary["uncovered"] == 0
    assert report.summary["covered"] == 4
    assert report.summary["orphan_vb_markers"] == 0

    errors: list[str] = []
    passed = run_implement_coverage_gate(
        project,
        config=config,
        design_node=None,
        output_paths=["tests/"],
        echo=lambda _m: None,
        echo_error=errors.append,
    )
    assert passed is True
    assert errors == []


def test_multiple_vb_tokens_on_one_line_all_recovered(tmp_path):
    project = tmp_path
    _write(
        project / "docs" / "test" / "behaviors.md",
        "| ID | D |\n| --- | --- |\n| VB-22 | a |\n| VB-23 | b |\n| VB-24 | c |\n",
    )
    _write(
        project / "tests" / "test_x.py",
        "# codd: covers vb=22 vb=23 vb=24\ndef test_x():\n    assert True\n",
    )
    report = build_vb_coverage_audit(project, config={"scan": {"test_dirs": ["tests/"]}})
    statuses = {row.vb_id: row.coverage_status for row in report.rows}
    assert statuses == {"VB-22": "covered", "VB-23": "covered", "VB-24": "covered"}


def test_gate_scans_source_tree_when_tests_live_under_src(tmp_path):
    # H-B in isolation: tests only under src/tests/, config points at tests/.
    project = tmp_path
    _write(
        project / "docs" / "test" / "b.md",
        "| ID | D |\n| --- | --- |\n| VB-01 | x |\n",
    )
    _write(
        project / "src" / "tests" / "test_a.py",
        "# codd: covers vb=01\ndef test_a():\n    assert True\n",
    )
    config = {"scan": {"test_dirs": ["tests/"], "source_dirs": ["src/"]}}
    report = build_vb_coverage_audit(project, config=config)
    assert report.summary["covered"] == 1
    assert report.summary["uncovered"] == 0


def test_gate_falls_back_to_project_root_without_scan_config(tmp_path):
    project = tmp_path
    _write(
        project / "docs" / "test" / "b.md",
        "| ID | D |\n| --- | --- |\n| VB-01 | x |\n",
    )
    _write(
        project / "src" / "tests" / "test_a.py",
        "# codd: covers vb=01\ndef test_a():\n    assert True\n",
    )
    report = build_vb_coverage_audit(project, config={})
    assert report.summary["covered"] == 1


# ---------------------------------------------------------------------------
# Preserved behavior: blocked opt-out and gate disable
# ---------------------------------------------------------------------------


def test_blocked_marker_normalizes_and_is_not_an_orphan(tmp_path):
    project = tmp_path
    _write(
        project / "docs" / "test" / "b.md",
        "| ID | D |\n| --- | --- |\n| VB-09 | x |\n",
    )
    _write(
        project / "tests" / "t.py",
        "# codd: blocked vb=09 reason=pending\ndef t():\n    assert True\n",
    )
    report = build_vb_coverage_audit(project, config={"scan": {"test_dirs": ["tests/"]}})
    assert report.summary["blocked"] == 1
    assert report.summary["uncovered"] == 0
    assert report.summary["orphan_vb_markers"] == 0
    assert report.rows[0].coverage_status == "blocked"
    assert report.rows[0].blocker_reason == "pending"


def test_opt_out_skips_gate_entirely(tmp_path):
    project = tmp_path
    _write(
        project / "docs" / "test" / "b.md",
        "| ID | D |\n| --- | --- |\n| VB-01 | x |\n",
    )
    # No covering marker anywhere — the gate would normally FAIL.
    _write(project / "tests" / "t.py", "def t():\n    assert True\n")
    errors: list[str] = []
    passed = run_implement_coverage_gate(
        project,
        config={"scan": {"test_dirs": ["tests/"]}},
        design_node=None,
        output_paths=["tests/"],
        opt_out=True,
        echo=lambda _m: None,
        echo_error=errors.append,
    )
    assert passed is True
    assert errors == []
