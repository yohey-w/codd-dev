"""codd drift - Detect design-to-implementation URL drift."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, Sequence


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

    return DriftResult(
        design_urls=normalized_design_urls,
        impl_urls=normalized_impl_urls,
        drift=drift,
        exit_code=1 if drift else 0,
    )


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
    for drift in DesignTokenDriftLinker(project_root).detect_drift():
        token = drift["token"]
        status = drift["status"]
        result.drift.append(
            DriftEntry(
                kind=drift["kind"],
                url=token,
                source="implementation" if status == "missing_in_design_md" else "DESIGN.md",
                closest_match="",
                status=status,
                token=token,
            )
        )

    if result.drift:
        result.exit_code = 1
    return result


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
