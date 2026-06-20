"""Failure observability: a greenfield verify failure names the failing check +
the individual failing tests INLINE, so a dogfood failure is diagnosable from the
log without digging into .codd/repair_history (surfaced by the C-Go run, where a
bare "verify failed (1 failure(s))" hid that go test had 3 failing tests).
"""
from __future__ import annotations

from codd.greenfield.pipeline import _format_verify_failure_lines
from codd.repair.verify_runner import VerificationFailure


def _f(check: str, source: str, msg: str) -> VerificationFailure:
    return VerificationFailure(check_name=check, source=source, message=msg)


def test_names_check_source_and_first_message_line():
    lines = _format_verify_failure_lines(
        [_f("test_command", "verify", "test command failed (exit 1): go test ./...\nmore")]
    )
    assert len(lines) == 1
    assert "test_command" in lines[0] and "[verify]" in lines[0]
    assert "go test ./..." in lines[0]


def test_surfaces_embedded_go_failing_tests():
    msg = (
        "test command failed (exit 1): go test ./...\n"
        "--- FAIL: TestGoBuildServerCommand (0.25s)\n"
        "    build_command_test.go:37: expected exit code 0, got 1\n"
        "--- FAIL: TestVBGeneratedE2ETestsAreSubprocessDrivenAndBuildArtifact (0.15s)\n"
        "FAIL"
    )
    lines = _format_verify_failure_lines([_f("test_command", "verify", msg)])
    joined = "\n".join(lines)
    assert "TestGoBuildServerCommand" in joined
    assert "TestVBGeneratedE2ETestsAreSubprocessDrivenAndBuildArtifact" in joined
    assert sum("failing:" in ln for ln in lines) == 2  # one line per failing test


def test_surfaces_pytest_FAILED_lines():
    msg = "FAILED tests/test_x.py::test_foo - AssertionError\nFAILED tests/test_x.py::test_bar"
    lines = _format_verify_failure_lines([_f("test_command", "verify", msg)])
    assert sum("failing:" in ln for ln in lines) == 2


def test_caps_embedded_failures_at_eight():
    msg = "\n".join(f"--- FAIL: Test{i} (0.0s)" for i in range(20))
    lines = _format_verify_failure_lines([_f("test_command", "verify", msg)])
    assert sum("failing:" in ln for ln in lines) == 8


def test_empty_message_is_safe():
    lines = _format_verify_failure_lines([_f("some_check", "gate", "")])
    assert lines == ["  - some_check [gate]: (no message)"]


def test_multiple_failures_each_summarized():
    lines = _format_verify_failure_lines(
        [_f("test_command", "verify", "go test failed"), _f("ci_health", "gate", "bad workflow")]
    )
    heads = [ln for ln in lines if ln.startswith("  - ")]
    assert len(heads) == 2
    assert any("test_command" in h for h in heads) and any("ci_health" in h for h in heads)
