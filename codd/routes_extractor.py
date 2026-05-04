"""Generate Mermaid screen-flow diagrams from filesystem routing conventions."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ScreenFlowDiagram:
    mermaid: str
    route_count: int


_TAIL_ROLES: tuple[str, ...] = ("api", "public")
_SPECIAL_ROLE_LABELS: dict[str, str] = {"api": "API", "public": "Public"}


def generate_mermaid_screen_flow(
    project_root: Path,
    route_configs: list[dict],
) -> ScreenFlowDiagram:
    """Generate a Mermaid screen-flow diagram from filesystem routes.

    Role grouping is derived from the first path segment of each route
    (kebab-case → snake_case). ``api`` and ``public`` are rendered last;
    other roles are rendered in alphabetical order. No project-specific
    role names are hardcoded.
    """
    routes = _extract_route_paths(project_root, route_configs)
    grouped: dict[str, list[str]] = defaultdict(list)
    for route in routes:
        grouped[_role_for_route(route)].append(route)

    head_roles = sorted(role for role in grouped if role not in _TAIL_ROLES)
    tail_roles = [role for role in _TAIL_ROLES if role in grouped]

    lines = ["graph LR"]
    for role in head_roles + tail_roles:
        role_routes = sorted(grouped.get(role, []))
        if not role_routes:
            continue
        lines.append(f'  subgraph {role}["{_label_for_role(role)}"]')
        for route in role_routes:
            lines.append(f"    {_quote_mermaid_node(route)}")
        lines.append("  end")

    return ScreenFlowDiagram(mermaid="\n".join(lines), route_count=len(routes))


def _extract_route_paths(project_root: Path, route_configs: list[dict]) -> list[str]:
    try:
        from codd.parsing import FileSystemRouteExtractor
    except ImportError:
        return []

    try:
        extractor = FileSystemRouteExtractor()
    except TypeError:
        extractor = FileSystemRouteExtractor(Path(project_root), route_configs)
        if not hasattr(extractor, "extract"):
            return []
        route_info = extractor.extract()
    else:
        if hasattr(extractor, "extract_routes"):
            route_info = extractor.extract_routes(Path(project_root), route_configs)
        elif hasattr(extractor, "extract"):
            route_info = extractor.extract()
        else:
            return []

    raw_routes = getattr(route_info, "routes", route_info)
    normalized = {_route_path_from_item(item) for item in raw_routes or []}
    return sorted(route for route in normalized if route)


def _route_path_from_item(item: Any) -> str:
    if isinstance(item, str):
        return _normalize_route_path(item)
    if isinstance(item, dict):
        for key in ("url", "path", "route", "endpoint"):
            value = item.get(key)
            if value:
                return _normalize_route_path(str(value))
    for attr in ("url", "path", "route", "endpoint"):
        value = getattr(item, attr, None)
        if value:
            return _normalize_route_path(str(value))
    return ""


def _normalize_route_path(route: str) -> str:
    normalized = route.strip()
    if not normalized:
        return ""
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    return normalized.rstrip("/") or "/"


def _role_for_route(route: str) -> str:
    if not route or route == "/":
        return "public"
    parts = [segment for segment in route.split("/") if segment]
    if not parts:
        return "public"
    return parts[0].replace("-", "_")


def _label_for_role(role: str) -> str:
    if role in _SPECIAL_ROLE_LABELS:
        return _SPECIAL_ROLE_LABELS[role]
    return role.replace("_", " ").title()


def _quote_mermaid_node(route: str) -> str:
    escaped = route.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
