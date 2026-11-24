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

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from codd.cli import main
from codd.verifiable_behavior_audit import (
    build_vb_coverage_audit,
    format_gap_feedback,
    run_implement_coverage_gate,
    scope_uncovered_rows,
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


@pytest.fixture
def conflicting_vb_project(tmp_path):
    _canonical_strategy(
        tmp_path,
        "| VB-01 | covered only |\n| VB-02 | blocked only |\n| VB-03 | conflicting markers |\n",
    )
    _write(tmp_path / "tests/test_covers.py", "# codd: covers vb=VB-01 vb=03\n")
    _write(tmp_path / "tests/test_external.py", "# codd: blocked vb=VB-02 reason=external_dependency\n")
    _write(tmp_path / "tests/test_blocked.py", "# codd: blocked vb=VB-3 reason=stale\n")
    return tmp_path


def test_audit_reports_marker_conflict_in_rows_and_files(conflicting_vb_project):
    project = conflicting_vb_project
    with (project / "docs/test/test_strategy.md").open("a", encoding="utf-8") as doc:
        doc.write("| VB-04 | no marker |\n")

    report = build_vb_coverage_audit(project)
    assert [row.coverage_status for row in report.rows] == ["covered", "blocked", "blocked", "uncovered"]
    assert [report.summary[key] for key in ("covered", "blocked", "uncovered")] == [1, 2, 1]
    assert report.summary["marker_conflicts"] == 1
    assert [row.marker_conflict for row in report.rows] == [False, False, True, False]
    conflict = report.rows[2]
    assert conflict.matched_tests == ["tests/test_covers.py"]
    assert conflict.blocker_evidence == ["tests/test_blocked.py"]
    assert conflict.suggested_next_action == (
        "Remove stale blocked markers if the covering tests prove this behavior. "
        "Otherwise, correct the covers markers to match what the tests prove."
    )

    for output_format in ("md", "json"):
        result = CliRunner().invoke(main, ["test", "audit", "--path", str(project), "--format", output_format])
        assert result.exit_code == 0, result.output
        assert not result.stderr_bytes
        assert result.stdout_bytes == (
            "Verifiable behavior audit: 4 behavior(s), 1 covered, 2 blocked, 1 uncovered, "
            f"0 orphan vb marker(s): docs/test/vb-coverage-audit.{output_format}\n"
            "1 covers/blocked marker conflict(s) included in blocked.\n"
        ).encode("utf-8")

    markdown = (project / "docs/test/vb-coverage-audit.md").read_text(encoding="utf-8")
    assert "Covers/blocked marker conflicts (included in blocked): 1" in markdown
    assert "| VB | Description | Source Doc | Status | Evidence | Next Action | Marker Conflict |" in markdown
    row_text = next(line for line in markdown.splitlines() if line.startswith("| VB-03 |"))
    assert row_text == (
        "| VB-03 | conflicting markers | docs/test/test_strategy.md | blocked | "
        "tests/test_covers.py; blocker: stale: tests/test_blocked.py | "
        f"{conflict.suggested_next_action} | covers: tests/test_covers.py; blocked: tests/test_blocked.py |"
    )
    # Rows without a conflict keep the extra column empty in the same table.
    other_rows = [
        line for line in markdown.splitlines()
        if line.startswith("| VB-") and not line.startswith("| VB-03 |")
    ]
    assert len(other_rows) == 3
    assert all(line.endswith(" | - |") for line in other_rows)

    payload = json.loads((project / "docs/test/vb-coverage-audit.json").read_text(encoding="utf-8"))
    assert payload["summary"] == {
        "vb_count": 4,
        "covered": 1,
        "blocked": 2,
        "uncovered": 1,
        "orphan_vb_markers": 0,
        "marker_conflicts": 1,
    }
    conflict_payload = payload["rows"][2]
    assert conflict_payload["marker_conflict"] is True
    assert conflict_payload == {
        "vb_id": "VB-03",
        "description": "conflicting markers",
        "source_doc": "docs/test/test_strategy.md",
        "coverage_status": "blocked",
        "matched_tests": ["tests/test_covers.py"],
        "blocker_reason": "stale",
        "blocker_details": "",
        "blocker_evidence": ["tests/test_blocked.py"],
        "suggested_next_action": conflict.suggested_next_action,
        "declared_scenarios": "",
        "marker_conflict": True,
        "marker_conflict_files": {"covers": ["tests/test_covers.py"], "blocked": ["tests/test_blocked.py"]},
    }
    for row in (payload["rows"][0], payload["rows"][1], payload["rows"][3]):
        assert row["marker_conflict"] is False
        assert "marker_conflict_files" not in row


def test_audit_without_marker_conflicts_preserves_legacy_output(tmp_path):
    _canonical_strategy(tmp_path, "| VB-01 | covered only |\n| VB-02 | blocked only |\n| VB-03 | no marker |\n")
    _write(tmp_path / "tests/test_covers.py", "# codd: covers vb=VB-01\n")
    _write(tmp_path / "tests/test_external.py", "# codd: blocked vb=VB-02 reason=external_dependency\n")
    _write(tmp_path / "tests/test_orphan.py", "# codd: covers vb=VB-99\n")

    # Capture from the same fixture under 126f427. The expectations are static so
    # the regression still runs in a source installation without a Git checkout.
    for output_format in ("md", "json"):
        result = CliRunner().invoke(main, ["test", "audit", "--path", str(tmp_path), "--format", output_format])
        assert result.exit_code == 0, result.output
        assert not result.stderr_bytes
        assert result.stdout_bytes == (
            "Verifiable behavior audit: 3 behavior(s), 1 covered, 1 blocked, 1 uncovered, "
            f"1 orphan vb marker(s): docs/test/vb-coverage-audit.{output_format}\n"
        ).encode("utf-8")

    assert (tmp_path / "docs/test/vb-coverage-audit.md").read_bytes() == (
        b"# Verifiable Behavior Coverage Audit\n\n"
        b"- Contract: verifiable-behavior-audit/v1\n"
        b"- Verifiable behaviors: 3\n"
        b"- Covered by `codd: covers vb=` marker: 1\n"
        b"- Blocked by explicit blocker marker: 1\n"
        b"- Uncovered: 1\n"
        b"- Orphan vb markers: 1\n\n"
        b"## Behavior Matrix\n"
        b"| VB | Description | Source Doc | Status | Evidence | Next Action |\n"
        b"| --- | --- | --- | --- | --- | --- |\n"
        b"| VB-01 | covered only | docs/test/test_strategy.md | covered | tests/test_covers.py | "
        b"Run the covering test(s) and keep the marker in sync on refactor. |\n"
        b"| VB-02 | blocked only | docs/test/test_strategy.md | blocked | "
        b"blocker: external_dependency: tests/test_external.py | "
        b"Resolve the explicit blocker, then replace the blocker marker with a covers marker. |\n"
        b"| VB-03 | no marker | docs/test/test_strategy.md | uncovered | - | "
        b"Create or extend a test with a `codd: covers vb=` marker. |\n\n"
        b"## Orphan VB Markers\n"
        b"These markers reference a VB id that no test document declares; declare the behavior or fix the marker.\n\n"
        b"| Test | VB | Marker |\n"
        b"| --- | --- | --- |\n"
        b"| tests/test_orphan.py | VB-99 | covers |\n"
    )

    payload = (tmp_path / "docs/test/vb-coverage-audit.json").read_bytes()
    summary_field = b',\n    "marker_conflicts": 0\n'
    row_field = b',\n      "marker_conflict": false\n'
    assert payload.count(summary_field) == 1
    assert payload.count(row_field) == 3
    legacy_payload = payload.replace(summary_field, b"\n").replace(row_field, b"\n")
    expected_payload = {
        "version": "verifiable-behavior-audit/v1",
        "summary": {"vb_count": 3, "covered": 1, "blocked": 1, "uncovered": 1, "orphan_vb_markers": 1},
        "rows": [
            {
                "vb_id": "VB-01",
                "description": "covered only",
                "source_doc": "docs/test/test_strategy.md",
                "coverage_status": "covered",
                "matched_tests": ["tests/test_covers.py"],
                "blocker_reason": "",
                "blocker_details": "",
                "blocker_evidence": [],
                "suggested_next_action": "Run the covering test(s) and keep the marker in sync on refactor.",
                "declared_scenarios": "",
            },
            {
                "vb_id": "VB-02",
                "description": "blocked only",
                "source_doc": "docs/test/test_strategy.md",
                "coverage_status": "blocked",
                "matched_tests": [],
                "blocker_reason": "external_dependency",
                "blocker_details": "",
                "blocker_evidence": ["tests/test_external.py"],
                "suggested_next_action": (
                    "Resolve the explicit blocker, then replace the blocker marker with a covers marker."
                ),
                "declared_scenarios": "",
            },
            {
                "vb_id": "VB-03",
                "description": "no marker",
                "source_doc": "docs/test/test_strategy.md",
                "coverage_status": "uncovered",
                "matched_tests": [],
                "blocker_reason": "",
                "blocker_details": "",
                "blocker_evidence": [],
                "suggested_next_action": "Create or extend a test with a `codd: covers vb=` marker.",
                "declared_scenarios": "",
            },
        ],
        "orphan_vb_markers": [{"path": "tests/test_orphan.py", "vb_id": "VB-99", "marker_kind": "covers"}],
    }
    assert legacy_payload == (json.dumps(expected_payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


@pytest.mark.parametrize("uncovered", [False, True])
def test_gate_reports_marker_conflict_without_changing_verdict(conflicting_vb_project, uncovered):
    project = conflicting_vb_project
    if uncovered:
        with (project / "docs/test/test_strategy.md").open("a", encoding="utf-8") as doc:
            doc.write("| VB-04 | no marker |\n")
    _write(project / "tests/test_orphan.py", "# codd: covers vb=VB-99\n")
    output, errors, retries = [], [], []

    passed = run_implement_coverage_gate(
        project,
        design_node=None,
        output_paths=["tests/"],
        config={"test_coverage": {"max_retries": 1}},
        rerun=retries.append,
        echo=output.append,
        echo_error=errors.append,
    )

    assert passed is (not uncovered)
    assert len(retries) == int(uncovered)
    assert any("[orphan_vb_marker]" in error and "VB-99" in error for error in errors)
    assert any("coverage gate FAILED" in error for error in errors) is uncovered
    assert any("1 covers/blocked marker conflict(s)" in line for line in output)
    detail = next(line for line in output if "VB-03" in line)
    assert "covers: tests/test_covers.py" in detail
    assert "blocked: tests/test_blocked.py" in detail
    assert "Remove stale blocked markers" in detail and "correct the covers markers" in detail


def test_gate_failure_and_gap_feedback_limit_blocked_marker_scope(tmp_path):
    _canonical_strategy(tmp_path, "| VB-01 | no marker |\n")
    errors = []
    assert not run_implement_coverage_gate(
        tmp_path, design_node=None, output_paths=["tests/"], echo_error=errors.append
    )
    gate_guidance = next(error for error in errors if "reason=..." in error)
    gap_feedback = format_gap_feedback(build_vb_coverage_audit(tmp_path).uncovered_rows)

    for guidance in (gate_guidance, gap_feedback):
        assert "this task's own outputs are responsible for the behavior" in guidance
        assert "Do not declare behaviors that belong to other modules or other tests blocked." in guidance


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


# ---------------------------------------------------------------------------
# .e2e.ts recognition — a genuine e2e naming convention codex emits unprompted.
# Before the fix the suffix filter skipped these files, so their `covers vb=`
# markers were NEVER scanned and the declared VBs read as uncovered (false-RED).
# ---------------------------------------------------------------------------


def test_is_test_file_recognizes_e2e_ts_variants():
    from codd.operational_e2e_audit import _is_test_file

    assert _is_test_file(Path("tests/e2e/foo.e2e.ts")) is True
    assert _is_test_file(Path("tests/e2e/foo.e2e.tsx")) is True
    assert _is_test_file(Path("tests/e2e/foo.e2e.js")) is True
    assert _is_test_file(Path("tests/e2e/foo.e2e-spec.ts")) is True
    # A non-test source module must NOT be treated as a test file.
    assert _is_test_file(Path("src/foo.ts")) is False


def test_vb_marker_in_e2e_ts_file_counts_as_covered(tmp_path):
    """A `.e2e.ts` file with `// codd: covers vb=` covers the VB (was missed)."""
    project = tmp_path
    _canonical_strategy(project, "| VB-CLI-01 | conversion via CLI |\n")
    # The marker lives ONLY in a `.e2e.ts` file (the convention codex chose).
    _write(
        project / "tests" / "e2e" / "tempconv_conversion.e2e.ts",
        "// codd: covers vb=VB-CLI-01\n"
        "import { describe, it } from 'vitest';\n"
        "describe('cli', () => { it('converts', () => {}); });\n",
    )
    config = {"scan": {"test_dirs": ["tests/"]}}
    report = build_vb_coverage_audit(project, config=config)
    statuses = {row.vb_id: row.coverage_status for row in report.rows}
    assert statuses["VB-CLI-01"] == "covered"

    passed = run_implement_coverage_gate(
        project,
        config=config,
        design_node="test:test-strategy",
        output_paths=["tests/e2e/tempconv_conversion.e2e.ts"],
        rerun=None,
        echo=lambda _m: None,
        echo_error=lambda _m: None,
    )
    assert passed is True


def test_e2e_ts_recognition_does_not_weaken_gate_on_genuine_omission(tmp_path):
    """ANTI-FALSE-GREEN: recognising `.e2e.ts` must NOT auto-cover an unmarked VB."""
    project = tmp_path
    _canonical_strategy(
        project, "| VB-CLI-01 | covered |\n| VB-VAL-02 | genuinely unmarked |\n"
    )
    _write(
        project / "tests" / "e2e" / "tempconv_conversion.e2e.ts",
        "// codd: covers vb=VB-CLI-01\nimport 'vitest';\n",
    )
    config = {"scan": {"test_dirs": ["tests/"]}}
    report = build_vb_coverage_audit(project, config=config)
    statuses = {row.vb_id: row.coverage_status for row in report.rows}
    assert statuses["VB-CLI-01"] == "covered"
    assert statuses["VB-VAL-02"] == "uncovered"  # no marker → still RED


# ===========================================================================
# parse_vb_references: a later-column VB-* is a REFERENCE, a first-column
# VB-* is a DECLARATION (mirror image of parse_vb_table).
# ===========================================================================

from codd.verifiable_behavior_audit import (  # noqa: E402
    parse_vb_references,
    project_expects_vb_registry,
    validate_vb_registry_completeness,
)


def test_parse_vb_references_extracts_later_column_tokens_with_row_id():
    text = (
        "# Acceptance Criteria\n"
        "| AC | Description | Verifies |\n"
        "| --- | --- | --- |\n"
        "| AC-05 | login works | VB-CLI-007, VB-CLI-008 |\n"
        "| AC-06 | logout works | VB-CLI-009 |\n"
    )
    refs = parse_vb_references(text, source_doc="docs/test/acceptance_criteria.md")
    assert [(r.vb_id, r.row_id) for r in refs] == [
        ("VB-CLI-007", "AC-05"),
        ("VB-CLI-008", "AC-05"),
        ("VB-CLI-009", "AC-06"),
    ]
    assert all(r.source_doc == "docs/test/acceptance_criteria.md" for r in refs)


def test_parse_vb_references_first_cell_declaration_row_is_skipped_entirely():
    # A first-column VB-* row is a DECLARATION; parse_vb_references skips the
    # WHOLE row — even a VB-* token in that declaration row's later "Related"
    # column is NOT an external reference/obligation. This is what lets the
    # canonical doc carry a self-referential "Related" column without tripping
    # the unresolved-reference check (false-RED guard, fixture d).
    text = "| VB | D | Related |\n| --- | --- | --- |\n| VB-CLI-007 | login | VB-CLI-008 |\n"
    assert parse_vb_references(text) == []
    # A reference is only counted from a row whose FIRST cell is NOT a VB-*.
    ac = "| AC | D | Verifies |\n| --- | --- | --- |\n| AC-01 | login | VB-CLI-008 |\n"
    assert [r.vb_id for r in parse_vb_references(ac)] == ["VB-CLI-008"]


def test_parse_vb_references_skips_fenced_code_blocks():
    text = "intro\n```\n| VB-X | nope | VB-Y |\n```\nafter\n"
    assert parse_vb_references(text) == []


# ===========================================================================
# validate_vb_registry_completeness — 4-fixture discipline
# ===========================================================================


def _ac_doc(project: Path, rows: str) -> None:
    """Write a non-canonical acceptance-criteria doc that REFERENCES VB ids."""
    _write(
        project / "docs" / "test" / "acceptance_criteria.md",
        "| AC | Description | Verifies |\n| --- | --- | --- |\n" + rows,
    )


def test_registry_completeness_fixture_a_dogfood_repro_positive(tmp_path):
    """(a) Dogfood repro: AC table references VB ids but the canonical registry
    declares ZERO → empty_canonical_registry + unresolved_reference issues."""
    project = tmp_path
    # Canonical doc exists but declares NO first-column VB rows (a reference-only
    # table — the exact weak-model failure).
    _write(
        project / "docs" / "test" / "test_strategy.md",
        "# Test Strategy\n\nNarrative only; no VB rows declared.\n",
    )
    _ac_doc(project, "| AC-01 | login | VB-CLI-007 |\n| AC-02 | logout | VB-CLI-008 |\n")

    issues = validate_vb_registry_completeness(project, {}, strict=True)
    kinds = {issue.kind for issue in issues}
    assert "empty_canonical_registry" in kinds
    assert "unresolved_reference" in kinds
    unresolved = {issue.vb_id for issue in issues if issue.kind == "unresolved_reference"}
    assert unresolved == {"VB-CLI-007", "VB-CLI-008"}
    assert all(issue.severity == "error" for issue in issues)


def test_registry_completeness_fixture_b_out_of_dogfood_positive(tmp_path):
    """(b) Proper canonical test_strategy.md (>=1 VB row) with all AC refs
    declared → NO issues."""
    project = tmp_path
    _canonical_strategy(project, "| VB-CLI-007 | login |\n| VB-CLI-008 | logout |\n")
    _ac_doc(project, "| AC-01 | login | VB-CLI-007 |\n| AC-02 | logout | VB-CLI-008 |\n")

    issues = validate_vb_registry_completeness(project, {}, strict=True)
    assert issues == [], [i.message for i in issues]


def test_registry_completeness_fixture_c_spoof_negative_refs_are_not_declarations(tmp_path):
    """(c) Spoof: AC-table VB references ALONE (no canonical first-column rows)
    must NOT satisfy the registry — references are not declarations."""
    project = tmp_path
    # NO canonical doc at all; the only VB-* tokens live in a non-canonical AC
    # doc's later column. A reference must never be promoted to a declaration.
    _ac_doc(project, "| AC-01 | login | VB-CLI-007 |\n")

    issues = validate_vb_registry_completeness(project, {}, strict=True)
    kinds = {issue.kind for issue in issues}
    # The canonical registry is absent → missing_canonical_doc (an error). The
    # AC references did NOT silently satisfy the contract.
    assert "missing_canonical_doc" in kinds
    assert any(issue.severity == "error" for issue in issues)
    # And the build audit still sees ZERO declared behaviors (refs are not rows).
    assert build_vb_coverage_audit(project, config={}).rows == []


def test_registry_completeness_fixture_d_false_red_guard_valid_shorthand_format(tmp_path):
    """(d) False-RED guard: a valid canonical registry using legitimate
    formatting (hyphenated scheme ids, bold/emphasis markup, a self-reference in
    a later column) must NOT be flagged incomplete."""
    project = tmp_path
    # Hyphenated multi-segment ids (VB-AUTH-1) are atomic and legitimate; the
    # canonical doc may also reference its own ids in a later "related" column.
    _write(
        project / "docs" / "test" / "test_strategy.md",
        "| VB | Description | Related |\n"
        "| --- | --- | --- |\n"
        "| **VB-AUTH-1** | sign in | VB-AUTH-2 |\n"
        "| VB-AUTH-2 | sign out | VB-AUTH-1 |\n",
    )
    _ac_doc(project, "| AC-01 | sign in | VB-AUTH-1 |\n| AC-02 | sign out | VB-AUTH-2 |\n")

    issues = validate_vb_registry_completeness(project, {}, strict=True)
    assert issues == [], [i.message for i in issues]


def test_registry_completeness_unresolved_is_warning_when_not_strict(tmp_path):
    """Severity respects strict: an unresolved reference is a warning, not an
    error, under strict=False (the canonical doc still has declarations)."""
    project = tmp_path
    _canonical_strategy(project, "| VB-CLI-007 | login |\n")
    _ac_doc(project, "| AC-02 | logout | VB-CLI-999 |\n")  # 999 undeclared

    strict_issues = validate_vb_registry_completeness(project, {}, strict=True)
    assert any(i.kind == "unresolved_reference" and i.severity == "error" for i in strict_issues)
    lax_issues = validate_vb_registry_completeness(project, {}, strict=False)
    assert any(i.kind == "unresolved_reference" and i.severity == "warning" for i in lax_issues)
    assert not any(i.severity == "error" for i in lax_issues)


# ===========================================================================
# project_expects_vb_registry — the gate trigger predicate (generic/structural).
# A project with NO VB surface (Shape 2) must NOT be gated.
# ===========================================================================


def test_project_expects_vb_registry_true_when_wave_config_plans_canonical_doc(tmp_path):
    config = {
        "wave_config": {
            "1": [{"node_id": "test:test-strategy", "output": "docs/test/test_strategy.md"}]
        }
    }
    assert project_expects_vb_registry(tmp_path, config) is True


def test_project_expects_vb_registry_true_when_test_coverage_docs_configured(tmp_path):
    config = {"test_coverage": {"docs": ["docs/test/test_strategy.md"]}}
    assert project_expects_vb_registry(tmp_path, config) is True


def test_project_expects_vb_registry_true_when_canonical_doc_on_disk(tmp_path):
    _canonical_strategy(tmp_path, "| VB-CLI-007 | login |\n")
    assert project_expects_vb_registry(tmp_path, {}) is True


def test_project_expects_vb_registry_false_for_no_vb_surface_project(tmp_path):
    # Shape 2: only design docs planned, no canonical artifact, no test_coverage
    # config, no canonical doc on disk → NOT expected to own a VB registry.
    config = {
        "wave_config": {
            "1": [{"node_id": "design:core", "output": "docs/design/core_design.md"}]
        }
    }
    assert project_expects_vb_registry(tmp_path, config) is False
    # A non-canonical VB doc on disk (behaviors.md) does NOT flip the predicate —
    # only the canonical doc / planned canonical artifact / explicit config do.
    _write(tmp_path / "docs" / "test" / "behaviors.md", "| VB-X | a |\n| --- | --- |\n")
    assert project_expects_vb_registry(tmp_path, config) is False


# ---------------------------------------------------------------------------
# Cross-module feedback leak (the "ExprCalcTs" dogfood failure): a design-node-
# scoped implement retry (e.g. ``codd implement --design
# docs/detailed_design/tokenizer_design.md --output tests/unit``) audited the
# WHOLE project's VB registry and handed the tokenizer-only retry every other
# module's gaps too (12 VB-PAR-*, 6 VB-EVA-*, 2 VB-ARC-*) — none of which a
# tokenizer-only test file can possibly cover (there is no parser.ts/
# evaluator.ts to import from that design node's own output). The tokenizer's
# OWN behaviors were already fully covered; the gate still reported the task
# as FAILED and kept retrying it with feedback it could never satisfy.
# ---------------------------------------------------------------------------


def _module_doc(project: Path, path: str, *, node_id: str, modules: list[str]) -> None:
    module_lines = "\n".join(f"  - {m}" for m in modules)
    _write(
        project / path,
        f"---\ncodd:\n  node_id: {node_id}\n  modules:\n{module_lines}\n---\n\n# {node_id}\n",
    )


def _three_module_project(project: Path) -> None:
    _module_doc(
        project,
        "docs/detailed_design/tokenizer_design.md",
        node_id="detailed_design:tokenizer",
        modules=["tokenizer"],
    )
    _module_doc(
        project,
        "docs/detailed_design/parser_design.md",
        node_id="detailed_design:parser",
        modules=["parser"],
    )
    _module_doc(
        project,
        "docs/detailed_design/evaluator_design.md",
        node_id="detailed_design:evaluator",
        modules=["evaluator"],
    )
    _canonical_strategy(
        project,
        "| VB-TOK-01 | tokenizer |\n"
        "| VB-PAR-01 | parser |\n"
        "| VB-PAR-02 | parser |\n"
        "| VB-EVA-01 | evaluator |\n",
    )


def test_module_scoped_retry_ignores_other_modules_uncovered_vbs(tmp_path):
    project = tmp_path
    _three_module_project(project)
    # Only the tokenizer's own behavior is covered; parser/evaluator have no
    # test files at all yet (a realistic mid-build state — those tasks simply
    # have not run yet, not a defect in the tokenizer task).
    _write(
        project / "tests" / "unit" / "tokenizer.test.ts",
        "// codd: covers vb=VB-TOK-01\ntest('tokenizes', () => { expect(1).toBe(1); });\n",
    )
    config = {"scan": {"test_dirs": ["tests/"]}, "test_coverage": {"max_retries": 2}}

    rerun_calls: list[str] = []
    errors: list[str] = []
    passed = run_implement_coverage_gate(
        project,
        config=config,
        design_node="docs/detailed_design/tokenizer_design.md",
        output_paths=["tests/unit"],
        rerun=rerun_calls.append,
        echo=lambda _m: None,
        echo_error=errors.append,
    )

    # The tokenizer task's own behavior is fully covered — it must PASS and
    # never retry, even though 3 VBs remain uncovered PROJECT-WIDE.
    assert passed is True
    assert rerun_calls == []
    assert errors == []
    # Ground truth: the whole-project audit still (correctly) sees the gap —
    # scoping narrows FEEDBACK, it never hides anything from the real audit.
    whole_project = build_vb_coverage_audit(project, config=config)
    assert whole_project.summary["uncovered"] == 3


def test_module_scoped_retry_feedback_never_names_other_modules_vbs(tmp_path):
    project = tmp_path
    _three_module_project(project)
    # Tokenizer's OWN behavior is genuinely missing too, this time — the gate
    # must still retry (and eventually fail) on VB-TOK-01, but the feedback
    # text handed to that retry must never mention VB-PAR-*/VB-EVA-*: a
    # tokenizer-only implement call has no parser.ts/evaluator.ts to cover
    # them from, so naming them is actionable-looking but impossible to obey.
    config = {"scan": {"test_dirs": ["tests/"]}, "test_coverage": {"max_retries": 2}}

    rerun_calls: list[str] = []
    errors: list[str] = []
    passed = run_implement_coverage_gate(
        project,
        config=config,
        design_node="docs/detailed_design/tokenizer_design.md",
        output_paths=["tests/unit"],
        rerun=rerun_calls.append,
        echo=lambda _m: None,
        echo_error=errors.append,
    )

    assert passed is False
    assert len(rerun_calls) == 2  # bounded retries, both spent on the real gap
    for feedback in rerun_calls:
        assert "VB-TOK-01" in feedback
        assert "VB-PAR-01" not in feedback
        assert "VB-PAR-02" not in feedback
        assert "VB-EVA-01" not in feedback
    assert any("VB-TOK-01" in message for message in errors)
    assert not any("VB-PAR-01" in message or "VB-EVA-01" in message for message in errors)


def test_scope_uncovered_rows_keeps_untagged_rows_regardless_of_module(tmp_path):
    """A row with no recognizable module tag is ambiguous — always kept.

    Mirrors cross-cutting VBs (e.g. error-type distinctness, import-graph
    direction) that legitimately span modules: absence of a tag must never be
    read as "not mine," only a positive tag for a DIFFERENT module is.
    """
    project = tmp_path
    _module_doc(
        project,
        "docs/detailed_design/tokenizer_design.md",
        node_id="detailed_design:tokenizer",
        modules=["tokenizer"],
    )
    _module_doc(
        project,
        "docs/detailed_design/parser_design.md",
        node_id="detailed_design:parser",
        modules=["parser"],
    )
    _canonical_strategy(
        project,
        "| VB-ARC-01 | cross-cutting error distinctness |\n"
        "| VB-PAR-01 | parser |\n",
    )
    report = build_vb_coverage_audit(project, config={"scan": {"test_dirs": ["tests/"]}})
    assert {row.vb_id for row in report.uncovered_rows} == {"VB-ARC-01", "VB-PAR-01"}

    scoped = scope_uncovered_rows(
        report.uncovered_rows,
        project_root=project,
        design_node="docs/detailed_design/tokenizer_design.md",
    )
    ids = {row.vb_id for row in scoped}
    assert "VB-ARC-01" in ids  # untagged/ambiguous — kept
    assert "VB-PAR-01" not in ids  # positively tagged for a different module — excluded


def test_scope_uncovered_rows_is_a_noop_when_design_node_unresolvable(tmp_path):
    """No scoping when the design node isn't an in-project file (e.g. a bare
    node id like ``test:test-strategy``) — this is the exact ``design_node``
    shape the pre-existing bounded-retry tests above use, so this pins that
    they see EVERY uncovered row, unchanged, exactly as before this feature.
    """
    project = tmp_path
    _canonical_strategy(project, "| VB-01 | a |\n| VB-02 | b |\n")
    report = build_vb_coverage_audit(project, config={"scan": {"test_dirs": ["tests/"]}})
    assert len(report.uncovered_rows) == 2

    scoped = scope_uncovered_rows(
        report.uncovered_rows, project_root=project, design_node="test:test-strategy"
    )
    assert scoped == report.uncovered_rows

    scoped_no_node = scope_uncovered_rows(
        report.uncovered_rows, project_root=project, design_node=None
    )
    assert scoped_no_node == report.uncovered_rows


def test_format_gap_feedback_accepts_a_plain_row_list(tmp_path):
    project = tmp_path
    _canonical_strategy(project, "| VB-01 | widgets |\n")
    report = build_vb_coverage_audit(project, config={"scan": {"test_dirs": ["tests/"]}})
    text = format_gap_feedback(report.uncovered_rows)
    assert "VB-01" in text
    assert "widgets" in text
