"""Anti-false-green regression (Contract Kernel Cut A.3, the source_root DIMENSION):
the strict v3 implement-oracle must observe the SAME resolved tree that GENERATION
routes to — or fail-closed if they would differ. It must NEVER return GREEN by
observing a different tree than generation routes to (a "proved the wrong tree"
false-green).

THE BLOCKER (the GPT-5.5 round-2 cross-check found this, blocking v3.0.0):

  * Greenfield generation routes generated code by the LEGACY layout resolved from
    ``scan.source_dirs`` / ``scan.test_dirs`` in ``codd.yaml``
    (``codd.project_types.resolve_layout_profile`` →
    ``LayoutProfile.source_root`` / ``package_root`` / ``test_root``;
    ``codd.greenfield.pipeline._route_source_into_package`` routes the generated
    tree there). With ``scan: {source_dirs: ["lib"], test_dirs: ["spec"]}`` the
    generated code lands in ``lib/<pkg>`` + ``spec``.
  * The v3 implement-oracle (post-v2.99 Cut A.3) certifies + executes against
    ``ctx.language_profile.layout`` (the registry ``LayoutSpec`` — Python's roots are
    the FIXED ``src/{package_name}`` + ``tests``), because the gate builds the
    ``OracleContext`` from ``lang_profile.layout`` and never threads the
    ``scan.*_dirs`` knob into it.

So a project with a configured ``lib``/``spec`` layout GENERATES into ``lib/app`` +
``spec`` but the oracle CHECKS ``src/app`` + ``tests``. If ``src/app`` + ``tests``
hold stale-but-coherent code, the oracle returns GREEN while the actually-generated
tree (``lib/app`` + ``spec``) is UNVERIFIED — the proved-the-wrong-tree false-green.

THE INVARIANT (this file is the test that would have caught the blocker): with a
configured ``scan.source_dirs``/``test_dirs`` that DIFFERS from the profile-resolved
roots, the strict v3 path MUST either (Option B) hard-fail before verification OR
(Option A) observe the configured ``lib/app`` + ``spec`` tree and return RED on the
broken intended code — it must NEVER pass by observing the stale ``src/app`` +
``tests`` tree.

The COMMON case (``scan`` unset, or ``scan`` == the default ``src``/``tests`` roots)
must stay behavior-preserving (coherent GREEN / broken RED unchanged) — false-RED
loses only to false-GREEN avoidance, never to the common path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.implement_oracle import (
    EVIDENCE_MODULE_RESOLUTION,
    OracleScopeError,
    run_implement_oracle_gate,
)

_PACKAGE = "app"

# A configured layout that DIFFERS from the Python profile default (src/tests). This
# is the exact ``scan`` knob greenfield reads to ROUTE generation (lib/app + spec).
_DIVERGENT_SCAN = {"scan": {"source_dirs": ["lib"], "test_dirs": ["spec"]}}


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _path_shim(root: Path, src_dir: str) -> None:
    """Put ``<src_dir>`` on sys.path so ``--collect-only`` resolves the package
    WITHOUT an editable install (the dependency-free equivalent of the greenfield
    flow — mirrors the parity fixtures' conftest shim)."""
    _write(
        root,
        "conftest.py",
        "import os, sys\n"
        f"sys.path.insert(0, os.path.join(os.path.dirname(__file__), {src_dir!r}))\n",
    )


def _stale_but_coherent_src_tree(root: Path) -> None:
    """A fully-coherent package under the DEFAULT ``src/app`` + ``tests`` roots.

    This is the stale-but-coherent decoy: if the oracle (wrongly) observes the
    default tree it sees only this and returns GREEN — proving nothing about the
    actually-generated ``lib/app`` + ``spec`` tree.
    """
    _write(root, "src/app/__init__.py", "")
    _write(root, "src/app/core.py", "def total(xs):\n    return sum(xs)\n")
    _write(root, "tests/__init__.py", "")
    _write(
        root,
        "tests/test_core.py",
        "from app.core import total\n\n\ndef test_total():\n    assert total([1, 2]) == 3\n",
    )


def _broken_intended_lib_tree(root: Path) -> None:
    """The ACTUALLY-GENERATED tree under the CONFIGURED ``lib/app`` + ``spec`` roots,
    with a provably-broken first-party import.

    ``lib/app/hidden.py: from .missing import X`` is the keystone defect: invisible
    to py_compile (no resolution) and to ``--collect-only`` (no test imports it) —
    only the first-party import resolver, run over the CORRECT tree, proves it
    absent (PY_MODULE_NOT_FOUND). If the oracle observes ``lib/app`` it MUST be RED.
    """
    _write(root, "lib/app/__init__.py", "")
    _write(root, "lib/app/hidden.py", "from .missing import X\n")
    _write(root, "lib/app/core.py", "def total(xs):\n    return sum(xs)\n")
    _write(root, "spec/__init__.py", "")
    _write(
        root,
        "spec/test_core.py",
        "from app.core import total\n\n\ndef test_total():\n    assert total([1, 2]) == 3\n",
    )


def _run_divergent(root: Path):
    """Run the gate EXACTLY as the greenfield pipeline does for a configured layout:
    pass ``source_dirs`` / ``test_dirs`` from ``scan`` AND the same ``scan`` config.
    (``codd.greenfield.pipeline`` threads ``scan.source_dirs`` / ``scan.test_dirs``
    into ``run_implement_oracle_gate`` — see the ``source_dirs=`` / ``test_dirs=``
    call sites — so the oracle has the information; the bug is it was dropped when the
    OracleContext was built from the fixed registry LayoutSpec.)"""
    return run_implement_oracle_gate(
        root,
        language="python",
        project_name=_PACKAGE,
        source_dirs=["lib"],
        test_dirs=["spec"],
        config=_DIVERGENT_SCAN,
        echo=lambda _m: None,
    )


# ════════════════════════════════════════════════════════════════════════════
# THE POISON REGRESSION — would have caught the blocker.
#
# Generation routes to lib/app + spec (broken); the stale src/app + tests is
# coherent. The strict v3 path must NOT return GREEN by observing src/app + tests.
# Acceptable verdicts: Option B → OracleScopeError (hard-fail before verification);
# Option A → RED observing the broken lib/app tree (PY_MODULE_NOT_FOUND). FORBIDDEN:
# passed=True (proved the wrong tree).
# ════════════════════════════════════════════════════════════════════════════


def test_configured_root_divergence_never_greens_on_the_wrong_tree(tmp_path: Path) -> None:
    _stale_but_coherent_src_tree(tmp_path)  # the decoy (default roots, coherent)
    _broken_intended_lib_tree(tmp_path)  # the truth (configured roots, BROKEN)
    _path_shim(tmp_path, "lib")  # the generated tree is importable from lib/

    try:
        result = _run_divergent(tmp_path)
    except OracleScopeError:
        # Option B (fail-closed): the configured layout differs from the profile —
        # the strict v3 path hard-fails before verification rather than risk proving
        # the wrong tree. This is an acceptable anti-false-green verdict.
        return

    # Option A (project-resolve): the oracle observed the CONFIGURED lib/app + spec
    # tree and found the broken intended code → RED. NEVER a pass on the stale tree.
    assert result.passed is False, (
        "FALSE-GREEN: the strict v3 oracle returned GREEN while the actually-generated "
        "tree (lib/app + spec, routed there by scan.source_dirs/test_dirs) holds a "
        "provably-broken first-party import. It proved the wrong tree (the stale-but-"
        "coherent src/app + tests). The oracle must observe the SAME tree generation "
        "routes to, or hard-fail — never GREEN on a different tree. findings="
        f"{[(f.category, f.code, f.message) for f in result.findings]}"
    )
    # When it observes the right tree (Option A), the defect is the keystone
    # first-party module-resolution failure under lib/app.
    assert EVIDENCE_MODULE_RESOLUTION in {f.category for f in result.findings} or any(
        f.path and "lib/app" in f.path for f in result.findings
    ), [(f.category, f.code, f.path) for f in result.findings]


# ════════════════════════════════════════════════════════════════════════════
# NEGATIVE CONTROL — the mirror: configured lib/app + spec is COHERENT, the stale
# src/app + tests is BROKEN. Under Option A this must be GREEN (the oracle observes
# the coherent generated tree, NOT the broken decoy → no false-RED). Under Option B
# it hard-fails (configured layout differs). Either is anti-false-green; what is
# FORBIDDEN is a RED that blames the stale src tree (proving the wrong tree, the
# false-RED twin of the blocker).
# ════════════════════════════════════════════════════════════════════════════


def test_configured_root_divergence_does_not_false_red_on_the_wrong_tree(tmp_path: Path) -> None:
    # Stale src tree: BROKEN (a missing first-party module) — must NOT drive the verdict.
    _write(tmp_path, "src/app/__init__.py", "")
    _write(tmp_path, "src/app/hidden.py", "from .gone import Y\n")
    _write(tmp_path, "tests/__init__.py", "")
    _write(tmp_path, "tests/test_core.py", "def test_noop():\n    assert True\n")
    # Configured (generated) tree: fully coherent.
    _write(tmp_path, "lib/app/__init__.py", "")
    _write(tmp_path, "lib/app/core.py", "def total(xs):\n    return sum(xs)\n")
    _write(tmp_path, "spec/__init__.py", "")
    _write(
        tmp_path,
        "spec/test_core.py",
        "from app.core import total\n\n\ndef test_total():\n    assert total([1]) == 1\n",
    )
    _path_shim(tmp_path, "lib")

    try:
        result = _run_divergent(tmp_path)
    except OracleScopeError:
        # Option B: configured layout differs → hard-fail (acceptable).
        return

    # Option A: observed the coherent lib/app + spec → GREEN. A RED here that blames
    # the stale src/app/hidden.py would be a false-RED (proving the WRONG tree).
    assert result.passed is True, (
        "FALSE-RED: the strict v3 oracle blamed the STALE src tree (src/app/hidden.py) "
        "instead of observing the actually-generated, coherent lib/app + spec tree. "
        "findings=" f"{[(f.category, f.code, f.path) for f in result.findings]}"
    )


# ════════════════════════════════════════════════════════════════════════════
# BEHAVIOR-PRESERVING — the COMMON case (scan unset / scan == default src+tests)
# is unchanged: coherent GREEN, broken RED. This is what must NOT regress to a
# false-RED when the divergence fix lands.
# ════════════════════════════════════════════════════════════════════════════


def _run_default(root: Path, config: dict | None = None):
    return run_implement_oracle_gate(
        root,
        language="python",
        project_name=_PACKAGE,
        config=config or {},
        echo=lambda _m: None,
    )


def test_common_case_default_layout_coherent_greens(tmp_path: Path) -> None:
    """scan unset → default src/app + tests; a coherent package PASSES (unchanged)."""
    _stale_but_coherent_src_tree(tmp_path)
    _path_shim(tmp_path, "src")
    result = _run_default(tmp_path)
    assert result.passed is True, [(f.category, f.code, f.message) for f in result.findings]
    assert result.executed is True


def test_common_case_default_layout_broken_reds(tmp_path: Path) -> None:
    """scan unset → default src/app + tests; a broken first-party import is RED (unchanged)."""
    _write(tmp_path, "src/app/__init__.py", "")
    _write(tmp_path, "src/app/hidden.py", "from .missing import X\n")
    _write(tmp_path, "src/app/core.py", "def total(xs):\n    return sum(xs)\n")
    _write(tmp_path, "tests/__init__.py", "")
    _write(
        tmp_path,
        "tests/test_core.py",
        "from app.core import total\n\n\ndef test_total():\n    assert total([1]) == 1\n",
    )
    _path_shim(tmp_path, "src")
    result = _run_default(tmp_path)
    assert result.passed is False
    assert EVIDENCE_MODULE_RESOLUTION in {f.category for f in result.findings}


def test_common_case_scan_equals_default_is_behavior_preserving(tmp_path: Path) -> None:
    """scan EXPLICITLY set to the default src/tests must behave like scan-unset
    (coherent GREEN) — the fix must treat ``scan == default`` as the common case, NOT
    as a divergence hard-fail (that would be a gratuitous false-RED)."""
    _stale_but_coherent_src_tree(tmp_path)
    _path_shim(tmp_path, "src")
    result = run_implement_oracle_gate(
        tmp_path,
        language="python",
        project_name=_PACKAGE,
        source_dirs=["src"],
        test_dirs=["tests"],
        config={"scan": {"source_dirs": ["src"], "test_dirs": ["tests"]}},
        echo=lambda _m: None,
    )
    assert result.passed is True, [(f.category, f.code, f.message) for f in result.findings]
