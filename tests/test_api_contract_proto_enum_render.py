"""Regression: the api-contract template must render proto/GraphQL enum schemas.

Piece 0 (blocking) — ``codd extract`` crashed on ANY project carrying a proto
enum because the Jinja template wrote ``schema.values``. On a dict that resolves
to the built-in ``dict.values`` METHOD (truthy → the ``elif`` branch is taken),
and the subsequent ``| join`` then explodes with
``'builtin_function_or_method' object is not iterable``. The fix accesses the
``values`` KEY explicitly and renders the proto list-of-dicts by their names.
"""

from __future__ import annotations

from codd.parsing.api_specs import ProtobufExtractor
from codd.synth import _build_environment, _render_api_contract


PROTO_SRC = """\
syntax = "proto3";

enum Status {
  STATUS_UNKNOWN = 0;
  STATUS_ACTIVE = 1;
  STATUS_ARCHIVED = 2;
}

message Item {
  string id = 1;
  Status status = 2;
}
"""


def test_render_api_contract_with_proto_enum_does_not_crash():
    spec = ProtobufExtractor().extract_services(PROTO_SRC, "api/item.proto")
    enum_schemas = [s for s in spec.schemas if s.get("kind") == "enum"]
    assert enum_schemas, "fixture must produce a proto enum schema (list-of-dicts values)"
    # Sanity: proto enum values are dicts {name, number} (the crash trigger).
    assert isinstance(enum_schemas[0]["values"][0], dict)

    env = _build_environment()
    # Must not raise; before the fix this raised
    # ``'builtin_function_or_method' object is not iterable``.
    rendered = _render_api_contract(env, "api/item.proto", spec, "2026-06-25")

    assert "Status" in rendered
    # The enum value NAMES must appear in the rendered "Values:" line.
    assert "STATUS_ACTIVE" in rendered
    assert "STATUS_ARCHIVED" in rendered


def test_render_api_contract_with_schema_lacking_values_key():
    """A schema with NO properties/fields/values must not crash.

    This is the EXACT Gson failure: a proto message/service schema reaches the
    enum ``elif`` without a ``values`` key. Jinja's ``schema["values"]`` falls
    back to ``getattr`` → the built-in ``dict.values`` METHOD (truthy), so the
    branch was wrongly entered and ``join`` exploded. The fix uses
    ``schema.get("values")`` (a real ``dict.get`` returning ``None``).
    """
    spec = ProtobufExtractor().extract_services(PROTO_SRC, "api/item.proto")
    # Inject a bare schema dict with none of properties/fields/values.
    spec.schemas.append({"name": "BareMessage", "kind": "message"})

    env = _build_environment()
    rendered = _render_api_contract(env, "api/item.proto", spec, "2026-06-25")

    assert "BareMessage" in rendered
    assert "Structural details unavailable" in rendered


def test_render_api_contract_with_graphql_style_string_enum_values():
    """GraphQL enums carry list-of-STRINGS values; the same line must render."""
    spec = ProtobufExtractor().extract_services(PROTO_SRC, "api/item.proto")
    # Replace the proto dict values with GraphQL-style plain strings to pin that
    # the fixed template handles both shapes (proto dicts vs graphql strings).
    for schema in spec.schemas:
        if schema.get("kind") == "enum":
            schema["values"] = ["ACTIVE", "ARCHIVED"]

    env = _build_environment()
    rendered = _render_api_contract(env, "api/item.proto", spec, "2026-06-25")

    assert "ACTIVE" in rendered
    assert "ARCHIVED" in rendered
