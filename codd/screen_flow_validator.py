"""Validate screen-flow route definitions against filesystem routes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from codd.coherence_engine import DriftEvent, EventBus


@dataclass(frozen=True)
class ScreenFlowDrift:
    """Route drift between screen-flow.md and extracted filesystem routes."""

    route: str
    source: str
    detail: str = ""


@dataclass(frozen=True)
class EdgeCoverageResult:
    """Coverage of screen-flow routes by extracted transition edges."""

    total_edges: int
    covered_nodes: set[str]
    orphan_nodes: list[str]
    dead_end_nodes: list[str]
    unreachable_nodes: list[str]
    coverage_ratio: float


_ROUTE_HEADING_RE = re.compile(
    r"^#{1,6}\s+(?:(?:route|screen|page|path|ルート|画面)\s*[:：]\s*)?"
    r"(?P<route>/[^\s`#]+)",
    re.IGNORECASE | re.MULTILINE,
)
_ROUTE_FIELD_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?route(?:\*\*)?\s*[:：]\s*(?P<route>/[^\s`\n]+)",
    re.IGNORECASE | re.MULTILINE,
)
_ROUTE_LIST_RE = re.compile(r"^\s*[-*]\s+(?P<route>/[^\s`]+)", re.MULTILINE)
_ROUTE_TOKEN_RE = re.compile(r"(?<![:\w])/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*")

_coherence_bus: EventBus | None = None


def parse_screen_flow_routes(screen_flow_path: Path) -> list[str]:
    """Extract unique route paths from a screen-flow.md file."""
    if not screen_flow_path.exists():
        return []

    text = screen_flow_path.read_text(encoding="utf-8", errors="ignore")
    routes: list[str] = []
    for pattern in (_ROUTE_HEADING_RE, _ROUTE_FIELD_RE, _ROUTE_LIST_RE):
        routes.extend(_normalize_route(match.group("route")) for match in pattern.finditer(text))
    routes.extend(_normalize_route(match.group(0)) for match in _ROUTE_TOKEN_RE.finditer(text))
    return _ordered_routes(route for route in routes if route)


def get_filesystem_routes(project_root: Path, config: dict[str, Any]) -> list[str]:
    """Extract route paths using configured filesystem route settings."""
    fs_route_configs = config.get("filesystem_routes", [])
    if not fs_route_configs:
        return []

    try:
        from codd.parsing import FileSystemRouteExtractor
    except ImportError:
        return []

    try:
        route_info = FileSystemRouteExtractor().extract_routes(project_root, fs_route_configs)
    except Exception:
        return []

    raw_routes = getattr(route_info, "routes", route_info)
    return _ordered_routes(_route_path_from_item(item) for item in raw_routes or [])


def compute_screen_flow_drifts(
    screen_flow_routes: list[str],
    filesystem_routes: list[str],
) -> list[ScreenFlowDrift]:
    """Return route differences between screen-flow.md and filesystem routes."""
    screen_flow_set = set(_normalize_route(route) for route in screen_flow_routes)
    filesystem_set = set(_normalize_route(route) for route in filesystem_routes)
    screen_flow_set.discard("")
    filesystem_set.discard("")

    drifts: list[ScreenFlowDrift] = []
    for route in sorted(screen_flow_set - filesystem_set):
        drifts.append(
            ScreenFlowDrift(
                route=route,
                source="screen_flow_only",
                detail=f"Route '{route}' is defined in screen-flow.md but was not found in filesystem routes.",
            )
        )
    for route in sorted(filesystem_set - screen_flow_set):
        drifts.append(
            ScreenFlowDrift(
                route=route,
                source="filesystem_only",
                detail=f"Route '{route}' was found in filesystem routes but is not defined in screen-flow.md.",
            )
        )
    return drifts


def validate_screen_flow(project_root: Path, config: dict[str, Any]) -> list[ScreenFlowDrift]:
    """Compare screen-flow.md routes with configured filesystem routes."""
    screen_flow_path = find_screen_flow_path(project_root)
    if screen_flow_path is None:
        return []

    screen_flow_routes = parse_screen_flow_routes(screen_flow_path)
    filesystem_routes = get_filesystem_routes(project_root, config)
    if not filesystem_routes:
        configured_dirs = [
            route_config.get("base_dir", "(unset)")
            for route_config in config.get("filesystem_routes", [])
            if isinstance(route_config, dict)
        ]
        if configured_dirs:
            from codd.cli import CoddCLIError

            raise CoddCLIError(
                "No filesystem routes found. "
                "Check filesystem_routes.base_dir in codd.yaml "
                f"(currently configured: {configured_dirs}). "
                "Example: base_dir should point to the actual route directory, such as 'src/app'."
            )
        return []

    drifts = compute_screen_flow_drifts(screen_flow_routes, filesystem_routes)
    _publish_screen_flow_drift_events(drifts)
    return drifts


def validate_screen_flow_edges(
    project_root: Path,
    screen_flow_nodes: list[str],
    config: dict[str, Any] | None = None,
) -> EdgeCoverageResult:
    """Check transition edge coverage and detect one-way or uncovered routes."""

    del config
    project_root = Path(project_root)
    ordered_nodes = _ordered_routes(_normalize_route(route) for route in screen_flow_nodes)
    all_nodes = set(ordered_nodes)
    transitions_path = project_root / "docs" / "extracted" / "screen-transitions.yaml"

    if not transitions_path.exists():
        return EdgeCoverageResult(
            total_edges=0,
            covered_nodes=set(),
            orphan_nodes=[],
            dead_end_nodes=[],
            unreachable_nodes=ordered_nodes,
            coverage_ratio=1.0,
        )

    import yaml

    data = yaml.safe_load(transitions_path.read_text(encoding="utf-8")) or {}
    raw_edges = data.get("edges", [])
    edges = raw_edges if isinstance(raw_edges, list) else []
    from_nodes = _edge_endpoint_set(edges, "from")
    to_nodes = _edge_endpoint_set(edges, "to")
    covered = from_nodes | to_nodes

    orphan = [node for node in ordered_nodes if node in to_nodes and node not in from_nodes]
    dead_end = [node for node in ordered_nodes if node in from_nodes and node not in to_nodes]
    unreachable = [node for node in ordered_nodes if node not in covered]
    ratio = len(covered & all_nodes) / len(all_nodes) if all_nodes else 1.0

    return EdgeCoverageResult(
        total_edges=len(edges),
        covered_nodes=covered,
        orphan_nodes=orphan,
        dead_end_nodes=dead_end,
        unreachable_nodes=unreachable,
        coverage_ratio=ratio,
    )


def find_screen_flow_path(project_root: Path) -> Path | None:
    """Find the conventional screen-flow.md location for a project."""
    candidates = (
        project_root / "docs" / "extracted" / "screen-flow.md",
        project_root / "docs" / "screen-flow.md",
        project_root / "screen-flow.md",
    )
    for path in candidates:
        if path.exists():
            return path
    return None


def set_coherence_bus(bus: EventBus | None) -> None:
    """Set an opt-in bus used to publish screen-flow drift events."""
    global _coherence_bus
    _coherence_bus = bus


def _publish_screen_flow_drift_events(drifts: list[ScreenFlowDrift]) -> None:
    if _coherence_bus is None:
        return
    for drift in drifts:
        _coherence_bus.publish(_screen_flow_drift_to_event(drift))


def _screen_flow_drift_to_event(drift: ScreenFlowDrift) -> DriftEvent:
    source_artifact = "screen_flow" if drift.source == "screen_flow_only" else "implementation"
    target_artifact = "implementation" if drift.source == "screen_flow_only" else "screen_flow"
    return DriftEvent(
        source_artifact=source_artifact,
        target_artifact=target_artifact,
        change_type="modified",
        payload={"route": drift.route, "source": drift.source, "description": drift.detail},
        severity="amber",
        fix_strategy="hitl",
        kind="screen_flow_drift",
    )


def _route_path_from_item(item: Any) -> str:
    if isinstance(item, str):
        return _normalize_route(item)
    if isinstance(item, dict):
        for key in ("url", "path", "route", "endpoint"):
            value = item.get(key)
            if value:
                return _normalize_route(str(value))
    for attr in ("url", "path", "route", "endpoint"):
        value = getattr(item, attr, None)
        if value:
            return _normalize_route(str(value))
    return ""


def _edge_endpoint_set(edges: list[Any], key: str) -> set[str]:
    endpoints: set[str] = set()
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        route = _normalize_route(str(edge.get(key, "")))
        if route:
            endpoints.add(route)
    return endpoints


def _normalize_route(route: str) -> str:
    normalized = route.strip().strip("`\"'")
    normalized = normalized.rstrip(".,;。、)")
    if not normalized.startswith("/") or normalized.startswith("//"):
        return ""
    return normalized.rstrip("/") or "/"


def _ordered_routes(routes: list[str] | Any) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for route in routes:
        if not route or route in seen:
            continue
        seen.add(route)
        ordered.append(route)
    return ordered
