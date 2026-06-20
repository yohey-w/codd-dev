"""F1: FrameworkProfile / AddonProfile schema (v3.0 composable Profile, framework half).

Mirrors the languages Phase-1 schema tests. Asserts the framework/addon profiles
construct from the design's taxonomy, are immutable contracts, and satisfy the
structural ``LayerProfile`` Protocol the composition layer depends on — while the
existing language layer is untouched (additive).
"""
from __future__ import annotations

import dataclasses

import pytest

import codd.stack as s


def _nextjs() -> s.FrameworkProfile:
    return s.FrameworkProfile(
        identity=s.LayerIdentity(
            id="nextjs", kind="framework", display_name="Next.js", aliases=("next",)
        ),
        requires=s.LayerRequirements(
            any_language=(s.LanguageRequirement(id="typescript", version=">=5.1"),)
        ),
        detection=s.Detection(
            manifests=(s.DetectionManifest(file="package.json", dependency="next"),),
            files=("next.config.*", "app/**"),
        ),
        variants=(s.Variant(id="app_router"), s.Variant(id="pages_router")),
        file_roles=(s.FileRole(pattern="app/**/page.tsx", role="route_page"),),
        commands={"build": s.CommandSpec(id="build", argv=("next", "build"))},
        obligations=(
            s.Obligation(id="no_ignore_build_errors", severity="error"),
        ),
        adapters={"route_resolver": s.AdapterRef(kind="route_resolver", id="nextjs")},
    )


def test_framework_profile_constructs_from_taxonomy():
    fw = _nextjs()
    assert fw.id == "nextjs"
    assert fw.kind == "framework"
    assert [v.id for v in fw.variants] == ["app_router", "pages_router"]
    assert fw.exclusive_variants is True
    assert fw.requires.any_language[0].id == "typescript"
    assert fw.commands["build"].argv == ("next", "build")
    assert fw.obligations[0].severity == "error"


def test_framework_profile_is_frozen():
    fw = _nextjs()
    with pytest.raises(dataclasses.FrozenInstanceError):
        fw.obligations = ()  # type: ignore[misc]


def test_framework_matches_id_and_alias_case_insensitive():
    fw = _nextjs()
    assert fw.matches("nextjs")
    assert fw.matches("Next")  # alias, case-insensitive
    assert not fw.matches("rails")


def test_addon_profile_constructs_with_narrower_surface():
    addon = s.AddonProfile(
        identity=s.LayerIdentity(id="prisma", kind="addon", aliases=()),
        capability="orm",
    )
    assert addon.id == "prisma"
    assert addon.kind == "addon"
    assert addon.capability == "orm"


def test_both_satisfy_layer_profile_protocol():
    fw = _nextjs()
    addon = s.AddonProfile(identity=s.LayerIdentity(id="playwright", kind="addon"), capability="e2e")
    # The composition layer depends only on this structural Protocol.
    assert isinstance(fw, s.LayerProfile)
    assert isinstance(addon, s.LayerProfile)


def test_language_layer_unaffected_by_additive_stack_module():
    # Importing the framework half must not perturb the language registry.
    from codd.languages.registry import default_registry

    assert set(default_registry.ids()) >= {"go", "python", "typescript"}
