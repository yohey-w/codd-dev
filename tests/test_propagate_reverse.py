"""Tests for codd propagate --reverse."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from codd._git_helper import _diff_files, _resolve_base_ref
from codd.cli import main
from codd.coherence_engine import EventBus, use_coherence_bus
from codd.propagator import (
    _detect_design_md_changes,
    _detect_lexicon_changes,
    propagate_reverse,
)


def _write_codd_config(project: Path) -> None:
    (project / "codd").mkdir(parents=True)
    (project / "codd" / "codd.yaml").write_text(
        yaml.safe_dump(
            {
                "version": "0.1.0",
                "project": {"name": "demo", "language": "python"},
                "scan": {"source_dirs": ["src"], "doc_dirs": ["docs/design"]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_git_helper_diff_files_no_changes(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("codd._git_helper.subprocess.run", fake_run)

    assert _diff_files("HEAD~1", cwd=tmp_path) == ""


def test_git_helper_diff_files_with_changes(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        assert command == [
            "git",
            "-c",
            "core.quotePath=false",
            "diff",
            "--unified=200",
            "HEAD~1",
            "--",
            "DESIGN.md",
        ]
        return subprocess.CompletedProcess(command, 0, stdout="diff text", stderr="")

    monkeypatch.setattr("codd._git_helper.subprocess.run", fake_run)

    assert _diff_files("HEAD~1", paths=["DESIGN.md"], cwd=tmp_path) == "diff text"


def test_resolve_base_ref_default(monkeypatch, tmp_path):
    seen: list[list[str]] = []

    def fake_run(command, **kwargs):
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr("codd._git_helper.subprocess.run", fake_run)

    assert _resolve_base_ref(None, cwd=tmp_path) == "HEAD~1"
    assert seen[0] == ["git", "rev-parse", "--verify", "HEAD~1"]


def test_resolve_base_ref_invalid(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 128, stdout="", stderr="bad ref")

    monkeypatch.setattr("codd._git_helper.subprocess.run", fake_run)

    with pytest.raises(ValueError, match="Cannot resolve git ref"):
        _resolve_base_ref("missing", cwd=tmp_path)


def test_detect_design_md_changes_no_diff(monkeypatch, tmp_path):
    monkeypatch.setattr("codd.propagator._diff_files", lambda *args, **kwargs: "")

    assert _detect_design_md_changes(tmp_path, "HEAD~1") == []


def test_detect_design_md_changes_with_diff(monkeypatch, tmp_path):
    diff = """diff --git a/DESIGN.md b/DESIGN.md
@@ -1,6 +1,6 @@
 colors:
-  primary: "#ffffff"
+  primary: "#111111"
"""
    monkeypatch.setattr("codd.propagator._diff_files", lambda *args, **kwargs: diff)

    assert _detect_design_md_changes(tmp_path, "HEAD~1") == [
        {
            "token": "colors.primary",
            "old": "#ffffff",
            "new": "#111111",
            "source_file": "DESIGN.md",
        }
    ]


def test_detect_design_md_changes_nested_value(monkeypatch, tmp_path):
    diff = """diff --git a/DESIGN.md b/DESIGN.md
@@ -1,8 +1,8 @@
 colors:
   primary:
-    $value: "#ffffff"
+    $value: "#222222"
     $type: color
"""
    monkeypatch.setattr("codd.propagator._diff_files", lambda *args, **kwargs: diff)

    assert _detect_design_md_changes(tmp_path, "HEAD~1")[0]["token"] == "colors.primary"


def test_detect_lexicon_changes_no_diff(monkeypatch, tmp_path):
    monkeypatch.setattr("codd.propagator._diff_files", lambda *args, **kwargs: "")

    assert _detect_lexicon_changes(tmp_path, "HEAD~1") == []


def test_detect_lexicon_changes_with_diff(monkeypatch, tmp_path):
    diff = """diff --git a/project_lexicon.yaml b/project_lexicon.yaml
@@ -1,5 +1,5 @@
 naming_conventions:
   - id: component_name
-    regex: "^[A-Z][A-Za-z0-9]+$"
+    regex: "^[a-z][A-Za-z0-9]+$"
"""
    monkeypatch.setattr("codd.propagator._diff_files", lambda *args, **kwargs: diff)

    assert _detect_lexicon_changes(tmp_path, "HEAD~1") == [
        {
            "convention": "component_name",
            "old": "^[A-Z][A-Za-z0-9]+$",
            "new": "^[a-z][A-Za-z0-9]+$",
            "kind": "regex",
            "source_file": "project_lexicon.yaml",
        }
    ]


def test_propagate_reverse_no_changes(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("codd.propagator._resolve_base_ref", lambda base, cwd=None: "HEAD~1")
    monkeypatch.setattr("codd.propagator._detect_design_md_changes", lambda root, base: [])

    assert propagate_reverse(tmp_path, "design_token", None) == 0

    assert "No changes detected" in capsys.readouterr().out


def test_propagate_reverse_design_token(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("codd.propagator._resolve_base_ref", lambda base, cwd=None: "HEAD~1")
    monkeypatch.setattr(
        "codd.propagator._detect_design_md_changes",
        lambda root, base: [
            {
                "token": "colors.primary",
                "old": "#ffffff",
                "new": "#000000",
                "source_file": "DESIGN.md",
            }
        ],
    )
    monkeypatch.setattr("codd.propagator.set_coherence_bus", lambda bus: None, raising=False)

    assert propagate_reverse(tmp_path, "design_token", None) == 0

    output = capsys.readouterr().out
    assert "Reverse propagation source=design_token" in output
    assert "colors.primary" in output
    assert "Dry run" in output


def test_propagate_reverse_apply_design_token_literal(monkeypatch, tmp_path):
    style = tmp_path / "src" / "app.css"
    style.parent.mkdir(parents=True)
    style.write_text(".button { color: #ffffff; }\n", encoding="utf-8")
    monkeypatch.setattr("codd.propagator._resolve_base_ref", lambda base, cwd=None: "HEAD~1")
    monkeypatch.setattr(
        "codd.propagator._detect_design_md_changes",
        lambda root, base: [
            {
                "token": "colors.primary",
                "old": "#ffffff",
                "new": "#000000",
                "source_file": "DESIGN.md",
            }
        ],
    )

    assert propagate_reverse(tmp_path, "design_token", None, apply=True) == 0

    assert style.read_text(encoding="utf-8") == ".button { color: #000000; }\n"


def test_propagate_reverse_unknown_source(monkeypatch, tmp_path):
    monkeypatch.setattr("codd.propagator._resolve_base_ref", lambda base, cwd=None: "HEAD~1")

    with pytest.raises(ValueError, match="Unknown source"):
        propagate_reverse(tmp_path, "unknown", None)


def test_use_coherence_bus_clears_on_exit(monkeypatch):
    calls = []
    bus = EventBus()

    monkeypatch.setattr("codd.coherence_engine.set_coherence_bus", calls.append)

    with use_coherence_bus(bus):
        assert calls == [bus]

    assert calls == [bus, None]


def test_use_coherence_bus_clears_on_exception(monkeypatch):
    calls = []
    bus = EventBus()

    monkeypatch.setattr("codd.coherence_engine.set_coherence_bus", calls.append)

    with pytest.raises(RuntimeError):
        with use_coherence_bus(bus):
            raise RuntimeError("boom")

    assert calls == [bus, None]


def test_cli_propagate_reverse_help():
    result = CliRunner().invoke(main, ["propagate", "--help"])

    assert result.exit_code == 0
    assert "--reverse" in result.output
    assert "--source" in result.output
    assert "--base" in result.output


def test_cli_propagate_reverse_invokes(monkeypatch, tmp_path):
    _write_codd_config(tmp_path)
    calls = {}

    def fake_reverse(project_root, source, base_ref, apply=False):
        calls["project_root"] = project_root
        calls["source"] = source
        calls["base_ref"] = base_ref
        calls["apply"] = apply
        return 0

    monkeypatch.setattr("codd.propagator.propagate_reverse", fake_reverse)

    result = CliRunner().invoke(
        main,
        [
            "propagate",
            "--path",
            str(tmp_path),
            "--reverse",
            "--source",
            "lexicon",
            "--base",
            "HEAD~2",
            "--apply",
        ],
    )

    assert result.exit_code == 0
    assert calls == {
        "project_root": tmp_path.resolve(),
        "source": "lexicon",
        "base_ref": "HEAD~2",
        "apply": True,
    }
