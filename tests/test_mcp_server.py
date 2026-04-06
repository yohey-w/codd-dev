"""Tests for CoDD MCP server — JSON-RPC protocol handling."""

from __future__ import annotations

import json

import pytest
import yaml

from codd.mcp_server import TOOLS, handle_request


class _FakeEntryPoint:
    def __init__(self, loaded):
        self._loaded = loaded

    def load(self):
        return self._loaded


@pytest.fixture
def codd_project(tmp_path):
    """Create a minimal CoDD project for testing."""
    project = tmp_path / "project"
    project.mkdir()
    src = project / "src"
    src.mkdir()
    (src / "main.py").write_text("import os\nx = 1\n", encoding="utf-8")

    codd_dir = project / "codd"
    codd_dir.mkdir()
    config = {
        "scan": {"source_dirs": ["src/"], "test_dirs": [], "doc_dirs": [], "config_files": [], "exclude": []},
        "policies": [],
    }
    (codd_dir / "codd.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return project


class TestProtocol:
    def test_initialize(self, codd_project):
        req = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        resp = handle_request(req, codd_project)
        assert resp["id"] == 1
        assert resp["result"]["protocolVersion"] == "2024-11-05"
        assert "tools" in resp["result"]["capabilities"]
        assert resp["result"]["serverInfo"]["name"] == "codd"

    def test_initialized_notification(self, codd_project):
        req = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        resp = handle_request(req, codd_project)
        assert resp is None

    def test_tools_list(self, codd_project):
        req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        resp = handle_request(req, codd_project)
        tools = resp["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        assert "codd_validate" in tool_names
        assert "codd_impact" in tool_names
        assert "codd_policy" in tool_names
        assert "codd_scan" in tool_names
        assert "codd_measure" in tool_names

    def test_ping(self, codd_project):
        req = {"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}}
        resp = handle_request(req, codd_project)
        assert resp["result"] == {}

    def test_unknown_method(self, codd_project):
        req = {"jsonrpc": "2.0", "id": 4, "method": "unknown/method", "params": {}}
        resp = handle_request(req, codd_project)
        assert "error" in resp
        assert resp["error"]["code"] == -32601

    def test_unknown_notification_ignored(self, codd_project):
        req = {"jsonrpc": "2.0", "method": "notifications/unknown"}
        resp = handle_request(req, codd_project)
        assert resp is None


class TestToolCalls:
    def test_validate(self, codd_project):
        req = {
            "jsonrpc": "2.0", "id": 10,
            "method": "tools/call",
            "params": {"name": "codd_validate", "arguments": {}},
        }
        resp = handle_request(req, codd_project)
        content = resp["result"]["content"][0]["text"]
        assert "Validation" in content
        assert "Documents checked" in content

    def test_policy_no_rules(self, codd_project):
        req = {
            "jsonrpc": "2.0", "id": 11,
            "method": "tools/call",
            "params": {"name": "codd_policy", "arguments": {}},
        }
        resp = handle_request(req, codd_project)
        content = resp["result"]["content"][0]["text"]
        assert "PASS" in content

    def test_policy_with_violation(self, codd_project):
        # Add a forbidden pattern rule
        config = {
            "scan": {"source_dirs": ["src/"], "test_dirs": [], "doc_dirs": [], "config_files": [], "exclude": []},
            "policies": [
                {"id": "SEC-001", "kind": "forbidden", "pattern": "import os", "severity": "CRITICAL", "glob": "*.py"}
            ],
        }
        (codd_project / "codd" / "codd.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

        req = {
            "jsonrpc": "2.0", "id": 12,
            "method": "tools/call",
            "params": {"name": "codd_policy", "arguments": {}},
        }
        resp = handle_request(req, codd_project)
        content = resp["result"]["content"][0]["text"]
        assert "FAIL" in content
        assert "SEC-001" in content

    def test_bridge_tool_registration(self, codd_project, monkeypatch):
        def register(registry):
            registry.register_mcp_tool(
                {
                    "name": "codd_pro_audit",
                    "description": "Bridge-provided audit tool.",
                    "inputSchema": {"type": "object", "properties": {}, "required": []},
                },
                lambda project_root, args: {"content": [{"type": "text", "text": f"audit:{project_root.name}:{args}"}]},
            )

        monkeypatch.setattr(
            "codd.bridge.entry_points",
            lambda *, group=None: (_FakeEntryPoint(register),),
        )

        list_req = {"jsonrpc": "2.0", "id": 13, "method": "tools/list", "params": {}}
        list_resp = handle_request(list_req, codd_project)
        tool_names = [t["name"] for t in list_resp["result"]["tools"]]
        assert "codd_pro_audit" in tool_names

        call_req = {
            "jsonrpc": "2.0", "id": 14,
            "method": "tools/call",
            "params": {"name": "codd_pro_audit", "arguments": {"diff_target": "HEAD"}},
        }
        call_resp = handle_request(call_req, codd_project)
        assert "audit:project" in call_resp["result"]["content"][0]["text"]

    def test_impact_missing_target(self, codd_project):
        req = {
            "jsonrpc": "2.0", "id": 14,
            "method": "tools/call",
            "params": {"name": "codd_impact", "arguments": {}},
        }
        resp = handle_request(req, codd_project)
        assert resp["result"]["isError"]
        assert "target is required" in resp["result"]["content"][0]["text"]

    def test_unknown_tool(self, codd_project):
        req = {
            "jsonrpc": "2.0", "id": 15,
            "method": "tools/call",
            "params": {"name": "nonexistent_tool", "arguments": {}},
        }
        resp = handle_request(req, codd_project)
        assert "error" in resp
        assert "Unknown tool" in resp["error"]["message"]


class TestToolDefinitions:
    def test_all_tools_have_required_fields(self):
        for tool in TOOLS:
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool
            assert tool["inputSchema"]["type"] == "object"

    def test_tool_count(self):
        assert len(TOOLS) == 5
