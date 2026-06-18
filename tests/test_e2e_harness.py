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
# A ``browser`` e2e modality WITHOUT a ``user_interface`` capability — used to
# exercise the constraint-driven http-sufficiency / unknown arms in isolation
# (so the positive UI-required capability does not pre-empt the classification).
BROWSER_NO_UI = ProjectCapabilities(
    user_interface=False,
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


def test_resolve_python_browser_ui_capability_is_playwright():
    # ANTI-FALSE-GREEN: ``BROWSER`` carries ``user_interface=True`` — a POSITIVE
    # structured signal that a UI is required. A Python browser-modality project
    # with a UI capability routes to the real browser harness (Playwright), NOT a
    # silent HTTP-only ``pytest_http`` downgrade. (The HISTORICAL behaviour — no
    # explicit browser flag => pytest_http — was itself the false-GREEN: a web app
    # whose UI/DOM need lived only in prose lost all browser evidence.)
    spec = resolve_e2e_harness(project_language="python", capabilities=BROWSER)
    assert spec == E2EHarnessSpec(
        runner="playwright",
        language="typescript",
        output_ext=".ts",
        template_ref="playwright",
        requires_node_manifest=True,
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


def test_resolve_python_browser_prose_constraint_is_not_an_http_sufficiency_signal():
    # NO-PROSE-INFERENCE (anti-false-green): a prose-y key claiming HTTP testing is
    # fine must NOT be read as the structured http-sufficiency opt-in. With no
    # POSITIVE structured signal authorising the downgrade, a UI-capability browser
    # project stays on the real browser harness (it must never silently become
    # HTTP-only ``pytest_http`` on the strength of prose).
    spec = resolve_e2e_harness(
        project_language="python",
        capabilities=BROWSER,
        constraints={"notes": "an http contract test is sufficient for this app"},
    )
    assert spec.runner == "playwright"
    assert spec.requires_node_manifest is True


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
# 3-way http-sufficiency classification for a BROWSER-modality project.
# The Python ``pytest_http`` downgrade is gated on POSITIVE structured evidence;
# absence of a browser flag is NEVER a downgrade signal (the historical
# false-GREEN). browser_ui_required / http_contract_sufficient / unknown.
# --------------------------------------------------------------------------- #


def test_resolve_python_browser_http_sufficiency_flag_is_pytest_http():
    # (a) DOGFOOD-REPRO POSITIVE: a Python browser-modality project WITH an explicit
    # http-sufficiency opt-in routes to ``pytest_http`` (the intended HTTP-contract
    # path still works). The opt-in is a POSITIVE, structured signal — not prose.
    spec = resolve_e2e_harness(
        project_language="python",
        capabilities=BROWSER_NO_UI,
        constraints={"http_contract_sufficient": True},
    )
    assert spec == E2EHarnessSpec(
        runner="pytest_http",
        language="python",
        output_ext=".py",
        template_ref="pytest_http",
        requires_node_manifest=False,
    )


def test_resolve_python_browser_http_harness_selector_is_pytest_http():
    # (a, variant) The harness-selector mirror: ``e2e_harness: pytest_http`` is the
    # POSITIVE selector form of the http-sufficiency opt-in → ``pytest_http``.
    spec = resolve_e2e_harness(
        project_language="python",
        capabilities=BROWSER_NO_UI,
        constraints={"e2e_harness": "pytest_http"},
    )
    assert spec.runner == "pytest_http"
    assert spec.output_ext == ".py"
    assert spec.requires_node_manifest is False


def test_resolve_python_browser_explicit_browser_flag_beats_http_sufficiency():
    # (b) OUT-OF-DOGFOOD POSITIVE: a Python browser-modality project WITH an explicit
    # browser flag/selector routes to Playwright. The explicit browser flag has the
    # HIGHEST precedence — it wins even if an http-sufficiency opt-in is also set.
    spec = resolve_e2e_harness(
        project_language="python",
        capabilities=BROWSER_NO_UI,
        constraints={
            "browser_automation_required": True,
            "http_contract_sufficient": True,
        },
    )
    assert spec.runner == "playwright"
    assert spec.output_ext == ".ts"
    assert spec.template_ref == "playwright"
    assert spec.requires_node_manifest is True


def test_resolve_python_browser_unknown_fails_closed_to_playwright_not_pytest_http():
    # (c) SPOOF NEGATIVE = THE FALSE-GREEN GUARD (core regression-prevention): a
    # Python browser-modality project with NO positive signal whatsoever (no browser
    # flag, no UI capability, no http-sufficiency opt-in) is ``unknown``. It MUST NOT
    # silently downgrade to ``pytest_http`` (which would run an HTTP-only test and
    # never capture real browser/DOM evidence — the false-GREEN). It FAILS CLOSED to
    # the browser harness (Playwright, node manifest required) — a false-RED /
    # explicit-toolchain requirement is acceptable; a silent pytest_http is NOT.
    spec = resolve_e2e_harness(
        project_language="python",
        capabilities=BROWSER_NO_UI,
    )
    assert spec.runner == "playwright"
    assert spec.runner != "pytest_http"
    assert spec.output_ext == ".ts"
    assert spec.template_ref == "playwright"
    assert spec.requires_node_manifest is True


def test_resolve_python_non_browser_http_surface_still_pytest_http_no_regression():
    # (d) FALSE-RED GUARD: the non-browser ``network_surface == "http"`` Python case
    # (an HTTP API service, e2e_modality != browser) still routes to ``pytest_http``.
    # The 3-way browser classification must NOT bleed into this branch — no false-RED
    # regression for a legitimately HTTP-only Python service.
    spec = resolve_e2e_harness(project_language="python", capabilities=HTTP_NO_UI)
    assert spec == E2EHarnessSpec(
        runner="pytest_http",
        language="python",
        output_ext=".py",
        template_ref="pytest_http",
        requires_node_manifest=False,
    )


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
