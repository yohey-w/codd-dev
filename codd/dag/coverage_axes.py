"""Coverage axis declarations used by DAG checks."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import fnmatch
from pathlib import Path
from typing import Any, Literal
import warnings

import yaml

from codd.dag import Node


Criticality = Literal["critical", "high", "medium", "info"]
AxisSource = Literal["design_doc", "lexicon", "llm_derived"]
VALID_CRITICALITIES = {"critical", "high", "medium", "info"}
VALID_SOURCES = {"design_doc", "lexicon", "llm_derived"}


@dataclass
class JourneyScope:
    """Opt-in declaration of which journeys a coverage axis/variant applies to.

    ``include``: journey-name patterns (``fnmatch``) the declarer applies to.
        ``None`` means "no restriction" (every journey); an empty list means
        "no journeys".
    ``exclude``: journey-name patterns the declarer does not apply to,
        evaluated after ``include``.

    When no scope is declared anywhere, checks must keep their historical
    behavior (the full axis x journey cross product).
    """

    include: list[str] | None = None
    exclude: list[str] = field(default_factory=list)

    def applies_to(self, journey_name: str) -> bool:
        name = str(journey_name or "").strip()
        if self.include is not None and not _matches_any_pattern(name, self.include):
            return False
        return not _matches_any_pattern(name, self.exclude)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.include is not None:
            payload["include"] = list(self.include)
        if self.exclude:
            payload["exclude"] = list(self.exclude)
        return payload

    @classmethod
    def from_value(cls, value: Any) -> "JourneyScope | None":
        if value is None:
            return None
        if isinstance(value, str):
            return cls(include=_pattern_list(value))
        if isinstance(value, list):
            return cls(include=_pattern_list(value))
        if isinstance(value, dict):
            include = value.get("include")
            exclude = value.get("exclude")
            if include is None and exclude is None:
                raise ValueError("journey_scope mapping requires include and/or exclude")
            return cls(
                include=_pattern_list(include) if include is not None else None,
                exclude=_pattern_list(exclude) if exclude is not None else [],
            )
        raise ValueError("journey_scope must be a string, list, or mapping")


def _pattern_list(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple, set)):
        patterns: list[str] = []
        for item in value:
            if not isinstance(item, (str, int, float)):
                raise ValueError("journey scope patterns must be strings")
            text = str(item).strip()
            if text:
                patterns.append(text)
        return patterns
    raise ValueError("journey scope patterns must be a string or a list of strings")


def _matches_any_pattern(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def _journey_scope_or_warn(payload: dict[str, Any], owner: str) -> JourneyScope | None:
    try:
        return JourneyScope.from_value(payload.get("journey_scope"))
    except ValueError as exc:
        warnings.warn(f"{owner} journey_scope ignored: {exc}", UserWarning, stacklevel=3)
        return None


@dataclass
class CoverageVariant:
    id: str
    label: str
    attributes: dict[str, Any] = field(default_factory=dict)
    criticality: Criticality | None = "medium"
    journey_scope: JourneyScope | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "attributes": deepcopy(self.attributes),
            "criticality": self.criticality,
        }
        if self.journey_scope is not None:
            payload["journey_scope"] = self.journey_scope.to_dict()
        return payload

    @classmethod
    def from_dict(cls, payload: Any) -> "CoverageVariant":
        if isinstance(payload, str):
            return cls(id=payload, label=payload, criticality=None)
        if not isinstance(payload, dict):
            raise ValueError("coverage variant must be a mapping or string")

        variant_id = str(payload.get("id") or "").strip()
        if not variant_id:
            raise ValueError("coverage variant missing id")

        label = str(payload.get("label") or variant_id).strip()
        attributes = payload.get("attributes", {})
        if not isinstance(attributes, dict):
            attributes = {}

        raw_criticality = payload.get("criticality")
        criticality = _criticality_or_none(raw_criticality)
        return cls(
            id=variant_id,
            label=label,
            attributes=deepcopy(attributes),
            criticality=criticality,
            journey_scope=_journey_scope_or_warn(payload, f"coverage variant {variant_id}"),
        )


@dataclass
class CoverageAxis:
    axis_type: str
    rationale: str
    variants: list[CoverageVariant]
    source: AxisSource
    owner_section: str = ""
    journey_scope: JourneyScope | None = None

    def journey_scope_for(self, variant: CoverageVariant) -> JourneyScope | None:
        """Effective scope for a variant: variant declaration wins, then the
        axis-level declaration; ``None`` keeps the historical full cross
        product."""
        if variant.journey_scope is not None:
            return variant.journey_scope
        return self.journey_scope

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "axis_type": self.axis_type,
            "rationale": self.rationale,
            "variants": [variant.to_dict() for variant in self.variants],
            "source": self.source,
            "owner_section": self.owner_section,
        }
        if self.journey_scope is not None:
            payload["journey_scope"] = self.journey_scope.to_dict()
        return payload

    @classmethod
    def from_dict(
        cls,
        payload: Any,
        *,
        default_source: AxisSource | None = None,
        default_owner_section: str = "",
    ) -> "CoverageAxis":
        if not isinstance(payload, dict):
            raise ValueError("coverage axis must be a mapping")

        axis_type = str(payload.get("axis_type") or "").strip()
        if not axis_type:
            raise ValueError("coverage axis missing axis_type")

        source = _source_or_default(payload.get("source"), default_source)
        raw_variants = payload.get("variants", [])
        if not isinstance(raw_variants, list):
            raise ValueError("coverage axis variants must be a list")

        variants: list[CoverageVariant] = []
        for index, item in enumerate(raw_variants):
            try:
                variants.append(CoverageVariant.from_dict(item))
            except ValueError as exc:
                warnings.warn(f"coverage axis {axis_type} variant {index} ignored: {exc}", UserWarning, stacklevel=2)

        return cls(
            axis_type=axis_type,
            rationale=str(payload.get("rationale") or "").strip(),
            variants=variants,
            source=source,
            owner_section=str(payload.get("owner_section") or default_owner_section).strip(),
            journey_scope=_journey_scope_or_warn(payload, f"coverage axis {axis_type}"),
        )


def extract_coverage_axes_from_lexicon(project_lexicon_path: Path) -> list[CoverageAxis]:
    if not project_lexicon_path.is_file():
        return []

    payload = yaml.safe_load(project_lexicon_path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        return []

    return _axes_from_entries(
        payload.get("coverage_axes", []),
        default_source="lexicon",
        default_owner_section=project_lexicon_path.name,
    )


def extract_coverage_axes_from_design_doc(design_doc_node: Node) -> list[CoverageAxis]:
    entries = design_doc_node.attributes.get("coverage_axes", [])
    return _axes_from_entries(
        entries,
        default_source="design_doc",
        default_owner_section=design_doc_node.id,
    )


def _axes_from_entries(
    entries: Any,
    *,
    default_source: AxisSource,
    default_owner_section: str,
) -> list[CoverageAxis]:
    if entries is None:
        return []
    if not isinstance(entries, list):
        warnings.warn("coverage_axes must be a list; ignoring value", UserWarning, stacklevel=2)
        return []

    axes: list[CoverageAxis] = []
    for index, item in enumerate(entries):
        try:
            axes.append(
                CoverageAxis.from_dict(
                    item,
                    default_source=default_source,
                    default_owner_section=default_owner_section,
                )
            )
        except ValueError as exc:
            warnings.warn(f"coverage axis {index} ignored: {exc}", UserWarning, stacklevel=2)
    return axes


def _criticality_or_none(value: Any) -> Criticality | None:
    if value is None:
        return None
    text = str(value).strip()
    if text in VALID_CRITICALITIES:
        return text  # type: ignore[return-value]
    return None


def _source_or_default(value: Any, default_source: AxisSource | None) -> AxisSource:
    text = str(value).strip() if value is not None else ""
    if text in VALID_SOURCES:
        return text  # type: ignore[return-value]
    if default_source is None:
        raise ValueError("coverage axis missing source")
    return default_source
