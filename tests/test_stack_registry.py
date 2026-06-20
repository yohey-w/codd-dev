"""F2/F3: framework & addon loader + registry + curated profiles (v3.0).

Asserts the loader parses the design taxonomy, the registry discovers/resolves
the bundled curated profiles (Next.js / Prisma / Playwright), every bundled YAML
round-trips, and kind mismatches are rejected.
"""
from __future__ import annotations

import pytest

from codd.stack.loader import StackProfileError, load_addon_profile, load_framework_profile
from codd.stack.registry import (
    ADDONS_DIR,
    FRAMEWORKS_DIR,
    UnknownLayerError,
    default_addon_registry,
    default_framework_registry,
)


def test_framework_registry_discovers_and_resolves_nextjs():
    fw = default_framework_registry
    assert "nextjs" in fw.ids()
    assert fw.resolve("nextjs").id == "nextjs"
    assert fw.resolve("next").id == "nextjs"  # alias
    assert fw.resolve("NEXT.JS").id == "nextjs"  # alias, case-insensitive


def test_addon_registry_discovers_prisma_and_playwright():
    ad = default_addon_registry
    assert set(ad.ids()) >= {"prisma", "playwright"}
    assert ad.resolve("pw").id == "playwright"  # alias
    assert ad.resolve("prisma").capability == "orm"
    assert ad.resolve("playwright").capability == "e2e"


def test_nextjs_profile_parsed_from_taxonomy():
    nx = default_framework_registry.resolve("nextjs")
    assert nx.kind == "framework"
    assert [v.id for v in nx.variants] == ["app_router", "pages_router"]
    assert nx.exclusive_variants is True
    assert len(nx.file_roles) >= 6
    assert {r.id for r in nx.requires.any_language} == {"typescript", "javascript"}
    # Next.js owns ONLY its framework slots; typecheck is the language's and
    # e2e_test is the Playwright addon's (namespace ownership, design §1).
    assert set(nx.commands) == {"dev", "framework_build", "start"}
    assert "typecheck" not in nx.commands and "e2e_test" not in nx.commands
    assert nx.detection.manifests[0].dependency == "next"


def test_nextjs_ignore_build_errors_obligation_is_release_blocking():
    """The design's anti-false-green guard: next build may not substitute for
    typecheck under ignoreBuildErrors — must be an error-severity obligation."""
    nx = default_framework_registry.resolve("nextjs")
    guard = next(o for o in nx.obligations if "ignore_build" in o.id)
    assert guard.severity == "error"
    assert guard.checker  # has an obligation-checker adapter binding
    assert guard.data.get("setting") == "typescript.ignoreBuildErrors"


def test_playwright_requires_real_execution():
    pw = default_addon_registry.resolve("playwright")
    obl = next(o for o in pw.obligations if "executed" in o.id)
    assert obl.severity == "error"  # a fully-skipped e2e run is not green


def test_all_bundled_framework_profiles_load():
    """Every shipped framework YAML must parse — a malformed curated profile
    fails CI rather than at a user's first run."""
    for path in sorted(FRAMEWORKS_DIR.glob("*.yaml")):
        prof = load_framework_profile(path)
        assert prof.kind == "framework"
        assert prof.id


def test_all_bundled_addon_profiles_load():
    for path in sorted(ADDONS_DIR.glob("*.yaml")):
        prof = load_addon_profile(path)
        assert prof.kind == "addon"
        assert prof.id


def test_kind_mismatch_is_rejected():
    """Loading a framework YAML as an addon (or vice-versa) must fail loudly."""
    nextjs_yaml = FRAMEWORKS_DIR / "nextjs.yaml"
    with pytest.raises(StackProfileError):
        load_addon_profile(nextjs_yaml)
    prisma_yaml = ADDONS_DIR / "prisma.yaml"
    with pytest.raises(StackProfileError):
        load_framework_profile(prisma_yaml)


def test_unknown_layer_raises():
    with pytest.raises(UnknownLayerError):
        default_framework_registry.resolve("svelte")
    with pytest.raises(UnknownLayerError):
        default_addon_registry.resolve("typeorm")
