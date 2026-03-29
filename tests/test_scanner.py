"""Tests for frontmatter-based document scanning."""

import pytest
import yaml
from pathlib import Path

from codd.graph import CEG
from codd.scanner import _extract_frontmatter, _load_frontmatter


@pytest.fixture
def ceg(tmp_path):
    scan_dir = tmp_path / "scan"
    graph = CEG(scan_dir)
    yield graph
    graph.close()


def test_extract_frontmatter_with_codd(tmp_path):
    doc = tmp_path / "test.md"
    doc.write_text("""---
codd:
  node_id: "req:FR-01"
  type: requirement
  depends_on:
    - id: "file:src/auth.ts"
      relation: implements
---

# FR-01: Authentication
""")
    result = _extract_frontmatter(doc)
    assert result is not None
    assert result["node_id"] == "req:FR-01"
    assert result["type"] == "requirement"
    assert len(result["depends_on"]) == 1


def test_extract_frontmatter_without_codd(tmp_path):
    doc = tmp_path / "test.md"
    doc.write_text("""---
title: Just a normal document
---

# Hello
""")
    result = _extract_frontmatter(doc)
    assert result is None


def test_extract_frontmatter_no_frontmatter(tmp_path):
    doc = tmp_path / "test.md"
    doc.write_text("# No frontmatter here\n")
    result = _extract_frontmatter(doc)
    assert result is None


def test_load_frontmatter_depends_on(ceg):
    codd_data = {
        "node_id": "req:FR-03",
        "type": "requirement",
        "depends_on": [
            {"id": "design:auth-service", "relation": "specifies"},
            {"id": "file:src/middleware/auth/", "relation": "implements"},
            {"id": "db:rls_policies", "relation": "requires"},
        ],
    }
    _load_frontmatter(ceg, "docs/requirements.md", codd_data)

    node = ceg.get_node("req:FR-03")
    assert node is not None
    assert node["type"] == "requirement"

    outgoing = ceg.get_outgoing_edges("req:FR-03")
    assert len(outgoing) == 3
    targets = {e["target_id"] for e in outgoing}
    assert "design:auth-service" in targets
    assert "file:src/middleware/auth/" in targets
    assert "db:rls_policies" in targets


def test_load_frontmatter_conventions(ceg):
    codd_data = {
        "node_id": "req:FR-03",
        "type": "requirement",
        "conventions": [
            {
                "targets": ["db:rls_policies", "test:test_tenant_isolation"],
                "reason": "テナント分離は最上位制約",
            }
        ],
    }
    _load_frontmatter(ceg, "docs/requirements.md", codd_data)

    outgoing = ceg.get_outgoing_edges("req:FR-03")
    must_reviews = [e for e in outgoing if e["relation"] == "must_review"]
    assert len(must_reviews) == 2


def test_load_frontmatter_data_dependencies(ceg):
    codd_data = {
        "node_id": "doc:db_design.md",
        "type": "design",
        "data_dependencies": [
            {
                "table": "tenants",
                "column": "status",
                "affects": ["file:src/middleware/auth/"],
                "condition": "status変更でAPI拒否",
            }
        ],
    }
    _load_frontmatter(ceg, "docs/db_design.md", codd_data)

    node = ceg.get_node("db_column:tenants.status")
    assert node is not None
    assert node["type"] == "db_column"

    outgoing = ceg.get_outgoing_edges("db_column:tenants.status")
    assert len(outgoing) == 1
    assert outgoing[0]["target_id"] == "file:src/middleware/auth/"


CoDD_YAML_TEMPLATE = """
project:
  name: test-project
  language: python
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


def _setup_project(tmp_path):
    """Helper: create a minimal project structure for scan tests."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "docs").mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(CoDD_YAML_TEMPLATE)
    return project, codd_dir


def test_scan_refreshes_auto_generated_not_accumulate(tmp_path):
    """Auto-generated data should refresh, not accumulate on repeated scans."""
    from codd.scanner import run_scan

    project, codd_dir = _setup_project(tmp_path)

    (project / "docs" / "requirements.md").write_text("""---
codd:
  node_id: "req:FR-01"
  type: requirement
  depends_on:
    - id: "file:src/tenant.ts"
      relation: implements
---

# FR-01
""")

    # First scan
    run_scan(project, codd_dir)
    ceg = CEG(codd_dir / "scan")
    assert ceg.count_nodes() == 2
    assert ceg.count_edges() == 1
    ceg.close()

    # Second scan — should NOT accumulate
    run_scan(project, codd_dir)
    ceg = CEG(codd_dir / "scan")
    assert ceg.count_nodes() == 2  # Still 2, not 4
    assert ceg.count_edges() == 1  # Still 1, not 2
    ceg.close()


def test_scan_preserves_human_evidence(tmp_path):
    """Human-added evidence (source_type='human') must survive scan refresh."""
    from codd.scanner import run_scan

    project, codd_dir = _setup_project(tmp_path)

    (project / "docs" / "requirements.md").write_text("""---
codd:
  node_id: "req:FR-01"
  type: requirement
  depends_on:
    - id: "file:src/tenant.ts"
      relation: implements
---

# FR-01
""")

    # First scan — creates auto-generated evidence
    run_scan(project, codd_dir)

    # Simulate a human adding manual evidence
    ceg = CEG(codd_dir / "scan")
    ceg.upsert_node("file:src/auth.ts", "file", name="auth.ts")
    ceg.upsert_node("db_table:users", "db_table", name="users")
    edge_id = ceg.add_edge("file:src/auth.ts", "db_table:users", "reads_table", "structural")
    ceg.add_evidence(edge_id, "human", "manual", 0.85,
                     detail="Senior dev confirmed: auth always reads users table")
    human_before = ceg.count_human_evidence()
    assert human_before == 1
    ceg.close()

    # Re-scan — auto-generated data refreshes, human data preserved
    run_scan(project, codd_dir)

    ceg = CEG(codd_dir / "scan")
    human_after = ceg.count_human_evidence()
    assert human_after == 1  # Human evidence survived!

    # The human-added edge should still exist
    node = ceg.get_node("file:src/auth.ts")
    assert node is not None
    outgoing = ceg.get_outgoing_edges("file:src/auth.ts")
    assert any(e["target_id"] == "db_table:users" for e in outgoing)

    # Auto-generated data from frontmatter should also be there
    node = ceg.get_node("req:FR-01")
    assert node is not None
    ceg.close()


def test_scan_warns_when_docs_markdown_is_missing_frontmatter(tmp_path, capsys):
    from codd.scanner import run_scan

    project, codd_dir = _setup_project(tmp_path)
    (project / "docs" / "notes.md").write_text("# Missing frontmatter\n")

    run_scan(project, codd_dir)

    output = capsys.readouterr().out
    assert "WARNING: docs/notes.md: missing CoDD YAML frontmatter" in output


def test_scan_warns_when_design_document_has_no_dependencies(tmp_path, capsys):
    from codd.scanner import run_scan

    project, codd_dir = _setup_project(tmp_path)
    (project / "docs" / "system_design.md").write_text("""---
codd:
  node_id: "design:system-design"
  type: design
---

# System Design
""")

    run_scan(project, codd_dir)

    output = capsys.readouterr().out
    assert "WARNING: docs/system_design.md: design document has empty depends_on" in output


def test_scan_warns_when_wave_config_output_is_missing(tmp_path, capsys):
    from codd.scanner import run_scan

    project, codd_dir = _setup_project(tmp_path)
    config_path = codd_dir / "codd.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["wave_config"] = {
        "1": [
            {
                "node_id": "design:acceptance-criteria",
                "output": "docs/test/acceptance_criteria.md",
                "title": "Acceptance Criteria",
                "depends_on": [{"id": "req:lms-requirements-v2.0"}],
            }
        ]
    }
    config_path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True))

    run_scan(project, codd_dir)

    output = capsys.readouterr().out
    assert (
        "WARNING: docs/test/acceptance_criteria.md: wave_config defines design:acceptance-criteria "
        "but the file has not been generated"
    ) in output
