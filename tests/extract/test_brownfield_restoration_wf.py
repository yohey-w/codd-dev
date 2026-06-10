"""Tests for W-F brownfield restoration fixes (P1/P2).

Covers:
  P1-1  extract output dir == planner extracted-doc search dir (single SSOT)
  P1-2  extract never writes outside its isolated output dir (fail-closed)
  P1-4  AI-extracted frontmatter normalized to canonical codd.node_id
  P2-2  bootstrap codd.yaml carries guided TODO stubs
"""

from pathlib import Path

import pytest
import yaml

from codd.cli import _ensure_bootstrap_codd_yaml
from codd.extract_ai import (
    _normalize_extracted_frontmatter,
    _parse_ai_output,
    _safe_output_path,
)
from codd.extract_paths import (
    default_extract_output_dir,
    extracted_doc_search_dirs,
)
from codd.extractor import add_extract_init_frontmatter, run_extract
from codd.planner import _load_extracted_documents


# ── P1-1: extract output path unification ─────────────────────────────────

def _write_minimal_project(project: Path) -> None:
    src = project / "src"
    src.mkdir(parents=True)
    (src / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")


def test_extract_output_is_discoverable_by_planner(tmp_path):
    """Docs written by run_extract (default path) are found by the planner."""
    _write_minimal_project(tmp_path)

    result = run_extract(tmp_path, "python", ["src"])

    # Output goes to the shared canonical default.
    assert result.output_dir == default_extract_output_dir(tmp_path)
    assert default_extract_output_dir(tmp_path) in extracted_doc_search_dirs(tmp_path)

    # The planner discovers those exact docs (same SSOT path).
    docs = _load_extracted_documents(tmp_path, config={})
    assert docs, "planner found no extracted docs at the extract output path"
    node_ids = {d.node_id for d in docs}
    assert "design:extract:system-context" in node_ids


def test_planner_still_discovers_legacy_extracted_dir(tmp_path):
    """Legacy <codd-dir>/extracted/ remains discoverable for older projects."""
    legacy = tmp_path / "codd" / "extracted"
    legacy.mkdir(parents=True)
    (legacy / "system-context.md").write_text(
        "---\ncodd:\n  node_id: design:extract:system-context\n  source: extracted\n---\n# ctx\n",
        encoding="utf-8",
    )

    docs = _load_extracted_documents(tmp_path, config={})
    assert [d.node_id for d in docs] == ["design:extract:system-context"]


def test_default_extract_output_dir_is_hidden(tmp_path):
    assert default_extract_output_dir(tmp_path) == tmp_path / ".codd" / "extract"


# ── P1-2: extract never overwrites source/user files ──────────────────────

def test_safe_output_path_rejects_parent_traversal(tmp_path):
    with pytest.raises(ValueError):
        _safe_output_path(tmp_path / ".codd" / "extract", "../../src/app.py")


def test_safe_output_path_rejects_absolute(tmp_path):
    with pytest.raises(ValueError):
        _safe_output_path(tmp_path / ".codd" / "extract", "/etc/passwd")


def test_safe_output_path_allows_nested_inside_output(tmp_path):
    out = tmp_path / ".codd" / "extract"
    out.mkdir(parents=True)
    resolved = _safe_output_path(out, "modules/auth.md")
    assert resolved == (out / "modules" / "auth.md").resolve()


def test_parse_ai_output_refuses_to_escape_output_dir(tmp_path):
    """A malicious/buggy AI FILE marker must not overwrite source files."""
    out = tmp_path / ".codd" / "extract"
    out.mkdir(parents=True)
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("ORIGINAL\n", encoding="utf-8")

    raw = "--- FILE: ../../src/app.py ---\nOVERWRITTEN\n"
    with pytest.raises(ValueError):
        _parse_ai_output(raw, out)

    # Source file is untouched (fail-closed).
    assert (src / "app.py").read_text(encoding="utf-8") == "ORIGINAL\n"


def test_parse_ai_output_writes_legitimate_files(tmp_path):
    out = tmp_path / ".codd" / "extract"
    out.mkdir(parents=True)
    raw = "--- FILE: L1_data_models.md ---\n# L1\n--- FILE: nested/L2.md ---\n# L2\n"
    files = _parse_ai_output(raw, out)
    names = sorted(p.name for p in files)
    assert names == ["L1_data_models.md", "L2.md"]
    assert (out / "nested" / "L2.md").read_text(encoding="utf-8").strip() == "# L2"


def test_add_init_frontmatter_skips_files_outside_output_dir(tmp_path):
    """--init annotation must never rewrite a file outside the extract output dir."""
    out = tmp_path / ".codd" / "extract"
    out.mkdir(parents=True)
    inside = out / "system-context.md"
    inside.write_text("# ctx\n", encoding="utf-8")

    outside = tmp_path / "src_doc.md"
    outside.write_text("# source doc — must not change\n", encoding="utf-8")

    add_extract_init_frontmatter(
        [inside, outside],
        {"version": "1.0", "extracted_at": "2026-06-11T00:00:00+09:00", "source": "/repo"},
        output_dir=out,
    )

    assert inside.read_text(encoding="utf-8").startswith("---\n")
    # Outside file untouched.
    assert outside.read_text(encoding="utf-8") == "# source doc — must not change\n"


# ── P1-4: AI extract frontmatter normalized to codd.node_id ───────────────

def test_normalize_lifts_top_level_id_into_codd_node_id(tmp_path):
    doc = tmp_path / "L1_data_models.md"
    doc.write_text(
        "---\nid: L1_data_models\ntype: design\n---\n# L1: Data Models\n",
        encoding="utf-8",
    )

    _normalize_extracted_frontmatter([doc])

    text = doc.read_text(encoding="utf-8")
    front = yaml.safe_load(text.split("---", 2)[1])
    assert front["codd"]["node_id"] == "L1_data_models"
    assert front["codd"]["source"] == "extracted"
    # Top-level alias folded away.
    assert "id" not in front
    assert text.rstrip().endswith("# L1: Data Models")


def test_normalize_preserves_existing_codd_node_id(tmp_path):
    doc = tmp_path / "L2.md"
    doc.write_text(
        "---\ncodd:\n  node_id: design:extract:api\n  confidence: 0.7\n---\n# L2\n",
        encoding="utf-8",
    )

    _normalize_extracted_frontmatter([doc])

    front = yaml.safe_load(doc.read_text(encoding="utf-8").split("---", 2)[1])
    assert front["codd"]["node_id"] == "design:extract:api"
    assert front["codd"]["source"] == "extracted"
    assert front["codd"]["confidence"] == 0.7


def test_normalized_ai_doc_is_loadable_by_planner(tmp_path):
    """End-to-end: normalized AI doc placed in the extract dir is planner-visible."""
    out = default_extract_output_dir(tmp_path)
    out.mkdir(parents=True)
    doc = out / "L4_business_logic.md"
    doc.write_text(
        "---\nid: L4_business_logic\n---\n# L4\n",
        encoding="utf-8",
    )

    _normalize_extracted_frontmatter([doc])
    docs = _load_extracted_documents(tmp_path, config={})
    assert [d.node_id for d in docs] == ["L4_business_logic"]


def test_normalize_leaves_doc_without_identity_untouched(tmp_path):
    doc = tmp_path / "notes.md"
    original = "# Just notes, no frontmatter\n"
    doc.write_text(original, encoding="utf-8")

    _normalize_extracted_frontmatter([doc])
    assert doc.read_text(encoding="utf-8") == original


# ── P1-3: generated-wrapper rule in baseline extract prompt ───────────────

def test_baseline_prompt_has_generated_wrapper_rule():
    from codd.extract_ai import PROMPT_TEMPLATE_FILE

    text = PROMPT_TEMPLATE_FILE.read_text(encoding="utf-8")
    low = text.lower()
    assert "tool-generated wrappers" in low or "generated/scaffolded" in low
    # Generic detection signals (no project-specific names).
    assert "@generated" in text
    assert "do not edit" in low


def test_baseline_prompt_stays_generic():
    """Generality Gate: no project-specific names in the baseline prompt."""
    from codd.extract_ai import PROMPT_TEMPLATE_FILE

    low = PROMPT_TEMPLATE_FILE.read_text(encoding="utf-8").lower()
    for forbidden in ("osato", "shogun"):
        assert forbidden not in low


# ── P2-2: bootstrap codd.yaml TODO stubs ──────────────────────────────────

def test_bootstrap_codd_yaml_has_todo_stubs(tmp_path):
    config_path, created = _ensure_bootstrap_codd_yaml(
        tmp_path, language="python", source_dirs=["src"]
    )
    assert created
    text = config_path.read_text(encoding="utf-8")

    # Guided TODO sections for the keys a brownfield project must fill in.
    assert "Brownfield bootstrap TODOs" in text
    assert "# TODO: operation_flow" in text
    assert "# TODO: scan dirs" in text
    assert "# TODO: dag.enabled_checks" in text

    # Still valid, parseable YAML (stubs are comments only).
    config = yaml.safe_load(text)
    assert config["scan"]["source_dirs"] == ["src"]


def test_bootstrap_todos_are_idempotent(tmp_path):
    config_path, _ = _ensure_bootstrap_codd_yaml(
        tmp_path, language="python", source_dirs=["src"]
    )
    # Second call does not re-create (config exists) and does not duplicate stubs.
    config_path2, created2 = _ensure_bootstrap_codd_yaml(
        tmp_path, language="python", source_dirs=["src"]
    )
    assert not created2
    assert config_path2.read_text(encoding="utf-8").count("Brownfield bootstrap TODOs") == 1
