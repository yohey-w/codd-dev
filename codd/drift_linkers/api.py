"""API design-to-implementation drift linker.

The linker compares ``docs/extracted/expected_catalog.yaml`` API endpoint
expectations with Next.js route handlers under ``app/api`` or ``src/app/api``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from codd.coherence_engine import DriftEvent, EventBus
from codd.drift_linkers import register_linker


HTTP_METHODS = {"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"}
DEFAULT_EXPECTED_CATALOG_PATH = "docs/extracted/expected_catalog.yaml"
DEFAULT_ROUTE_GLOBS = ("app/api/**/route.ts", "src/app/api/**/route.ts")
EXPECTED_CATALOG_SCHEMA: dict[str, Any] = {
    "api_endpoints": [
        {
            "path": "str",
            "method": "str",
            "auth_required": "bool optional",
        }
    ]
}

_coherence_bus: EventBus | None = None


@dataclass(frozen=True)
class ApiEndpoint:
    """Normalized API endpoint definition."""

    path: str
    method: str
    auth_required: bool | None = None
    source: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        """Return the comparison key for design-vs-implementation matching."""
        return (self.path, self.method)

    def to_dict(self) -> dict[str, Any]:
        """Serialize endpoint data for drift payloads and tests."""
        payload: dict[str, Any] = {"path": self.path, "method": self.method}
        if self.auth_required is not None:
            payload["auth_required"] = self.auth_required
        if self.source is not None:
            payload["source"] = self.source
        return payload


@dataclass(frozen=True)
class ApiAuthMismatch:
    """Auth expectation mismatch for an otherwise matching endpoint."""

    path: str
    method: str
    expected_auth_required: bool
    actual_auth_required: bool
    source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize mismatch data for drift payloads and tests."""
        payload: dict[str, Any] = {
            "path": self.path,
            "method": self.method,
            "expected_auth_required": self.expected_auth_required,
            "actual_auth_required": self.actual_auth_required,
        }
        if self.source is not None:
            payload["source"] = self.source
        return payload


@dataclass
class ApiDriftResult:
    """Result of API design-vs-route comparison."""

    status: str
    expected: list[ApiEndpoint] = field(default_factory=list)
    implemented: list[ApiEndpoint] = field(default_factory=list)
    missing: list[ApiEndpoint] = field(default_factory=list)
    extra: list[ApiEndpoint] = field(default_factory=list)
    auth_mismatches: list[ApiAuthMismatch] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    events: list[DriftEvent] = field(default_factory=list)

    @property
    def has_drift(self) -> bool:
        """Return true when any endpoint or auth drift was found."""
        return bool(self.missing or self.extra or self.auth_mismatches)


def set_coherence_bus(bus: EventBus | None) -> None:
    """Install an optional coherence bus for published API drift events."""
    global _coherence_bus
    _coherence_bus = bus


@register_linker("api")
class ApiDriftLinker:
    """Compare expected API endpoints with Next.js ``route.ts`` handlers."""

    def __init__(self, expected_catalog_path, project_root, settings):
        self.project_root = Path(project_root)
        self.settings = _load_settings(settings)
        self.project_type = str(self.settings.get("project_type", "web"))
        self.expected_catalog_path = self._resolve_catalog_path(expected_catalog_path)
        self.design_file_path = self._resolve_design_file_path()
        self.route_globs = tuple(
            self.settings.get("route_globs")
            or self.settings.get("api_route_globs")
            or DEFAULT_ROUTE_GLOBS
        )

    def run(self) -> ApiDriftResult:
        """Run API endpoint drift detection."""
        if not self._is_enabled():
            return ApiDriftResult(
                status="skipped",
                warnings=[f"api drift linker disabled for project_type={self.project_type}"],
            )

        expected, warnings, catalog_available = self._load_expected_catalog()
        if not catalog_available:
            return ApiDriftResult(status="skipped", warnings=warnings)

        if not self.design_file_path.exists():
            return ApiDriftResult(
                status="skipped",
                expected=expected,
                warnings=[
                    *warnings,
                    f"api design file not found: {_display_path(self.project_root, self.design_file_path)}",
                ],
            )

        implemented = self._collect_implemented_endpoints()
        if not implemented:
            return ApiDriftResult(
                status="skipped",
                expected=expected,
                warnings=[
                    *warnings,
                    f"no API route files found for globs: {', '.join(self.route_globs)}",
                ],
            )

        result = self._compare(expected, implemented, warnings)
        if result.has_drift:
            event = self._publish_drift_event(result)
            if event is not None:
                result.events.append(event)
        return result

    def _is_enabled(self) -> bool:
        enabled = self.settings.get("enabled")
        if isinstance(enabled, list):
            return "api" in enabled
        return True

    def _resolve_catalog_path(self, expected_catalog_path) -> Path:
        configured = (
            expected_catalog_path
            or self.settings.get("expected_catalog_path")
            or self.settings.get("catalog_path")
            or DEFAULT_EXPECTED_CATALOG_PATH
        )
        path = Path(configured)
        if not path.is_absolute():
            path = self.project_root / path
        return path

    def _resolve_design_file_path(self) -> Path:
        design_files = self.settings.get("design_files")
        configured = self.settings.get("api_design_path") or self.settings.get("design_file")
        if configured is None and isinstance(design_files, dict):
            configured = design_files.get("api")
        if configured is None:
            configured = "docs/design/api_design.md"

        path = Path(str(configured))
        if not path.is_absolute():
            path = self.project_root / path
        return path

    def _load_expected_catalog(self) -> tuple[list[ApiEndpoint], list[str], bool]:
        warnings: list[str] = []
        if not self.expected_catalog_path.exists():
            return (
                [],
                [f"expected catalog not found: {_display_path(self.project_root, self.expected_catalog_path)}"],
                False,
            )

        raw = yaml.safe_load(self.expected_catalog_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            return [], ["expected catalog must be a YAML mapping"], False
        if "api_endpoints" not in raw:
            return [], ["expected catalog has no api_endpoints section"], False

        entries = raw["api_endpoints"]
        if not isinstance(entries, list):
            return [], ["expected catalog api_endpoints must be a list"], False

        endpoints: list[ApiEndpoint] = []
        for index, entry in enumerate(entries):
            endpoint, warning = _catalog_entry_to_endpoint(entry, index)
            if warning:
                warnings.append(warning)
            if endpoint is not None:
                endpoints.append(endpoint)
        return _sort_endpoints(endpoints), warnings, True

    def _collect_implemented_endpoints(self) -> list[ApiEndpoint]:
        endpoints: list[ApiEndpoint] = []
        for route_file in self._route_files():
            route_path = _route_file_to_api_path(self.project_root, route_file)
            if route_path is None:
                continue
            source = route_file.read_text(encoding="utf-8")
            methods = _extract_exported_http_methods(source)
            auth_required = _infer_auth_required(source, route_path)
            relative_source = _display_path(self.project_root, route_file)
            endpoints.extend(
                ApiEndpoint(route_path, method, auth_required, relative_source)
                for method in sorted(methods)
            )
        return _sort_endpoints(endpoints)

    def _route_files(self) -> list[Path]:
        files: set[Path] = set()
        for pattern in self.route_globs:
            files.update(path for path in self.project_root.glob(str(pattern)) if path.is_file())
        return sorted(files)

    def _compare(
        self,
        expected: list[ApiEndpoint],
        implemented: list[ApiEndpoint],
        warnings: list[str],
    ) -> ApiDriftResult:
        expected_by_key = {endpoint.key: endpoint for endpoint in expected}
        implemented_by_key = {endpoint.key: endpoint for endpoint in implemented}

        missing = [
            endpoint
            for key, endpoint in expected_by_key.items()
            if key not in implemented_by_key
        ]
        extra = [
            endpoint
            for key, endpoint in implemented_by_key.items()
            if key not in expected_by_key
        ]
        auth_mismatches = [
            ApiAuthMismatch(
                path=expected_endpoint.path,
                method=expected_endpoint.method,
                expected_auth_required=expected_endpoint.auth_required,
                actual_auth_required=implemented_by_key[key].auth_required or False,
                source=implemented_by_key[key].source,
            )
            for key, expected_endpoint in expected_by_key.items()
            if key in implemented_by_key
            and expected_endpoint.auth_required is not None
            and implemented_by_key[key].auth_required != expected_endpoint.auth_required
        ]

        status = "drift" if missing or extra or auth_mismatches else "ok"
        return ApiDriftResult(
            status=status,
            expected=_sort_endpoints(expected),
            implemented=_sort_endpoints(implemented),
            missing=_sort_endpoints(missing),
            extra=_sort_endpoints(extra),
            auth_mismatches=sorted(auth_mismatches, key=lambda item: (item.path, item.method)),
            warnings=list(warnings),
        )

    def _publish_drift_event(self, result: ApiDriftResult) -> DriftEvent | None:
        bus = _event_bus_from_settings(self.settings)
        if bus is None:
            return None

        event = DriftEvent(
            source_artifact="design_md",
            target_artifact="implementation",
            change_type="modified",
            payload={
                "description": "API endpoint drift detected between expected catalog and route.ts files.",
                "expected_catalog_path": _display_path(self.project_root, self.expected_catalog_path),
                "design_file_path": _display_path(self.project_root, self.design_file_path),
                "missing": [endpoint.to_dict() for endpoint in result.missing],
                "extra": [endpoint.to_dict() for endpoint in result.extra],
                "auth_mismatches": [mismatch.to_dict() for mismatch in result.auth_mismatches],
                "suggested_action": "Align docs/extracted/expected_catalog.yaml and app/api/**/route.ts.",
            },
            severity="amber",
            fix_strategy="hitl",
            kind="api_drift",
        )
        bus.publish(event)
        return event


def _load_settings(settings: Any) -> dict[str, Any]:
    provided = settings if isinstance(settings, dict) else {}
    project_type = str(provided.get("project_type", "web"))
    defaults_path = Path(__file__).with_name("defaults") / f"{project_type}.yaml"
    defaults: dict[str, Any] = {}
    if defaults_path.exists():
        defaults = yaml.safe_load(defaults_path.read_text(encoding="utf-8")) or {}
        if not isinstance(defaults, dict):
            defaults = {}
    merged = dict(defaults)
    merged.update(provided)
    merged["project_type"] = project_type
    return merged


def _catalog_entry_to_endpoint(entry: Any, index: int) -> tuple[ApiEndpoint | None, str | None]:
    if not isinstance(entry, dict):
        return None, f"api_endpoints[{index}] must be a mapping"

    raw_path = entry.get("path")
    raw_method = entry.get("method")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None, f"api_endpoints[{index}].path must be a non-empty string"
    if not isinstance(raw_method, str) or not raw_method.strip():
        return None, f"api_endpoints[{index}].method must be a non-empty string"

    method = raw_method.upper()
    if method not in HTTP_METHODS:
        return None, f"api_endpoints[{index}].method is not a supported HTTP method: {raw_method}"

    raw_auth = entry.get("auth_required")
    if raw_auth is not None and not isinstance(raw_auth, bool):
        return None, f"api_endpoints[{index}].auth_required must be a boolean when present"

    return ApiEndpoint(_normalize_api_path(raw_path), method, raw_auth), None


def _route_file_to_api_path(project_root: Path, route_file: Path) -> str | None:
    try:
        relative = route_file.relative_to(project_root)
    except ValueError:
        relative = route_file

    parts = list(relative.parts)
    for index in range(len(parts) - 1):
        if parts[index] == "app" and index + 1 < len(parts) and parts[index + 1] == "api":
            route_parts = parts[index + 2 : -1]
            break
    else:
        return None

    visible_parts = [
        _normalize_route_segment(part)
        for part in route_parts
        if not (part.startswith("(") and part.endswith(")"))
    ]
    return _normalize_api_path("/api/" + "/".join(part for part in visible_parts if part))


def _normalize_route_segment(segment: str) -> str:
    if segment.startswith("[[...") and segment.endswith("]]"):
        return "{..." + segment[5:-2] + "}"
    if segment.startswith("[...") and segment.endswith("]"):
        return "{..." + segment[4:-1] + "}"
    if segment.startswith("[") and segment.endswith("]"):
        return "{" + segment[1:-1] + "}"
    return segment


def _normalize_api_path(path: str) -> str:
    normalized = path.strip().split("?", 1)[0]
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    normalized = re.sub(r"/:([A-Za-z_][A-Za-z0-9_]*)", r"/{\1}", normalized)
    normalized = re.sub(r"/\[([A-Za-z_][A-Za-z0-9_]*)\]", r"/{\1}", normalized)
    normalized = re.sub(r"/+", "/", normalized)
    if len(normalized) > 1:
        normalized = normalized.rstrip("/")
    return normalized


def _extract_exported_http_methods(source: str) -> set[str]:
    methods: set[str] = set()
    method_pattern = "|".join(sorted(HTTP_METHODS))

    for match in re.finditer(
        rf"\bexport\s+(?:async\s+)?function\s+({method_pattern})\b",
        source,
    ):
        methods.add(match.group(1))

    for match in re.finditer(
        rf"\bexport\s+const\s+({method_pattern})\b\s*=",
        source,
    ):
        methods.add(match.group(1))

    for export_block in re.findall(r"\bexport\s*\{([^}]+)\}", source):
        for token in export_block.split(","):
            name = token.strip().split(" as ")[-1].strip()
            if name in HTTP_METHODS:
                methods.add(name)

    return methods


def _infer_auth_required(source: str, path: str) -> bool:
    directive = re.search(r"auth_required\s*:\s*(true|false)", source, flags=re.IGNORECASE)
    if directive:
        return directive.group(1).lower() == "true"

    if re.search(r"@auth\s+(required|private|protected)", source, flags=re.IGNORECASE):
        return True
    if re.search(r"@auth\s+(public|none|optional)", source, flags=re.IGNORECASE):
        return False

    if _is_conventionally_public_path(path):
        return False

    auth_markers = (
        "getServerSession",
        "requireAuth",
        "requireSession",
        "withAuth",
        "verifySession",
        "currentUser",
        "auth(",
        "verifyApiKey",
        "requireApiKey",
        "apiKey",
    )
    return any(marker in source for marker in auth_markers)


def _is_conventionally_public_path(path: str) -> bool:
    return (
        path == "/api/health"
        or path.endswith("/health")
        or "/webhooks/" in path
        or path.startswith("/api/auth/")
        or path == "/api/auth/{...nextauth}"
    )


def _event_bus_from_settings(settings: dict[str, Any]) -> EventBus | None:
    configured = settings.get("event_bus") or settings.get("coherence_bus") or settings.get("bus")
    if isinstance(configured, EventBus):
        return configured
    return _coherence_bus


def _sort_endpoints(endpoints: list[ApiEndpoint]) -> list[ApiEndpoint]:
    return sorted(endpoints, key=lambda endpoint: (endpoint.path, endpoint.method))


def _display_path(project_root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(project_root))
    except ValueError:
        return str(path)


__all__ = [
    "ApiAuthMismatch",
    "ApiDriftLinker",
    "ApiDriftResult",
    "ApiEndpoint",
    "EXPECTED_CATALOG_SCHEMA",
    "set_coherence_bus",
]
