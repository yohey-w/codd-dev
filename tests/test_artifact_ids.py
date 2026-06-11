"""Tests for codd.artifact_ids — the cross-space artifact-id resolver.

Covers: resolution from BOTH id spaces (catalog short ids and namespaced
required-artifact ids), lenient lookup normalization, the drift guard across
every shipped required-artifacts profile, catalog-load validation of the
mapping (id syntax, at-most-one owner per required id, intentionally-unmapped
declarations), profile loading in its accepted shapes, and the `contract show`
mapping render. All synthetic scenarios are project-agnostic.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codd.artifact_contract import CatalogError, load_catalog
from codd.artifact_ids import (
    PROFILE_DEFAULTS_DIR,
    ArtifactIdResolver,
    catalog_id_for,
    get_resolver,
    profile_required_ids,
    profile_required_ids_for_project,
    render_required_id_mapping,
    required_ids_for,
    resolve_artifact_id,
    shipped_profile_names,
    unmapped_required_ids,
)


# ---------------------------------------------------------------------------
# Synthetic catalog helper
# ---------------------------------------------------------------------------
def _write_catalog(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "catalog.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _basic_catalog_payload() -> dict:
    return {
        "version": 1,
        "artifacts": [
            {
                "id": "alpha_doc",
                "description": "a",
                "kind": "ssot",
                "produced_by": "generate",
                "default_path_globs": ["docs/**/*.md"],
                "required_artifact_ids": ["space:alpha", "space:alpha_detail"],
            },
            {
                "id": "beta_doc",
                "description": "b",
                "kind": "ssot",
                "produced_by": "plan",
                "required_artifact_ids": ["space:beta"],
            },
            {
                "id": "gamma_doc",
                "description": "c (no mapping declared)",
                "kind": "ssot",
                "produced_by": "implement",
            },
        ],
    }


# ---------------------------------------------------------------------------
# Resolution — both id spaces
# ---------------------------------------------------------------------------
def test_resolver_accepts_catalog_ids():
    artifact = resolve_artifact_id("design_spec")
    assert artifact is not None
    assert artifact.id == "design_spec"


def test_resolver_accepts_required_ids():
    artifact = resolve_artifact_id("design:system_design")
    assert artifact is not None
    assert artifact.id == "design_spec"


@pytest.mark.parametrize(
    "any_id,expected",
    [
        ("requirements", "requirements"),
        ("design:requirements", "requirements"),
        ("design:deployment_design", "deployment_design"),
        ("deployment_design", "deployment_design"),
        ("design:infrastructure_design", "infrastructure_design"),
        ("design:non_functional_requirements", "non_functional_requirements"),
        ("design:operations_runbook", "operations_runbook"),
        ("design:api_design", "design_spec"),
        ("design:screen_transitions", "design_spec"),
    ],
)
def test_same_concept_resolves_from_either_space(any_id: str, expected: str):
    """The historical disjointness is closed: both spellings reach one artifact."""

    artifact = resolve_artifact_id(any_id)
    assert artifact is not None
    assert artifact.id == expected


@pytest.mark.parametrize(
    "variant",
    [
        "design:system-design",  # DAG node ids use hyphens
        "DESIGN:SYSTEM_DESIGN",
        "  design:system_design  ",
    ],
)
def test_resolver_lookup_is_lenient_about_cosmetic_variance(variant: str):
    artifact = resolve_artifact_id(variant)
    assert artifact is not None
    assert artifact.id == "design_spec"


def test_resolver_unknown_and_empty_yield_none():
    assert resolve_artifact_id("design:nonexistent_thing") is None
    assert resolve_artifact_id("not_a_catalog_id") is None
    assert resolve_artifact_id("") is None
    assert resolve_artifact_id(None) is None


def test_catalog_id_for_and_required_ids_for_roundtrip():
    """Every declared mapping is navigable in both directions."""

    catalog = load_catalog()
    for artifact in catalog.artifacts:
        assert required_ids_for(artifact.id) == artifact.required_artifact_ids
        for required_id in artifact.required_artifact_ids:
            assert catalog_id_for(required_id) == artifact.id


def test_required_ids_for_accepts_either_space():
    via_catalog = required_ids_for("design_spec")
    via_required = required_ids_for("design:system_design")
    assert via_catalog == via_required
    assert "design:system_design" in via_catalog


def test_catalog_id_for_unmapped_is_none():
    assert catalog_id_for("design:nonexistent_thing") is None


# ---------------------------------------------------------------------------
# Drift guard — every required id across ALL shipped profiles resolves
# ---------------------------------------------------------------------------
def test_shipped_profiles_enumerate_at_least_known_five():
    names = shipped_profile_names()
    assert {"cli", "generic", "iot", "mobile", "web"}.issubset(set(names))


@pytest.mark.parametrize("profile_name", sorted(p.stem for p in PROFILE_DEFAULTS_DIR.glob("*.yaml")))
def test_every_profile_required_id_is_mapped_or_intentionally_unmapped(profile_name: str):
    """Drift guard: a future profile addition MUST be mapped in
    codd/artifacts/catalog.yaml `required_artifact_ids` or consciously declared
    in `intentionally_unmapped_required_ids` — never silently unresolvable."""

    unmapped = unmapped_required_ids(profile_name)
    assert unmapped == (), (
        f"profile '{profile_name}' declares required ids with no catalog mapping: "
        f"{unmapped}. Map each in codd/artifacts/catalog.yaml required_artifact_ids "
        "or add it to intentionally_unmapped_required_ids."
    )


def test_profile_ids_resolve_to_ssot_catalog_artifacts():
    """A required (authored) artifact never maps to a machine-built view."""

    for name in shipped_profile_names():
        for required_id in profile_required_ids(name):
            artifact = resolve_artifact_id(required_id)
            assert artifact is not None
            assert artifact.is_ssot


# ---------------------------------------------------------------------------
# Catalog-load validation of the mapping
# ---------------------------------------------------------------------------
def test_catalog_rejects_required_id_bad_syntax(tmp_path):
    payload = _basic_catalog_payload()
    payload["artifacts"][0]["required_artifact_ids"] = ["NotNamespaced"]
    with pytest.raises(CatalogError, match="invalid required_artifact_id"):
        load_catalog(_write_catalog(tmp_path, payload))


@pytest.mark.parametrize(
    "bad_id",
    ["space:Upper", "space alpha", "space:al-pha", ":alpha", "space:", "a:b:c"],
)
def test_catalog_rejects_required_id_syntax_variants(tmp_path, bad_id):
    payload = _basic_catalog_payload()
    payload["artifacts"][1]["required_artifact_ids"] = [bad_id]
    with pytest.raises(CatalogError):
        load_catalog(_write_catalog(tmp_path, payload))


def test_catalog_rejects_required_id_mapped_by_two_artifacts(tmp_path):
    payload = _basic_catalog_payload()
    payload["artifacts"][1]["required_artifact_ids"] = ["space:alpha"]  # also on alpha_doc
    with pytest.raises(CatalogError, match="at most one catalog artifact"):
        load_catalog(_write_catalog(tmp_path, payload))


def test_catalog_rejects_intentionally_unmapped_bad_syntax(tmp_path):
    payload = _basic_catalog_payload()
    payload["intentionally_unmapped_required_ids"] = ["no_namespace"]
    with pytest.raises(CatalogError, match="intentionally_unmapped_required_ids"):
        load_catalog(_write_catalog(tmp_path, payload))


def test_catalog_rejects_intentionally_unmapped_overlapping_a_mapping(tmp_path):
    payload = _basic_catalog_payload()
    payload["intentionally_unmapped_required_ids"] = ["space:alpha"]
    with pytest.raises(CatalogError, match="intentionally-unmapped"):
        load_catalog(_write_catalog(tmp_path, payload))


def test_catalog_without_mapping_fields_still_loads(tmp_path):
    """Backward compatibility: the new fields are strictly optional."""

    payload = _basic_catalog_payload()
    for entry in payload["artifacts"]:
        entry.pop("required_artifact_ids", None)
    catalog = load_catalog(_write_catalog(tmp_path, payload))
    assert catalog.intentionally_unmapped_required_ids == ()
    resolver = get_resolver(catalog)
    assert resolver.resolve("alpha_doc") is not None  # catalog space still works
    assert resolver.resolve("space:alpha") is None  # required space simply unmapped


# ---------------------------------------------------------------------------
# unmapped_required_ids diagnostics (explicit-profile shapes)
# ---------------------------------------------------------------------------
def test_unmapped_required_ids_reports_unknown_and_honors_intentional(tmp_path):
    payload = _basic_catalog_payload()
    payload["intentionally_unmapped_required_ids"] = ["space:consciously_skipped"]
    catalog = load_catalog(_write_catalog(tmp_path, payload))

    profile = [
        {"id": "space:alpha"},  # mapped
        {"id": "space:consciously_skipped"},  # intentionally unmapped — not drift
        {"id": "space:dangling"},  # DRIFT
        "space:dangling",  # duplicate — reported once
        "space:beta",  # mapped
    ]
    assert unmapped_required_ids(profile, catalog) == ("space:dangling",)


def test_unmapped_required_ids_accepts_mapping_and_path(tmp_path):
    catalog = load_catalog(_write_catalog(tmp_path, _basic_catalog_payload()))

    as_mapping = {"default_artifacts": [{"id": "space:alpha"}, {"id": "space:other"}]}
    assert unmapped_required_ids(as_mapping, catalog) == ("space:other",)

    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(yaml.safe_dump(as_mapping), encoding="utf-8")
    assert unmapped_required_ids(profile_path, catalog) == ("space:other",)


def test_unmapped_required_ids_unknown_profile_name_raises():
    with pytest.raises(FileNotFoundError):
        unmapped_required_ids("no_such_profile_xyz")


# ---------------------------------------------------------------------------
# Profile loading
# ---------------------------------------------------------------------------
def test_profile_required_ids_by_name_matches_yaml():
    payload = yaml.safe_load((PROFILE_DEFAULTS_DIR / "web.yaml").read_text(encoding="utf-8"))
    expected = tuple(entry["id"] for entry in payload["default_artifacts"])
    assert profile_required_ids("web") == expected


def test_profile_required_ids_from_iterable_of_strings():
    assert profile_required_ids(["a:b", "", "c:d"]) == ("a:b", "c:d")


def test_profile_required_ids_for_project_configured_type(tmp_path):
    config = {"required_artifacts": {"project_type": "cli"}}
    ids = profile_required_ids_for_project(tmp_path, config)
    assert ids == profile_required_ids("cli")


def test_profile_required_ids_for_project_defaults_to_generic(tmp_path):
    ids = profile_required_ids_for_project(tmp_path, {})
    assert ids == profile_required_ids("generic")


def test_profile_required_ids_for_project_custom_is_empty(tmp_path):
    """`custom` is the empty-artifacts sentinel — fail-open to no profile."""

    config = {"required_artifacts": {"project_type": "custom"}}
    assert profile_required_ids_for_project(tmp_path, config) == ()


# ---------------------------------------------------------------------------
# Rendering (`codd contract show` section)
# ---------------------------------------------------------------------------
def test_render_required_id_mapping_shipped_catalog_reports_drift_ok():
    text = render_required_id_mapping()
    assert "Required-artifact id mapping" in text
    assert "design_spec <- " in text
    assert "design:system_design" in text
    assert "Drift guard OK" in text
    assert "UNMAPPED" not in text


def test_render_required_id_mapping_lists_intentionally_unmapped(tmp_path):
    payload = _basic_catalog_payload()
    payload["intentionally_unmapped_required_ids"] = ["space:consciously_skipped"]
    catalog = load_catalog(_write_catalog(tmp_path, payload))
    text = render_required_id_mapping(catalog)
    assert "intentionally unmapped: space:consciously_skipped" in text


def test_resolver_class_direct_construction(tmp_path):
    catalog = load_catalog(_write_catalog(tmp_path, _basic_catalog_payload()))
    resolver = ArtifactIdResolver(catalog)
    assert resolver.resolve("space:alpha_detail").id == "alpha_doc"
    assert resolver.catalog_id_for("space:beta") == "beta_doc"
    assert resolver.required_ids_for("gamma_doc") == ()
    assert not resolver.is_intentionally_unmapped("space:alpha")
