"""B0 — failure attribution: classify + attribute a test_command failure so it
becomes addressable by the repair engine (non-empty failed_nodes), without
faking green or sliding into B-full test-vs-code arbitration."""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.repair.test_failure_attribution import (
    CODE_ADDRESSABLE_CLASSES,
    PROVENANCE_SOURCE,
    PROVENANCE_TEST,
    attribute_command_failure,
    parse_pytest_failure,
)


def _provenance(result):
    return {item.path: (item.provenance, item.editable) for item in result.attributed}


ROOT = Path("/tmp/b0_proj")


# ── classification ───────────────────────────────────────────

def test_assertion_failure_with_source_frame_attributes_source_first() -> None:
    out = (
        "=================================== FAILURES ===================================\n"
        "tests/test_cart.py:7: in test_total\n"
        "    assert cart.total() == 10\n"
        "src/cart.py:12: in total\n"
        "    assert self._sum >= 0\n"
        "E   AssertionError\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_cart.py::test_total - AssertionError\n"
    )
    result = parse_pytest_failure(out, ROOT, "test_command")
    assert result.failure_class == "assertion_failure"
    assert result.code_addressable is True
    # EDITABLE target is the source only; the test is read-only evidence (the
    # B-full guardrail against "rewrite the test to pass").
    assert result.failed_nodes == ["src/cart.py"]
    assert result.evidence_nodes == ["tests/test_cart.py"]
    prov = _provenance(result)
    assert prov["src/cart.py"] == (PROVENANCE_SOURCE, True)
    assert prov["tests/test_cart.py"] == (PROVENANCE_TEST, False)  # NOT editable


def test_assertion_failure_with_only_test_frame_yields_no_editable_target() -> None:
    # An assert evaluated in the test body with no descent into source: B0 has
    # no deterministic SOURCE target. Per the anti-false-green rule it must NOT
    # hand the engine the test file (which it could "fix" by neutering). So
    # failed_nodes is empty and the failure is NOT code-addressable; the test is
    # kept only as read-only evidence for the RCA.
    out = (
        "=================================== FAILURES ===================================\n"
        "tests/test_calc.py:4: in test_add\n"
        "    assert add(2, 3) == 5\n"
        "E   assert -1 == 5\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_calc.py::test_add - assert -1 == 5\n"
    )
    result = parse_pytest_failure(out, ROOT, "test_command")
    assert result.failure_class == "assertion_failure"
    assert result.failed_nodes == []  # no test-rewrite target
    assert result.code_addressable is False
    assert result.evidence_nodes == ["tests/test_calc.py"]


def test_runtime_exception_attributes_source_then_test() -> None:
    out = (
        "=================================== FAILURES ===================================\n"
        "tests/test_db.py:5: in test_lookup\n"
        "    return repo.get('x')\n"
        "src/repo.py:20: in get\n"
        "    return self._store[key]\n"
        "E   KeyError: 'x'\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_db.py::test_lookup - KeyError: 'x'\n"
    )
    result = parse_pytest_failure(out, ROOT, "test_command")
    assert result.failure_class == "runtime_exception"
    # Only the source is an EDITABLE target; the test is read-only evidence.
    assert result.failed_nodes == ["src/repo.py"]
    assert result.evidence_nodes == ["tests/test_db.py"]
    prov = _provenance(result)
    assert prov["src/repo.py"] == (PROVENANCE_SOURCE, True)
    assert prov["tests/test_db.py"] == (PROVENANCE_TEST, False)


def test_import_collection_error_attributes_resolved_source_module() -> None:
    # The real culprit (source missing the symbol) is in the ImportError
    # parenthetical, not in a call frame — it must still be attributed.
    out = (
        "ERROR collecting tests/test_import_err.py\n"
        "ImportError while importing test module '/tmp/b0_proj/tests/test_import_err.py'.\n"
        "Traceback:\n"
        "tests/test_import_err.py:1: in <module>\n"
        "    from src.calc import nonexistent_symbol\n"
        "E   ImportError: cannot import name 'nonexistent_symbol' from 'src.calc' (/tmp/b0_proj/src/calc.py)\n"
        "=========================== short test summary info ============================\n"
        "ERROR tests/test_import_err.py\n"
        "!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!\n"
    )
    result = parse_pytest_failure(out, ROOT, "test_command")
    assert result.failure_class == "import_collection_error"
    assert result.code_addressable is True
    # The resolved SOURCE module is the editable target; the test is evidence.
    assert result.failed_nodes == ["src/calc.py"]
    assert result.evidence_nodes == ["tests/test_import_err.py"]


def test_missing_dependency_is_environment_not_code_addressable() -> None:
    out = (
        "ERROR collecting tests/test_x.py\n"
        "ModuleNotFoundError: No module named 'requests'\n"
        "=========================== short test summary info ============================\n"
        "ERROR tests/test_x.py\n"
    )
    result = parse_pytest_failure(out, ROOT, "test_command")
    assert result.failure_class == "environment_build_error"
    # NOT code-addressable — the repair engine must not thrash trying to "fix"
    # a missing third-party dependency.
    assert result.code_addressable is False
    assert "environment" in result.diagnosis


def test_syntax_error_in_source_is_attributed() -> None:
    out = (
        "ERROR collecting tests/test_y.py\n"
        "tests/test_y.py:1: in <module>\n"
        "    import src.broken\n"
        "src/broken.py:3\n"
        "    def f(:\n"
        "         ^\n"
        "E   SyntaxError: invalid syntax\n"
        "=========================== short test summary info ============================\n"
        "ERROR tests/test_y.py\n"
    )
    result = parse_pytest_failure(out, ROOT, "test_command")
    assert result.failure_class == "import_collection_error"
    assert "src/broken.py" in result.failed_nodes


def test_broken_test_itself_is_a_harness_contract_violation_test_editable() -> None:
    # A collection failure with NO resolvable source culprit: the test/scaffold
    # is itself the defect. This is the ONE class where the test file is an
    # editable target.
    out = (
        "ERROR collecting tests/test_bad.py\n"
        "tests/test_bad.py:3: in <module>\n"
        "    def f(:\n"
        "E   SyntaxError: invalid syntax\n"
        "ERROR tests/test_bad.py\n"
    )
    result = parse_pytest_failure(out, ROOT, "test_command")
    assert result.failure_class == "harness_contract_violation"
    assert result.failed_nodes == ["tests/test_bad.py"]  # test IS the defect here
    assert result.code_addressable is True
    prov = _provenance(result)
    assert prov["tests/test_bad.py"] == (PROVENANCE_TEST, True)  # editable for harness class


def test_environment_class_is_excluded_from_code_addressable_set() -> None:
    assert "environment_build_error" not in CODE_ADDRESSABLE_CLASSES
    # harness_contract IS code-addressable, but only its OWN test file is editable.
    assert {
        "import_collection_error",
        "assertion_failure",
        "runtime_exception",
        "harness_contract_violation",
    } <= CODE_ADDRESSABLE_CLASSES


# ── path hygiene ─────────────────────────────────────────────

def test_site_packages_and_external_frames_are_not_attributed() -> None:
    out = (
        "=================================== FAILURES ===================================\n"
        ".venv/lib/python3.12/site-packages/lib/x.py:5: in helper\n"
        "    raise ValueError('boom')\n"
        "E   ValueError: boom\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_a.py::test_a - ValueError: boom\n"
    )
    result = parse_pytest_failure(out, ROOT, "test_command")
    assert all("site-packages" not in node for node in result.failed_nodes)
    # The in-project test is still attributed — as read-only evidence (a runtime
    # exception with no in-project source frame ⇒ no editable target).
    assert "tests/test_a.py" in result.evidence_nodes
    assert result.failed_nodes == []


def test_non_pytest_command_returns_none_preserving_legacy_behaviour() -> None:
    assert (
        attribute_command_failure(
            command="go test ./...", output="--- FAIL: TestFoo", project_root=ROOT
        )
        is None
    )


def test_attribution_is_best_effort_and_never_raises(monkeypatch) -> None:
    # Even on garbage output the pytest adapter must produce a result, not raise.
    result = attribute_command_failure(
        command="pytest -q", output="\x00\x00 not real pytest output", project_root=ROOT
    )
    assert result is not None
    assert result.failure_class in {"unknown", "runtime_exception"}
