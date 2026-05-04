"""Tests for codd validate --screen-flow."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from codd.cli import CoddCLIError, main
from codd.coherence_engine import EventBus
from codd.screen_flow_validator import (
    compute_screen_flow_drifts,
    get_filesystem_routes,
    parse_screen_flow_routes,
    set_coherence_bus,
    validate_screen_flow,
)


def test_parse_routes_heading(tmp_path):
    screen_flow = tmp_path / "screen-flow.md"
    screen_flow.write_text("## /login\n", encoding="utf-8")

    assert parse_screen_flow_routes(screen_flow) == ["/login"]


def test_parse_routes_inline(tmp_path):
    screen_flow = tmp_path / "screen-flow.md"
    screen_flow.write_text("route: /dashboard\n", encoding="utf-8")

    assert parse_screen_flow_routes(screen_flow) == ["/dashboard"]


def test_parse_routes_missing_file(tmp_path):
    assert parse_screen_flow_routes(tmp_path / "missing.md") == []


def test_parse_routes_bullet_and_mermaid_tokens(tmp_path):
    screen_flow = tmp_path / "screen-flow.md"
    screen_flow.write_text(
        """# Flow
- /settings

```mermaid
graph LR
  "/courses/:id"
```
""",
        encoding="utf-8",
    )

    assert parse_screen_flow_routes(screen_flow) == ["/settings", "/courses/:id"]


def test_compute_drifts_screen_only():
    drifts = compute_screen_flow_drifts(["/login"], [])

    assert [(drift.route, drift.source) for drift in drifts] == [("/login", "screen_flow_only")]


def test_compute_drifts_fs_only():
    drifts = compute_screen_flow_drifts([], ["/dashboard"])

    assert [(drift.route, drift.source) for drift in drifts] == [("/dashboard", "filesystem_only")]


def test_compute_drifts_no_diff():
    assert compute_screen_flow_drifts(["/login/"], ["/login"]) == []


def test_validate_no_screen_flow_file(tmp_path):
    assert validate_screen_flow(tmp_path, {"filesystem_routes": []}) == []


def test_no_routes_with_configured_base_dir_raises(tmp_path):
    (tmp_path / "screen-flow.md").write_text("## /login\n", encoding="utf-8")

    with pytest.raises(CoddCLIError, match="No filesystem routes found"):
        validate_screen_flow(
            tmp_path,
            {
                "filesystem_routes": [
                    {
                        "base_dir": "app",
                        "page_pattern": "page.tsx",
                        "url_template": "/{relative_dir}",
                    }
                ]
            },
        )


def test_no_routes_without_config_returns_empty(tmp_path):
    (tmp_path / "screen-flow.md").write_text("## /login\n", encoding="utf-8")

    assert validate_screen_flow(tmp_path, {}) == []


def test_error_message_includes_configured_dir(tmp_path):
    (tmp_path / "screen-flow.md").write_text("## /dashboard\n", encoding="utf-8")

    with pytest.raises(CoddCLIError) as exc_info:
        validate_screen_flow(
            tmp_path,
            {
                "filesystem_routes": [
                    {
                        "base_dir": "src/app",
                        "page_pattern": "page.tsx",
                    }
                ]
            },
        )

    message = str(exc_info.value)
    assert "src/app" in message
    assert "base_dir should point to the actual route directory" in message


def test_validate_screen_flow_compares_filesystem_routes(tmp_path):
    (tmp_path / "docs" / "extracted").mkdir(parents=True)
    (tmp_path / "docs" / "extracted" / "screen-flow.md").write_text("## /login\n", encoding="utf-8")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.tsx").write_text("// route fixture\n", encoding="utf-8")

    drifts = validate_screen_flow(
        tmp_path,
        {
            "filesystem_routes": [
                {
                    "base_dir": "app",
                    "page_pattern": "page.tsx",
                    "url_template": "/{relative_dir}",
                }
            ]
        },
    )

    assert [(drift.route, drift.source) for drift in drifts] == [
        ("/login", "screen_flow_only"),
        ("/", "filesystem_only"),
    ]


def test_get_filesystem_routes_handles_missing_config(tmp_path):
    assert get_filesystem_routes(tmp_path, {}) == []


def test_validate_publishes_screen_flow_drift_events(tmp_path):
    (tmp_path / "screen-flow.md").write_text("## /login\n## /missing\n", encoding="utf-8")
    (tmp_path / "app" / "login").mkdir(parents=True)
    (tmp_path / "app" / "login" / "page.tsx").write_text("// route fixture\n", encoding="utf-8")
    bus = EventBus()
    set_coherence_bus(bus)
    try:
        validate_screen_flow(
            tmp_path,
            {
                "filesystem_routes": [
                    {
                        "base_dir": "app",
                        "page_pattern": "page.tsx",
                        "url_template": "/{relative_dir}",
                    }
                ]
            },
        )
    finally:
        set_coherence_bus(None)

    events = bus.published_events()
    assert len(events) == 1
    assert events[0].kind == "screen_flow_drift"
    assert events[0].payload["route"] == "/missing"


def test_cli_validate_screen_flow_help():
    result = CliRunner().invoke(main, ["validate", "--screen-flow", "--help"])

    assert result.exit_code == 0
