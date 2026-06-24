"""Tests for codd.frontmatter — the single source of truth for frontmatter parsing.

Covers every semantic divergence the seven legacy parsers had: nested vs
flat ``codd:`` shapes, strict vs lenient error handling, scalar list
coercion, alias mapping, frontmatter-or-whole-file-YAML duality, malformed
YAML, and missing files — plus the deliberate behavior fixes applied to
call sites during unification.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codd.frontmatter import (
    FrontmatterError,
    apply_aliases,
    as_list,
    codd_block,
    frontmatter_or_yaml_payload,
    parse_frontmatter,
    read_frontmatter,
    split_frontmatter,
)

NESTED_DOC = """---
codd:
  node_id: "req:FR-01"
  type: requirement
  depends_on:
    - id: "file:src/auth.ts"
      relation: implements
---

# FR-01
"""

FLAT_DOC = """---
node_id: "design:auth"
depends_on:
  - docs/requirements.md
---

# Design
"""


# ── split_frontmatter ──────────────────────────────────────────


def test_split_nested_codd_doc():
    front, body = split_frontmatter(NESTED_DOC)
    assert front["codd"]["node_id"] == "req:FR-01"
    assert body == "\n# FR-01\n"


def test_split_flat_doc():
    front, body = split_frontmatter(FLAT_DOC)
    assert front["node_id"] == "design:auth"
    assert front["depends_on"] == ["docs/requirements.md"]
    assert body == "\n# Design\n"


def test_split_no_frontmatter_returns_original_text():
    text = "# Just markdown\n\ncontent\n"
    assert split_frontmatter(text) == ({}, text)


def test_split_unclosed_block_lenient_returns_original():
    text = "---\nnode_id: x\n# never closed\n"
    assert split_frontmatter(text) == ({}, text)


def test_split_unclosed_block_strict_raises():
    with pytest.raises(FrontmatterError) as exc_info:
        split_frontmatter("---\nnode_id: x\n", strict=True)
    assert exc_info.value.code == "unclosed"


def test_split_invalid_yaml_lenient_returns_original():
    text = "---\nfoo: [unclosed\n---\nbody\n"
    assert split_frontmatter(text) == ({}, text)


def test_split_invalid_yaml_strict_raises():
    with pytest.raises(FrontmatterError) as exc_info:
        split_frontmatter("---\nfoo: [unclosed\n---\nbody\n", strict=True)
    assert exc_info.value.code == "invalid_yaml"
    assert "invalid YAML frontmatter" in str(exc_info.value)


def test_split_non_mapping_lenient_returns_body_after_block():
    front, body = split_frontmatter("---\n- a\n- b\n---\nbody\n")
    assert front == {}
    assert body == "body\n"


def test_split_non_mapping_strict_raises():
    with pytest.raises(FrontmatterError) as exc_info:
        split_frontmatter("---\n- a\n---\nbody\n", strict=True)
    assert exc_info.value.code == "not_mapping"


def test_split_empty_block_is_empty_mapping():
    front, body = split_frontmatter("---\n---\nbody\n")
    assert front == {}
    assert body == "body\n"


def test_split_tolerates_bom_before_opening_delimiter():
    front, _body = split_frontmatter("﻿---\nnode_id: x\n---\nbody\n")
    assert front == {"node_id": "x"}


def test_split_body_preserves_exact_bytes():
    text = "---\na: 1\n---\n\n\nline1\nline2"
    _front, body = split_frontmatter(text)
    assert body == "\n\nline1\nline2"


def test_split_absence_in_strict_mode_does_not_raise():
    text = "# no frontmatter\n"
    assert split_frontmatter(text, strict=True) == ({}, text)


# ── read_frontmatter ───────────────────────────────────────────


def test_read_frontmatter_returns_full_mapping(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text(NESTED_DOC, encoding="utf-8")
    front = read_frontmatter(doc)
    assert front["codd"]["type"] == "requirement"


def test_read_frontmatter_missing_file_lenient(tmp_path):
    assert read_frontmatter(tmp_path / "absent.md") is None


def test_read_frontmatter_missing_file_strict(tmp_path):
    with pytest.raises(FrontmatterError) as exc_info:
        read_frontmatter(tmp_path / "absent.md", strict=True)
    assert exc_info.value.code == "read_error"


def test_read_frontmatter_no_block_returns_none(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("# plain\n", encoding="utf-8")
    assert read_frontmatter(doc) is None


def test_read_frontmatter_malformed_lenient_none_strict_raises(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("---\nfoo: [bad\n---\n", encoding="utf-8")
    assert read_frontmatter(doc) is None
    with pytest.raises(FrontmatterError):
        read_frontmatter(doc, strict=True)


# ── read_frontmatter sink-level path jail (project_root) ───────
#
# read_frontmatter is the shared read sink for many user-path-controllable
# callers. When ``project_root`` is supplied it must resolve+confine the path
# (defense-in-depth) so an external doc's frontmatter is never consumed as
# evidence. Lenient escape -> None (indistinguishable from unreadable, which is
# the correct semantics: an escaped path is not a readable in-root file).
# Strict escape -> FrontmatterError(code="read_error") (explicit fail-closed).


def _outside_frontmatter_doc(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    doc = outside / "secret.md"
    doc.write_text("---\ncodd:\n  node_id: leaked\n---\n# secret\n", encoding="utf-8")
    return doc


def test_read_frontmatter_parent_traversal_with_root_is_none(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _outside_frontmatter_doc(tmp_path)
    assert read_frontmatter("../outside/secret.md", project_root=project_root) is None


def test_read_frontmatter_absolute_outside_with_root_is_none(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    doc = _outside_frontmatter_doc(tmp_path)
    assert read_frontmatter(str(doc), project_root=project_root) is None


def test_read_frontmatter_in_root_symlink_escape_with_root_is_none(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    doc = _outside_frontmatter_doc(tmp_path)
    (project_root / "alias.md").symlink_to(doc)
    assert read_frontmatter("alias.md", project_root=project_root) is None


def test_read_frontmatter_escape_strict_raises_read_error(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    doc = _outside_frontmatter_doc(tmp_path)
    with pytest.raises(FrontmatterError) as exc_info:
        read_frontmatter(str(doc), project_root=project_root, strict=True)
    assert exc_info.value.code == "read_error"


def test_read_frontmatter_in_root_with_root_still_read(tmp_path):
    """Anti-false-red: an in-root doc with project_root still reads normally."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "doc.md").write_text(NESTED_DOC, encoding="utf-8")
    front = read_frontmatter("doc.md", project_root=project_root)
    assert front["codd"]["node_id"] == "req:FR-01"


def test_read_frontmatter_without_root_unchanged(tmp_path):
    """Anti-false-red: omitting project_root preserves legacy behavior — even an
    absolute path outside any project is read (no jail applied)."""
    outside = tmp_path / "outside"
    outside.mkdir()
    doc = outside / "secret.md"
    doc.write_text(NESTED_DOC, encoding="utf-8")
    front = read_frontmatter(doc)
    assert front["codd"]["node_id"] == "req:FR-01"


# ── codd_block ─────────────────────────────────────────────────


def test_codd_block_returns_nested_mapping_identity():
    front = {"codd": {"node_id": "req:FR-01"}, "title": "x"}
    block = codd_block(front)
    assert block is front["codd"]


def test_codd_block_missing_without_fallback():
    assert codd_block({"node_id": "design:auth"}) is None


def test_codd_block_missing_with_top_level_fallback():
    front = {"node_id": "design:auth"}
    assert codd_block(front, fallback_top_level=True) is front


def test_codd_block_non_mapping_codd_value():
    front = {"codd": "not-a-mapping", "node_id": "x"}
    assert codd_block(front) is None
    assert codd_block(front, fallback_top_level=True) is front


def test_codd_block_none_frontmatter():
    assert codd_block(None) is None
    assert codd_block(None, fallback_top_level=True) is None


# ── frontmatter_or_yaml_payload ────────────────────────────────


def test_payload_md_with_frontmatter(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("---\noperation_flow:\n  actor: admin\n---\nbody\n", encoding="utf-8")
    payload = frontmatter_or_yaml_payload(doc)
    assert payload == {"operation_flow": {"actor": "admin"}}


def test_payload_md_without_frontmatter_is_none(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("# nothing\n", encoding="utf-8")
    assert frontmatter_or_yaml_payload(doc) is None


def test_payload_md_invalid_yaml_is_none(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("---\nfoo: [bad\n---\n", encoding="utf-8")
    assert frontmatter_or_yaml_payload(doc) is None


def test_payload_whole_file_yaml(tmp_path):
    doc = tmp_path / "flow.yaml"
    doc.write_text("operation_flow:\n  actor: admin\n", encoding="utf-8")
    payload = frontmatter_or_yaml_payload(doc)
    assert payload == {"operation_flow": {"actor": "admin"}}


def test_payload_empty_yaml_file_is_empty_mapping(tmp_path):
    doc = tmp_path / "empty.yaml"
    doc.write_text("", encoding="utf-8")
    assert frontmatter_or_yaml_payload(doc) == {}


def test_payload_non_mapping_yaml_is_none(tmp_path):
    doc = tmp_path / "list.yaml"
    doc.write_text("- a\n- b\n", encoding="utf-8")
    assert frontmatter_or_yaml_payload(doc) is None


def test_payload_missing_file_is_none(tmp_path):
    assert frontmatter_or_yaml_payload(tmp_path / "absent.yaml") is None


# ── as_list ────────────────────────────────────────────────────


def test_as_list_none_is_empty():
    assert as_list(None) == []


def test_as_list_scalar_is_wrapped():
    assert as_list("single") == ["single"]


def test_as_list_list_passthrough_identity():
    value = ["a", "b"]
    assert as_list(value) is value


def test_as_list_tuple_becomes_list():
    assert as_list(("a", "b")) == ["a", "b"]


# ── apply_aliases ──────────────────────────────────────────────


def test_apply_aliases_copies_alias_to_canonical():
    resolved = apply_aliases(
        {"interaction_flows": [{"name": "j"}]},
        {"interaction_flows": "user_journeys"},
    )
    assert resolved["user_journeys"] == [{"name": "j"}]
    assert "interaction_flows" in resolved


def test_apply_aliases_canonical_key_wins():
    resolved = apply_aliases(
        {"user_journeys": ["canonical"], "interaction_flows": ["alias"]},
        {"interaction_flows": "user_journeys"},
    )
    assert resolved["user_journeys"] == ["canonical"]


def test_apply_aliases_without_map_returns_deepcopy():
    original = {"a": [{"b": 1}]}
    resolved = apply_aliases(original, None)
    assert resolved == original
    assert resolved is not original
    assert resolved["a"][0] is not original["a"][0]


def test_apply_aliases_does_not_mutate_input():
    original = {"alias_key": ["v"]}
    apply_aliases(original, {"alias_key": "canonical"})
    assert "canonical" not in original


def test_apply_aliases_filters_blank_keys():
    resolved = apply_aliases({"x": 1}, {"  ": "y", "x": " "})
    assert resolved == {"x": 1}


# ── parse_frontmatter primitive ────────────────────────────────


def test_parse_invalid_yaml_carries_original_exception():
    result = parse_frontmatter("---\nfoo: [bad\n---\nbody\n")
    assert result.error == "invalid_yaml"
    assert isinstance(result.exception, yaml.YAMLError)
    assert result.body == "body\n"


def test_parse_unclosed_keeps_original_text_as_body():
    text = "---\nfoo: 1\n"
    result = parse_frontmatter(text)
    assert result.error == "unclosed"
    assert result.body == text
    assert result.has_block is False


# ── unified semantics across migrated call sites ───────────────


def test_scanner_validator_dag_agree_on_nested_doc(tmp_path):
    from codd.dag.extractor import extract_design_doc_metadata
    from codd.scanner import _extract_frontmatter
    from codd.validator import _parse_codd_frontmatter

    doc = tmp_path / "doc.md"
    doc.write_text(NESTED_DOC, encoding="utf-8")

    scanner_codd = _extract_frontmatter(doc)
    validator_result = _parse_codd_frontmatter(doc)
    dag_metadata = extract_design_doc_metadata(doc)

    assert scanner_codd["node_id"] == "req:FR-01"
    assert validator_result.codd["node_id"] == "req:FR-01"
    assert dag_metadata["node_id"] == "req:FR-01"
    assert dag_metadata["depends_on"] == scanner_codd["depends_on"]


def test_validator_reports_invalid_yaml_with_code(tmp_path):
    from codd.validator import _parse_codd_frontmatter

    doc = tmp_path / "doc.md"
    doc.write_text("---\nfoo: [bad\n---\n", encoding="utf-8")
    result = _parse_codd_frontmatter(doc)
    assert result.codd is None
    assert result.error["code"] == "invalid_frontmatter"
    assert "invalid YAML frontmatter" in result.error["message"]


def test_validator_reports_missing_frontmatter_for_flat_doc(tmp_path):
    from codd.validator import _parse_codd_frontmatter

    doc = tmp_path / "doc.md"
    doc.write_text(FLAT_DOC, encoding="utf-8")
    result = _parse_codd_frontmatter(doc)
    assert result.error["code"] == "missing_frontmatter"


def test_dag_extractor_still_raises_on_invalid_yaml(tmp_path):
    from codd.dag.extractor import extract_design_doc_metadata

    doc = tmp_path / "doc.md"
    doc.write_text("---\nfoo: [bad\n---\nbody\n", encoding="utf-8")
    with pytest.raises(yaml.YAMLError):
        extract_design_doc_metadata(doc)


def test_dag_extractor_scalar_depends_on_coerced(tmp_path):
    from codd.dag.extractor import extract_design_doc_metadata

    doc = tmp_path / "doc.md"
    doc.write_text("---\ndepends_on: docs/single.md\n---\n# Doc\n", encoding="utf-8")
    metadata = extract_design_doc_metadata(doc)
    assert metadata["depends_on"] == ["docs/single.md"]


def test_lexicon_loader_error_messages_preserved():
    from codd.elicit.lexicon_loader import LexiconLoadError, _split_frontmatter

    with pytest.raises(LexiconLoadError, match="frontmatter is invalid"):
        _split_frontmatter("---\nfoo: [bad\n---\nbody\n")
    with pytest.raises(LexiconLoadError, match="must be a mapping"):
        _split_frontmatter("---\n- a\n---\nbody\n")
    with pytest.raises(LexiconLoadError, match="missing a closing delimiter"):
        _split_frontmatter("---\nfoo: 1\n")


def test_lexicon_loader_splits_body_exactly():
    from codd.elicit.lexicon_loader import _split_frontmatter

    metadata, body = _split_frontmatter("---\nextends: base.md\n---\nPrompt body\n")
    assert metadata == {"extends": "base.md"}
    assert body == "Prompt body\n"


# ── deliberate behavior fixes (deployment extractor) ───────────


def test_deployment_extractor_survives_malformed_frontmatter(tmp_path):
    """DELIBERATE FIX: legacy _split_frontmatter raised yaml.YAMLError on a
    malformed block, crashing the whole deployment extraction; the unified
    lenient parser treats the doc as frontmatter-less instead."""
    from codd.deployment.extractor import extract_deployment_docs

    (tmp_path / "DEPLOYMENT.md").write_text(
        "---\nfoo: [unclosed\n---\n## Migrate\n", encoding="utf-8"
    )

    docs = extract_deployment_docs(tmp_path)

    assert docs[0].path == "DEPLOYMENT.md"
    assert docs[0].sections == ["migrate"]
    assert docs[0].depends_on == []


def test_deployment_doc_reads_nested_codd_block(tmp_path):
    """DELIBERATE FIX: deployment docs written in the canonical nested
    ``codd:`` shape were invisible to the deployment extractor, which only
    read top-level keys."""
    from codd.deployment.extractor import extract_deployment_docs

    (tmp_path / "DEPLOYMENT.md").write_text(
        "---\ncodd:\n  deploy_target_ref: vps\n  depends_on:\n"
        "    - docs/design/api_design.md\n---\n## Seed\n",
        encoding="utf-8",
    )

    doc = extract_deployment_docs(tmp_path)[0]

    assert doc.deploy_target_ref == "vps"
    assert doc.depends_on == ["docs/design/api_design.md"]


def test_deployment_doc_top_level_keys_still_win(tmp_path):
    from codd.deployment.extractor import extract_deployment_docs

    (tmp_path / "DEPLOYMENT.md").write_text(
        "---\ndeploy_target_ref: top\ncodd:\n  deploy_target_ref: nested\n---\n## Seed\n",
        encoding="utf-8",
    )

    assert extract_deployment_docs(tmp_path)[0].deploy_target_ref == "top"


def test_deployment_design_records_read_nested_acceptance_criteria(tmp_path):
    """DELIBERATE FIX: design-doc records now fall back to the nested
    ``codd:`` block for acceptance criteria."""
    from codd.deployment.extractor import _load_design_doc_records

    doc = tmp_path / "docs" / "design" / "api.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(
        "---\ncodd:\n  acceptance_criteria:\n    - User can login\n---\n# API\n",
        encoding="utf-8",
    )

    records = _load_design_doc_records(tmp_path, {"design_doc_patterns": ["docs/design/*.md"]})

    assert records[0]["acceptance_criteria"] == ["User can login"]
