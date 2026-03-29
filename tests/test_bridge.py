"""Tests for R6 — extract→impact bridge (source_files frontmatter)."""

import yaml
import pytest
from pathlib import Path

from codd.graph import CEG
from codd.synth import _build_frontmatter
from codd.scanner import _load_frontmatter


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def ceg(tmp_path):
    scan_dir = tmp_path / "scan"
    graph = CEG(scan_dir)
    yield graph
    graph.close()


# ── _build_frontmatter tests ─────────────────────────────────────────────────

def test_build_frontmatter_with_source_files():
    """YAML output must contain source_files list when provided."""
    result = _build_frontmatter(
        node_id="design:auth",
        confidence=0.9,
        today="2026-03-30",
        source_files=["src/auth.py", "src/auth_utils.py"],
    )
    data = yaml.safe_load(result)
    assert "source_files" in data["codd"]
    assert data["codd"]["source_files"] == ["src/auth.py", "src/auth_utils.py"]


def test_build_frontmatter_without_source_files():
    """YAML output must not contain source_files key when parameter is omitted."""
    result = _build_frontmatter(
        node_id="design:auth",
        confidence=0.9,
        today="2026-03-30",
    )
    data = yaml.safe_load(result)
    assert "source_files" not in data["codd"]


def test_build_frontmatter_empty_source_files():
    """Empty source_files list must produce no source_files key (falsy guard)."""
    result = _build_frontmatter(
        node_id="design:auth",
        confidence=0.9,
        today="2026-03-30",
        source_files=[],
    )
    data = yaml.safe_load(result)
    assert "source_files" not in data["codd"]


def test_build_frontmatter_preserves_other_fields():
    """node_id, confidence, and last_extracted must be present regardless of source_files."""
    result = _build_frontmatter(
        node_id="design:payment",
        confidence=0.75,
        today="2026-03-30",
        source_files=["src/payment.py"],
    )
    data = yaml.safe_load(result)
    codd = data["codd"]
    assert codd["node_id"] == "design:payment"
    assert codd["confidence"] == 0.75
    assert codd["last_extracted"] == "2026-03-30"
    assert codd["type"] == "design"
    assert codd["source"] == "extracted"


# ── _load_frontmatter bridge-edge tests ──────────────────────────────────────

def test_load_frontmatter_source_files_creates_file_nodes(ceg):
    """file: nodes must be created for each entry in source_files."""
    codd = {
        "node_id": "design:auth",
        "type": "design",
        "source_files": ["src/auth.py", "src/auth_utils.py"],
    }
    _load_frontmatter(ceg, "docs/auth.md", codd)

    assert "file:src/auth.py" in ceg.nodes
    assert "file:src/auth_utils.py" in ceg.nodes
    assert ceg.nodes["file:src/auth.py"]["type"] == "file"


def test_load_frontmatter_source_files_creates_bridge_edges(ceg):
    """Edges from design node to file nodes must be created."""
    codd = {
        "node_id": "design:auth",
        "type": "design",
        "source_files": ["src/auth.py"],
    }
    _load_frontmatter(ceg, "docs/auth.md", codd)

    outgoing = ceg.get_outgoing_edges("design:auth")
    target_ids = [e["target_id"] for e in outgoing]
    assert "file:src/auth.py" in target_ids


def test_load_frontmatter_bridge_edge_relation_is_extracted_from(ceg):
    """Bridge edges must have relation == 'extracted_from'."""
    codd = {
        "node_id": "design:auth",
        "type": "design",
        "source_files": ["src/auth.py"],
    }
    _load_frontmatter(ceg, "docs/auth.md", codd)

    outgoing = ceg.get_outgoing_edges("design:auth")
    bridge_edges = [e for e in outgoing if e["target_id"] == "file:src/auth.py"]
    assert len(bridge_edges) == 1
    assert bridge_edges[0]["relation"] == "extracted_from"


def test_load_frontmatter_no_source_files_creates_no_bridge_edges(ceg):
    """Without source_files, no bridge edges should be created."""
    codd = {
        "node_id": "design:auth",
        "type": "design",
    }
    _load_frontmatter(ceg, "docs/auth.md", codd)

    outgoing = ceg.get_outgoing_edges("design:auth")
    bridge_edges = [e for e in outgoing if e["relation"] == "extracted_from"]
    assert bridge_edges == []


def test_load_frontmatter_multiple_source_files_multiple_edges(ceg):
    """Each entry in source_files must produce its own bridge edge."""
    source_files = ["src/a.py", "src/b.py", "src/c.py"]
    codd = {
        "node_id": "design:core",
        "type": "design",
        "source_files": source_files,
    }
    _load_frontmatter(ceg, "docs/core.md", codd)

    outgoing = ceg.get_outgoing_edges("design:core")
    bridge_targets = {e["target_id"] for e in outgoing if e["relation"] == "extracted_from"}
    assert bridge_targets == {f"file:{f}" for f in source_files}


def test_load_frontmatter_bridge_edge_confidence_is_0_85(ceg):
    """Bridge edge evidence must have score 0.85 (R6 spec)."""
    codd = {
        "node_id": "design:auth",
        "type": "design",
        "source_files": ["src/auth.py"],
    }
    _load_frontmatter(ceg, "docs/auth.md", codd)

    outgoing = ceg.get_outgoing_edges("design:auth")
    bridge_edges = [e for e in outgoing if e["relation"] == "extracted_from"]
    assert len(bridge_edges) == 1
    evidence = bridge_edges[0].get("evidence", [])
    assert any(ev["score"] == 0.85 for ev in evidence)
