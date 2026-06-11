"""Cross-space artifact-id resolution — ONE resolver for both id spaces.

CoDD names design artifacts in two historically disjoint id spaces:

* **Catalog ids** (SHORT): ``requirements``, ``design_spec``,
  ``infrastructure_design`` … — declared by ``codd/artifacts/catalog.yaml`` and
  consumed by the artifact contract (verify / suggest / adopt, stage gates) and
  the restoration report.
* **Required-artifact ids** (NAMESPACED, ``category:name``):
  ``design:requirements``, ``design:system_design``,
  ``design:infrastructure_design`` … — declared by
  ``codd/required_artifacts/defaults/*.yaml`` and consumed by the
  required-artifacts deriver and the plan/wave flows.

Before this module the two spaces never matched — even the same concept
(``deployment_design`` vs ``design:deployment_design``) was unresolvable, so
contract verification and the required-artifacts flow could not reconcile.

The mapping itself is DATA, not code: each catalog entry's optional
``required_artifact_ids`` field links it to the required-artifact ids it
covers (validated on catalog load: id syntax + at-most-one catalog owner per
required id). Required ids with no catalog counterpart must be declared in the
catalog's ``intentionally_unmapped_required_ids`` — anything neither mapped nor
declared is reported by :func:`unmapped_required_ids` (the drift guard), never
silently dropped.

Lookups are lenient about cosmetic variance (case, surrounding whitespace,
hyphen/underscore — DAG node ids conventionally use hyphens, e.g.
``design:system-design``), while the catalog-side declarations stay strict.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from codd.artifact_contract import (
    ArtifactCatalog,
    CatalogArtifact,
    load_catalog,
)

# The shipped required-artifacts profiles (one YAML per project type).
PROFILE_DEFAULTS_DIR = Path(__file__).parent / "required_artifacts" / "defaults"


def _normalize(any_id: Any) -> str:
    """Normalize an id for lookup: lowercase, strip, hyphens → underscores.

    Tolerates the cosmetic variance between declared ids
    (``design:system_design``) and DAG node ids (``design:system-design``).
    """

    return str(any_id or "").strip().lower().replace("-", "_")


class ArtifactIdResolver:
    """Resolves ids from EITHER space to the canonical catalog artifact."""

    def __init__(self, catalog: ArtifactCatalog):
        self.catalog = catalog
        # normalized required id -> catalog id (uniqueness enforced at load).
        self._required_to_catalog: dict[str, str] = {}
        # normalized catalog id -> catalog id (exact-id fast path).
        self._catalog_by_norm: dict[str, str] = {}
        for artifact in catalog.artifacts:
            self._catalog_by_norm[_normalize(artifact.id)] = artifact.id
            for required_id in artifact.required_artifact_ids:
                self._required_to_catalog[_normalize(required_id)] = artifact.id
        self._intentionally_unmapped = frozenset(
            _normalize(r) for r in catalog.intentionally_unmapped_required_ids
        )

    # -- resolution ----------------------------------------------------------
    def resolve(self, any_id: Any) -> CatalogArtifact | None:
        """Resolve a catalog id OR a required-artifact id to its catalog artifact.

        Returns ``None`` for unknown / intentionally-unmapped / empty ids.
        """

        key = _normalize(any_id)
        if not key:
            return None
        catalog_id = self._catalog_by_norm.get(key)
        if catalog_id is None:
            catalog_id = self._required_to_catalog.get(key)
        return self.catalog.get(catalog_id) if catalog_id else None

    def catalog_id_for(self, required_id: Any) -> str | None:
        """The catalog id covering ``required_id`` (None when unmapped)."""

        return self._required_to_catalog.get(_normalize(required_id))

    def required_ids_for(self, catalog_id: Any) -> tuple[str, ...]:
        """The required-artifact ids a catalog artifact covers (declared order).

        Accepts either space (a required id is resolved to its catalog artifact
        first). Unknown ids yield ``()``.
        """

        artifact = self.resolve(catalog_id)
        return artifact.required_artifact_ids if artifact is not None else ()

    def is_intentionally_unmapped(self, required_id: Any) -> bool:
        return _normalize(required_id) in self._intentionally_unmapped

    # -- diagnostics (the drift guard) ----------------------------------------
    def unmapped_required_ids(self, profile: Any) -> tuple[str, ...]:
        """Required ids in ``profile`` that are neither mapped to a catalog
        artifact nor declared intentionally-unmapped.

        ``profile`` may be a profile name (loaded from the shipped defaults),
        a path to a profile YAML, a parsed profile mapping, or an iterable of
        required ids / artifact entries. The returned ids are reported in the
        profile's declared order, de-duplicated, original spelling preserved.
        """

        out: list[str] = []
        seen: set[str] = set()
        for required_id in profile_required_ids(profile):
            key = _normalize(required_id)
            if key in seen:
                continue
            seen.add(key)
            if key in self._required_to_catalog:
                continue
            if key in self._intentionally_unmapped:
                continue
            out.append(required_id)
        return tuple(out)


# ---------------------------------------------------------------------------
# Default-resolver convenience API (module-level functions)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _default_resolver() -> ArtifactIdResolver:
    return ArtifactIdResolver(load_catalog())


def get_resolver(catalog: ArtifactCatalog | None = None) -> ArtifactIdResolver:
    """The resolver for ``catalog`` (default: the shipped catalog, cached)."""

    if catalog is None:
        return _default_resolver()
    return ArtifactIdResolver(catalog)


def resolve_artifact_id(
    any_id: Any, catalog: ArtifactCatalog | None = None
) -> CatalogArtifact | None:
    """Resolve an id from EITHER space to its canonical catalog artifact."""

    return get_resolver(catalog).resolve(any_id)


def catalog_id_for(required_id: Any, catalog: ArtifactCatalog | None = None) -> str | None:
    """The catalog id covering a required-artifact id (None when unmapped)."""

    return get_resolver(catalog).catalog_id_for(required_id)


def required_ids_for(catalog_id: Any, catalog: ArtifactCatalog | None = None) -> tuple[str, ...]:
    """The required-artifact ids a catalog artifact covers."""

    return get_resolver(catalog).required_ids_for(catalog_id)


def unmapped_required_ids(profile: Any, catalog: ArtifactCatalog | None = None) -> tuple[str, ...]:
    """Drift guard: profile required ids that resolve to nothing (see class doc)."""

    return get_resolver(catalog).unmapped_required_ids(profile)


# ---------------------------------------------------------------------------
# Profile loading (required_artifacts defaults)
# ---------------------------------------------------------------------------
def shipped_profile_names() -> tuple[str, ...]:
    """Names of the shipped required-artifacts profiles (e.g. web, cli …)."""

    return tuple(sorted(p.stem for p in PROFILE_DEFAULTS_DIR.glob("*.yaml")))


def profile_required_ids(profile: Any) -> tuple[str, ...]:
    """Extract the required-artifact ids declared by a profile.

    Accepts, in order of recognition:

    * a profile *name* (``"web"``) — loaded from the shipped defaults dir;
    * a *path* to a profile YAML (``.../my_profile.yaml``);
    * a parsed profile *mapping* (``{"default_artifacts": [...]}``);
    * an *iterable* of artifact entries (mappings with ``id``) or plain id
      strings.
    """

    if isinstance(profile, (str, Path)):
        path = Path(profile)
        if path.suffix not in (".yaml", ".yml") and not path.exists():
            path = PROFILE_DEFAULTS_DIR / f"{profile}.yaml"
        if not path.exists():
            raise FileNotFoundError(f"required-artifacts profile not found: {profile}")
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return profile_required_ids(payload)

    if isinstance(profile, Mapping):
        return profile_required_ids(profile.get("default_artifacts") or [])

    if isinstance(profile, Iterable):
        ids: list[str] = []
        for entry in profile:
            if isinstance(entry, Mapping):
                entry_id = str(entry.get("id") or "").strip()
            else:
                entry_id = str(entry or "").strip()
            if entry_id:
                ids.append(entry_id)
        return tuple(ids)

    return ()


def profile_required_ids_for_project(
    project_root: str | Path,
    config: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Best-effort required ids for the project's resolvable profile.

    Resolves the project type the same way the required-artifacts flow does
    (configured ``required_artifacts.project_type`` / ``project.type`` /
    ``project_type``, else the generic baseline) and loads that profile's ids.
    Fail-open: anything unresolvable (including ``custom``, the
    empty-artifacts sentinel) yields ``()``.
    """

    configured = ""
    if isinstance(config, Mapping):
        section = config.get("required_artifacts")
        if isinstance(section, Mapping):
            configured = str(section.get("project_type") or "").strip().lower()
        if not configured:
            project = config.get("project")
            if isinstance(project, Mapping):
                configured = str(project.get("type") or "").strip().lower()
        if not configured:
            configured = str(config.get("project_type") or "").strip().lower()

    try:
        from codd.project_types import resolve_project_type

        resolved, _reason = resolve_project_type(configured or None, None, Path(project_root))
    except Exception:
        resolved = configured or "generic"

    try:
        return profile_required_ids(resolved)
    except (FileNotFoundError, OSError, ValueError, yaml.YAMLError):
        return ()


# ---------------------------------------------------------------------------
# Rendering — `codd contract show` mapping/drift section
# ---------------------------------------------------------------------------
def render_required_id_mapping(catalog: ArtifactCatalog | None = None) -> str:
    """Readable cross-space mapping + drift-guard status for `contract show`."""

    resolver = get_resolver(catalog)
    cat = resolver.catalog

    lines = ["\nRequired-artifact id mapping (catalog id <- required_artifacts ids):"]
    mapped_any = False
    for artifact in cat.artifacts:
        if not artifact.required_artifact_ids:
            continue
        mapped_any = True
        lines.append(
            f"  {artifact.id} <- {', '.join(artifact.required_artifact_ids)}"
        )
    if not mapped_any:
        lines.append("  (no catalog artifact declares required_artifact_ids)")

    if cat.intentionally_unmapped_required_ids:
        lines.append(
            "  intentionally unmapped: "
            + ", ".join(cat.intentionally_unmapped_required_ids)
        )

    # Drift guard across the shipped profiles.
    unmapped_by_profile: dict[str, tuple[str, ...]] = {}
    profiles = shipped_profile_names()
    for name in profiles:
        try:
            unmapped = resolver.unmapped_required_ids(name)
        except (FileNotFoundError, OSError, ValueError, yaml.YAMLError):
            continue
        if unmapped:
            unmapped_by_profile[name] = unmapped

    if unmapped_by_profile:
        lines.append("  UNMAPPED required id(s) — map them in artifacts/catalog.yaml")
        lines.append("  required_artifact_ids or declare intentionally_unmapped_required_ids:")
        for name, unmapped in unmapped_by_profile.items():
            lines.append(f"    [{name}] {', '.join(unmapped)}")
    else:
        lines.append(
            f"  Drift guard OK: every required id across {len(profiles)} shipped "
            "profile(s) is mapped or intentionally unmapped."
        )
    return "\n".join(lines)
