"""Contract Kernel oracle dispatch — UNSUPPORTED → EXPLICIT RED (GPT design §9).

THE FALSE-GREEN §9 CLOSES
=========================
The implement-time oracle gate (:func:`codd.implement_oracle.run_implement_oracle_gate`)
is the "first head" of the contract kernel: a DECLARED stack must PROVE its
independently-generated artifacts cohere before the run advances. Up to §8 the gate
collapsed every "no runnable oracle" reason to ONE silent NO-OP PASS
(``passed=True, executed=False``). That silently waved through a DECLARED-but-
UNSUPPORTED stack — a language CoDD was TOLD to build (``language="ruby"``) but has
NO registered oracle adapter for. A declared stack CoDD cannot prove is a false-
green; §9 makes it an EXPLICIT RED.

THE 3-STATE MODEL (``dogfood/gpt_ck_oracle_dispatch.md`` §6)
-----------------------------------------------------------
* SUPPORTED            → run the oracle; GREEN only if it actually passes.
* UNSUPPORTED_EXPLICIT → a NON-EMPTY language is declared but no oracle resolves
                         (no registered adapter) AND not opted out →
                         ``passed=False, executed=False, code="implement_oracle_
                         unsupported"`` (NOT CI-green). The §9 closure.
* LEGACY_ABSENT        → language is None/empty (NO declared stack) → NO-OP, but
                         emit a VISIBLE trace (a fallback, never silent) and stay
                         NON-RED (nothing to be "unsupported" about).
* OPT-OUT              → ``implement.implement_oracle: false`` → NO-OP-WITH-TRACE
                         (``unsupported_oracle_allowed_by_config``), excluded from
                         the green-gate; NON-RED by default (preserves the
                         documented opt-out contract), never silent.

THE CARDINAL RULE
-----------------
false-GREEN is forbidden, but do NOT over-RED a legitimate no-oracle case: no
language at all, or an explicit opt-out, become NO-OP-WITH-TRACE (visible) — never
silent, never RED.

CHARACTERIZATION-FIRST
----------------------
This file is written CHARACTERIZATION-FIRST: every assertion below pins behaviour,
and the ones that FLIP from "today's silent NO-OP pass" to "the §9 RED" are marked
``# §9:`` with both the OLD and the NEW expectation, so the diff is auditable.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from codd.implement_oracle import resolve_implement_oracle, run_implement_oracle_gate
from codd.implement_oracle_types import EVIDENCE_ENVIRONMENT_BUILD


def _python_coherent_project(root: Path) -> None:
    """A minimal COHERENT Python project (the SUPPORTED control — passes the oracle)."""
    (root / "src" / "calc_lib").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "src" / "calc_lib" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "calc_lib" / "core.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests" / "test_core.py").write_text(
        "from calc_lib.core import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    (root / "conftest.py").write_text(
        "import os, sys\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))\n",
        encoding="utf-8",
    )


def _gate(root: Path, language, config=None):
    return run_implement_oracle_gate(
        root,
        language=language,
        project_name="demo",
        config=config or {},
        echo=lambda _m: None,
    )


# ════════════════════════════════════════════════════════════════════════════
# 1. SUPPORTED — python coherent → passed=True (UNCHANGED by §9)
# ════════════════════════════════════════════════════════════════════════════


def test_supported_python_coherent_passes(tmp_path: Path) -> None:
    """A SUPPORTED language with a coherent project runs the oracle and passes.

    This is the control: §9 must NOT regress the supported path — a coherent
    project still GREENs, and it actually RAN (executed=True).
    """
    _python_coherent_project(tmp_path)
    result = _gate(tmp_path, "python")
    assert result.executed is True, "the supported oracle must RUN"
    assert result.passed is True, [
        (f.category, f.code, f.message) for f in result.findings
    ]


# ════════════════════════════════════════════════════════════════════════════
# 2. UNSUPPORTED_EXPLICIT — a DECLARED but unsupported language (the §9 flip)
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("language", ["ruby", "rust"])
def test_declared_unsupported_language_is_red(tmp_path: Path, language: str) -> None:
    """§9: a DECLARED but UNSUPPORTED language is NOT a silent pass — it is RED.

    A non-empty ``language`` with no registered oracle adapter is a stack CoDD was
    told to build but cannot prove. That MUST NOT pass.

      OLD (pre-§9): passed=True,  executed=False  (silent NO-OP — the false-green)
      NEW (§9):     passed=False, executed=False, code="implement_oracle_unsupported"
    """
    result = _gate(tmp_path, language)
    # §9: was a silent NO-OP pass; now an explicit unsupported RED.
    assert result.passed is False, (
        f"a declared-unsupported language ({language!r}) must RED, never silently pass"
    )
    assert result.executed is False, "nothing was executed (no adapter to run)"
    assert "implement_oracle_unsupported" in {f.code for f in result.findings}
    assert any(
        f.category == EVIDENCE_ENVIRONMENT_BUILD for f in result.findings
    ), "the unsupported finding is an environment/build-class evidence"


def test_declared_unsupported_resolve_still_returns_none(tmp_path: Path) -> None:
    """``resolve_implement_oracle`` STILL returns None for an unsupported language.

    §9 changes the GATE's handling of ``None`` (it now distinguishes WHY), not the
    resolver's contract — the resolver still signals "no runnable oracle" with None.
    The 4-state classification lives in the gate.
    """
    assert (
        resolve_implement_oracle(tmp_path, language="ruby", project_name="demo", config={})
        is None
    )


# ════════════════════════════════════════════════════════════════════════════
# 3. LEGACY_ABSENT — no language declared → NO-OP, but VISIBLE (non-RED)
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("language", [None, ""])
def test_no_language_declared_is_noop_with_trace(language) -> None:
    """No declared stack (language None/empty) → NO-OP, non-RED, but VISIBLE.

    There is nothing to be "unsupported" about — no stack was declared. So it stays
    a passing NO-OP (passed=True, executed=False), but §9 makes it emit a VISIBLE
    fallback trace (it is no longer SILENT). Captured via the echo sink below.
    """
    lines: list[str] = []
    with tempfile.TemporaryDirectory() as d:
        result = run_implement_oracle_gate(
            Path(d),
            language=language,
            project_name="demo",
            config={},
            echo=lines.append,
        )
    assert result.passed is True, "no declared stack must stay non-RED"
    assert result.executed is False
    # §9: the NO-OP must be VISIBLE (a fallback trace), never silent.
    assert any(
        "no language declared" in ln or "oracle skipped" in ln for ln in lines
    ), f"the no-language NO-OP must emit a visible trace; got {lines!r}"


# ════════════════════════════════════════════════════════════════════════════
# 4. OPT-OUT — explicit opt-out on a SUPPORTED language → NO-OP-WITH-TRACE
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("language", ["typescript", "python"])
def test_opt_out_is_noop_with_trace_not_red(language: str) -> None:
    """An explicit opt-out stays NON-RED NO-OP, but VISIBLE (excluded from green-gate).

    The user explicitly set ``implement.implement_oracle: false``. §9 preserves the
    DOCUMENTED opt-out contract (NOT RED by default — bounding blast radius), but the
    NO-OP becomes VISIBLE: a trace naming the opt-out + that it is excluded from the
    green-gate. (GPT §6 notes a strict-RED reading for greenfield; we keep NO-OP-with-
    trace as the default so opt-out is never silently broken.)
    """
    lines: list[str] = []
    with tempfile.TemporaryDirectory() as d:
        result = run_implement_oracle_gate(
            Path(d),
            language=language,
            project_name="demo",
            config={"implement": {"implement_oracle": False}},
            echo=lines.append,
        )
    assert result.passed is True, "opt-out is a documented contract — NOT RED by default"
    assert result.executed is False
    # §9: opt-out NO-OP must be VISIBLE, naming the opt-out + green-gate exclusion.
    assert any(
        "opted out" in ln or "implement_oracle=false" in ln for ln in lines
    ), f"the opt-out NO-OP must emit a visible trace; got {lines!r}"
