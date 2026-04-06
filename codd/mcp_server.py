"""CoDD MCP Server — expose CoDD tools via Model Context Protocol (stdio).

Zero external dependencies — implements MCP JSON-RPC over stdin/stdout directly.
This allows any MCP-compatible client (Claude Code, Cursor, etc.) to call CoDD
tools without installing the MCP Python SDK.

Usage:
    codd mcp-server                     # stdio mode (default)
    codd mcp-server --project /my/repo  # specify project root

Claude Code config (~/.claude/claude_code_config.json):
    {
      "mcpServers": {
        "codd": {
          "command": "codd",
          "args": ["mcp-server", "--project", "/path/to/your/project"]
        }
      }
    }
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from codd.bridge import load_bridge_registry


# ── JSON-RPC helpers ──────────────────────────────────────────────

def _jsonrpc_response(id: int | str | None, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": id, "result": result}


def _jsonrpc_error(id: int | str | None, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}


# ── Tool definitions ──────────────────────────────────────────────

TOOLS = [
    {
        "name": "codd_validate",
        "description": "Check frontmatter integrity and graph consistency of CoDD documents.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "codd_impact",
        "description": "Analyze change impact for a given document or file. Returns affected nodes with depth and confidence.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "Document node ID or file path to analyze impact for",
                },
            },
            "required": ["target"],
        },
    },
    {
        "name": "codd_policy",
        "description": "Check source code against enterprise policy rules defined in codd.yaml. Reports forbidden patterns (e.g., hardcoded passwords) and missing required patterns (e.g., logging imports).",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "codd_scan",
        "description": "Build or rebuild the dependency graph from frontmatter in design documents.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "codd_measure",
        "description": "Show project metrics: graph health, document coverage, validation status, policy compliance, and overall health score (0-100).",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


# ── Tool handlers ─────────────────────────────────────────────────

def _handle_validate(project_root: Path, _args: dict) -> dict:
    from codd.config import find_codd_dir
    from codd.validator import validate_project

    codd_dir = find_codd_dir(project_root)
    if codd_dir is None:
        return {"isError": True, "content": [{"type": "text", "text": "CoDD config dir not found. Run 'codd init' first."}]}

    result = validate_project(project_root, codd_dir)
    lines = [
        f"Documents checked: {result.documents_checked}",
        f"Errors: {result.error_count}  Warnings: {result.warning_count}",
    ]
    for issue in result.issues:
        lines.append(f"  [{issue.level}] {issue.location}: {issue.message}")

    status = "PASS" if result.error_count == 0 else "FAIL"
    return {"content": [{"type": "text", "text": f"Validation: {status}\n" + "\n".join(lines)}]}


def _handle_impact(project_root: Path, args: dict) -> dict:
    from codd.config import find_codd_dir, load_project_config
    from codd.graph import CEG

    codd_dir = find_codd_dir(project_root)
    if codd_dir is None:
        return {"isError": True, "content": [{"type": "text", "text": "CoDD config dir not found."}]}

    target = args.get("target", "")
    if not target:
        return {"isError": True, "content": [{"type": "text", "text": "target is required."}]}

    scan_dir = codd_dir / "scan"
    if not scan_dir.exists():
        return {"isError": True, "content": [{"type": "text", "text": "No scan data. Run 'codd scan' first."}]}

    config = load_project_config(project_root)
    max_depth = config.get("propagation", {}).get("max_depth", 10)

    ceg = CEG(scan_dir)
    try:
        impacts = ceg.propagate_impact(target, max_depth=max_depth)
    finally:
        ceg.close()

    if not impacts:
        return {"content": [{"type": "text", "text": f"No downstream impact from '{target}'."}]}

    lines = [f"Impact from '{target}': {len(impacts)} affected nodes\n"]
    for node_id, info in sorted(impacts.items(), key=lambda x: x[1].get("depth", 0)):
        depth = info.get("depth", "?")
        conf = info.get("confidence", 0)
        lines.append(f"  {node_id}  depth={depth}  confidence={conf:.2f}")

    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


def _handle_policy(project_root: Path, _args: dict) -> dict:
    from codd.policy import run_policy, format_policy_text
    result = run_policy(project_root)
    return {"content": [{"type": "text", "text": format_policy_text(result)}]}


def _handle_scan(project_root: Path, _args: dict) -> dict:
    from codd.config import find_codd_dir, load_project_config
    from codd.scanner import scan_project

    codd_dir = find_codd_dir(project_root)
    if codd_dir is None:
        return {"isError": True, "content": [{"type": "text", "text": "CoDD config dir not found."}]}

    config = load_project_config(project_root)
    scan_config = config.get("scan", {})
    result = scan_project(project_root, codd_dir, scan_config)

    return {"content": [{"type": "text", "text": f"Scan complete. Nodes: {result.get('nodes', 0)}, Edges: {result.get('edges', 0)}"}]}


def _handle_measure(project_root: Path, _args: dict) -> dict:
    from codd.measure import run_measure, format_measure_text
    result = run_measure(project_root)
    return {"content": [{"type": "text", "text": format_measure_text(result)}]}


HANDLERS = {
    "codd_validate": _handle_validate,
    "codd_impact": _handle_impact,
    "codd_policy": _handle_policy,
    "codd_scan": _handle_scan,
    "codd_measure": _handle_measure,
}


def _registered_tools() -> list[dict]:
    tools = list(TOOLS)
    tools.extend(load_bridge_registry().mcp_tools.values())
    return tools


def _registered_handlers() -> dict[str, object]:
    handlers = dict(HANDLERS)
    handlers.update(load_bridge_registry().mcp_handlers)
    return handlers


# ── MCP Protocol Handler ─────────────────────────────────────────

def handle_request(request: dict, project_root: Path) -> dict | None:
    """Handle a single MCP JSON-RPC request. Returns response dict or None for notifications."""
    method = request.get("method", "")
    req_id = request.get("id")
    params = request.get("params", {})

    if method == "initialize":
        return _jsonrpc_response(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {},
            },
            "serverInfo": {
                "name": "codd",
                "version": "1.6.0",
            },
        })

    if method == "notifications/initialized":
        return None  # No response for notifications

    if method == "tools/list":
        return _jsonrpc_response(req_id, {"tools": _registered_tools()})

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        handler = _registered_handlers().get(tool_name)
        if handler is None:
            return _jsonrpc_error(req_id, -32601, f"Unknown tool: {tool_name}")

        try:
            result = handler(project_root, arguments)
        except Exception as e:
            result = {"isError": True, "content": [{"type": "text", "text": f"Error: {e}"}]}

        return _jsonrpc_response(req_id, result)

    if method == "ping":
        return _jsonrpc_response(req_id, {})

    # Unknown method
    if req_id is not None:
        return _jsonrpc_error(req_id, -32601, f"Method not found: {method}")

    return None  # Ignore unknown notifications


def run_stdio(project_root: Path) -> None:
    """Run the MCP server on stdio (JSON-RPC over stdin/stdout)."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            response = _jsonrpc_error(None, -32700, "Parse error")
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
            continue

        response = handle_request(request, project_root)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
