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


def extract_imports(file_path: Path) -> list[str]:
    """Return import specifiers from a source file.

    The existing source extractor classifies imports for scan output. The DAG
    builder needs raw specifiers so it can resolve them against the final node
    set, including aliases from project configuration.
    """

    content = file_path.read_text(encoding="utf-8", errors="ignore")
    return [match.group(1) for match in _IMPORT_SPECIFIER_RE.finditer(content)]


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
