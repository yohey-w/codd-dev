"""Thin extraction adapters used by the DAG builder."""

from __future__ import annotations

import re
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
        "attributes": _extract_design_doc_attributes(frontmatter),
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


def _extract_design_doc_attributes(frontmatter: dict[str, Any]) -> dict[str, Any]:
    """Return declarative DAG attributes that CoDD core passes through."""

    attributes: dict[str, Any] = {}
    for key in DESIGN_DOC_ATTRIBUTE_KEYS:
        if key in frontmatter:
            attributes[key] = frontmatter[key]
    return attributes


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
