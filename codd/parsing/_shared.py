"""Shared dataclasses, constants, and cross-cluster helpers for ``codd.parsing``.

This module owns everything that more than one extraction cluster needs:
the info dataclasses, the :class:`LanguageExtractor` protocol, the regex
fallback adapter, project-walk helpers, and small text utilities.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import yaml

from codd.discovery import DEFAULT_IGNORED_DIRS

if TYPE_CHECKING:
    from codd.extractor import CallEdge, ModuleInfo, Symbol


# Unified ignore set — single source of truth lives in codd.discovery
# (kept under the historical local name for the in-module walkers).
_IGNORED_DIR_NAMES = DEFAULT_IGNORED_DIRS

@dataclass
class ApiSpecInfo:
    """Normalized representation of a discovered API definition file."""

    format: str
    file_path: str
    endpoints: list[dict[str, Any]] = field(default_factory=list)
    schemas: list[dict[str, Any]] = field(default_factory=list)
    services: list[dict[str, Any]] = field(default_factory=list)

@dataclass
class ConfigInfo:
    """Normalized representation of infrastructure/configuration files.

    ``services`` and ``resources`` are the original, stable surfaces that
    existing consumers rely on. The optional fields below are additive (default
    empty) and carry the richer, NFR-relevant facts surfaced by the R1 IaC
    parsing expansion (CI/CD pipelines, Dockerfile build stages, and bare
    "recognized but not deep-parsed" evidence files). Backward compatible: a
    consumer reading only ``services``/``resources`` keeps working unchanged.
    """

    format: str
    file_path: str
    services: list[dict[str, Any]] = field(default_factory=list)
    resources: list[dict[str, Any]] = field(default_factory=list)
    # Additive, optional surfaces (R1 expansion):
    pipelines: list[dict[str, Any]] = field(default_factory=list)
    images: list[dict[str, Any]] = field(default_factory=list)
    # Mark a file that was *recognized* as ops/observability evidence but not
    # structurally parsed (Prometheus rules, Ansible, Helm Chart.yaml, …).
    recognized_kind: str = ""

@dataclass
class BuildDepsInfo:
    """Build and dependency metadata extracted from project manifests."""

    file_path: str
    runtime: list[str] = field(default_factory=list)
    dev: list[str] = field(default_factory=list)
    scripts: dict[str, str] = field(default_factory=dict)

@dataclass
class TestInfo:
    """Per-test-file metadata used for test-to-source mapping."""

    file_path: str
    test_functions: list[str] = field(default_factory=list)
    fixtures: list[str] = field(default_factory=list)
    source_module: str | None = None

@dataclass
class SqlSchemaInfo:
    """Extracted SQL DDL metadata."""

    file_path: str
    tables: list[dict[str, Any]] = field(default_factory=list)
    foreign_keys: list[dict[str, Any]] = field(default_factory=list)
    indexes: list[dict[str, Any]] = field(default_factory=list)
    views: list[dict[str, Any]] = field(default_factory=list)

@dataclass
class PrismaSchemaInfo:
    """Extracted Prisma schema metadata."""

    file_path: str
    models: list[dict[str, Any]] = field(default_factory=list)
    enums: list[dict[str, Any]] = field(default_factory=list)

class LanguageExtractor(Protocol):
    """Common interface for language-aware symbol/import extraction."""

    language: str
    category: str

    def extract_symbols(self, content: str, file_path: str) -> list[Symbol]:
        """Return symbols found in the given source content."""

    def extract_imports(
        self,
        content: str,
        file_path: Path,
        project_root: Path,
        src_dir: Path,
    ) -> tuple[dict[str, list[str]], set[str]]:
        """Return internal and external imports for the given source content."""

    def detect_code_patterns(self, mod: ModuleInfo, content: str) -> None:
        """Mutate ModuleInfo with any detected structural patterns."""

    def extract_schema(self, content: str, file_path: str | Path) -> SqlSchemaInfo | PrismaSchemaInfo | None:
        """Return schema information when supported by the extractor."""

    def extract_call_graph(self, content: str, file_path: str, symbols: list[Symbol]) -> list[CallEdge]:
        """Return call edges found in the given source content."""

class RegexExtractor:
    """Adapter for regex-based extraction and schema parsing."""

    def __init__(self, language: str, category: str = "source"):
        self.language = language.lower()
        self.category = category.lower()

    def extract_symbols(self, content: str, file_path: str) -> list[Symbol]:
        from codd import extractor as extractor_module

        return extractor_module._extract_symbols(content, file_path, self.language)

    def extract_imports(
        self,
        content: str,
        file_path: Path,
        project_root: Path,
        src_dir: Path,
    ) -> tuple[dict[str, list[str]], set[str]]:
        from codd import extractor as extractor_module

        return extractor_module._extract_imports(
            content,
            self.language,
            project_root,
            src_dir,
            file_path,
        )

    def detect_code_patterns(self, mod: ModuleInfo, content: str) -> None:
        from codd import extractor as extractor_module

        extractor_module._detect_code_patterns(mod, content, self.language)
        return None

    def extract_schema(self, content: str, file_path: str | Path) -> SqlSchemaInfo | PrismaSchemaInfo | None:
        if self.category != "schema":
            return None

        # Deferred sibling import: ``codd.parsing.schemas`` itself depends on
        # this module (and on the Tree-sitter backend, which uses this class
        # as its fallback), so a top-level import would be circular.
        from codd.parsing.schemas import _extract_prisma_schema, _extract_sql_schema

        normalized_path = Path(file_path).as_posix()
        if self.language == "sql":
            return _extract_sql_schema(content, normalized_path)
        if self.language == "prisma":
            return _extract_prisma_schema(content, normalized_path)
        return None

    def extract_call_graph(self, content: str, file_path: str, symbols: list[Symbol]) -> list[CallEdge]:
        return []  # Regex fallback doesn't support call graph

def _make_symbol(
    name: str,
    kind: str,
    file_path: str,
    line: int,
    *,
    params: str = "",
    return_type: str = "",
    decorators: list[str] | None = None,
    is_async: bool = False,
    bases: list[str] | None = None,
    implements: list[str] | None = None,
):
    from codd.extractor import Symbol

    return Symbol(
        name=name,
        kind=kind,
        file=file_path,
        line=line,
        params=params,
        return_type=return_type,
        decorators=list(decorators or []),
        is_async=is_async,
        bases=list(bases or []),
        implements=list(implements or []),
    )

def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()

def _split_csv(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]

def _route_from_decorator(decorator: str) -> str | None:
    match = re.search(
        r'(?:^|\.)\s*(?:route|get|post|put|delete|patch)\s*\(\s*(?:path\s*=\s*)?["\']([^"\']+)["\']',
        decorator.replace("\n", " "),
    )
    if match:
        return match.group(1)
    return None

def _load_structured_document(content: str, file_path: str | Path) -> Any:
    suffix = Path(file_path).suffix.lower()
    try:
        if suffix == ".json":
            return json.loads(content)
        return yaml.safe_load(content)
    except Exception:
        return None

def _load_yaml_documents(file_path: Path) -> list[dict[str, Any]]:
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
        docs = list(yaml.safe_load_all(content))
    except Exception:
        return []
    return [doc for doc in docs if isinstance(doc, dict)]

def _iter_project_files(project_root: Path, suffixes: set[str]):
    normalized = {suffix.lower() for suffix in suffixes}
    for root, dirs, files in os.walk(project_root):
        dirs[:] = [
            directory
            for directory in dirs
            if directory not in _IGNORED_DIR_NAMES and not directory.startswith(".pytest_cache")
        ]
        for filename in files:
            file_path = Path(root) / filename
            if file_path.suffix.lower() in normalized:
                yield file_path

def _normalize_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if value is None or value == "":
        return []
    return [str(value)]

def _normalize_environment(value: Any) -> dict[str, Any] | list[str]:
    if isinstance(value, dict):
        return {str(key): val for key, val in value.items()}
    if isinstance(value, list):
        return [str(item) for item in value]
    return {}

def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered

def _find_named_blocks(content: str, keyword: str) -> list[tuple[str, str]]:
    matches: list[tuple[str, str]] = []
    pattern = re.compile(rf"\b{keyword}\s+(\w+)\b[^\{{]*\{{", re.MULTILINE)

    for match in pattern.finditer(content):
        brace_index = content.find("{", match.start())
        body = _extract_braced_body(content, brace_index)
        if body is not None:
            matches.append((match.group(1), body))

    return matches

def _extract_braced_body(content: str, brace_index: int) -> str | None:
    if brace_index < 0:
        return None

    depth = 0
    for index in range(brace_index, len(content)):
        char = content[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return content[brace_index + 1 : index]
    return None
