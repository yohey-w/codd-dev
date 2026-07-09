"""Tombstone: v3.22.0 Increment 1 (the dependency-boundary import gate) is REVERTED.

Permanent REINTRODUCTION GUARD. v3.22.0 shipped a deterministic gate that proved a
generated source file's imports against the transitive ``depends_on`` closure of its
owning design doc, failing the implement oracle on any import outside that closure.
The premise was unsound: frontmatter ``depends_on`` is an OPEN-WORLD ordering/context
declaration (producer-first order, (B') injection), NOT a closed-world import
allow-list. On correct greenfield output the module graph routinely exceeds the
conceptual doc closure (first dogfood: 7/7 findings false positives), and the
prescribed repair (add a ``depends_on`` edge) lies outside implement's write
authority, making the rerun ladder unwinnable. It was reverted in v3.22.1 (Fable5
ruling: ``dogfood/fable5_reply_2026-07-09_inc1-flaw.md``). Open-world declarations
STEER; they never JUDGE.

RED at HEAD f55f3fd (the gate present); GREEN after the revert. If any assertion
here fails in the future, the gate — or its residue — has been reintroduced. Do NOT
"fix" this test by relaxing it; delete the reintroduced gate instead.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import codd.implement_oracle as implement_oracle


def test_dependency_boundary_coherence_module_is_gone() -> None:
    """(a) The gate's home module must not be importable — the file is deleted."""
    assert importlib.util.find_spec("codd.dependency_boundary_coherence") is None


def test_implement_oracle_has_no_boundary_gate_attributes() -> None:
    """(b) The gate's functions/knob are excised from ``codd.implement_oracle``."""
    for name in (
        "_apply_dependency_boundary_gate",
        "_boundary_violation_block",
        "_dependency_boundary_gate_enabled",
    ):
        assert not hasattr(implement_oracle, name), (
            f"codd.implement_oracle still exposes {name!r} — the boundary gate was "
            "reintroduced. Delete it (see dogfood/fable5_reply_2026-07-09_inc1-flaw.md)."
        )


def test_dependency_boundary_token_absent_from_implement_oracle_source() -> None:
    """(c) The ``dependency_boundary`` token appears NOWHERE in the gate module's source."""
    source = Path(implement_oracle.__file__).read_text(encoding="utf-8")
    assert "dependency_boundary" not in source, (
        "The token 'dependency_boundary' resurfaced in codd/implement_oracle.py — a "
        "live import-topology gate (or its residue) was reintroduced. The reverted "
        "premise remains unsound; open-world graphs steer, they do not judge."
    )
