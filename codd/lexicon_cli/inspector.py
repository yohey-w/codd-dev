"""Lightweight lexicon coverage inspection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from codd.lexicon_cli.manager import LexiconManager, LexiconRecord


@dataclass(frozen=True)
class TextHit:
    path: str
    line: int
    term: str
    preview: str


@dataclass(frozen=True)
class AxisInspection:
    axis_type: str
    status: str
    hit_count: int
    hits: tuple[TextHit, ...]


@dataclass(frozen=True)
class LexiconDiffResult:
    project_root: str
    lexicon_id: str
    lexicon_name: str
    mode: str
    axes: tuple[AxisInspection, ...]
    metadata: dict[str, Any]

    @property
    def covered_count(self) -> int:
        return sum(1 for axis in self.axes if axis.status != "unknown")

    @property
    def total_count(self) -> int:
        return len(self.axes)


class LexiconInspector:
    """Inspect one lexicon against project text without hardcoded lexicon knowledge."""

    def __init__(self, project_root: Path | str, lexicon_root: Path | str | None = None):
        self.project_root = Path(project_root).resolve()
        self.manager = LexiconManager(self.project_root, lexicon_root)

    def inspect(self, lexicon_id: str, *, with_ai: bool = False, ai_command: Any | None = None) -> LexiconDiffResult:
        record = self.manager.resolve(lexicon_id)
        axes = _load_axes(record)
        if with_ai:
            return self._inspect_with_ai(record, axes, ai_command=ai_command)
        return self._inspect_text(record, axes)

    def _inspect_text(self, record: LexiconRecord, axes: list[dict[str, Any]]) -> LexiconDiffResult:
        documents = _collect_documents(self.project_root)
        inspected: list[AxisInspection] = []
        for axis in axes:
            axis_type = str(axis.get("axis_type", "")).strip()
            if not axis_type:
                continue
            terms = _axis_terms(axis)
            hits, hit_count = _find_hits(documents, terms, self.project_root)
            inspected.append(
                AxisInspection(
                    axis_type=axis_type,
                    status="covered_text_match" if hit_count else "unknown",
                    hit_count=hit_count,
                    hits=tuple(hits),
                )
            )
        return LexiconDiffResult(
            project_root=self.project_root.as_posix(),
            lexicon_id=record.id,
            lexicon_name=record.lexicon_name,
            mode="text-grep",
            axes=tuple(inspected),
            metadata={"document_count": len(documents)},
        )

    def _inspect_with_ai(
        self,
        record: LexiconRecord,
        axes: list[dict[str, Any]],
        *,
        ai_command: Any | None,
    ) -> LexiconDiffResult:
        from codd.elicit.engine import ElicitEngine
        from codd.elicit.lexicon_loader import load_lexicon

        lexicon_config = load_lexicon(record.path)
        result = ElicitEngine(ai_command=ai_command).run(self.project_root, lexicon_config=lexicon_config)
        coverage = result.lexicon_coverage_report
        inspected: list[AxisInspection] = []
        seen: set[str] = set()
        for axis in axes:
            axis_type = str(axis.get("axis_type", "")).strip()
            if not axis_type:
                continue
            seen.add(axis_type)
            inspected.append(
                AxisInspection(
                    axis_type=axis_type,
                    status=str(coverage.get(axis_type, "unknown")),
                    hit_count=0,
                    hits=(),
                )
            )
        for axis_type, status in coverage.items():
            if axis_type in seen:
                continue
            inspected.append(
                AxisInspection(
                    axis_type=str(axis_type),
                    status=str(status),
                    hit_count=0,
                    hits=(),
                )
            )
        return LexiconDiffResult(
            project_root=self.project_root.as_posix(),
            lexicon_id=record.id,
            lexicon_name=record.lexicon_name,
            mode="with-ai",
            axes=tuple(inspected),
            metadata={
                "all_covered": result.all_covered,
                "findings": len(result.findings),
            },
        )


def _load_axes(record: LexiconRecord) -> list[dict[str, Any]]:
    manifest = _load_yaml_mapping(record.path / "manifest.yaml")
    declared = str(manifest.get("lexicon") or "lexicon.yaml")
    lexicon_yaml = Path(declared)
    if not lexicon_yaml.is_absolute():
        lexicon_yaml = record.path / lexicon_yaml
    if lexicon_yaml.is_file():
        payload = _load_yaml_mapping(lexicon_yaml)
        raw_axes = payload.get("coverage_axes", [])
    else:
        raw_axes = manifest.get("coverage_axes", [])
    if not isinstance(raw_axes, list):
        return []
    return [axis for axis in raw_axes if isinstance(axis, dict)]


def _axis_terms(axis: dict[str, Any]) -> list[str]:
    terms: list[str] = []
    axis_type = axis.get("axis_type")
    if isinstance(axis_type, str):
        terms.append(axis_type)
    variants = axis.get("variants", [])
    if isinstance(variants, list):
        for variant in variants:
            if isinstance(variant, str):
                terms.append(variant)
                continue
            if not isinstance(variant, dict):
                continue
            for key in ("id",):
                value = variant.get(key)
                if isinstance(value, str):
                    terms.append(value)
            attributes = variant.get("attributes", {})
            if isinstance(attributes, dict):
                value = attributes.get("source_literal")
                if isinstance(value, str):
                    terms.append(value)
    return _dedupe_terms(terms)


def _collect_documents(project_root: Path) -> list[Path]:
    candidates: list[Path] = []
    for relative in (
        "requirements.md",
        "REQUIREMENTS.md",
        "design.md",
        "DESIGN.md",
        "project_lexicon.yaml",
        ".codd/requirements.md",
        ".codd/design.md",
        "codd/requirements.md",
        "codd/design.md",
    ):
        candidates.append(project_root / relative)

    docs_dir = project_root / "docs"
    for dirname in ("requirements", "design", "architecture"):
        directory = docs_dir / dirname
        if directory.is_dir():
            candidates.extend(sorted(directory.rglob("*.md")))

    seen: set[Path] = set()
    result: list[Path] = []
    for path in candidates:
        if not path.is_file():
            continue
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(path)
    return result


def _find_hits(documents: list[Path], terms: list[str], project_root: Path) -> tuple[list[TextHit], int]:
    hits: list[TextHit] = []
    hit_count = 0
    lowered_terms = [(term, term.casefold()) for term in terms if term.strip()]
    for path in documents:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_no, line in enumerate(lines, start=1):
            lowered = line.casefold()
            for term, lowered_term in lowered_terms:
                if lowered_term not in lowered:
                    continue
                hit_count += 1
                if len(hits) < 10:
                    hits.append(
                        TextHit(
                            path=_display_path(path, project_root),
                            line=line_no,
                            term=term,
                            preview=line.strip()[:160],
                        )
                    )
    return hits, hit_count


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"YAML file must contain a mapping: {path}")
    return payload


def _dedupe_terms(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = value.strip()
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _display_path(path: Path, project_root: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()
