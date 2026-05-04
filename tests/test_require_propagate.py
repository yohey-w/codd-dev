"""Tests for codd require --propagate."""

from __future__ import annotations

from pathlib import Path

import yaml
from click.testing import CliRunner

from codd import require_propagate as require_module
from codd.cli import main
from codd.graph import CEG
from codd.require_propagate import (
    _detect_requirements_changes,
    require_propagate,
)


def _write_config(project: Path) -> None:
    codd_dir = project / ".codd"
    codd_dir.mkdir(parents=True, exist_ok=True)
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(
            {
                "version": "0.1.0",
                "project": {"name": "demo", "language": "python"},
                "graph": {"store": "jsonl", "path": ".codd/scan"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_find_depended_by_empty(tmp_path):
    ceg = CEG(tmp_path / "scan")
    ceg.upsert_node("req:auth", "requirement", path="docs/requirements/auth.md")

    assert ceg.find_depended_by("req:auth") == []


def test_find_depended_by_single(tmp_path):
    ceg = CEG(tmp_path / "scan")
    ceg.upsert_node("req:auth", "requirement", path="docs/requirements/auth.md")
    ceg.upsert_node("design:auth", "design", path="docs/design/auth.md")
    ceg.add_edge("design:auth", "req:auth", "depends_on", "traceability")
    ceg.add_edge("design:other", "req:auth", "derives_from", "traceability")

    depended_by = ceg.find_depended_by("req:auth")

    assert len(depended_by) == 1
    assert depended_by[0]["source_id"] == "design:auth"
    assert depended_by[0]["source_path"] == "docs/design/auth.md"


def test_find_depended_by_circular(tmp_path):
    ceg = CEG(tmp_path / "scan")
    ceg.upsert_node("node:a", "design", path="docs/design/a.md")
    ceg.upsert_node("node:b", "design", path="docs/design/b.md")
    ceg.add_edge("node:a", "node:b", "depends_on", "traceability")
    ceg.add_edge("node:b", "node:a", "depends_on", "traceability")

    depended_by = ceg.find_depended_by("node:b")

    assert [edge["source_id"] for edge in depended_by] == ["node:a"]


def test_detect_requirements_changes_no_diff(tmp_path, monkeypatch):
    monkeypatch.setattr(require_module, "_diff_files", None, raising=False)

    def fake_diff_files(base_ref, *, cwd, paths):
        return ""

    monkeypatch.setattr("codd._git_helper._diff_files", fake_diff_files)

    assert _detect_requirements_changes(tmp_path, "HEAD~1") == []


def test_detect_requirements_changes_with_diff(tmp_path, monkeypatch):
    diff_text = """diff --git a/docs/requirements/auth.md b/docs/requirements/auth.md
index 1111111..2222222 100644
--- a/docs/requirements/auth.md
+++ b/docs/requirements/auth.md
@@ -1,7 +1,7 @@
 ---
 codd:
   node_id: req:auth
-status: draft
+status: approved
 priority: high
 ---
 """

    def fake_diff_files(base_ref, *, cwd, paths):
        return diff_text

    monkeypatch.setattr("codd._git_helper._diff_files", fake_diff_files)

    changes = _detect_requirements_changes(tmp_path, "HEAD~1")

    assert changes == [
        {
            "file": "docs/requirements/auth.md",
            "field": "status",
            "old": "draft",
            "new": "approved",
        }
    ]


def test_require_propagate_no_changes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(require_module, "_detect_requirements_changes", lambda *_: [])

    exit_code = require_propagate(tmp_path, "HEAD~9")

    assert exit_code == 0
    assert "No requirements changes detected since HEAD~9." in capsys.readouterr().out


def test_require_propagate_no_ceg(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        require_module,
        "_detect_requirements_changes",
        lambda *_: [
            {
                "file": "docs/requirements/auth.md",
                "field": "status",
                "old": "draft",
                "new": "approved",
            }
        ],
    )

    exit_code = require_propagate(tmp_path, "HEAD~1")

    assert exit_code == 1
    assert "Warning: CoDD graph not found. Run `codd scan` first." in capsys.readouterr().out


def test_require_propagate_lists_affected_design_docs(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    project.mkdir()
    _write_config(project)

    ceg = CEG(project / ".codd" / "scan")
    ceg.upsert_node("req:auth", "requirement", path="docs/requirements/auth.md")
    ceg.upsert_node("design:auth", "design", path="docs/design/auth.md")
    ceg.add_edge("design:auth", "req:auth", "depends_on", "traceability")
    ceg.close()

    monkeypatch.setattr(
        require_module,
        "_detect_requirements_changes",
        lambda *_: [
            {
                "file": "docs/requirements/auth.md",
                "field": "status",
                "old": "draft",
                "new": "approved",
            }
        ],
    )

    exit_code = require_propagate(project, "HEAD~1")
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Requirements changes detected (1):" in output
    assert "Affected design docs (1):" in output
    assert "docs/design/auth.md (design:auth)" in output
    assert "triggered_by: docs/requirements/auth.md:status" in output


def test_cli_require_propagate_help():
    runner = CliRunner()

    result = runner.invoke(main, ["require", "--help"])

    assert result.exit_code == 0
    assert "--propagate" in result.output
    assert "--base" in result.output
