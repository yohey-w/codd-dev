"""Tests for frontmatter-based document scanning."""

import os
import pytest
import yaml
from pathlib import Path

from codd.graph import CEG
from codd.scanner import (
    _extract_frontmatter,
    _load_frontmatter,
    build_document_reference_index,
)


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
    doc.write_text("# No frontmatter here\n", encoding="utf-8")
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
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
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


# ═══════════════════════════════════════════════════════════
# Path-escape jail: scan.doc_dirs / scan.source_dirs (RC-1)
# ═══════════════════════════════════════════════════════════
#
# ``scan.doc_dirs`` / ``scan.source_dirs`` are user-controllable (codd.yaml). A
# ``../`` traversal, an absolute out-of-root path, or an in-root symlink whose
# target escapes the tree must NOT cause the scanner to walk/read files outside
# the project root, and must NOT inject out-of-root paths into the graph or the
# DAG document node→path map (``build_document_reference_index``) that the
# implementer / generator / assembler consume. Escapes are silently skipped
# (no crash, no false-green from an out-of-root file "satisfying" a node).


def _write_doc(path: Path, node_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "codd:\n"
        f'  node_id: "{node_id}"\n'
        "  type: requirement\n"
        "  depends_on:\n"
        '    - id: "file:src/x.py"\n'
        "      relation: implements\n"
        "---\n\n# doc\n"
    )


def _set_scan_dirs(codd_dir: Path, *, doc_dirs=None, source_dirs=None) -> dict:
    config_path = codd_dir / "codd.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    scan = config.setdefault("scan", {})
    if doc_dirs is not None:
        scan["doc_dirs"] = doc_dirs
    if source_dirs is not None:
        scan["source_dirs"] = source_dirs
    config_path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True))
    return config


def test_run_scan_does_not_walk_doc_dirs_parent_escape(tmp_path):
    """``doc_dirs: ['../outside']`` must not read frontmatter from outside root."""
    from codd.scanner import run_scan

    project, codd_dir = _setup_project(tmp_path)
    # A planted secret doc OUTSIDE the project tree.
    outside = tmp_path / "outside"
    _write_doc(outside / "secret.md", "req:OUTSIDE-ESCAPE")
    _set_scan_dirs(codd_dir, doc_dirs=["../outside"])

    run_scan(project, codd_dir)

    ceg = CEG(codd_dir / "scan")
    assert ceg.get_node("req:OUTSIDE-ESCAPE") is None
    ceg.close()


def test_run_scan_does_not_walk_doc_dirs_absolute_escape(tmp_path):
    """An absolute out-of-root ``doc_dirs`` entry must be skipped, not walked."""
    from codd.scanner import run_scan

    project, codd_dir = _setup_project(tmp_path)
    outside = tmp_path / "abs_outside"
    _write_doc(outside / "secret.md", "req:ABS-ESCAPE")
    _set_scan_dirs(codd_dir, doc_dirs=[str(outside)])

    run_scan(project, codd_dir)

    ceg = CEG(codd_dir / "scan")
    assert ceg.get_node("req:ABS-ESCAPE") is None
    ceg.close()


def test_run_scan_does_not_follow_in_root_symlink_escaping_root(tmp_path):
    """An in-root .md symlink whose target is OUTSIDE the root must not be read.

    ``os.walk`` does not descend a symlinked *directory* (followlinks=False), so
    the escape vector that the per-file re-confine actually guards is a symlinked
    *file* sitting inside a real in-root dir but pointing outside the tree —
    ``os.walk`` lists it, and only the resolve-and-confine gate rejects it.
    """
    from codd.scanner import run_scan

    project, codd_dir = _setup_project(tmp_path)
    outside = tmp_path / "linktarget"
    _write_doc(outside / "secret.md", "req:SYMLINK-ESCAPE")
    link = project / "docs" / "secret.md"
    try:
        link.symlink_to(outside / "secret.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")

    run_scan(project, codd_dir)

    ceg = CEG(codd_dir / "scan")
    assert ceg.get_node("req:SYMLINK-ESCAPE") is None
    ceg.close()


def test_run_scan_does_not_walk_source_dirs_parent_escape(tmp_path):
    """``source_dirs: ['../src_out']`` must not register out-of-root file nodes."""
    from codd.scanner import run_scan

    project, codd_dir = _setup_project(tmp_path)
    outside = tmp_path / "src_out"
    outside.mkdir()
    (outside / "leak.py").write_text("import os\n")
    _set_scan_dirs(codd_dir, source_dirs=["../src_out"])

    run_scan(project, codd_dir)

    ceg = CEG(codd_dir / "scan")
    nodes = ceg.all_nodes() if hasattr(ceg, "all_nodes") else []
    # No file node may point outside the project root.
    for node in nodes:
        path = node.get("path") or ""
        assert ".." not in str(path), f"out-of-root source node leaked: {node}"
    assert ceg.get_node("file:../src_out/leak.py") is None
    assert ceg.get_node("file:src_out/leak.py") is None
    ceg.close()


def test_build_document_reference_index_skips_doc_dirs_parent_escape(tmp_path):
    """The DAG node→path map must not include out-of-root docs via ``../``."""
    project, codd_dir = _setup_project(tmp_path)
    outside = tmp_path / "outside"
    _write_doc(outside / "secret.md", "req:IDX-ESCAPE")
    config = _set_scan_dirs(codd_dir, doc_dirs=["../outside"])

    index = build_document_reference_index(project, config)

    assert "req:IDX-ESCAPE" not in index.by_node_id
    for rel_posix in index.by_path:
        assert ".." not in rel_posix, f"out-of-root path in index: {rel_posix}"


def test_build_document_reference_index_skips_absolute_escape(tmp_path):
    project, codd_dir = _setup_project(tmp_path)
    outside = tmp_path / "abs_outside"
    _write_doc(outside / "secret.md", "req:IDX-ABS-ESCAPE")
    config = _set_scan_dirs(codd_dir, doc_dirs=[str(outside)])

    index = build_document_reference_index(project, config)

    assert "req:IDX-ABS-ESCAPE" not in index.by_node_id


def test_build_document_reference_index_skips_symlink_escape(tmp_path):
    project, codd_dir = _setup_project(tmp_path)
    outside = tmp_path / "linktarget"
    _write_doc(outside / "secret.md", "req:IDX-SYMLINK-ESCAPE")
    # Symlinked FILE inside the real in-root docs/ dir (os.walk lists it; only
    # the resolve-and-confine gate rejects the out-of-root target).
    link = project / "docs" / "secret.md"
    try:
        link.symlink_to(outside / "secret.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    config = yaml.safe_load((codd_dir / "codd.yaml").read_text(encoding="utf-8"))

    index = build_document_reference_index(project, config)

    assert "req:IDX-SYMLINK-ESCAPE" not in index.by_node_id


# --- ANTI-FALSE-RED: in-root doc/source scanning is unchanged ------------------


def test_run_scan_in_root_docs_still_scanned(tmp_path):
    """Regression guard: ordinary in-root ``docs/`` frontmatter is still read."""
    from codd.scanner import run_scan

    project, codd_dir = _setup_project(tmp_path)
    _write_doc(project / "docs" / "req.md", "req:IN-ROOT-OK")

    run_scan(project, codd_dir)

    ceg = CEG(codd_dir / "scan")
    assert ceg.get_node("req:IN-ROOT-OK") is not None
    ceg.close()


def test_build_document_reference_index_in_root_doc_present(tmp_path):
    """Regression guard: in-root docs ARE in the DAG node→path map."""
    project, codd_dir = _setup_project(tmp_path)
    _write_doc(project / "docs" / "req.md", "req:IN-ROOT-IDX-OK")
    config = yaml.safe_load((codd_dir / "codd.yaml").read_text(encoding="utf-8"))

    index = build_document_reference_index(project, config)

    assert "req:IN-ROOT-IDX-OK" in index.by_node_id
    assert any(
        entry.path.as_posix() == "docs/req.md"
        for entry in index.by_node_id["req:IN-ROOT-IDX-OK"]
    )
