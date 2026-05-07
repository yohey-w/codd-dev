from __future__ import annotations

import pytest

from codd.repair.proof_breaks import (
    PROOF_BREAK_PLACEHOLDER,
    ProofBreakNotFound,
    ensure_proof_break_placeholder,
    replace_yaml_list_item_block_by_value,
    replace_yaml_list_item_by_value,
)


def test_replace_yaml_list_item_by_value_uses_structure_not_exact_block() -> None:
    content = """---
runtime_constraints:
  - capability: stable_endpoint
    rationale: "text that can change without breaking the locator"
    required: false
user_journeys:
  - name: sign_in
    steps:
      - { action: navigate, target: "/start" }
---
# Design
"""

    updated = replace_yaml_list_item_by_value(
        content,
        list_key="runtime_constraints",
        match_key="capability",
        match_value="stable_endpoint",
        replacement_item={
            "capability": "stable_endpoint_missing_for_repair_proof",
            "required": True,
            "rationale": "intentional proof break",
        },
    )

    assert "stable_endpoint_missing_for_repair_proof" in updated
    assert "text that can change without breaking the locator" not in updated
    assert "name: sign_in" in updated
    assert updated.endswith("# Design\n")


def test_replace_yaml_list_item_by_value_handles_quoted_match_value() -> None:
    content = """---
coverage_axes:
  - id: "desktop"
    criticality: high
---
# Design
"""

    updated = replace_yaml_list_item_by_value(
        content,
        list_key="coverage_axes",
        match_key="id",
        match_value="desktop",
        replacement_item={"id": "small_viewport", "criticality": "critical"},
    )

    assert "id: small_viewport" in updated
    assert "criticality: critical" in updated


def test_replace_yaml_list_item_block_by_value_preserves_rendered_block() -> None:
    content = """---
runtime_constraints:
  - capability: stable_endpoint
    required: false
    rationale: "keep explicit quotes"
---
# Design
"""
    replacement = """  - capability: stable_endpoint_missing_for_repair_proof
    required: true
    rationale: "intentional proof break"
"""

    updated = replace_yaml_list_item_block_by_value(
        content,
        list_key="runtime_constraints",
        match_key="capability",
        match_value="stable_endpoint",
        replacement_block=replacement,
    )

    assert replacement in updated
    assert "keep explicit quotes" not in updated


def test_replace_yaml_list_item_by_value_raises_for_missing_anchor() -> None:
    content = """---
runtime_constraints: []
---
# Design
"""

    with pytest.raises(ProofBreakNotFound, match="runtime_constraints item"):
        replace_yaml_list_item_by_value(
            content,
            list_key="runtime_constraints",
            match_key="capability",
            match_value="missing",
            replacement_item={"capability": "replacement"},
        )


def test_ensure_proof_break_placeholder_inserts_after_frontmatter() -> None:
    content = """---
title: Example
---
# Design
"""

    updated = ensure_proof_break_placeholder(content)

    assert updated == f"""---
title: Example
---
{PROOF_BREAK_PLACEHOLDER}

# Design
"""
    assert ensure_proof_break_placeholder(updated) == updated
