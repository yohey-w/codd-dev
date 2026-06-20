"""Load a ``FrameworkProfile`` / ``AddonProfile`` from a YAML file.

Mirrors ``codd/languages/loader.py`` (and reuses its primitive parsers for the
shared types — commands, reports, source sets). Parsing rules are identical:
PyYAML ``safe_load``; placeholder templates kept literal; unknown top-level keys
preserved under ``.extra`` and the whole document under ``.raw``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import yaml

# Reuse the language loader's primitive helpers + shared-type parsers.
from codd.languages.loader import (
    LanguageProfileError,
    _as_mapping,
    _as_str_tuple,
    _as_tuple,
    _parse_commands,
    _parse_report,
    _parse_source_set,
    _require,
)

from .profile import (
    AddonProfile,
    AdapterRef,
    AssertionsSpec,
    ConformanceSpec,
    Detection,
    DetectionManifest,
    FileRole,
    FrameworkProfile,
    LanguageRequirement,
    LayerArtifactsSpec,
    LayerIdentity,
    LayerLayoutSpec,
    LayerRequirements,
    Obligation,
    OperationsSpec,
    Variant,
)


class StackProfileError(LanguageProfileError):
    """Raised when a framework/addon profile YAML is malformed.

    Subclasses :class:`LanguageProfileError` so a caller can catch the one
    profile-load error type for both halves of the stack.
    """


# ---------------------------------------------------------------------------
# section parsers (framework/addon-specific)
# ---------------------------------------------------------------------------


def _parse_layer_identity(doc: Mapping[str, Any], *, expected_kind: str) -> LayerIdentity:
    where = "identity"
    layer_id = _require(doc, "id", where=where)
    kind = doc.get("kind", expected_kind)
    if kind != expected_kind:
        raise StackProfileError(
            f"{where}: expected kind '{expected_kind}', got {kind!r}"
        )
    strictness = doc.get("strictness", "strict")
    if strictness not in ("strict", "legacy_compatible"):
        raise StackProfileError(
            f"{where}: strictness must be 'strict' or 'legacy_compatible', got {strictness!r}"
        )
    return LayerIdentity(
        id=str(layer_id),
        kind=kind,
        display_name=str(doc.get("display_name") or layer_id),
        aliases=_as_str_tuple(doc.get("aliases")),
        schema_version=str(doc.get("schema_version", "1")),
        profile_version=str(doc.get("profile_version", "0.1.0")),
        strictness=strictness,
    )


def _parse_requires(doc: Mapping[str, Any]) -> LayerRequirements:
    m = _as_mapping(doc.get("requires"), where="requires")
    any_lang = []
    for i, raw in enumerate(_as_tuple(m.get("any_language"))):
        lm = _as_mapping(raw, where=f"requires.any_language[{i}]")
        any_lang.append(
            LanguageRequirement(
                id=str(_require(lm, "id", where=f"requires.any_language[{i}]")),
                version=(str(lm["version"]) if lm.get("version") is not None else None),
            )
        )
    return LayerRequirements(
        any_language=tuple(any_lang),
        runtime=_as_mapping(m.get("runtime"), where="requires.runtime"),
        addons=_as_str_tuple(m.get("addons")),
    )


def _parse_detection(doc: Mapping[str, Any]) -> Detection:
    m = _as_mapping(doc.get("detection"), where="detection")
    manifests = []
    for i, raw in enumerate(_as_tuple(m.get("manifests"))):
        dm = _as_mapping(raw, where=f"detection.manifests[{i}]")
        manifests.append(
            DetectionManifest(
                file=str(_require(dm, "file", where=f"detection.manifests[{i}]")),
                dependency=(str(dm["dependency"]) if dm.get("dependency") is not None else None),
            )
        )
    return Detection(manifests=tuple(manifests), files=_as_str_tuple(m.get("files")))


def _parse_variants(doc: Mapping[str, Any]) -> tuple[Variant, ...]:
    variants = []
    for i, raw in enumerate(_as_tuple(doc.get("variants"))):
        vm = _as_mapping(raw, where=f"variants[{i}]")
        variants.append(
            Variant(
                id=str(_require(vm, "id", where=f"variants[{i}]")),
                detect=_as_mapping(vm.get("detect"), where=f"variants[{i}].detect"),
            )
        )
    return tuple(variants)


def _parse_layer_layout(doc: Mapping[str, Any]) -> LayerLayoutSpec:
    m = _as_mapping(doc.get("layout"), where="layout")
    source_sets = tuple(
        _parse_source_set(s, idx=i) for i, s in enumerate(_as_tuple(m.get("source_sets")))
    )
    return LayerLayoutSpec(
        source_sets=source_sets,
        generated=_as_str_tuple(m.get("generated")),
        ignored=_as_str_tuple(m.get("ignored")),
    )


def _parse_file_roles(doc: Mapping[str, Any]) -> tuple[FileRole, ...]:
    roles = []
    for i, raw in enumerate(_as_tuple(doc.get("file_roles"))):
        rm = _as_mapping(raw, where=f"file_roles[{i}]")
        roles.append(
            FileRole(
                pattern=str(_require(rm, "pattern", where=f"file_roles[{i}]")),
                role=str(_require(rm, "role", where=f"file_roles[{i}]")),
            )
        )
    return tuple(roles)


def _parse_operations(doc: Mapping[str, Any]) -> OperationsSpec:
    m = _as_mapping(doc.get("operations"), where="operations")
    return OperationsSpec(
        route_discovery=_as_mapping(m.get("route_discovery"), where="operations.route_discovery"),
        operation_flow=_as_mapping(m.get("operation_flow"), where="operations.operation_flow"),
    )


def _parse_assertions(doc: Mapping[str, Any]) -> AssertionsSpec:
    m = _as_mapping(doc.get("assertions"), where="assertions")
    extra = {k: v for k, v in m.items() if k != "idioms"}
    return AssertionsSpec(idioms=_as_str_tuple(m.get("idioms")), extra=extra)


def _parse_layer_artifacts(doc: Mapping[str, Any]) -> LayerArtifactsSpec:
    m = _as_mapping(doc.get("artifacts"), where="artifacts")
    reports = tuple(
        r for r in (_parse_report(raw) for raw in _as_tuple(m.get("reports"))) if r is not None
    )
    return LayerArtifactsSpec(build_outputs=_as_str_tuple(m.get("build_outputs")), reports=reports)


def _parse_obligations(doc: Mapping[str, Any]) -> tuple[Obligation, ...]:
    obligations = []
    for i, raw in enumerate(_as_tuple(doc.get("obligations"))):
        om = _as_mapping(raw, where=f"obligations[{i}]")
        severity = om.get("severity", "error")
        if severity not in ("error", "warn"):
            raise StackProfileError(f"obligations[{i}]: severity must be 'error' or 'warn', got {severity!r}")
        known = {"id", "description", "checker", "severity"}
        obligations.append(
            Obligation(
                id=str(_require(om, "id", where=f"obligations[{i}]")),
                description=str(om.get("description", "")),
                checker=(str(om["checker"]) if om.get("checker") is not None else None),
                severity=severity,
                data={k: v for k, v in om.items() if k not in known},
            )
        )
    return tuple(obligations)


def _parse_adapters(doc: Mapping[str, Any]) -> dict[str, AdapterRef]:
    m = _as_mapping(doc.get("adapters"), where="adapters")
    # Each role maps to an adapter reference string (e.g. "nextjs_adapter:routes").
    return {str(role): AdapterRef(kind=str(role), id=str(ref)) for role, ref in m.items()}


def _parse_conformance(doc: Mapping[str, Any]) -> ConformanceSpec | None:
    raw = doc.get("conformance")
    if raw is None:
        return None
    m = _as_mapping(raw, where="conformance")
    fixtures = tuple(
        _as_mapping(f, where=f"conformance.fixtures[{i}]")
        for i, f in enumerate(_as_tuple(m.get("fixtures")))
    )
    return ConformanceSpec(
        fixtures=fixtures,
        adapter=(str(m["adapter"]) if m.get("adapter") is not None else None),
    )


# ---------------------------------------------------------------------------
# public entry points
# ---------------------------------------------------------------------------

_FRAMEWORK_KNOWN = frozenset(
    {
        "id", "kind", "display_name", "aliases", "schema_version", "profile_version",
        "strictness", "requires", "detection", "variants", "exclusive_variants",
        "layout", "file_roles", "operations", "commands", "assertions", "artifacts",
        "obligations", "adapters", "conformance", "provides", "trust",
    }
)
_ADDON_KNOWN = frozenset(
    {
        "id", "kind", "display_name", "aliases", "schema_version", "profile_version",
        "strictness", "requires", "detection", "capability", "commands", "obligations",
        "artifacts", "adapters", "conformance", "provides", "trust",
    }
)


def _load_doc(path: str | Path) -> tuple[Path, dict[str, Any]]:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover
        raise StackProfileError(f"cannot read stack profile {p}: {exc}") from exc
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise StackProfileError(f"invalid YAML in stack profile {p}: {exc}") from exc
    if doc is None:
        raise StackProfileError(f"stack profile {p} is empty")
    if not isinstance(doc, Mapping):
        raise StackProfileError(
            f"stack profile {p} must be a mapping at the top level, got {type(doc).__name__}"
        )
    return p, dict(doc)


def load_framework_profile(path: str | Path) -> FrameworkProfile:
    """Parse a framework-profile YAML into a :class:`FrameworkProfile`."""
    _p, doc = _load_doc(path)
    extra = {k: v for k, v in doc.items() if k not in _FRAMEWORK_KNOWN}
    return FrameworkProfile(
        identity=_parse_layer_identity(doc, expected_kind="framework"),
        requires=_parse_requires(doc),
        detection=_parse_detection(doc),
        variants=_parse_variants(doc),
        exclusive_variants=bool(doc.get("exclusive_variants", True)),
        layout=_parse_layer_layout(doc),
        file_roles=_parse_file_roles(doc),
        operations=_parse_operations(doc),
        commands=_parse_commands(doc),
        assertions=_parse_assertions(doc),
        artifacts=_parse_layer_artifacts(doc),
        obligations=_parse_obligations(doc),
        adapters=_parse_adapters(doc),
        conformance=_parse_conformance(doc),
        provides=_as_mapping(doc.get("provides"), where="provides"),
        trust=_as_mapping(doc.get("trust"), where="trust"),
        extra=extra,
        raw=dict(doc),
    )


def load_addon_profile(path: str | Path) -> AddonProfile:
    """Parse an addon-profile YAML into an :class:`AddonProfile`."""
    _p, doc = _load_doc(path)
    extra = {k: v for k, v in doc.items() if k not in _ADDON_KNOWN}
    return AddonProfile(
        identity=_parse_layer_identity(doc, expected_kind="addon"),
        requires=_parse_requires(doc),
        detection=_parse_detection(doc),
        capability=str(doc.get("capability", "")),
        commands=_parse_commands(doc),
        obligations=_parse_obligations(doc),
        artifacts=_parse_layer_artifacts(doc),
        adapters=_parse_adapters(doc),
        conformance=_parse_conformance(doc),
        provides=_as_mapping(doc.get("provides"), where="provides"),
        trust=_as_mapping(doc.get("trust"), where="trust"),
        extra=extra,
        raw=dict(doc),
    )
