"""Regression test for Issue #21 (v-kato) — `detailed_design:` node_id prefix.

`codd plan --init` could emit node ids like `detailed_design:shared-domain-model`
(particularly when AI-driven planning saw an artifact under `docs/detailed_design/`).
`validator.DEFAULT_NODE_PREFIXES` listed `detail` and `detailed` but **not**
`detailed_design`, so a node CoDD itself just produced was immediately rejected by
`codd validate` — a self-inconsistency that hits every Greenfield Wave-5 run.

v2.18.0 adds `detailed_design` to the prefix set (backward-compatible additive
change). This test pins that the prefix is recognised by the validator.
"""

from __future__ import annotations

from codd.validator import DEFAULT_NODE_PREFIXES, NODE_ID_PATTERN


def test_detailed_design_is_a_default_node_prefix():
    """`detailed_design:` MUST appear in DEFAULT_NODE_PREFIXES.

    Without this entry, `codd plan --init` can generate node ids that the
    same project's `codd validate` immediately rejects.
    """
    assert "detailed_design" in DEFAULT_NODE_PREFIXES


def test_detailed_design_node_id_matches_naming_pattern():
    """A typical Wave-5 detailed_design node id parses cleanly and its prefix
    is in the allowed set."""
    node_id = "detailed_design:shared-domain-model"
    match = NODE_ID_PATTERN.match(node_id)
    assert match is not None, "detailed_design:* must satisfy NODE_ID_PATTERN"
    assert match.group("prefix") in DEFAULT_NODE_PREFIXES
    assert match.group("prefix") == "detailed_design"


def test_legacy_detail_and_detailed_prefixes_still_recognised():
    """The fix is additive — pre-existing prefixes must stay accepted."""
    assert "detail" in DEFAULT_NODE_PREFIXES
    assert "detailed" in DEFAULT_NODE_PREFIXES
    assert "design" in DEFAULT_NODE_PREFIXES
