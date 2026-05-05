from __future__ import annotations

import importlib
from pathlib import Path

import yaml

from codd import drift_linkers
from codd.coherence_engine import EventBus
from codd.drift_linkers.api import ApiDriftLinker, EXPECTED_CATALOG_SCHEMA


def _write_design(project: Path) -> None:
    path = project / "docs" / "design" / "api_design.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# API design\n", encoding="utf-8")


def _write_catalog(project: Path, endpoints: list[dict]) -> Path:
    path = project / "docs" / "extracted" / "expected_catalog.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"api_endpoints": endpoints}), encoding="utf-8")
    return path


def _write_route(project: Path, relative: str, source: str = "export async function GET() {}\n") -> Path:
    path = project / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def test_api_drift_linker_registered(monkeypatch):
    monkeypatch.setattr(drift_linkers, "_REGISTRY", {})

    module = importlib.reload(importlib.import_module("codd.drift_linkers.api"))

    assert drift_linkers.get_registry()["api"] is module.ApiDriftLinker


def test_expected_catalog_schema_defines_api_endpoints_only():
    assert set(EXPECTED_CATALOG_SCHEMA) == {"api_endpoints"}
    endpoint_schema = EXPECTED_CATALOG_SCHEMA["api_endpoints"][0]
    assert endpoint_schema == {
        "path": "str",
        "method": "str",
        "auth_required": "bool optional",
    }


def test_no_catalog_skip_with_warn(tmp_path):
    _write_design(tmp_path)

    result = ApiDriftLinker(None, tmp_path, {"project_type": "web"}).run()

    assert result.status == "skipped"
    assert result.warnings == ["expected catalog not found: docs/extracted/expected_catalog.yaml"]


def test_no_route_files_skip_with_warn(tmp_path):
    _write_design(tmp_path)
    _write_catalog(tmp_path, [{"path": "/api/courses", "method": "GET", "auth_required": True}])

    result = ApiDriftLinker(None, tmp_path, {"project_type": "web"}).run()

    assert result.status == "skipped"
    assert "no API route files found" in result.warnings[-1]


def test_missing_endpoint_detected(tmp_path):
    _write_design(tmp_path)
    _write_catalog(
        tmp_path,
        [
            {"path": "/api/courses", "method": "GET", "auth_required": True},
            {"path": "/api/health", "method": "GET", "auth_required": False},
        ],
    )
    _write_route(tmp_path, "app/api/health/route.ts")

    result = ApiDriftLinker(None, tmp_path, {"project_type": "web"}).run()

    assert result.status == "drift"
    assert [(endpoint.path, endpoint.method) for endpoint in result.missing] == [
        ("/api/courses", "GET")
    ]


def test_extra_endpoint_detected(tmp_path):
    _write_design(tmp_path)
    _write_catalog(tmp_path, [])
    _write_route(tmp_path, "app/api/courses/route.ts")

    result = ApiDriftLinker(None, tmp_path, {"project_type": "web"}).run()

    assert result.status == "drift"
    assert [(endpoint.path, endpoint.method) for endpoint in result.extra] == [
        ("/api/courses", "GET")
    ]


def test_exact_match_no_drift(tmp_path):
    _write_design(tmp_path)
    _write_catalog(tmp_path, [{"path": "/api/courses", "method": "GET", "auth_required": True}])
    _write_route(
        tmp_path,
        "app/api/courses/route.ts",
        "import { getServerSession } from 'next-auth';\n"
        "export async function GET() { return getServerSession(); }\n",
    )

    result = ApiDriftLinker(None, tmp_path, {"project_type": "web"}).run()

    assert result.status == "ok"
    assert result.has_drift is False
    assert result.missing == []
    assert result.extra == []
    assert result.auth_mismatches == []


def test_drift_event_published(tmp_path):
    _write_design(tmp_path)
    _write_catalog(tmp_path, [{"path": "/api/courses", "method": "GET", "auth_required": True}])
    _write_route(tmp_path, "app/api/health/route.ts")
    bus = EventBus()

    result = ApiDriftLinker(None, tmp_path, {"project_type": "web", "event_bus": bus}).run()

    events = bus.published_events()
    assert result.events == events
    assert [event.kind for event in events] == ["api_drift"]
    assert events[0].payload["missing"][0]["path"] == "/api/courses"
    assert events[0].severity == "amber"


def test_auth_required_mismatch_detected(tmp_path):
    _write_design(tmp_path)
    _write_catalog(tmp_path, [{"path": "/api/courses", "method": "GET", "auth_required": True}])
    _write_route(tmp_path, "app/api/courses/route.ts")

    result = ApiDriftLinker(None, tmp_path, {"project_type": "web"}).run()

    assert result.status == "drift"
    assert [mismatch.to_dict() for mismatch in result.auth_mismatches] == [
        {
            "path": "/api/courses",
            "method": "GET",
            "expected_auth_required": True,
            "actual_auth_required": False,
            "source": "app/api/courses/route.ts",
        }
    ]


def test_generality_web_default_design_file_path(tmp_path):
    linker = ApiDriftLinker(None, tmp_path, {"project_type": "web"})

    assert linker.design_file_path == tmp_path / "docs" / "design" / "api_design.md"
    assert linker.expected_catalog_path == tmp_path / "docs" / "extracted" / "expected_catalog.yaml"


def test_generality_cli_default_disabled(tmp_path):
    _write_design(tmp_path)
    _write_catalog(tmp_path, [{"path": "/api/courses", "method": "GET"}])
    _write_route(tmp_path, "app/api/courses/route.ts")

    result = ApiDriftLinker(None, tmp_path, {"project_type": "cli"}).run()

    assert result.status == "skipped"
    assert result.warnings == ["api drift linker disabled for project_type=cli"]


def test_src_app_dynamic_route_normalizes_to_catalog_braces(tmp_path):
    _write_design(tmp_path)
    _write_catalog(tmp_path, [{"path": "/api/courses/{id}", "method": "GET"}])
    _write_route(tmp_path, "src/app/api/courses/[id]/route.ts")

    result = ApiDriftLinker(None, tmp_path, {"project_type": "web"}).run()

    assert result.status == "ok"
    assert [(endpoint.path, endpoint.method) for endpoint in result.implemented] == [
        ("/api/courses/{id}", "GET")
    ]


def test_catalog_colon_dynamic_route_normalizes_to_braces(tmp_path):
    _write_design(tmp_path)
    _write_catalog(tmp_path, [{"path": "/api/courses/:id", "method": "GET"}])
    _write_route(tmp_path, "app/api/courses/[id]/route.ts")

    result = ApiDriftLinker(None, tmp_path, {"project_type": "web"}).run()

    assert result.status == "ok"
    assert result.expected[0].path == "/api/courses/{id}"


def test_const_export_methods_detected(tmp_path):
    _write_design(tmp_path)
    _write_catalog(
        tmp_path,
        [
            {"path": "/api/courses", "method": "GET"},
            {"path": "/api/courses", "method": "POST"},
        ],
    )
    _write_route(
        tmp_path,
        "app/api/courses/route.ts",
        "export const GET = async () => Response.json({});\n"
        "export const POST = async () => Response.json({});\n",
    )

    result = ApiDriftLinker(None, tmp_path, {"project_type": "web"}).run()

    assert result.status == "ok"
    assert [(endpoint.path, endpoint.method) for endpoint in result.implemented] == [
        ("/api/courses", "GET"),
        ("/api/courses", "POST"),
    ]


def test_export_list_methods_detected(tmp_path):
    _write_design(tmp_path)
    _write_catalog(tmp_path, [{"path": "/api/courses", "method": "PUT"}])
    _write_route(
        tmp_path,
        "app/api/courses/route.ts",
        "const handler = async () => Response.json({});\nexport { handler as PUT };\n",
    )

    result = ApiDriftLinker(None, tmp_path, {"project_type": "web"}).run()

    assert result.status == "ok"
    assert result.implemented[0].method == "PUT"


def test_invalid_catalog_entry_warns_and_is_ignored(tmp_path):
    _write_design(tmp_path)
    _write_catalog(
        tmp_path,
        [
            {"path": "", "method": "GET"},
            {"path": "/api/health", "method": "GET", "auth_required": False},
        ],
    )
    _write_route(tmp_path, "app/api/health/route.ts")

    result = ApiDriftLinker(None, tmp_path, {"project_type": "web"}).run()

    assert result.status == "ok"
    assert result.warnings == ["api_endpoints[0].path must be a non-empty string"]
    assert [(endpoint.path, endpoint.method) for endpoint in result.expected] == [
        ("/api/health", "GET")
    ]
