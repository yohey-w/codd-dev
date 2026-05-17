"""Tests for the cmd_345 K-2 warning emitter."""

from __future__ import annotations

import io

from codd.dag import Node
from codd.llm.criteria_expander import warn_if_operation_flow_unused


def _make_node_with_operation_flow() -> Node:
    return Node(
        id="docs/requirements/sample.md",
        kind="design_doc",
        path="docs/requirements/sample.md",
        attributes={
            "operation_flow": {
                "operations": [
                    {
                        "id": "create_x",
                        "actor": "admin",
                        "verb": "create",
                        "target": "x",
                        "ui_pattern": "single_form",
                    }
                ]
            }
        },
    )


def _make_plain_node() -> Node:
    return Node(
        id="docs/requirements/plain.md",
        kind="design_doc",
        path="docs/requirements/plain.md",
        attributes={"body": "no operation_flow here"},
    )


def test_warns_when_operation_flow_declared_and_impl_step_derive_missing():
    config = {"ai_commands": {"derive_considerations": "claude --print"}}
    nodes = [_make_node_with_operation_flow()]
    stream = io.StringIO()

    emitted = warn_if_operation_flow_unused(config, nodes, stream=stream)

    assert emitted is True
    output = stream.getvalue()
    assert "operation_flow" in output
    assert "impl_step_derive" in output


def test_no_warning_when_no_operation_flow_in_design_docs():
    config = {"ai_commands": {}}
    nodes = [_make_plain_node()]
    stream = io.StringIO()

    emitted = warn_if_operation_flow_unused(config, nodes, stream=stream)

    assert emitted is False
    assert stream.getvalue() == ""


def test_no_warning_when_impl_step_derive_is_configured():
    config = {
        "ai_commands": {
            "impl_step_derive": "codex exec --full-auto --model gpt-5.5 -",
        }
    }
    nodes = [_make_node_with_operation_flow()]
    stream = io.StringIO()

    emitted = warn_if_operation_flow_unused(config, nodes, stream=stream)

    assert emitted is False
    assert stream.getvalue() == ""


def test_no_warning_when_config_is_none():
    emitted = warn_if_operation_flow_unused(None, None)
    assert emitted is False


def test_warning_treats_empty_string_command_as_unset():
    config = {"ai_commands": {"impl_step_derive": "   "}}
    nodes = [_make_node_with_operation_flow()]
    stream = io.StringIO()

    emitted = warn_if_operation_flow_unused(config, nodes, stream=stream)

    assert emitted is True
    assert "impl_step_derive" in stream.getvalue()
