"""Tests that bridge plugin loading remains intact after Pro Gate removal."""

from codd.bridge import BridgeRegistry, load_bridge_registry


def test_bridge_registry_loads():
    """BridgeRegistry still loads through the plugin entry-point path."""
    registry = load_bridge_registry()

    assert isinstance(registry, BridgeRegistry)


def test_bridge_registry_has_no_legacy_command_registry():
    """The old Pro Gate registry field is removed."""
    registry = load_bridge_registry()

    assert not hasattr(registry, "command" + "_handlers")


def test_bridge_registry_plugin_system_intact():
    """Core plugin registration fields and methods are preserved."""
    registry = load_bridge_registry()

    assert hasattr(registry, "require_plugin")
    assert hasattr(registry, "validator_handler")
    assert hasattr(registry, "policy_handler")
    assert hasattr(registry, "mcp_tools")
    assert hasattr(registry, "mcp_handlers")
    assert hasattr(registry, "register_require_plugin")
    assert hasattr(registry, "register_validator")
    assert hasattr(registry, "register_policy")
    assert hasattr(registry, "register_mcp_tool")
