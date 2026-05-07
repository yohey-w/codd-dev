"""Loadability tests for the backend_grpc_proto lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "backend_grpc_proto"
EXPECTED_KINDS = {
    "grpc_service_gap",
    "grpc_message_gap",
    "grpc_scalar_type_gap",
    "grpc_enum_gap",
    "grpc_stream_gap",
    "grpc_status_code_gap",
    "grpc_deadline_gap",
    "grpc_metadata_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_backend_grpc_proto_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "backend_grpc_proto"
    assert "proto3 and gRPC Coverage Lexicon" in config.prompt_extension_content
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_backend_grpc_proto_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "backend_grpc_proto"
    assert manifest["lexicon_name"] == "backend_grpc_proto"
    assert manifest["source_url"] == "https://protobuf.dev/programming-guides/proto3/"
    assert "proto3" in manifest["source_version"]
    assert manifest["observation_dimensions"] == 8
    titles = {item["title"] for item in manifest["references"]}
    assert "Protocol Buffers Language Guide (proto3)" in titles
    assert "gRPC Core concepts, architecture and lifecycle" in titles
    assert "gRPC Status Codes" in titles


def test_backend_grpc_proto_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_backend_grpc_proto_loaded_prompt_includes_base_prompt_without_frontmatter():
    config = load_lexicon(LEXICON_ROOT)

    assert "Elicitation Prompt L0" in config.prompt_extension_content
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )
