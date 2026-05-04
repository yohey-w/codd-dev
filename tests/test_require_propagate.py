"""Tests for codd require --propagate."""

from __future__ import annotations

from pathlib import Path

import yaml
from click.testing import CliRunner

from codd import require_propagate as require_module
from codd.cli import main
from codd.graph import CEG
from codd.require_propagate import (
    _apply_proposals,
    _detect_requirements_changes,
    _format_changes_for_prompt,
    _generate_update_proposals,
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


def _write_design_doc(path: Path, title: str = "Auth Design", body: str = "Old content.\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: {title}\n---\n# {title}\n\n{body}",
        encoding="utf-8",
    )


def _requirement_change() -> dict:
    return {
        "file": "docs/requirements/auth.md",
        "field": "status",
        "old": "draft",
        "new": "approved",
    }


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
    assert "--apply" in result.output


def test_require_propagate_dry_run_shows_proposals(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    project.mkdir()
    _write_config(project)
    _write_design_doc(project / "docs" / "design" / "auth.md")

    ceg = CEG(project / ".codd" / "scan")
    ceg.upsert_node("req:auth", "requirement", path="docs/requirements/auth.md")
    ceg.upsert_node("design:auth", "design", path="docs/design/auth.md")
    ceg.add_edge("design:auth", "req:auth", "depends_on", "traceability")
    ceg.close()

    monkeypatch.setattr(
        require_module,
        "_detect_requirements_changes",
        lambda *_: [_requirement_change()],
    )
    monkeypatch.setattr(
        require_module,
        "_invoke_ai_command",
        lambda *_: "# Auth Design\n\nUpdated content.\n",
    )

    exit_code = require_propagate(project, "HEAD~1")
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Proposal for docs/design/auth.md:" in output
    assert "+Updated content." in output
    assert "1 proposal(s) generated. Use --apply to write changes." in output
    assert "Old content." in (project / "docs" / "design" / "auth.md").read_text(encoding="utf-8")


def test_require_propagate_apply_writes_file(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    project.mkdir()
    _write_config(project)
    doc_path = project / "docs" / "design" / "auth.md"
    _write_design_doc(doc_path)

    ceg = CEG(project / ".codd" / "scan")
    ceg.upsert_node("req:auth", "requirement", path="docs/requirements/auth.md")
    ceg.upsert_node("design:auth", "design", path="docs/design/auth.md")
    ceg.add_edge("design:auth", "req:auth", "depends_on", "traceability")
    ceg.close()

    monkeypatch.setattr(
        require_module,
        "_detect_requirements_changes",
        lambda *_: [_requirement_change()],
    )
    monkeypatch.setattr(
        require_module,
        "_invoke_ai_command",
        lambda *_: "# Auth Design\n\nUpdated content.\n",
    )

    exit_code = require_propagate(project, "HEAD~1", apply=True)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Applied proposal to docs/design/auth.md." in output
    assert "Updated content." in doc_path.read_text(encoding="utf-8")


def test_generate_update_proposals_no_nodes(tmp_path):
    ceg = CEG(tmp_path / "scan")

    assert _generate_update_proposals(tmp_path, [_requirement_change()], [], ceg) == []


def test_generate_update_proposals_with_nodes(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    _write_config(project)
    _write_design_doc(project / "docs" / "design" / "auth.md")

    ceg = CEG(project / ".codd" / "scan")
    ceg.upsert_node("design:auth", "design", path="docs/design/auth.md", name="Auth Design")

    calls = {}

    def fake_invoke(ai_command, prompt):
        calls["ai_command"] = ai_command
        calls["prompt"] = prompt
        return "# Auth Design\n\nUpdated content.\n"

    monkeypatch.setattr(require_module, "_invoke_ai_command", fake_invoke)

    proposals = _generate_update_proposals(
        project,
        [_requirement_change()],
        [{"node_id": "design:auth", "path": "docs/design/auth.md", "triggered_by": []}],
        ceg,
    )

    assert len(proposals) == 1
    assert proposals[0]["path"] == project / "docs" / "design" / "auth.md"
    assert proposals[0]["proposal"] == "# Auth Design\n\nUpdated content.\n"
    assert "docs/requirements/auth.md" in calls["prompt"]


def test_generate_update_proposals_llm_error(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    project.mkdir()
    _write_config(project)
    _write_design_doc(project / "docs" / "design" / "auth.md")

    ceg = CEG(project / ".codd" / "scan")
    ceg.upsert_node("design:auth", "design", path="docs/design/auth.md")

    def fake_invoke(*_):
        raise ValueError("boom")

    monkeypatch.setattr(require_module, "_invoke_ai_command", fake_invoke)

    proposals = _generate_update_proposals(
        project,
        [_requirement_change()],
        [{"node_id": "design:auth", "path": "docs/design/auth.md", "triggered_by": []}],
        ceg,
    )

    assert proposals == []
    assert "Warning: proposal generation failed for docs/design/auth.md: boom" in capsys.readouterr().out


def test_apply_proposals_writes_content(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    doc_path = project / "docs" / "design" / "auth.md"
    _write_design_doc(doc_path)

    exit_code = _apply_proposals(
        project,
        [
            {
                "path": doc_path,
                "original": doc_path.read_text(encoding="utf-8"),
                "proposal": "# Auth Design\n\nUpdated content.\n",
            }
        ],
    )

    assert exit_code == 0
    assert "Updated content." in doc_path.read_text(encoding="utf-8")


def test_cli_require_propagate_apply(monkeypatch, tmp_path):
    runner = CliRunner()
    captured = {}

    def fake_require_propagate(project_root, base_ref, apply=False, ai_command=None):
        captured["project_root"] = project_root
        captured["base_ref"] = base_ref
        captured["apply"] = apply
        captured["ai_command"] = ai_command
        return 0

    monkeypatch.setattr(require_module, "require_propagate", fake_require_propagate)

    result = runner.invoke(
        main,
        [
            "require",
            "--path",
            str(tmp_path),
            "--propagate",
            "--apply",
            "--base",
            "HEAD~2",
            "--ai-cmd",
            "mock-ai --print",
        ],
    )

    assert result.exit_code == 0
    assert captured == {
        "project_root": tmp_path.resolve(),
        "base_ref": "HEAD~2",
        "apply": True,
        "ai_command": "mock-ai --print",
    }


def test_format_changes_for_prompt():
    text = _format_changes_for_prompt([_requirement_change()])

    assert "Requirements frontmatter changes detected:" in text
    assert "docs/requirements/auth.md" in text
    assert "status changed from 'draft' to 'approved'" in text
