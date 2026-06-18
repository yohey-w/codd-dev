"""Tests for B: language-aware web/HTTP E2E harness selection.

Covers the deterministic resolver (:mod:`codd.e2e_harness`) and the Python HTTP
e2e verification template (:class:`PytestHttpTemplate`), including its
anti-false-green 0-collected guard.
"""

from __future__ import annotations

import sys
from pathlib import Path

from codd.deployment.providers import VERIFICATION_TEMPLATES, VerificationResult
from codd.deployment.providers.verification.pytest_http import PytestHttpTemplate
from codd.e2e_harness import E2EHarnessSpec, resolve_e2e_harness
from codd.project_types import ProjectCapabilities


BROWSER = ProjectCapabilities(
    user_interface=True,
    network_surface="http",
    e2e_modality="browser",
    long_running_service=True,
)
CLI = ProjectCapabilities(e2e_modality="cli")
HTTP_NO_UI = ProjectCapabilities(network_surface="http", e2e_modality="none")
NONE = ProjectCapabilities(network_surface="none", e2e_modality="none")


# --------------------------------------------------------------------------- #
# resolve_e2e_harness — deterministic decision matrix.
# --------------------------------------------------------------------------- #


def test_resolve_python_browser_no_explicit_browser_is_pytest_http():
    spec = resolve_e2e_harness(project_language="python", capabilities=BROWSER)
    assert spec == E2EHarnessSpec(
        runner="pytest_http",
        language="python",
        output_ext=".py",
        template_ref="pytest_http",
        requires_node_manifest=False,
    )


def test_resolve_python_browser_explicit_harness_value_is_playwright():
    spec = resolve_e2e_harness(
        project_language="python",
        capabilities=BROWSER,
        constraints={"e2e_harness": "playwright_node"},
    )
    assert spec.runner == "playwright"
    assert spec.output_ext == ".ts"
    assert spec.template_ref == "playwright"
    assert spec.requires_node_manifest is True


def test_resolve_python_browser_explicit_browser_flag_is_playwright():
    spec = resolve_e2e_harness(
        project_language="python",
        capabilities=BROWSER,
        constraints={"browser_automation_required": True},
    )
    assert spec.runner == "playwright"
    assert spec.requires_node_manifest is True


def test_resolve_python_browser_ambiguous_constraints_stay_pytest_http():
    # A non-selector prose-y key must NOT flip to Playwright (no prose inference).
    spec = resolve_e2e_harness(
        project_language="python",
        capabilities=BROWSER,
        constraints={"notes": "the team likes browser testing in general"},
    )
    assert spec.runner == "pytest_http"
    assert spec.requires_node_manifest is False


def test_resolve_typescript_browser_is_playwright_unchanged():
    spec = resolve_e2e_harness(project_language="typescript", capabilities=BROWSER)
    assert spec.runner == "playwright"
    assert spec.language == "typescript"
    assert spec.output_ext == ".ts"
    assert spec.template_ref == "playwright"
    assert spec.requires_node_manifest is True


def test_resolve_unknown_language_browser_is_playwright():
    # Language unknown (None) must preserve the legacy TS Playwright path.
    spec = resolve_e2e_harness(project_language=None, capabilities=BROWSER)
    assert spec.runner == "playwright"
    assert spec.output_ext == ".ts"
    assert spec.requires_node_manifest is True


def test_resolve_cli_is_native():
    spec = resolve_e2e_harness(project_language="python", capabilities=CLI)
    assert spec.runner == "native_cli"
    assert spec.output_ext == ".py"
    assert spec.template_ref == "native_cli"

    ts_spec = resolve_e2e_harness(project_language="typescript", capabilities=CLI)
    assert ts_spec.runner == "native_cli"
    assert ts_spec.output_ext == ".ts"


def test_resolve_python_http_surface_non_browser_is_pytest_http():
    spec = resolve_e2e_harness(project_language="python", capabilities=HTTP_NO_UI)
    assert spec.runner == "pytest_http"
    assert spec.output_ext == ".py"


def test_resolve_typescript_http_surface_non_browser_keeps_node():
    spec = resolve_e2e_harness(project_language="typescript", capabilities=HTTP_NO_UI)
    assert spec.runner == "playwright"
    assert spec.output_ext == ".ts"
    assert spec.requires_node_manifest is True


def test_resolve_no_surface_is_none_spec():
    spec = resolve_e2e_harness(project_language="python", capabilities=NONE)
    assert spec.runner == "none"
    assert spec.template_ref == "none"


# --------------------------------------------------------------------------- #
# PytestHttpTemplate — command generation + anti-false-green execution.
# --------------------------------------------------------------------------- #


class _RuntimeState:
    def __init__(self, *, project_root: Path, source: str | None = None,
                 target: str = "", actual_check_command: str | None = None) -> None:
        self.project_root = project_root
        self.source = source
        self.target = target
        self.actual_check_command = actual_check_command


def test_pytest_http_template_registers():
    assert VERIFICATION_TEMPLATES["pytest_http"] is PytestHttpTemplate


def test_pytest_http_generate_test_command_e2e(tmp_path):
    state = _RuntimeState(project_root=tmp_path)
    command = PytestHttpTemplate().generate_test_command(state, "e2e")
    assert command == "python -m pytest -q tests/e2e/"


def test_pytest_http_generate_test_command_prefers_source_file(tmp_path):
    spec = tmp_path / "tests" / "e2e" / "test_items.py"
    spec.parent.mkdir(parents=True)
    spec.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    state = _RuntimeState(project_root=tmp_path, source="tests/e2e/test_items.py")
    command = PytestHttpTemplate().generate_test_command(state, "e2e")
    assert command == "python -m pytest -q tests/e2e/test_items.py"


def test_pytest_http_generate_test_command_honors_actual_check_command(tmp_path):
    state = _RuntimeState(
        project_root=tmp_path,
        actual_check_command="python -m pytest tests/e2e/test_login.py -k smoke",
    )
    command = PytestHttpTemplate().generate_test_command(state, "e2e")
    assert command == "python -m pytest tests/e2e/test_login.py -k smoke"


# ``execute`` runs whatever command string it is handed; these tests exercise its
# result-ANALYSIS logic (the anti-false-green guard), so they run pytest via the
# current interpreter (the literal ``python -m pytest`` default is asserted in the
# generate_test_command tests above; some CI sandboxes have only ``python3``).
def _pytest_cmd(target: str) -> str:
    return f"{sys.executable} -m pytest -q {target}"


def test_pytest_http_execute_passes_on_real_passing_test(tmp_path):
    e2e_dir = tmp_path / "tests" / "e2e"
    e2e_dir.mkdir(parents=True)
    (e2e_dir / "test_pass.py").write_text(
        "def test_truth():\n    assert 1 + 1 == 2\n", encoding="utf-8"
    )
    result = PytestHttpTemplate().execute(_pytest_cmd("tests/e2e/"), cwd=tmp_path)
    assert isinstance(result, VerificationResult)
    assert result.passed is True


def test_pytest_http_execute_fails_anti_false_green_on_zero_collected(tmp_path):
    # An EMPTY tests/e2e dir -> pytest exits 5 (no tests collected). The template
    # MUST hard-fail (anti-false-green): exit 0-on-nothing must never pass.
    e2e_dir = tmp_path / "tests" / "e2e"
    e2e_dir.mkdir(parents=True)
    result = PytestHttpTemplate().execute(_pytest_cmd("tests/e2e/"), cwd=tmp_path)
    assert result.passed is False
    assert "0 tests" in result.output or "no tests ran" in result.output.lower()


def test_pytest_http_execute_fails_on_failing_test(tmp_path):
    e2e_dir = tmp_path / "tests" / "e2e"
    e2e_dir.mkdir(parents=True)
    (e2e_dir / "test_fail.py").write_text(
        "def test_broken():\n    assert False\n", encoding="utf-8"
    )
    result = PytestHttpTemplate().execute(_pytest_cmd("tests/e2e/"), cwd=tmp_path)
    assert result.passed is False


def test_pytest_http_execute_fails_on_exit0_without_execution_evidence(tmp_path):
    # ANTI-FALSE-GREEN (v2.47): a wrapper / actual_check_command that EXITS 0 but
    # emits NO pytest summary (no "collected N items" / "N passed|failed|error")
    # must NOT pass — exit 0 alone cannot prove >=1 test ran. ``true`` exits 0
    # with empty output, simulating a wrapper that swallows pytest's exit-5.
    result = PytestHttpTemplate().execute("true", cwd=tmp_path)
    assert result.passed is False
    assert "no positive execution evidence" in result.output
