"""F4: stack composition + codd.stack.lock (v3.0 composable Profile).

Asserts the curated stack composes clean with correct namespace ownership, that
conflicts (command / semantic) are surfaced (never silently resolved), that the
resolved-contract hash is deterministic, and that the lock pins it (CI reds on
drift). The cardinal anti-false-green guard: a layer may not silently replace a
verification command (typecheck/e2e/...) — that is a command conflict.
"""
from __future__ import annotations

from codd.languages.registry import default_registry as LANG
from codd.stack.compose import compose
from codd.stack.lock import build_lock, dump_lock, parse_lock, verify_lock
from codd.stack.profile import (
    AddonProfile,
    CommandSpec,
    FrameworkProfile,
    LayerIdentity,
    Obligation,
)
from codd.stack.registry import default_addon_registry as ADDON
from codd.stack.registry import default_framework_registry as FW


def _curated():
    ts = LANG.resolve("typescript")
    nx = FW.resolve("nextjs")
    return compose(ts, [nx], [ADDON.resolve("prisma"), ADDON.resolve("playwright")])


def test_curated_stack_composes_clean():
    c = _curated()
    assert c.stack_id == "typescript+nextjs+prisma+playwright"
    assert c.is_clean and c.strict_ok
    assert not c.conflicts


def test_command_namespace_ownership():
    c = _curated()
    # Each layer owns its slots; nothing is silently overridden.
    assert c.command_owners["typecheck"] == "language:typescript"
    assert c.command_owners["framework_build"] == "framework:nextjs"
    assert c.command_owners["e2e_test"] == "addon:playwright"
    assert {"dev", "start", "generate"} <= set(c.commands)


def test_obligations_unioned_from_all_layers():
    c = _curated()
    ids = {o.id for o in c.obligations}
    assert "no_ignore_build_errors_as_typecheck" in ids  # framework anti-false-green guard
    assert "client_in_sync_with_schema" in ids  # prisma
    assert "e2e_actually_executed" in ids  # playwright


def test_compose_is_deterministic():
    assert _curated().content_hash == _curated().content_hash


def test_identical_command_argv_is_not_a_conflict():
    ts = LANG.resolve("typescript")
    # A framework re-declaring typecheck with the SAME argv as the language is harmless.
    same = FrameworkProfile(
        identity=LayerIdentity(id="echo", kind="framework"),
        commands={"typecheck": ts.commands["typecheck"]},
    )
    c = compose(ts, [same])
    assert c.is_clean


def test_command_conflict_on_verification_slot_is_detected():
    ts = LANG.resolve("typescript")
    bad = FrameworkProfile(
        identity=LayerIdentity(id="badfw", kind="framework"),
        commands={"typecheck": CommandSpec(id="typecheck", argv=("tsc", "--different"))},
    )
    c = compose(ts, [bad])
    assert any(x.kind == "command" for x in c.conflicts)
    assert not c.strict_ok  # strict mode reds on the conflict


def test_semantic_conflict_addon_weakening_obligation_is_detected():
    ts = LANG.resolve("typescript")
    strong = FrameworkProfile(
        identity=LayerIdentity(id="sfw", kind="framework"),
        obligations=(Obligation(id="must_hold", severity="error"),),
    )
    weak = AddonProfile(
        identity=LayerIdentity(id="wad", kind="addon"),
        obligations=(Obligation(id="must_hold", severity="warn"),),
    )
    c = compose(ts, [strong], [weak])
    assert any(x.kind == "semantic" for x in c.conflicts)
    assert not c.strict_ok


def test_lock_roundtrips_and_verifies():
    c = _curated()
    lock = parse_lock(dump_lock(build_lock(c)))
    ok, diffs = verify_lock(c, lock)
    assert ok and not diffs


def test_lock_catches_contract_drift():
    ts = LANG.resolve("typescript")
    full = _curated()
    lock = build_lock(full)
    # A different resolved stack (dropped the addons) must fail against the lock.
    drifted = compose(ts, [FW.resolve("nextjs")])
    ok, diffs = verify_lock(drifted, lock)
    assert not ok and diffs


# --- resolve from a stack declaration (the codd.yaml stack: block) -----------

from codd.languages.registry import UnknownLanguageError  # noqa: E402
from codd.stack.registry import UnknownLayerError  # noqa: E402
from codd.stack.resolve import resolve_stack, resolve_stack_from_declaration  # noqa: E402


def test_resolve_stack_by_ids_matches_direct_compose():
    by_ids = resolve_stack("typescript", ["nextjs"], ["prisma", "playwright"])
    assert by_ids.stack_id == _curated().stack_id
    assert by_ids.content_hash == _curated().content_hash
    assert by_ids.is_clean


def test_resolve_stack_from_declaration():
    decl = {"language": "typescript", "frameworks": ["next"], "addons": ["prisma", "pw"]}
    c = resolve_stack_from_declaration(decl)  # aliases resolve
    assert c.stack_id == "typescript+nextjs+prisma+playwright"
    assert c.is_clean


def test_resolve_stack_from_declaration_language_only():
    c = resolve_stack_from_declaration({"language": "typescript"})
    assert c.stack_id == "typescript"


def test_resolve_stack_unknown_raises():
    import pytest

    with pytest.raises(UnknownLanguageError):
        resolve_stack("cobol")
    with pytest.raises(UnknownLayerError):
        resolve_stack("typescript", ["svelte"])


def test_resolve_stack_from_declaration_requires_language():
    import pytest

    with pytest.raises(ValueError):
        resolve_stack_from_declaration({"frameworks": ["nextjs"]})
