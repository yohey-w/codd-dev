"""KnowledgeFetcher: web-search-first knowledge acquisition layer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
from typing import Any


CACHE_DIR = ".codd/knowledge_cache"
CACHE_TTL_DAYS = 30
SEARCH_COMMAND_ENV = "CODD_KNOWLEDGE_SEARCH_COMMAND"
SEARCH_TIMEOUT_SECONDS = 30


@dataclass
class KnowledgeEntry:
    query: str
    result: str
    sources: list[str] = field(default_factory=list)
    confidence: float = 0.5
    provenance: str = "web_search"
    fetched_at: str = ""

    def __post_init__(self) -> None:
        if not self.fetched_at:
            self.fetched_at = _utc_now_iso()
        self.sources = [str(source) for source in self.sources]
        self.confidence = max(0.0, min(1.0, float(self.confidence)))

    def is_stale(self, ttl_days: int = CACHE_TTL_DAYS) -> bool:
        try:
            fetched = datetime.fromisoformat(self.fetched_at.replace("Z", "+00:00"))
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=UTC)
            return (datetime.now(UTC) - fetched) > timedelta(days=ttl_days)
        except (TypeError, ValueError):
            return True


class KnowledgeFetcher:
    def __init__(self, project_root: str | Path = ".", cache_ttl_days: int = CACHE_TTL_DAYS):
        self.project_root = Path(project_root)
        self.cache_ttl_days = cache_ttl_days
        self._cache_dir = self.project_root / CACHE_DIR
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def fetch(self, query: str, *, force_refresh: bool = False) -> KnowledgeEntry:
        """Fetch knowledge for a query, reusing fresh cache entries."""
        cache_file = self._cache_dir / f"{_slugify(query)}.json"
        if not force_refresh and cache_file.exists():
            entry = _load_cache(cache_file)
            if not entry.is_stale(self.cache_ttl_days):
                return entry

        entry = self._search_web(query)
        _save_cache(cache_file, entry)
        return entry

    def detect_tech_stack(self) -> list[str]:
        """Detect project tech stack from common manifest files."""
        markers = {
            "package.json": "Node.js/JavaScript/TypeScript",
            "Cargo.toml": "Rust",
            "pyproject.toml": "Python",
            "go.mod": "Go",
            "Gemfile": "Ruby",
            "composer.json": "PHP",
            "pom.xml": "Java/Maven",
            "build.gradle": "Java/Kotlin/Gradle",
        }
        return [
            stack
            for filename, stack in markers.items()
            if (self.project_root / filename).exists()
        ]

    def _search_web(self, query: str) -> KnowledgeEntry:
        """Run an optional web-search command, otherwise return an explicit fallback."""
        command = _read_search_command(self.project_root)
        if not command:
            return _fallback_entry(query)

        try:
            result = subprocess.run(
                _search_args(command, query),
                capture_output=True,
                text=True,
                timeout=SEARCH_TIMEOUT_SECONDS,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError) as exc:
            return KnowledgeEntry(
                query=query,
                result=f"Web search unavailable: {exc}",
                sources=[],
                confidence=0.1,
                provenance="inferred",
            )

        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            return KnowledgeEntry(
                query=query,
                result=f"Web search command failed: {detail}",
                sources=[],
                confidence=0.1,
                provenance="inferred",
            )

        return _entry_from_search_output(query, result.stdout)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", text.lower()).strip("_")
    return (slug or "query")[:64]


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _load_cache(path: Path) -> KnowledgeEntry:
    data = json.loads(path.read_text(encoding="utf-8"))
    allowed = {entry_field.name for entry_field in fields(KnowledgeEntry)}
    return KnowledgeEntry(**{key: value for key, value in data.items() if key in allowed})


def _save_cache(path: Path, entry: KnowledgeEntry) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(entry), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _read_search_command(project_root: Path) -> str | None:
    command = os.environ.get(SEARCH_COMMAND_ENV, "").strip()
    if command:
        return command

    env_path = project_root / ".codd" / "knowledge_search_command"
    if env_path.exists():
        command = env_path.read_text(encoding="utf-8").strip()
        if command:
            return command
    return None


def _search_args(command: str, query: str) -> list[str]:
    if "{query}" in command:
        return shlex.split(command.format(query=query))
    return [*shlex.split(command), query]


def _entry_from_search_output(query: str, output: str) -> KnowledgeEntry:
    text = output.strip()
    if not text:
        return _fallback_entry(query)

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        sources = _extract_sources(text)
        return KnowledgeEntry(
            query=query,
            result=text,
            sources=sources,
            confidence=0.8 if len(sources) >= 2 else 0.4,
            provenance="web_search" if sources else "inferred",
        )

    if isinstance(payload, dict):
        result = str(payload.get("result") or payload.get("summary") or text)
        sources = _coerce_sources(payload.get("sources", []))
        return KnowledgeEntry(
            query=str(payload.get("query") or query),
            result=result,
            sources=sources,
            confidence=payload.get("confidence", 0.8 if len(sources) >= 2 else 0.5),
            provenance=str(payload.get("provenance") or "web_search"),
        )

    sources = _coerce_sources(payload)
    return KnowledgeEntry(
        query=query,
        result=text,
        sources=sources,
        confidence=0.8 if len(sources) >= 2 else 0.5,
        provenance="web_search",
    )


def _extract_sources(text: str) -> list[str]:
    return sorted(set(re.findall(r"https?://[^\s)>\"]+", text)))


def _coerce_sources(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [str(source) for source in value if source]
    return []


def _fallback_entry(query: str) -> KnowledgeEntry:
    return KnowledgeEntry(
        query=query,
        result=f"Web search not available in current environment; manual confirmation needed for: {query}",
        sources=[],
        confidence=0.1,
        provenance="inferred",
    )
