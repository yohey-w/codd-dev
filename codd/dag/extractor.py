"""Thin extraction adapters used by the DAG builder."""

from __future__ import annotations

import re
import warnings
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


_IMPORT_SPECIFIER_RE = re.compile(
    r"""
    (?:
        import\s+(?:type\s+)?(?:[^'"]*?\s+from\s*)?
      | export\s+(?:type\s+)?[^'"]*?\s+from\s*
      | require\(\s*
      | import\(\s*
    )
    ['"]([^'"]+)['"]
    """,
    re.VERBOSE,
)

DESIGN_DOC_ATTRIBUTE_KEYS = ("runtime_constraints", "user_journeys")
RUNTIME_CONSTRAINT_REQUIRED_FIELDS = ("capability", "required", "rationale")
USER_JOURNEY_REQUIRED_FIELDS = ("name", "criticality", "steps", "required_capabilities", "expected_outcome_refs")
USER_JOURNEY_LIST_FIELDS = ("steps", "required_capabilities", "expected_outcome_refs")
LANGUAGE_SUFFIXES = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".java": "java",
}


def extract_imports(file_path: Path) -> list[str]:
    """Return import specifiers from a source file.

    The existing source extractor classifies imports for scan output. The DAG
    builder needs raw specifiers so it can resolve them against the final node
    set, including aliases from project configuration.
    """

    content = file_path.read_text(encoding="utf-8", errors="ignore")
    return [match.group(1) for match in _IMPORT_SPECIFIER_RE.finditer(content)]


def scan_capability_evidence(impl_file_path: Path, capability_patterns: dict) -> list[dict]:
    """Scan an implementation file using project-declared capability patterns."""

    if not isinstance(capability_patterns, dict) or not capability_patterns:
        return []

    matchers = _capability_matchers(capability_patterns, impl_file_path)
    if not matchers:
        return []

    evidence: list[dict] = []
    content = impl_file_path.read_text(encoding="utf-8", errors="ignore")
    display_path = impl_file_path.as_posix()
    for line_number, line in enumerate(content.splitlines(), start=1):
        for capability_kind, regex, value in matchers:
            if regex.search(line):
                evidence.append(
                    {
                        "capability_kind": capability_kind,
                        "value": value,
                        "line_ref": f"{display_path}:{line_number}",
                        "source": "capability_patterns",
                    }
                )
    return evidence


def extract_design_doc_metadata(md_path: Path) -> dict[str, Any]:
    """Return Markdown frontmatter and normalized ``depends_on`` entries."""

    content = md_path.read_text(encoding="utf-8", errors="ignore")
    frontmatter: dict[str, Any] = {}
    body = content

    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) == 3:
            loaded = yaml.safe_load(parts[1]) or {}
            if isinstance(loaded, dict):
                frontmatter = loaded
            body = parts[2]

    codd_meta = frontmatter.get("codd", {})
    if not isinstance(codd_meta, dict):
        codd_meta = {}

    depends_on = _as_list(
        frontmatter.get("depends_on", codd_meta.get("depends_on", frontmatter.get("dependencies", [])))
    )

    return {
        "frontmatter": frontmatter,
        "depends_on": depends_on,
        "node_id": codd_meta.get("node_id") or frontmatter.get("node_id"),
        "attributes": extract_design_doc_journey_attrs(frontmatter),
        "body": body,
    }


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def extract_design_doc_journey_attrs(frontmatter: dict[str, Any], *, strict: bool = False) -> dict[str, Any]:
    """Extract declarative journey attributes from design-doc frontmatter.

    Invalid entries emit warnings by default so existing design documents stay
    buildable while authors get actionable feedback.
    """

    attributes: dict[str, Any] = {"runtime_constraints": [], "user_journeys": []}
    if "runtime_constraints" in frontmatter:
        attributes["runtime_constraints"] = _extract_structured_entries(
            frontmatter,
            key="runtime_constraints",
            required_fields=RUNTIME_CONSTRAINT_REQUIRED_FIELDS,
            list_fields=(),
            strict=strict,
        )
    if "user_journeys" in frontmatter:
        attributes["user_journeys"] = _extract_structured_entries(
            frontmatter,
            key="user_journeys",
            required_fields=USER_JOURNEY_REQUIRED_FIELDS,
            list_fields=USER_JOURNEY_LIST_FIELDS,
            strict=strict,
        )
    return attributes


def _extract_structured_entries(
    frontmatter: dict[str, Any],
    *,
    key: str,
    required_fields: tuple[str, ...],
    list_fields: tuple[str, ...],
    strict: bool,
) -> list[dict[str, Any]]:
    value = frontmatter.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list):
        _warn_or_raise(f"design_doc.{key} must be a list; ignoring {type(value).__name__}", strict=strict)
        return []

    entries: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            _warn_or_raise(f"design_doc.{key}[{index}] must be a mapping; ignoring entry", strict=strict)
            continue

        entry = deepcopy(item)
        missing = [field for field in required_fields if field not in entry]
        if missing:
            _warn_or_raise(
                f"design_doc.{key}[{index}] missing required field(s): {', '.join(missing)}",
                strict=strict,
            )

        for field in list_fields:
            if field not in entry or entry[field] is None:
                entry[field] = []
                continue
            if not isinstance(entry[field], list):
                _warn_or_raise(
                    f"design_doc.{key}[{index}].{field} should be a list; coercing value",
                    strict=strict,
                )
                entry[field] = _as_list(entry[field])

        entries.append(entry)
    return entries


def _warn_or_raise(message: str, *, strict: bool) -> None:
    if strict:
        raise ValueError(message)
    warnings.warn(message, UserWarning, stacklevel=3)


def _capability_matchers(capability_patterns: dict, impl_file_path: Path) -> list[tuple[str, re.Pattern[str], Any]]:
    matchers: list[tuple[str, re.Pattern[str], Any]] = []
    file_language = _language_for_path(impl_file_path)
    for capability_kind, pattern_spec in capability_patterns.items():
        for match_spec in _pattern_match_specs(pattern_spec):
            if not _match_spec_applies_to_language(match_spec, file_language, impl_file_path.suffix):
                continue
            regex_text = match_spec.get("regex")
            if not isinstance(regex_text, str) or not regex_text:
                continue
            try:
                regex = re.compile(regex_text)
            except re.error:
                continue
            value = match_spec.get("value", pattern_spec.get("value", True) if isinstance(pattern_spec, dict) else True)
            matchers.append((str(capability_kind), regex, value))
    return matchers


def _pattern_match_specs(pattern_spec: Any) -> list[dict[str, Any]]:
    if isinstance(pattern_spec, dict):
        matches = pattern_spec.get("matches")
        if isinstance(matches, list):
            return [match for match in matches if isinstance(match, dict)]
        if "regex" in pattern_spec:
            return [pattern_spec]
    if isinstance(pattern_spec, list):
        return [match for match in pattern_spec if isinstance(match, dict)]
    return []


def _match_spec_applies_to_language(match_spec: dict[str, Any], file_language: str, suffix: str) -> bool:
    languages = match_spec.get("languages")
    if not languages:
        return True
    values = languages if isinstance(languages, list) else [languages]
    normalized = {_normalize_language(value) for value in values}
    return file_language in normalized or suffix.lstrip(".").lower() in normalized


def _normalize_language(value: Any) -> str:
    text = str(value).lower().strip().lstrip(".")
    return {
        "py": "python",
        "ts": "typescript",
        "tsx": "typescript",
        "js": "javascript",
        "jsx": "javascript",
    }.get(text, text)


def _language_for_path(path: Path) -> str:
    return LANGUAGE_SUFFIXES.get(path.suffix, "unknown")
