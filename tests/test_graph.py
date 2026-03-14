"""Tests for CEG graph operations."""

from pathlib import Path

import pytest

from codd.graph import CEG


@pytest.fixture
def ceg(tmp_path):
    """Create a temporary CEG backed by JSONL files."""
    scan_dir = tmp_path / "scan"
    graph = CEG(scan_dir)
    yield graph
    graph.close()


def test_upsert_and_get_node(ceg):
    ceg.upsert_node("file:src/main.py", "file", path="src/main.py", name="main.py")
    node = ceg.get_node("file:src/main.py")
    assert node is not None
    assert node["type"] == "file"
    assert node["name"] == "main.py"


def test_add_edge_and_query(ceg):
    ceg.upsert_node("file:a.py", "file", name="a.py")
    ceg.upsert_node("file:b.py", "file", name="b.py")
    edge_id = ceg.add_edge("file:a.py", "file:b.py", "imports", "structural")
    assert edge_id > 0

    outgoing = ceg.get_outgoing_edges("file:a.py")
    assert len(outgoing) == 1
    assert outgoing[0]["target_id"] == "file:b.py"

    incoming = ceg.get_incoming_edges("file:b.py")
    assert len(incoming) == 1
    assert incoming[0]["source_id"] == "file:a.py"


def test_noisy_or_confidence(ceg):
    ceg.upsert_node("file:a.py", "file")
    ceg.upsert_node("file:b.py", "file")
    edge_id = ceg.add_edge("file:a.py", "file:b.py", "imports", "structural", confidence=0.5)

    # Add positive evidence
    ceg.add_evidence(edge_id, "static", "ast", 0.9)
    ceg.add_evidence(edge_id, "human", "manual", 0.8)

    # Check confidence was updated via Noisy-OR
    edges = ceg.get_outgoing_edges("file:a.py")
    assert len(edges) == 1
    # Noisy-OR: 1 - (1-0.9)*(1-0.8) = 1 - 0.1*0.2 = 0.98
    assert edges[0]["confidence"] == pytest.approx(0.98, abs=0.01)


def test_negative_evidence_reduces_confidence(ceg):
    ceg.upsert_node("file:a.py", "file")
    ceg.upsert_node("file:b.py", "file")
    edge_id = ceg.add_edge("file:a.py", "file:b.py", "imports", "structural")

    ceg.add_evidence(edge_id, "static", "ast", 0.9)
    ceg.add_evidence(edge_id, "human", "override", 0.7, is_negative=True)

    edges = ceg.get_outgoing_edges("file:a.py")
    assert edges[0]["confidence"] < 0.9  # Reduced by negative evidence


def test_propagation_bfs(ceg):
    # Build chain: a → b → c → d
    for name in ["a", "b", "c", "d"]:
        ceg.upsert_node(f"file:{name}.py", "file", name=f"{name}.py")

    ceg.add_edge("file:a.py", "file:b.py", "imports", "structural", confidence=0.9)
    ceg.add_edge("file:b.py", "file:c.py", "imports", "structural", confidence=0.8)
    ceg.add_edge("file:c.py", "file:d.py", "imports", "structural", confidence=0.7)

    impacts = ceg.propagate_impact("file:a.py", max_depth=10)
    assert "file:b.py" in impacts
    assert "file:c.py" in impacts
    assert "file:d.py" in impacts
    assert impacts["file:b.py"]["depth"] == 1
    assert impacts["file:c.py"]["depth"] == 2
    assert impacts["file:d.py"]["depth"] == 3


def test_propagation_max_depth(ceg):
    for name in ["a", "b", "c", "d"]:
        ceg.upsert_node(f"file:{name}.py", "file")
    ceg.add_edge("file:a.py", "file:b.py", "imports", "structural", confidence=0.9)
    ceg.add_edge("file:b.py", "file:c.py", "imports", "structural", confidence=0.8)
    ceg.add_edge("file:c.py", "file:d.py", "imports", "structural", confidence=0.7)

    impacts = ceg.propagate_impact("file:a.py", max_depth=1)
    assert "file:b.py" in impacts
    assert "file:c.py" not in impacts  # Depth 2, beyond max_depth=1


def test_band_classification(ceg):
    assert ceg.classify_band(0.95, 3) == "green"
    assert ceg.classify_band(0.95, 1) == "amber"   # Not enough evidence
    assert ceg.classify_band(0.60, 5) == "amber"
    assert ceg.classify_band(0.30, 1) == "gray"


def test_stats(ceg):
    ceg.upsert_node("file:a.py", "file")
    ceg.upsert_node("file:b.py", "file")
    edge_id = ceg.add_edge("file:a.py", "file:b.py", "imports", "structural")
    ceg.add_evidence(edge_id, "static", "ast", 0.9)

    stats = ceg.stats()
    assert stats["nodes"] == 2
    assert stats["edges"] == 1
    assert stats["evidence"] == 1
