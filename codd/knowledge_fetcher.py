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
UI_TECH_STACKS = {"React", "Vue", "Svelte", "Flutter", "SwiftUI", "Jetpack Compose"}
PACKAGE_DEPENDENCY_SECTIONS = (
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
)
IGNORED_SCAN_DIRS = {
    ".codd",
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}


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
        stacks: list[str] = []
        for filename, stack in markers.items():
            if (self.project_root / filename).exists():
                _append_unique(stacks, stack)

        for stack in _detect_package_ui_stack(self.project_root / "package.json"):
            _append_unique(stacks, stack)
        if _detect_flutter(self.project_root / "pubspec.yaml"):
            _append_unique(stacks, "Flutter")
        if _detect_swiftui(self.project_root):
            _append_unique(stacks, "SwiftUI")
        if _detect_jetpack_compose(self.project_root):
            _append_unique(stacks, "Jetpack Compose")
        return stacks

    def suggest_design_md_for_ui(self, stacks: list[str]) -> dict[str, str] | None:
        """Return a DESIGN.md spec suggestion when a UI stack is present."""
        if not any(stack in UI_TECH_STACKS for stack in stacks):
            return None

        design_md_path = self.project_root / "DESIGN.md"
        if design_md_path.exists():
            return {
                "ui_design_source": "DESIGN.md (found)",
                "spec": "https://github.com/google-labs-code/design.md",
            }
        return {
            "ui_design_source": "DESIGN.md (recommended, not found)",
            "warning": (
                "DESIGN.md not found. Consider creating it "
                "(https://github.com/google-labs-code/design.md)"
            ),
            "spec": "https://github.com/google-labs-code/design.md",
        }

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


def _append_unique(items: list[str], item: str) -> None:
    if item not in items:
        items.append(item)


def _detect_package_ui_stack(package_json_path: Path) -> list[str]:
    dependencies = _read_package_dependencies(package_json_path)
    stacks: list[str] = []
    if _has_dependency(
        dependencies,
        exact={"react", "next", "@react-native-community"},
        prefixes=("@react-native-community/",),
    ):
        stacks.append("React")
    if _has_dependency(dependencies, exact={"vue", "@vue/core", "nuxt"}):
        stacks.append("Vue")
    if _has_dependency(
        dependencies,
        exact={"svelte", "@sveltejs"},
        prefixes=("@sveltejs/",),
    ):
        stacks.append("Svelte")
    return stacks


def _read_package_dependencies(package_json_path: Path) -> set[str]:
    if not package_json_path.exists():
        return set()
    try:
        payload = json.loads(package_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()

    dependencies: set[str] = set()
    for section in PACKAGE_DEPENDENCY_SECTIONS:
        section_dependencies = payload.get(section, {})
        if isinstance(section_dependencies, dict):
            dependencies.update(str(name) for name in section_dependencies)
    return dependencies


def _has_dependency(
    dependencies: set[str],
    *,
    exact: set[str],
    prefixes: tuple[str, ...] = (),
) -> bool:
    if dependencies.intersection(exact):
        return True
    return any(
        dependency.startswith(prefix)
        for dependency in dependencies
        for prefix in prefixes
    )


def _detect_flutter(pubspec_path: Path) -> bool:
    if not pubspec_path.exists():
        return False
    text = _read_text(pubspec_path).lower()
    return bool(re.search(r"(?m)^\s*flutter\s*:", text))


def _detect_swiftui(project_root: Path) -> bool:
    has_swift_file = any(_iter_project_files(project_root, ("*.swift",)))
    if not has_swift_file:
        return False
    return any(
        "SwiftUI" in _read_text(path)
        for path in _iter_project_files(project_root, ("Packages.resolved",))
    )


def _detect_jetpack_compose(project_root: Path) -> bool:
    return any(
        "compose" in _read_text(path).lower()
        for path in _iter_project_files(project_root, ("build.gradle*", "*.kt", "*.kts"))
    )


def _iter_project_files(project_root: Path, patterns: tuple[str, ...]):
    for pattern in patterns:
        for path in project_root.rglob(pattern):
            if path.is_file() and not _is_ignored_path(path):
                yield path


def _is_ignored_path(path: Path) -> bool:
    return any(part in IGNORED_SCAN_DIRS for part in path.parts)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
