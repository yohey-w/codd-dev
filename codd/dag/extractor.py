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

RUNTIME_CONSTRAINT_REQUIRED_FIELDS = ("capability", "required", "rationale")
USER_JOURNEY_REQUIRED_FIELDS = ("name", "criticality", "steps", "required_capabilities", "expected_outcome_refs")
USER_JOURNEY_LIST_FIELDS = ("steps", "required_capabilities", "expected_outcome_refs")
DESIGN_DOC_ATTRIBUTE_SCHEMAS = {
    "runtime_constraints": {
        "required_fields": RUNTIME_CONSTRAINT_REQUIRED_FIELDS,
        "list_fields": (),
    },
    "user_journeys": {
        "required_fields": USER_JOURNEY_REQUIRED_FIELDS,
        "list_fields": USER_JOURNEY_LIST_FIELDS,
    },
}
DESIGN_DOC_ATTRIBUTE_KEYS = tuple(DESIGN_DOC_ATTRIBUTE_SCHEMAS)
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


def extract_design_doc_metadata(
    md_path: Path,
    *,
    frontmatter_alias: dict[str, str] | None = None,
) -> dict[str, Any]:
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

    frontmatter = resolve_frontmatter_aliases(frontmatter, frontmatter_alias)
    codd_meta = frontmatter.get("codd", {})
    if not isinstance(codd_meta, dict):
        codd_meta = {}

    depends_on = _as_list(
        frontmatter.get("depends_on", codd_meta.get("depends_on", frontmatter.get("dependencies", [])))
    )

    attributes = extract_design_doc_journey_attrs(frontmatter)
    expected_extraction = codd_meta.get("expected_extraction", frontmatter.get("expected_extraction"))
    if isinstance(expected_extraction, dict):
        attributes["expected_extraction"] = deepcopy(expected_extraction)

    return {
        "frontmatter": frontmatter,
        "depends_on": depends_on,
        "node_id": codd_meta.get("node_id") or frontmatter.get("node_id"),
        "attributes": attributes,
        "body": body,
    }


def extract_verification_means_catalog(project_lexicon_path: Path) -> dict[str, Any] | None:
    """Return a project lexicon catalog override for LLM prompt resolution."""

    if not project_lexicon_path.is_file():
        return None
    payload = yaml.safe_load(project_lexicon_path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        return None
    catalog = payload.get("verification_means_catalog")
    return deepcopy(catalog) if isinstance(catalog, dict) else None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def resolve_frontmatter_aliases(
    frontmatter: dict[str, Any],
    frontmatter_alias: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return frontmatter with configured alias keys copied to canonical keys."""

    if not isinstance(frontmatter_alias, dict) or not frontmatter_alias:
        return deepcopy(frontmatter)

    resolved = deepcopy(frontmatter)
    for alias_key, canonical_key in _frontmatter_alias_map(frontmatter_alias).items():
        if alias_key in resolved and canonical_key not in resolved:
            resolved[canonical_key] = deepcopy(resolved[alias_key])
    return resolved


def extract_design_doc_journey_attrs(
    frontmatter: dict[str, Any],
    *,
    strict: bool = False,
    frontmatter_alias: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Extract declarative journey attributes from design-doc frontmatter.

    Invalid entries emit warnings by default so existing design documents stay
    buildable while authors get actionable feedback.
    """

    resolved = resolve_frontmatter_aliases(frontmatter, frontmatter_alias)
    attributes: dict[str, Any] = {key: [] for key in DESIGN_DOC_ATTRIBUTE_KEYS}
    for key, schema in DESIGN_DOC_ATTRIBUTE_SCHEMAS.items():
        if key not in resolved:
            continue
        attributes[key] = _extract_structured_entries(
            resolved,
            key=key,
            required_fields=tuple(schema["required_fields"]),
            list_fields=tuple(schema["list_fields"]),
            strict=strict,
        )
    return attributes


def _frontmatter_alias_map(frontmatter_alias: dict[str, str]) -> dict[str, str]:
    return {
        str(alias_key).strip(): str(canonical_key).strip()
        for alias_key, canonical_key in frontmatter_alias.items()
        if str(alias_key).strip() and str(canonical_key).strip()
    }


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
