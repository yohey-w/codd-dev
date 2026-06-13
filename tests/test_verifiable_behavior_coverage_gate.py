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


# ---------------------------------------------------------------------------
# Single-canonical-doc fix: coherent project reaches 100%; real omissions RED;
# the existing bounded retry still drives real coverage (no second gate loop).
# ---------------------------------------------------------------------------


def _canonical_strategy(project: Path, rows: str) -> None:
    """Write the canonical VB declaration doc (docs/test/test_strategy.md)."""
    _write(project / "docs" / "test" / "test_strategy.md", "| VB | D |\n| --- | --- |\n" + rows)


def test_coherent_canonical_project_reaches_full_coverage(tmp_path):
    """ANTI_FALSE_RED: one canonical doc + matching markers → uncovered=0, OK."""
    project = tmp_path
    _canonical_strategy(
        project,
        "| VB-01 | add creates a task |\n| VB-02 | done exits nonzero |\n| VB-39 | no browser/HTTP |\n",
    )
    # A reference-only acceptance doc maps AC ids to canonical VBs (no first-col VB).
    _write(
        project / "docs" / "test" / "acceptance_criteria.md",
        "| AC ID | Criterion | Canonical VBs |\n| --- | --- | --- |\n| AC-07 | errors | VB-02 |\n",
    )
    _write(
        project / "tests" / "test_app.py",
        "# codd: covers vb=VB-01\n"
        "# codd: covers vb=VB-02\n"
        "# codd: covers vb=VB-39\n"
        "def test_app():\n    assert True\n",
    )
    config = {"scan": {"test_dirs": ["tests/"]}}
    report = build_vb_coverage_audit(project, config=config)
    assert report.summary["vb_count"] == 3
    assert report.summary["uncovered"] == 0
    assert report.summary["covered"] == 3

    errors: list[str] = []
    passed = run_implement_coverage_gate(
        project,
        config=config,
        design_node="test:test-strategy",
        output_paths=["tests/"],
        echo=lambda _m: None,
        echo_error=errors.append,
    )
    assert passed is True
    assert errors == []


def test_genuine_omission_still_reds_with_no_sibling_autocover(tmp_path):
    """ANTI_FALSE_GREEN: drop one marker → that exact VB REDs; no sibling cover."""
    project = tmp_path
    # VB-14 ("no GUI") and VB-39 ("no Selenium") are semantic siblings — proving
    # one must NOT auto-cover the other.
    _canonical_strategy(
        project,
        "| VB-14 | CLI has no GUI/browser/web |\n| VB-39 | E2E imports no Selenium/HTTP |\n",
    )
    # Only VB-14 is covered; VB-39 has NO marker.
    _write(
        project / "tests" / "test_noweb.py",
        "# codd: covers vb=VB-14\ndef test_noweb():\n    assert True\n",
    )
    config = {"scan": {"test_dirs": ["tests/"]}}
    report = build_vb_coverage_audit(project, config=config)
    statuses = {row.vb_id: row.coverage_status for row in report.rows}
    assert statuses["VB-14"] == "covered"
    assert statuses["VB-39"] == "uncovered"  # sibling did NOT cover it

    errors: list[str] = []
    passed = run_implement_coverage_gate(
        project,
        config=config,
        design_node="test:test-strategy",
        output_paths=["tests/"],
        rerun=None,  # no retry callback → fail immediately on the real gap
        echo=lambda _m: None,
        echo_error=errors.append,
    )
    assert passed is False
    assert any("VB-39" in message for message in errors)


def test_blocked_is_distinct_from_covered(tmp_path):
    """ANTI_FALSE_GREEN: an explicit blocked marker is reported, not counted covered."""
    project = tmp_path
    _canonical_strategy(project, "| VB-01 | a |\n| VB-02 | b |\n")
    _write(
        project / "tests" / "t.py",
        "# codd: covers vb=VB-01\n"
        "# codd: blocked vb=VB-02 reason=needs-hardware\n"
        "def t():\n    assert True\n",
    )
    report = build_vb_coverage_audit(project, config={"scan": {"test_dirs": ["tests/"]}})
    assert report.summary["covered"] == 1
    assert report.summary["blocked"] == 1
    assert report.summary["uncovered"] == 0
    statuses = {row.vb_id: row.coverage_status for row in report.rows}
    assert statuses == {"VB-01": "covered", "VB-02": "blocked"}


def test_existing_bounded_retry_still_drives_real_coverage(tmp_path):
    """The kept implement-stage retry feeds gap feedback then PASSes once fixed.

    Guards that the fix did NOT add a second gate-side repair loop and did NOT
    break the existing ``test_coverage.max_retries`` path (a real omission is
    repaired by adding a genuine covering marker, not duplicate markers).
    """
    project = tmp_path
    _canonical_strategy(project, "| VB-01 | a |\n| VB-31 | missing id keeps list unchanged |\n")
    # Start with only VB-01 covered (VB-31 is a genuine omission, like the dogfood case).
    _write(
        project / "tests" / "t.py",
        "# codd: covers vb=VB-01\ndef t():\n    assert True\n",
    )
    config = {"scan": {"test_dirs": ["tests/"]}, "test_coverage": {"max_retries": 2}}

    rerun_feedback: list[str] = []

    def rerun(feedback: str) -> None:
        rerun_feedback.append(feedback)
        # The "implementer" adds the genuine missing marker on the first retry.
        _write(
            project / "tests" / "t2.py",
            "# codd: covers vb=VB-31\ndef t2():\n    assert True\n",
        )

    errors: list[str] = []
    passed = run_implement_coverage_gate(
        project,
        config=config,
        design_node="test:test-strategy",
        output_paths=["tests/"],
        rerun=rerun,
        echo=lambda _m: None,
        echo_error=errors.append,
    )
    assert passed is True
    assert len(rerun_feedback) == 1  # bounded: one retry sufficed
    assert "VB-31" in rerun_feedback[0]  # gap feedback named the real omission
    assert errors == []


def test_bounded_retry_exhausts_and_fails_on_persistent_omission(tmp_path):
    """The retry is bounded by max_retries and still REDs if the gap remains."""
    project = tmp_path
    _canonical_strategy(project, "| VB-01 | a |\n| VB-31 | persistent omission |\n")
    _write(project / "tests" / "t.py", "# codd: covers vb=VB-01\ndef t():\n    assert True\n")
    config = {"scan": {"test_dirs": ["tests/"]}, "test_coverage": {"max_retries": 2}}

    attempts: list[str] = []

    def rerun(feedback: str) -> None:
        attempts.append(feedback)  # never actually adds the marker

    errors: list[str] = []
    passed = run_implement_coverage_gate(
        project,
        config=config,
        design_node="test:test-strategy",
        output_paths=["tests/"],
        rerun=rerun,
        echo=lambda _m: None,
        echo_error=errors.append,
    )
    assert passed is False
    assert len(attempts) == 2  # bounded by max_retries
    assert any("VB-31" in message for message in errors)
