"""Static lock (Contract Kernel Cut A.3, GPT stage 5): the legacy ``source_root``
BRIDGE stays retired from the v3 implement-oracle pass-authority path — FOREVER.

Cut A.3 requirement (goal doc): in the strict v3.0.0 path, legacy
``LayoutProfile.source_root`` is UNUSED or hard-fail. The v3 PASS-AUTHORITY (the
合否 verdict: the implement-oracle, and the verify runner) must derive layout from
the authoritative ``LayoutSpec.source_sets`` + registered adapters, never the legacy
single ``source_root``.

What v2.99 did (the core retirement): the Python oracle adapter now reads
``ctx.language_profile.layout`` (source_sets) + the gate-resolved ``package_name``,
NOT ``ctx.layout_profile.source_root``; and the bridge that projected
``source_sets → a synthesized LayoutProfile.source_root`` was DELETED
(``synthesize_minimal_layout_view`` + the ``layout_override`` mechanism + the
``kind=="adapter"`` legacy-LayoutProfile handoff). ``OracleContext.layout_profile``
is now the ``LayoutSpec`` for every kind.

This module is the COMPILE-TIME lock (the bridge cannot silently come back). It
complements two other locks already in place:
  * the DYNAMIC poison guard
    (``tests/languages/test_oracle_python_parity.py::
    test_cut_a3_oracle_path_does_not_read_legacy_source_root``): a ``layout_profile``
    that RAISES on any ``.source_root`` access still lets the gate derive correct
    roots — proving the v3 oracle RUN never reads the legacy field.
  * the typed boundary: ``OracleContext.layout_profile`` is the ``LayoutSpec``;
    ``layout_override`` is gone.

QUARANTINED-LEGACY (allowed to keep ``source_root`` — they are NOT the v3
pass-authority verdict, per the GPT-5.5 scope "legacy/compat may keep source_root,
just not in the strict v3 run"):
  * ``codd/project_types.py`` — the legacy LayoutProfile scaffold/layout/pytest-ini.
  * ``codd/languages/compat.py`` — the single-root compat shim (hard-fails multi-root).
  * ``codd/import_coherence.py`` — CEG drift ANALYSIS (not a green/red gate — see
    the v2.87 finding that extraction/analysis never drives a verdict).
  * ``codd/greenfield/pipeline.py`` ``_route_source_into_package`` — OUTPUT-PATH
    routing for code GENERATION, not a verdict.
  * ``codd/languages/adapters/oracle_python.py`` ``certify_python_oracle_scope`` —
    a legacy-SIGNATURE back-compat shim (takes a caller-supplied LayoutProfile),
    invoked only by tests, never by the v3 ``OracleContext`` path.
"""

from __future__ import annotations

import re
from pathlib import Path

import codd

#: The bridge that collapsed ``source_sets`` into a single legacy ``source_root``.
#: Deleted in v2.99; this lock keeps it deleted (a reappearance = a Cut A.3 regression).
_BRIDGE_TOKENS = ("synthesize_minimal_layout_view", "layout_override")

_PKG_ROOT = Path(codd.__file__).resolve().parent
_TRIPLE = re.compile(r'""".*?"""|\'\'\'.*?\'\'\'', re.DOTALL)
_LINE_COMMENT = re.compile(r"#.*?$", re.MULTILINE)


def _code_only(path: Path) -> str:
    src = path.read_text(encoding="utf-8")
    return _LINE_COMMENT.sub("", _TRIPLE.sub("\n", src))


def test_source_root_synthesize_bridge_is_retired() -> None:
    """The ``synthesize_minimal_layout_view`` / ``layout_override`` bridge stays
    DELETED from all of ``codd/`` (code only — docstrings naming it as retired are
    fine). Its return would re-introduce the legacy single-``source_root`` view into
    the v3 oracle pass-authority — the exact Cut A.3 violation v2.99 removed."""
    violations: list[str] = []
    for path in _PKG_ROOT.rglob("*.py"):
        code = _code_only(path)
        for token in _BRIDGE_TOKENS:
            if token in code:
                violations.append(f"{path.relative_to(_PKG_ROOT)}: {token}")
    assert not violations, (
        "Cut A.3 regression — the legacy source_root bridge reappeared in v3 code "
        "(the v3 oracle path must read LayoutSpec.source_sets + the gate-resolved "
        "package topology, never a synthesized single source_root):\n  "
        + "\n  ".join(violations)
    )


def test_v3_oracle_adapter_does_not_read_layout_profile_source_root() -> None:
    """The Python oracle adapter's v3 EXECUTION path does not read
    ``ctx.layout_profile.source_root`` (the retired bridge field).

    The only ``source_root`` read left in ``oracle_python.py`` is inside the
    legacy-signature shim ``certify_python_oracle_scope`` (caller-supplied
    LayoutProfile, tests only) — quarantined legacy, never the v3 ``OracleContext``
    path (the dynamic poison guard proves the v3 run is clean). So the FORBIDDEN
    idiom here is specifically reading ``source_root`` off the OracleContext /
    ``ctx.layout_profile``."""
    code = _code_only(_PKG_ROOT / "languages" / "adapters" / "oracle_python.py")
    forbidden = re.findall(r"ctx\.layout_profile\.source_root|ctx\.layout\.source_root", code)
    assert not forbidden, (
        "the v3 Python oracle adapter reads source_root off the OracleContext — it "
        "must read ctx.language_profile.layout.source_sets + ctx.package_name:\n  "
        + "\n  ".join(forbidden)
    )
