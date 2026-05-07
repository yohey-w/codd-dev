"""Threshold gate for lexicon coverage matrix reports."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from codd.lexicon_cli.reporter import CoverageMatrixReport, CoverageRow


@dataclass(frozen=True)
class ThresholdConfig:
    default_pct: float = 0.0
    per_lexicon: dict[str, float] = field(default_factory=dict)
    per_axis: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class CoverageViolation:
    lexicon_id: str
    axis: str | None
    observed_pct: float
    required_pct: float


def load_thresholds(codd_yaml_path: Path | None) -> ThresholdConfig:
    """Load coverage thresholds from codd.yaml, defaulting to no enforcement."""
    if codd_yaml_path is None or not codd_yaml_path.exists():
        return ThresholdConfig()

    payload = yaml.safe_load(codd_yaml_path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, Mapping):
        raise ValueError(f"{codd_yaml_path} must contain a YAML mapping")

    coverage = _mapping(payload.get("coverage"), "coverage", allow_missing=True)
    thresholds = _mapping(coverage.get("thresholds"), "coverage.thresholds", allow_missing=True)
    if not thresholds:
        return ThresholdConfig()

    default_pct = _threshold_pct(thresholds.get("default"), "coverage.thresholds.default", default=0.0)
    per_lexicon = _load_per_lexicon(thresholds.get("per_lexicon"))
    per_axis = _load_per_axis(thresholds.get("per_axis"))
    return ThresholdConfig(default_pct=default_pct, per_lexicon=per_lexicon, per_axis=per_axis)


def evaluate(matrix_report: CoverageMatrixReport, config: ThresholdConfig) -> list[CoverageViolation]:
    """Compare a coverage matrix report with configured thresholds."""
    violations: list[CoverageViolation] = []
    rows_by_lexicon: dict[str, list[CoverageRow]] = defaultdict(list)
    for row in matrix_report.rows:
        rows_by_lexicon[row.lexicon_id].append(row)

    for lexicon_id, rows in sorted(rows_by_lexicon.items()):
        required_pct = config.per_lexicon.get(lexicon_id, config.default_pct)
        observed_pct = _covered_pct(rows)
        if _is_violation(observed_pct, required_pct):
            violations.append(
                CoverageViolation(
                    lexicon_id=lexicon_id,
                    axis=None,
                    observed_pct=observed_pct,
                    required_pct=required_pct,
                )
            )

        axis_thresholds = config.per_axis.get(lexicon_id, {})
        for row in rows:
            if row.axis_type not in axis_thresholds:
                continue
            axis_observed_pct = 100.0 if _is_covered_status(row.status) else 0.0
            axis_required_pct = axis_thresholds[row.axis_type]
            if _is_violation(axis_observed_pct, axis_required_pct):
                violations.append(
                    CoverageViolation(
                        lexicon_id=lexicon_id,
                        axis=row.axis_type,
                        observed_pct=axis_observed_pct,
                        required_pct=axis_required_pct,
                    )
                )

    return violations


def _load_per_lexicon(value: Any) -> dict[str, float]:
    data = _mapping(value, "coverage.thresholds.per_lexicon", allow_missing=True)
    result: dict[str, float] = {}
    for key, item in data.items():
        lexicon_id = _non_empty_key(key, "coverage.thresholds.per_lexicon")
        result[lexicon_id] = _threshold_pct(item, f"coverage.thresholds.per_lexicon.{lexicon_id}")
    return result


def _load_per_axis(value: Any) -> dict[str, dict[str, float]]:
    data = _mapping(value, "coverage.thresholds.per_axis", allow_missing=True)
    result: dict[str, dict[str, float]] = {}
    for key, item in data.items():
        lexicon_id = _non_empty_key(key, "coverage.thresholds.per_axis")
        axes = _mapping(item, f"coverage.thresholds.per_axis.{lexicon_id}")
        result[lexicon_id] = {
            _non_empty_key(axis, f"coverage.thresholds.per_axis.{lexicon_id}"): _threshold_pct(
                threshold,
                f"coverage.thresholds.per_axis.{lexicon_id}.{axis}",
            )
            for axis, threshold in axes.items()
        }
    return result


def _threshold_pct(value: Any, path: str, *, default: float | None = None) -> float:
    if value is None and default is not None:
        return default
    if isinstance(value, Mapping):
        if "covered_text_match_pct" not in value:
            if default is not None:
                return default
            raise ValueError(f"{path}.covered_text_match_pct is required")
        value = value["covered_text_match_pct"]
    try:
        pct = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path}.covered_text_match_pct must be a number") from exc
    if pct < 0 or pct > 100:
        raise ValueError(f"{path}.covered_text_match_pct must be between 0 and 100")
    return pct


def _mapping(value: Any, path: str, *, allow_missing: bool = False) -> Mapping[str, Any]:
    if value is None and allow_missing:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be a YAML mapping")
    return value


def _non_empty_key(value: Any, path: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{path} keys must be non-empty")
    return text


def _covered_pct(rows: list[CoverageRow]) -> float:
    if not rows:
        return 0.0
    covered = sum(1 for row in rows if _is_covered_status(row.status))
    return round((covered / len(rows)) * 100, 2)


def _is_covered_status(status: str) -> bool:
    return status.casefold() in {"covered", "covered_text_match", "implicit"}


def _is_violation(observed_pct: float, required_pct: float) -> bool:
    return observed_pct + 1e-9 < required_pct
