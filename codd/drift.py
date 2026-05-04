"""codd drift - Detect design-to-implementation URL drift."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, Sequence

from codd.coherence_adapters import drift_entry_to_event
from codd.coherence_engine import EventBus


_coherence_bus: EventBus | None = None


@dataclass
class DriftEntry:
    kind: str
    url: str
    source: str
    closest_match: str
    status: str = ""
    token: str = ""


@dataclass
class DriftResult:
    design_urls: list[str]
    impl_urls: list[str]
    drift: list[DriftEntry] = field(default_factory=list)
    exit_code: int = 0


@dataclass
class ScreenTransitionDrift:
    missing_in_e2e: list[str]
    extra_in_e2e: list[str]
    coverage_ratio: float


_DESIGN_TOKEN_REF_RE = re.compile(r"\{([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+)\}")
_DESIGN_TOKEN_ID_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+\b")
_SKIPPED_PATH_PARTS = {".codd", ".git", ".hg", ".mypy_cache", ".pytest_cache", ".venv", "node_modules"}
_UI_EXTENSIONS = {".tsx", ".jsx", ".vue", ".svelte", ".swift", ".kt", ".dart"}


class DesignTokenDriftLinker:
    """Detect drift between DESIGN.md design tokens and UI token references."""

    kind: str = "design_token"

    def __init__(self, project_root: Path | str):
        self.project_root = Path(project_root)

    def detect_drift(self) -> list[dict[str, str]]:
        design_md_path = self.project_root / "DESIGN.md"
        if not design_md_path.exists():
            return []

        defined_tokens = _extract_defined_design_tokens(design_md_path)
        used_tokens = _extract_used_design_tokens(self.project_root)

        drifts: list[dict[str, str]] = []
        for token in sorted(used_tokens - defined_tokens):
            drifts.append({"token": token, "status": "missing_in_design_md", "kind": self.kind})
        for token in sorted(defined_tokens - used_tokens):
            drifts.append({"token": token, "status": "unused_design_token", "kind": self.kind})
        return drifts


def compute_drift(
    design_urls: Sequence[str],
    impl_urls: Sequence[str],
    design_sources: dict[str, str] | None = None,
) -> DriftResult:
    """Compute drift between design-referenced URLs and implementation endpoints."""
    design_sources = design_sources or {}
    normalized_design_urls = _unique_urls(design_urls)
    normalized_impl_urls = _unique_urls(impl_urls)
    design_set = set(normalized_design_urls)
    impl_set = set(normalized_impl_urls)

    drift: list[DriftEntry] = []
    for url in normalized_design_urls:
        if url not in impl_set:
            drift.append(
                DriftEntry(
                    kind="design-only",
                    url=url,
                    source=design_sources.get(url, ""),
                    closest_match=_find_closest(url, normalized_impl_urls),
                )
            )

    for url in normalized_impl_urls:
        if url not in design_set:
            drift.append(
                DriftEntry(
                    kind="impl-only",
                    url=url,
                    source="implementation",
                    closest_match=_find_closest(url, normalized_design_urls),
                )
            )

    result = DriftResult(
        design_urls=normalized_design_urls,
        impl_urls=normalized_impl_urls,
        drift=drift,
        exit_code=1 if drift else 0,
    )
    _publish_drift_events(result.drift)
    return result


def _find_closest(url: str, candidates: list[str]) -> str:
    """Find closest URL from candidates using common-prefix heuristic."""
    if not candidates:
        return ""

    def score(candidate: str) -> int:
        prefix = 0
        for left, right in zip(url, candidate):
            if left != right:
                break
            prefix += 1
        return prefix

    return max(candidates, key=score)


def run_drift(project_root: Path, codd_dir: Path) -> DriftResult:
    """Full drift run: read codd.yaml, extract URLs, and compute drift."""
    import yaml

    config_path = codd_dir / "codd.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    impl_urls = _extract_impl_urls(project_root, config)
    design_urls, design_sources = _extract_design_urls(project_root, config)

    result = compute_drift(design_urls, impl_urls, design_sources)
    design_token_entries: list[DriftEntry] = []
    for drift in DesignTokenDriftLinker(project_root).detect_drift():
        token = drift["token"]
        status = drift["status"]
        entry = DriftEntry(
            kind=drift["kind"],
            url=token,
            source="implementation" if status == "missing_in_design_md" else "DESIGN.md",
            closest_match="",
            status=status,
            token=token,
        )
        result.drift.append(entry)
        design_token_entries.append(entry)

    if result.drift:
        result.exit_code = 1
    _publish_drift_events(design_token_entries)
    return result


def extract_e2e_have_url_assertions(
    project_root: Path,
    config: dict[str, Any] | None = None,
) -> list[str]:
    """Extract configured URL assertion values from E2E test files."""
    project_root = Path(project_root)
    e2e_config = _e2e_config(config)
    assertion_pattern = _string_config(e2e_config.get("assertion_pattern"), "toHaveURL")
    test_dir = _resolve_project_path(
        project_root,
        _string_config(e2e_config.get("test_dir"), "tests/e2e"),
    )
    spec_globs = _string_list_config(
        e2e_config.get("spec_globs", e2e_config.get("file_globs")),
        ["*.spec.ts"],
    )

    if not assertion_pattern or not test_dir.exists():
        return []

    assertion_re = re.compile(rf"{re.escape(assertion_pattern)}\s*\(\s*(['\"])(?P<url>[^'\"]+)\1")
    urls: list[str] = []
    for spec_file in _iter_e2e_spec_files(test_dir, spec_globs, project_root):
        try:
            content = spec_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        urls.extend(match.group("url") for match in assertion_re.finditer(content))
    return _unique_urls(urls)


def detect_screen_transition_drift(
    project_root: Path,
    config: dict[str, Any] | None = None,
) -> ScreenTransitionDrift:
    """Compare extracted screen-transition destinations against E2E URL assertions."""
    project_root = Path(project_root)
    e2e_config = _e2e_config(config)
    transitions_path = _resolve_project_path(
        project_root,
        _string_config(
            e2e_config.get("screen_transitions_path"),
            "docs/extracted/screen-transitions.yaml",
        ),
    )

    if not transitions_path.exists():
        return ScreenTransitionDrift(missing_in_e2e=[], extra_in_e2e=[], coverage_ratio=1.0)

    design_routes = _read_screen_transition_routes(transitions_path)
    e2e_routes = set(extract_e2e_have_url_assertions(project_root, config))

    missing = sorted(design_routes - e2e_routes)
    extra = sorted(e2e_routes - design_routes)
    coverage = len(design_routes & e2e_routes) / len(design_routes) if design_routes else 1.0
    result = ScreenTransitionDrift(
        missing_in_e2e=missing,
        extra_in_e2e=extra,
        coverage_ratio=coverage,
    )
    _publish_screen_transition_drift_events(result)
    return result


def set_coherence_bus(bus: EventBus | None) -> None:
    """Set an opt-in bus used to publish drift events."""
    global _coherence_bus
    _coherence_bus = bus


def _publish_drift_events(entries: Sequence[Any]) -> None:
    if _coherence_bus is None:
        return
    for entry in entries:
        drift_entry_to_event(entry, bus=_coherence_bus)


def _publish_screen_transition_drift_events(result: ScreenTransitionDrift) -> None:
    entries: list[DriftEntry] = [
        DriftEntry(
            kind="screen_transition_e2e",
            url=route,
            source="screen-transitions.yaml",
            closest_match="",
            status="missing_in_e2e",
        )
        for route in result.missing_in_e2e
    ]
    entries.extend(
        DriftEntry(
            kind="screen_transition_e2e",
            url=route,
            source="tests/e2e",
            closest_match="",
            status="extra_in_e2e",
        )
        for route in result.extra_in_e2e
    )
    _publish_drift_events(entries)


def _extract_defined_design_tokens(design_md_path: Path) -> set[str]:
    try:
        from codd.design_md import DesignMdExtractor
    except ImportError:
        return _extract_defined_design_tokens_fallback(design_md_path)

    try:
        result = DesignMdExtractor().extract(design_md_path)
    except Exception:
        return _extract_defined_design_tokens_fallback(design_md_path)

    return {
        token_id
        for token_id in (str(getattr(token, "id", "")).strip() for token in getattr(result, "tokens", []))
        if _is_design_token(token_id)
    }


def _extract_defined_design_tokens_fallback(design_md_path: Path) -> set[str]:
    try:
        text = design_md_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return set()

    tokens: set[str] = set()
    for pattern in (_DESIGN_TOKEN_REF_RE, _DESIGN_TOKEN_ID_RE):
        for match in pattern.finditer(text):
            token = match.group(1) if pattern is _DESIGN_TOKEN_REF_RE else match.group(0)
            if _is_design_token(token):
                tokens.add(token)
    return tokens


def _extract_used_design_tokens(project_root: Path) -> set[str]:
    used_tokens: set[str] = set()
    for ui_file in project_root.rglob("*"):
        if not ui_file.is_file() or ui_file.suffix.lower() not in _UI_EXTENSIONS:
            continue
        if _should_skip_path(ui_file, project_root):
            continue
        try:
            text = ui_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for match in _DESIGN_TOKEN_REF_RE.finditer(text):
            token = match.group(1)
            if _is_design_token(token):
                used_tokens.add(token)
    return used_tokens


def _should_skip_path(path: Path, project_root: Path) -> bool:
    try:
        parts = path.relative_to(project_root).parts
    except ValueError:
        parts = path.parts
    return any(part in _SKIPPED_PATH_PARTS for part in parts)


def _is_design_token(token: str) -> bool:
    return bool(_DESIGN_TOKEN_ID_RE.fullmatch(token))


def _extract_impl_urls(project_root: Path, config: dict[str, Any]) -> list[str]:
    fs_route_configs = config.get("filesystem_routes", [])
    if not fs_route_configs:
        return []

    try:
        from codd.parsing import FileSystemRouteExtractor
    except ImportError:
        return []

    extractor = FileSystemRouteExtractor()
    route_info = extractor.extract_routes(project_root, fs_route_configs)
    return [_route_url(route) for route in getattr(route_info, "routes", []) if _route_url(route)]


def _extract_design_urls(project_root: Path, config: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    doc_link_config = config.get("document_url_linking", {})
    if not doc_link_config.get("enabled", False):
        return [], {}

    try:
        from codd.extractor import DocumentUrlLinker
    except ImportError:
        return [], {}

    linker = DocumentUrlLinker(doc_link_config)
    design_urls: list[str] = []
    design_sources: dict[str, str] = {}
    doc_dirs = config.get("scan", {}).get("doc_dirs", [])

    for doc_dir in doc_dirs:
        full_dir = project_root / doc_dir
        if not full_dir.exists():
            continue
        for md_file in full_dir.rglob("*.md"):
            text = md_file.read_text(encoding="utf-8", errors="ignore")
            rel_path = md_file.relative_to(project_root).as_posix()
            result = linker.extract_urls(text, rel_path)
            for url in getattr(result, "urls", []):
                design_urls.append(url)
                design_sources.setdefault(url, getattr(result, "node_id", rel_path))

    return design_urls, design_sources


def _unique_urls(urls: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        unique.append(url)
    return unique


def _route_url(route: Any) -> str:
    if isinstance(route, dict):
        return str(route.get("url", ""))
    return str(getattr(route, "url", ""))


def _e2e_config(config: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, dict):
        return {}
    value = config.get("e2e", {})
    return value if isinstance(value, dict) else {}


def _string_config(value: Any, default: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _string_list_config(value: Any, default: list[str]) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, list):
        items = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        if items:
            return items
    return default


def _resolve_project_path(project_root: Path, path_text: str) -> Path:
    path = Path(path_text).expanduser()
    if path.is_absolute():
        return path
    return project_root / path


def _iter_e2e_spec_files(e2e_dir: Path, spec_globs: list[str], project_root: Path):
    seen: set[Path] = set()
    for spec_glob in spec_globs:
        for spec_file in sorted(e2e_dir.rglob(spec_glob)):
            if not spec_file.is_file() or _should_skip_path(spec_file, project_root):
                continue
            resolved = spec_file.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            yield spec_file


def _read_screen_transition_routes(transitions_path: Path) -> set[str]:
    import yaml

    payload = yaml.safe_load(transitions_path.read_text(encoding="utf-8")) or {}
    edges = payload.get("edges", []) if isinstance(payload, dict) else []
    routes: set[str] = set()
    if not isinstance(edges, list):
        return routes
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        route = edge.get("to") or edge.get("to_route")
        if route:
            routes.add(str(route))
    return routes
