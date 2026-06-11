"""Filesystem-convention route extraction (Next.js/SvelteKit-style routers)."""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codd.parsing._shared import _IGNORED_DIR_NAMES, _normalize_list


@dataclass
class FilesystemRouteInfo:
    """Extracted filesystem routing data."""

    routes: list[dict[str, str]] = field(default_factory=list)

class FileSystemRouteExtractor:
    """Extract URL endpoints from filesystem-based routing conventions.

    Framework-agnostic: driven by codd.yaml filesystem_routes config.
    Supports Next.js App/Pages, SvelteKit, Nuxt 3, Astro, Remix.
    """

    def extract_routes(self, project_root: Path, route_configs: list[dict]) -> FilesystemRouteInfo:
        """Extract routes from all configured base_dirs.

        Args:
            project_root: Project root path
            route_configs: List of filesystem_routes config blocks from codd.yaml
        Returns:
            FilesystemRouteInfo with all discovered routes
        """
        info = FilesystemRouteInfo()
        root = Path(project_root)

        for config in route_configs or []:
            if not isinstance(config, dict):
                continue

            base_dir_value = config.get("base_dir")
            if not base_dir_value:
                continue

            base_dir = _resolve_route_base_dir(root, str(base_dir_value))
            if not base_dir.is_dir():
                continue

            page_patterns = _expand_route_patterns(config.get("page_pattern"))
            api_patterns = _expand_route_patterns(config.get("api_pattern"))

            for file_path in _iter_filesystem_route_files(base_dir):
                relative_path = file_path.relative_to(base_dir)
                kind, matched_pattern = _match_filesystem_route_kind(
                    relative_path,
                    api_patterns=api_patterns,
                    page_patterns=page_patterns,
                )
                if kind is None or matched_pattern is None:
                    continue

                route_path = _filesystem_route_path(relative_path, matched_pattern, config)
                info.routes.append(
                    {
                        "url": _format_filesystem_route_url(route_path, config),
                        "file": str(file_path),
                        "kind": kind,
                    }
                )

        info.routes.sort(key=lambda route: (route["url"], route["kind"], route["file"]))
        return info

def _resolve_route_base_dir(project_root: Path, base_dir: str) -> Path:
    path = Path(base_dir)
    if path.is_absolute():
        return path
    return project_root / path

def _expand_route_patterns(value: Any) -> list[str]:
    patterns = _normalize_list(value)
    expanded: list[str] = []
    for pattern in patterns:
        expanded.extend(_expand_braced_route_pattern(pattern))
    return [pattern for pattern in expanded if pattern]

def _expand_braced_route_pattern(pattern: str) -> list[str]:
    match = re.search(r"\{([^{}]+)\}", pattern)
    if match is None:
        return [pattern]

    prefix = pattern[: match.start()]
    suffix = pattern[match.end() :]
    expanded: list[str] = []
    for option in match.group(1).split(","):
        expanded.extend(_expand_braced_route_pattern(f"{prefix}{option}{suffix}"))
    return expanded

def _iter_filesystem_route_files(base_dir: Path):
    for root, dirs, files in os.walk(base_dir):
        dirs[:] = [
            directory
            for directory in dirs
            if directory not in _IGNORED_DIR_NAMES and not directory.startswith(".pytest_cache")
        ]
        for filename in sorted(files):
            yield Path(root) / filename

def _match_filesystem_route_kind(
    relative_path: Path,
    *,
    api_patterns: list[str],
    page_patterns: list[str],
) -> tuple[str | None, str | None]:
    api_pattern = _matching_route_pattern(relative_path, api_patterns)
    if api_pattern is not None:
        return "api", api_pattern

    page_pattern = _matching_route_pattern(relative_path, page_patterns)
    if page_pattern is not None:
        return "page", page_pattern

    return None, None

def _matching_route_pattern(relative_path: Path, patterns: list[str]) -> str | None:
    candidates = (relative_path.as_posix(), relative_path.name)
    for pattern in patterns:
        if any(fnmatch.fnmatchcase(candidate, pattern) for candidate in candidates):
            return pattern
    return None

def _filesystem_route_path(relative_path: Path, matched_pattern: str, config: dict) -> str:
    route_segments: list[tuple[str, str]] = [(segment, segment) for segment in relative_path.parent.parts]
    file_segment = _route_file_segment(relative_path.name, matched_pattern)
    if file_segment:
        route_segments.extend(
            (segment, relative_path.name) for segment in _split_filesystem_route_segment(file_segment, config)
        )

    normalized_segments = _normalize_filesystem_route_segments(route_segments, config)
    return "/".join(normalized_segments)

def _route_file_segment(filename: str, matched_pattern: str) -> str:
    stem = Path(filename).stem
    if stem == "index":
        return ""
    if _pattern_identifies_route_marker(matched_pattern, stem):
        return ""
    return stem

def _pattern_identifies_route_marker(pattern: str, stem: str) -> bool:
    pattern_name = Path(pattern).name
    if any(char in pattern_name for char in "*?["):
        return False
    return Path(pattern_name).stem == stem

def _split_filesystem_route_segment(segment: str, config: dict) -> list[str]:
    split_pattern = config.get("split_segment")
    if split_pattern:
        return [part for part in re.split(str(split_pattern), segment) if part]
    if "." in segment and "[" not in segment and "]" not in segment:
        return [part for part in segment.split(".") if part]
    return [segment]

def _normalize_filesystem_route_segments(route_segments: list[tuple[str, str]], config: dict) -> list[str]:
    ignored_patterns = _normalize_list(config.get("ignore_segment"))
    dynamic_rules = _normalize_dynamic_route_rules(config.get("dynamic_segment"))
    normalized: list[str] = []

    for segment, original in route_segments:
        if _is_ignored_route_segment(segment, ignored_patterns):
            continue

        rewritten = _rewrite_dynamic_route_segment(segment, original, dynamic_rules)
        if rewritten in {"", ".", "/"}:
            continue
        normalized.append(rewritten.strip("/"))

    return [segment for segment in normalized if segment]

def _is_ignored_route_segment(segment: str, ignored_patterns: list[str]) -> bool:
    return any(re.fullmatch(pattern, segment) for pattern in ignored_patterns)

def _normalize_dynamic_route_rules(value: Any) -> list[dict[str, str]]:
    if isinstance(value, dict):
        raw_rules = [value]
    elif isinstance(value, list):
        raw_rules = [rule for rule in value if isinstance(rule, dict)]
    else:
        raw_rules = []

    rules: list[dict[str, str]] = []
    for rule in raw_rules:
        from_pattern = rule.get("from")
        to_pattern = rule.get("to")
        if from_pattern is None or to_pattern is None:
            continue
        rules.append({"from": str(from_pattern), "to": str(to_pattern)})
    return rules

def _rewrite_dynamic_route_segment(segment: str, original: str, dynamic_rules: list[dict[str, str]]) -> str:
    rewritten = segment
    for rule in dynamic_rules:
        updated = _apply_dynamic_route_rule(rewritten, rule)
        if updated != rewritten:
            rewritten = updated
            continue

        if original != rewritten:
            updated = _apply_dynamic_route_rule(original, rule)
            if updated != original:
                rewritten = updated

    return rewritten

def _apply_dynamic_route_rule(value: str, rule: dict[str, str]) -> str:
    pattern = re.compile(rule["from"])
    replacement = re.sub(r"\$(\d+)", r"\\g<\1>", rule["to"])
    return pattern.sub(lambda match: match.expand(replacement), value)

def _format_filesystem_route_url(route_path: str, config: dict) -> str:
    relative_dir = route_path.strip("/")
    template = str(config.get("url_template") or "/{relative_dir}")
    url_path = template.replace("{relative_dir}", relative_dir)
    normalized_path = _normalize_filesystem_url_path(url_path)
    base_url = str(config.get("base_url") or "").strip()
    if not base_url:
        return normalized_path
    if normalized_path == "/":
        return base_url.rstrip("/") or "/"
    return f"{base_url.rstrip('/')}{normalized_path}"

def _normalize_filesystem_url_path(url_path: str) -> str:
    normalized = url_path.strip()
    if not normalized:
        return "/"
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    normalized = re.sub(r"/+", "/", normalized)
    if len(normalized) > 1:
        normalized = normalized.rstrip("/")
    return normalized or "/"
