"""Tests that the legacy Pro Gate has been removed."""

import pytest


def test_legacy_pro_install_message_removed():
    """The legacy Pro install message is no longer exported from bridge."""
    name = "PRO_COMMAND" + "_INSTALL_MESSAGE"

    with pytest.raises(ImportError):
        exec(f"from codd.bridge import {name}", {})


def test_legacy_pro_dispatcher_removed():
    """The legacy Pro dispatcher is no longer present in the CLI module."""
    import codd.cli as cli_module

    assert not hasattr(cli_module, "_run" + "_pro_command")
