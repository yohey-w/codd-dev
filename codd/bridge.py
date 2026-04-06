"""Bridge helpers for optional codd-pro extensions and plugin registration."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any, Callable


PLUGIN_GROUP = "codd.plugins"
PRO_COMMAND_INSTALL_MESSAGE = (
    "このコマンドは codd-pro に移動しました。"
    "pip install codd-pro でインストールできます。"
)


@dataclass
class BridgeRegistry:
    """Mutable registry populated by entry-point plugins."""

    require_plugin: Any | None = None
    validator_handler: Callable[..., Any] | None = None
    policy_handler: Callable[..., Any] | None = None
    risk_builder: Callable[..., Any] | None = None
    command_handlers: dict[str, Callable[..., Any]] = field(default_factory=dict)
    mcp_tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    mcp_handlers: dict[str, Callable[..., Any]] = field(default_factory=dict)

    def register_require_plugin(self, plugin: Any) -> None:
        self.require_plugin = plugin

    def register_validator(self, handler: Callable[..., Any]) -> None:
        self.validator_handler = handler

    def register_policy(self, handler: Callable[..., Any]) -> None:
        self.policy_handler = handler

    def register_risk_builder(self, handler: Callable[..., Any]) -> None:
        self.risk_builder = handler

    def register_command(self, name: str, handler: Callable[..., Any]) -> None:
        self.command_handlers[name] = handler

    def register_mcp_tool(self, tool: dict[str, Any], handler: Callable[..., Any]) -> None:
        name = str(tool.get("name") or "")
        if not name:
            raise ValueError("MCP tool registration requires a non-empty tool name")
        self.mcp_tools[name] = tool
        self.mcp_handlers[name] = handler


def _iter_plugin_entry_points():
    try:
        return tuple(entry_points(group=PLUGIN_GROUP))
    except TypeError:
        return tuple(entry_points().select(group=PLUGIN_GROUP))


def load_bridge_registry() -> BridgeRegistry:
    """Load all registered bridge plugins, ignoring broken extensions."""
    registry = BridgeRegistry()

    for plugin_entry in _iter_plugin_entry_points():
        try:
            plugin = plugin_entry.load()
        except Exception:
            continue

        register = getattr(plugin, "register", plugin)
        if not callable(register):
            continue

        try:
            register(registry)
        except Exception:
            continue

    return registry


def get_command_handler(name: str) -> Callable[..., Any] | None:
    """Return the registered handler for a Pro-only CLI command."""
    return load_bridge_registry().command_handlers.get(name)
