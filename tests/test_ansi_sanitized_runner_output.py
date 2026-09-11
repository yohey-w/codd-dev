r"""Colour-contaminated runner output must not change a verdict or an attribution.

A test runner that emits ANSI colour into a *pipe* is a normal production
condition, not an exotic one: ``FORCE_COLOR`` / ``CLICOLOR_FORCE`` override
pytest's is-a-tty check, a project's ``addopts`` may carry ``--color=yes``, and
CI wrappers allocate pseudo-TTYs. Claude Code — the harness CoDD ships a plugin
for — exports ``FORCE_COLOR=3`` into every command it spawns, so ``codd verify``
run from inside it reads coloured output every single time.

Everything CoDD concludes from a test run is a regex over that text, and colour
breaks those regexes in BOTH directions:

* false RED — ``\x1b[32m1 passed\x1b[0m`` does not match ``\b\d+\s+passed``
  (``m1`` has no word boundary), so a green run is hard-failed for "no positive
  execution evidence"; and an ANSI-prefixed traceback frame does not match a
  ``^``-anchored path regex, so attribution returns no target and auto-repair
  has nothing to engage.
* false GREEN — the zero-collected guard looks for the literal substring
  ``collected 0 items``; a colour code inserted mid-phrase hides it. Scope of
  what was actually OBSERVED: only the false RED was reproduced live. The false
  GREEN is verified at the guard level (a split marker does defeat the substring
  match) but plain pytest exits 5 on an empty run, and the exit-code check fires
  first — so this half is a defensive hole, reachable only through a wrapper
  that swallows exit 5. It is guarded because the guard exists precisely for
  that wrapper case, not because it was seen in the wild.

These tests feed LITERAL escape sequences rather than relying on the ambient
environment, because CI does not set ``FORCE_COLOR`` — an environment-driven
test would be green in CI while the defect it guards is live for users. One test
sets ``FORCE_COLOR`` explicitly to pin the real end-to-end reproduction.

Falsification (measured, not asserted): replacing ``strip_ansi`` with the
identity function fails 6 of the 9 tests below. The other 3 are property tests
(idempotence, clean-text no-op, and the zero-guard's behaviour on already-clean
text) that hold either way.
"""

from __future__ import annotations

from pathlib import Path

from codd.ansi import strip_ansi
from codd.deployment.providers.verification.pytest_http import (
    PytestHttpTemplate,
    _collected_zero,
    _has_positive_execution,
)
from codd.repair.test_failure_attribution import attribute_command_failure


# pytest output as captured with FORCE_COLOR=3, escapes written out. Adapted, not
# verbatim: the traceback line is given in pytest's ``path:line: in func`` long-tb
# form (``--tb=long``), which is the shape the attributor's frame regex targets.
COLOURED_GREEN_SUMMARY = "\x1b[32m.\x1b[0m\x1b[32m   [100%]\x1b[0m\n\x1b[32m\x1b[32m\x1b[1m1 passed\x1b[0m\x1b[32m in 0.00s\x1b[0m\x1b[0m\n"
# Both zero-markers deliberately SPLIT by an escape (pytest colours the count
# separately from the label), so neither literal substring survives verbatim —
# the false-GREEN shape.
COLOURED_ZERO_COLLECTED = "\x1b[33mcollected \x1b[0m\x1b[1m0 items\x1b[0m\n\x1b[33mno tests \x1b[0m\x1b[1mran\x1b[0m in 0.01s\n"
COLOURED_PYTEST_FAILURE = (
    "\x1b[31mF\x1b[0m\x1b[31m    [100%]\x1b[0m\n"
    "=================================== FAILURES ===================================\n"
    "\x1b[31m\x1b[1m___________________________________ test_add ___________________________________\x1b[0m\n"
    "\n"
    "\x1b[1m\x1b[31msrc/calc.py\x1b[0m:2: in add\n"
    "    \x1b[94mraise\x1b[39;49;00m \x1b[96mValueError\x1b[39;49;00m(\x1b[33m'boom'\x1b[39;49;00m)\n"
    "\x1b[1m\x1b[31mE       ValueError: boom\x1b[0m\n"
    "\x1b[36m\x1b[1m=========================== short test summary info ============================\x1b[0m\n"
    "\x1b[31mFAILED\x1b[0m tests/test_calc.py::\x1b[1mtest_add\x1b[0m - ValueError: boom\n"
    "\x1b[31m\x1b[31m\x1b[1m1 failed\x1b[0m\x1b[31m in 0.03s\x1b[0m\x1b[0m\n"
)


# ── the stripper itself ──────────────────────────────────────────────────────

def test_strip_ansi_removes_sgr_colour_without_touching_the_text():
    assert strip_ansi(COLOURED_GREEN_SUMMARY).strip().endswith("1 passed in 0.00s")
    assert "\x1b" not in strip_ansi(COLOURED_PYTEST_FAILURE)


def test_strip_ansi_is_idempotent_and_a_noop_on_clean_text():
    clean = "collected 3 items\n3 passed in 0.10s\n"
    assert strip_ansi(clean) == clean
    once = strip_ansi(COLOURED_PYTEST_FAILURE)
    assert strip_ansi(once) == once


def test_strip_ansi_removes_osc_hyperlinks_and_handles_none():
    assert strip_ansi("\x1b]8;;https://example.test\x07link\x1b]8;;\x07") == "link"
    assert strip_ansi(None) == ""


# ── false RED: the positive-execution evidence check ─────────────────────────

def test_positive_execution_evidence_survives_colour():
    """The exact false RED: a green run reported as 'no evidence >=1 test ran'."""
    assert _has_positive_execution(strip_ansi(COLOURED_GREEN_SUMMARY)) is True


def test_pytest_http_template_passes_a_green_run_under_force_color(tmp_path, monkeypatch):
    """End-to-end pin of the live reproduction: FORCE_COLOR set, real subprocess."""
    e2e = tmp_path / "tests" / "e2e"
    e2e.mkdir(parents=True)
    (e2e / "test_pass.py").write_text("def test_truth():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    monkeypatch.setenv("FORCE_COLOR", "3")
    result = PytestHttpTemplate().execute("python3 -m pytest -q tests/e2e/", cwd=tmp_path)
    assert result.passed is True, result.output
    assert "\x1b" not in (result.output or "")


# ── false GREEN: the zero-collected guard ────────────────────────────────────

def test_zero_collected_is_still_detected_under_colour():
    """ANTI-FALSE-GREEN: colour must not hide 'collected 0 items' / 'no tests ran'."""
    # The raw text defeats the guard — that is the false GREEN being fixed.
    assert _collected_zero(COLOURED_ZERO_COLLECTED) is False
    assert _collected_zero(strip_ansi(COLOURED_ZERO_COLLECTED)) is True


def test_pytest_http_template_hard_fails_zero_tests_under_force_color(tmp_path, monkeypatch):
    e2e = tmp_path / "tests" / "e2e"
    e2e.mkdir(parents=True)
    (e2e / "test_empty.py").write_text("# no tests here\n", encoding="utf-8")
    monkeypatch.setenv("FORCE_COLOR", "3")
    result = PytestHttpTemplate().execute("python3 -m pytest -q tests/e2e/", cwd=tmp_path)
    assert result.passed is False
    assert "0 tests" in (result.output or "")


# ── false RED: B0 failure attribution ────────────────────────────────────────

def test_attribution_finds_the_source_frame_under_colour(tmp_path: Path):
    """The exact false RED: failed_nodes=[] / failure_class='unknown' on a coloured
    traceback, which leaves auto-repair with no editable target."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text("def add(a, b):\n    raise ValueError('boom')\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text("def test_add():\n    pass\n", encoding="utf-8")

    attribution = attribute_command_failure(
        command="python -m pytest -q",
        output=COLOURED_PYTEST_FAILURE,
        project_root=tmp_path,
    )

    assert attribution is not None
    assert attribution.failure_class != "unknown"
    assert "src/calc.py" in attribution.failed_nodes
    # The failing test file is read-only evidence, never a patch target.
    assert "tests/test_calc.py" in attribution.evidence_nodes


def test_attribution_is_identical_with_and_without_colour(tmp_path: Path):
    """The sanitizer must change nothing except the escapes."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text("def add(a, b):\n    raise ValueError('boom')\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text("def test_add():\n    pass\n", encoding="utf-8")

    coloured = attribute_command_failure(
        command="python -m pytest -q", output=COLOURED_PYTEST_FAILURE, project_root=tmp_path
    )
    plain = attribute_command_failure(
        command="python -m pytest -q", output=strip_ansi(COLOURED_PYTEST_FAILURE), project_root=tmp_path
    )
    assert coloured is not None and plain is not None
    assert coloured.failure_class == plain.failure_class
    assert coloured.failed_nodes == plain.failed_nodes
    assert coloured.evidence_nodes == plain.evidence_nodes
