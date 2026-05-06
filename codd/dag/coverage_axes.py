"""Coverage axis declarations used by DAG checks."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
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
class CoverageVariant:
    id: str
    label: str
    attributes: dict[str, Any] = field(default_factory=dict)
    criticality: Criticality | None = "medium"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

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
        )


@dataclass
class CoverageAxis:
    axis_type: str
    rationale: str
    variants: list[CoverageVariant]
    source: AxisSource
    owner_section: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "axis_type": self.axis_type,
            "rationale": self.rationale,
            "variants": [variant.to_dict() for variant in self.variants],
            "source": self.source,
            "owner_section": self.owner_section,
        }

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
