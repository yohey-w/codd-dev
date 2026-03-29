"""Tests for change propagation / impact analysis."""

import pytest
from pathlib import Path

from codd.graph import CEG
from codd.propagate import _resolve_start_nodes, _check_conventions_from_graph


@pytest.fixture
def ceg(tmp_path):
    scan_dir = tmp_path / "scan"
    graph = CEG(scan_dir)
    yield graph
    graph.close()


def test_resolve_start_nodes_from_frontmatter(ceg, tmp_path):
    """A changed .md with CoDD frontmatter resolves to its node_id."""
    # Create a doc with frontmatter
    doc = tmp_path / "docs" / "requirements.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("""---
codd:
  node_id: "req:FR-01"
  type: requirement
---

# FR-01
""")
    # Register node in graph
    ceg.upsert_node("req:FR-01", "requirement", path="docs/requirements.md")

    start_nodes = _resolve_start_nodes(ceg, tmp_path, ["docs/requirements.md"])
    assert len(start_nodes) == 1
    assert start_nodes[0][0] == "req:FR-01"
    assert start_nodes[0][1] == "docs/requirements.md"


def test_resolve_start_nodes_from_file_node(ceg, tmp_path):
    """A changed source file resolves to file:path node."""
    ceg.upsert_node("file:src/auth.ts", "file", path="src/auth.ts")

    start_nodes = _resolve_start_nodes(ceg, tmp_path, ["src/auth.ts"])
    assert len(start_nodes) == 1
    assert start_nodes[0][0] == "file:src/auth.ts"


def test_resolve_start_nodes_unknown_file(ceg, tmp_path):
    """A changed file not in graph returns empty."""
    start_nodes = _resolve_start_nodes(ceg, tmp_path, ["src/unknown.ts"])
    assert len(start_nodes) == 0


def test_resolve_start_nodes_no_duplicates(ceg, tmp_path):
    """Same node_id from frontmatter and path should not duplicate."""
    doc = tmp_path / "design.md"
    doc.write_text("""---
codd:
  node_id: "design:system"
  type: design
---

# System Design
""")
    ceg.upsert_node("design:system", "design", path="design.md")

    start_nodes = _resolve_start_nodes(ceg, tmp_path, ["design.md"])
    assert len(start_nodes) == 1


def test_check_conventions_from_graph_direct(ceg):
    """Must_review edges from changed node are detected."""
    ceg.upsert_node("design:db", "design")
    ceg.upsert_node("db:rls_policies", "db_object")
    ceg.upsert_node("db_table:audit_logs", "db_table")

    edge1 = ceg.add_edge("design:db", "db:rls_policies", "must_review", "governance")
    ceg.add_evidence(edge1, "frontmatter", "convention", 0.8, detail="RLS必須")

    edge2 = ceg.add_edge("design:db", "db_table:audit_logs", "must_review", "governance")
    ceg.add_evidence(edge2, "frontmatter", "convention", 0.8, detail="監査ログ削除禁止")

    start_nodes = [("design:db", "docs/database_design.md")]
    triggered = _check_conventions_from_graph(ceg, start_nodes)

    assert len(triggered) == 2
    targets = {t["target_id"] for t in triggered}
    assert "db:rls_policies" in targets
    assert "db_table:audit_logs" in targets
    assert triggered[0]["reason"] in ("RLS必須", "監査ログ削除禁止")


def test_check_conventions_from_graph_via_parent(ceg):
    """Must_review edges from parent of changed node are also detected."""
    # req:FR-01 --depends_on--> design:db --must_review--> db:rls
    ceg.upsert_node("req:FR-01", "requirement")
    ceg.upsert_node("design:db", "design")
    ceg.upsert_node("db:rls", "db_object")

    edge1 = ceg.add_edge("req:FR-01", "design:db", "depends_on", "governance")
    ceg.add_evidence(edge1, "frontmatter", "frontmatter", 0.9)

    edge2 = ceg.add_edge("req:FR-01", "db:rls", "must_review", "governance")
    ceg.add_evidence(edge2, "frontmatter", "convention", 0.8, detail="テナント分離")

    # Changed node is design:db, parent is req:FR-01 which has must_review
    start_nodes = [("design:db", "docs/database_design.md")]
    triggered = _check_conventions_from_graph(ceg, start_nodes)

    assert len(triggered) >= 1
    assert any(t["target_id"] == "db:rls" for t in triggered)


def test_impact_propagation_from_document_node(ceg):
    """Impact propagation works from document nodes (not just file: nodes).

    Edge semantics: source depends_on target.
    db depends_on api depends_on ui depends_on plan.
    When plan changes, impact propagates: plan → ui (1) → api (2) → db (3).
    """
    ceg.upsert_node("design:db", "design")
    ceg.upsert_node("design:api", "design")
    ceg.upsert_node("design:ui", "design")
    ceg.upsert_node("design:plan", "design")

    # db -> api -> ui -> plan (source depends on target)
    e1 = ceg.add_edge("design:db", "design:api", "derives_from", "governance")
    ceg.add_evidence(e1, "frontmatter", "frontmatter", 0.9)
    e2 = ceg.add_edge("design:api", "design:ui", "consumes", "governance")
    ceg.add_evidence(e2, "frontmatter", "frontmatter", 0.9)
    e3 = ceg.add_edge("design:ui", "design:plan", "schedules", "governance")
    ceg.add_evidence(e3, "frontmatter", "frontmatter", 0.9)

    # plan changes → ui, api, db are impacted (reverse propagation)
    impacts = ceg.propagate_impact("design:plan", max_depth=10)
    assert "design:ui" in impacts
    assert "design:api" in impacts
    assert "design:db" in impacts
    assert impacts["design:ui"]["depth"] == 1
    assert impacts["design:api"]["depth"] == 2
    assert impacts["design:db"]["depth"] == 3


def test_find_nodes_by_path(ceg):
    """find_nodes_by_path returns nodes with matching path."""
    ceg.upsert_node("design:system", "design", path="docs/system_design.md")
    ceg.upsert_node("file:src/app.ts", "file", path="src/app.ts")

    results = ceg.find_nodes_by_path("docs/system_design.md")
    assert len(results) == 1
    assert results[0]["id"] == "design:system"

    results = ceg.find_nodes_by_path("nonexistent.md")
    assert len(results) == 0


def test_get_convention_edges(ceg):
    """get_convention_edges returns only must_review edges."""
    ceg.upsert_node("req:FR-01", "requirement")
    ceg.upsert_node("db:rls", "db_object")
    ceg.upsert_node("design:api", "design")

    e1 = ceg.add_edge("req:FR-01", "db:rls", "must_review", "governance")
    ceg.add_evidence(e1, "frontmatter", "convention", 0.8)
    e2 = ceg.add_edge("req:FR-01", "design:api", "depends_on", "governance")
    ceg.add_evidence(e2, "frontmatter", "frontmatter", 0.9)

    conv_edges = ceg.get_convention_edges("req:FR-01")
    assert len(conv_edges) == 1
    assert conv_edges[0]["target_id"] == "db:rls"


CoDD_YAML = """
project:
  name: test-project
  language: typescript
scan:
  source_dirs: []
  doc_dirs:
    - "docs/"
  exclude: []
graph:
  store: jsonl
  path: codd/scan
bands:
  green:
    min_confidence: 0.90
    min_evidence_count: 2
  amber:
    min_confidence: 0.50
propagation:
  max_depth: 10
"""


def test_full_impact_from_document_change(tmp_path):
    """End-to-end: changing a design doc triggers impact on dependent docs."""
    from codd.scanner import run_scan

    # Setup project
    project = tmp_path / "project"
    project.mkdir()
    (project / "docs").mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(CoDD_YAML)

    # Create docs with frontmatter
    (project / "docs" / "db_design.md").write_text("""---
codd:
  node_id: "design:db"
  type: design
  depends_on:
    - id: "design:api"
      relation: derives_from
---

# DB Design
""")

    (project / "docs" / "api_design.md").write_text("""---
codd:
  node_id: "design:api"
  type: design
  depends_on:
    - id: "design:ui"
      relation: consumes
  conventions:
    - targets: ["design:db"]
      reason: "API変更時にDB設計レビュー必須"
---

# API Design
""")

    (project / "docs" / "ui_design.md").write_text("""---
codd:
  node_id: "design:ui"
  type: design
---

# UI Design
""")

    # Scan to build graph
    run_scan(project, codd_dir)

    # Verify graph was built
    ceg = CEG(codd_dir / "scan")
    assert ceg.get_node("design:db") is not None
    assert ceg.get_node("design:api") is not None
    assert ceg.get_node("design:ui") is not None

    # Simulate: ui_design.md changed → should impact api (depends on ui), db (depends on api)
    # Edge semantics: db→api→ui means db depends on api depends on ui
    # When ui changes, api and db are impacted (reverse propagation)
    start_nodes = _resolve_start_nodes(ceg, project, ["docs/ui_design.md"])
    assert len(start_nodes) == 1
    assert start_nodes[0][0] == "design:ui"

    impacts = ceg.propagate_impact("design:ui", max_depth=10)
    assert "design:api" in impacts
    assert "design:db" in impacts

    # Convention check from db_design change (for must_review edges)
    db_start = _resolve_start_nodes(ceg, project, ["docs/db_design.md"])
    conventions = _check_conventions_from_graph(ceg, db_start)

    ceg.close()
