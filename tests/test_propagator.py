"""Tests for codd propagate — reverse propagation from code to design docs."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

import codd.generator as generator_module
from codd.cli import main
from codd.propagator import (
    AffectedDoc,
    VerifiedDoc,
    VerifyResult,
    CommitResult,
    _build_update_prompt,
    _classify_docs_by_band,
    _find_design_docs_by_modules,
    _get_doc_confidence,
    _map_files_to_modules,
    _sanitize_update_body,
    _save_verify_state,
    _load_verify_state,
    _clear_verify_state,
    run_propagate,
    run_verify,
    run_commit,
    VERIFY_STATE_FILE,
)


# -- Fixtures ----------------------------------------------------------------


BASE_CONFIG = {
    "version": "0.1.0",
    "project": {"name": "taskboard", "language": "python"},
    "ai_command": "mock-ai --print",
    "scan": {
        "source_dirs": ["src"],
        "test_dirs": ["tests"],
        "doc_dirs": ["docs/design/", "docs/requirements/", "docs/detailed_design/"],
        "config_files": [],
        "exclude": [],
    },
    "graph": {"store": "jsonl", "path": "codd/scan"},
    "bands": {"green": {"min_confidence": 0.90, "min_evidence_count": 2}},
    "wave_config": {
        "1": [
            {
                "node_id": "req:taskboard-requirements",
                "output": "docs/requirements/requirements.md",
                "title": "TaskBoard Requirements",
                "modules": ["auth", "tasks", "notifications"],
                "depends_on": [],
                "conventions": [],
            },
        ],
        "2": [
            {
                "node_id": "design:system-design",
                "output": "docs/design/system_design.md",
                "title": "TaskBoard System Design",
                "modules": ["auth", "tasks", "notifications"],
                "depends_on": [],
                "conventions": [],
            },
        ],
        "3": [
            {
                "node_id": "design:auth-detail",
                "output": "docs/detailed_design/auth_detail.md",
                "title": "Auth Module Detailed Design",
                "modules": ["auth"],
                "depends_on": [],
                "conventions": [],
            },
        ],
    },
}


def _setup_project(tmp_path: Path) -> Path:
    """Create a project with config, source files, and design docs."""
    project = tmp_path / "project"
    project.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(BASE_CONFIG, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    # Source files
    (project / "src" / "auth").mkdir(parents=True)
    (project / "src" / "auth" / "service.py").write_text("class AuthService:\n    pass\n")
    (project / "src" / "tasks").mkdir(parents=True)
    (project / "src" / "tasks" / "service.py").write_text("class TaskService:\n    pass\n")

    # Design docs with modules frontmatter
    _write_design_doc(
        project / "docs" / "design" / "system_design.md",
        node_id="design:system-design",
        title="TaskBoard System Design",
        modules=["auth", "tasks", "notifications"],
        body="## 1. Overview\n\nSystem overview.\n\n## 2. Architecture\n\nArch details.\n",
    )
    _write_design_doc(
        project / "docs" / "detailed_design" / "auth_detail.md",
        node_id="design:auth-detail",
        title="Auth Module Detailed Design",
        modules=["auth"],
        body="## 1. Overview\n\nAuth detail.\n",
    )
    _write_design_doc(
        project / "docs" / "requirements" / "requirements.md",
        node_id="req:taskboard-requirements",
        title="TaskBoard Requirements",
        modules=["auth", "tasks", "notifications"],
        body="## 1. Overview\n\nRequirements.\n",
    )

    return project


def _write_design_doc(
    path: Path,
    *,
    node_id: str,
    title: str,
    modules: list[str],
    body: str,
):
    path.parent.mkdir(parents=True, exist_ok=True)
    codd_meta = {
        "node_id": node_id,
        "type": "design",
        "title": title,
        "modules": modules,
    }
    frontmatter = yaml.safe_dump({"codd": codd_meta}, sort_keys=False)
    path.write_text(f"---\n{frontmatter}---\n\n# {title}\n\n{body}", encoding="utf-8")


@pytest.fixture
def mock_propagate_ai(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake_run(command, *, input, capture_output, text, check):
        calls.append({"command": command, "input": input})
        # Return updated body
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=(
                "## 1. Overview\n\n"
                "Updated system overview reflecting code changes.\n\n"
                "## 2. Architecture\n\n"
                "Updated arch details.\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(generator_module.subprocess, "run", fake_run)
    return calls


# -- Unit tests: _map_files_to_modules --------------------------------------


def test_map_files_to_modules_basic():
    files = ["src/auth/service.py", "src/tasks/models.py", "README.md"]
    result = _map_files_to_modules(files, ["src"])
    assert result == {
        "src/auth/service.py": "auth",
        "src/tasks/models.py": "tasks",
    }
    # README not in any source dir → excluded
    assert "README.md" not in result


def test_map_files_to_modules_nested_source_dir():
    files = ["packages/core/auth/handler.ts"]
    result = _map_files_to_modules(files, ["packages/core"])
    assert result == {"packages/core/auth/handler.ts": "auth"}


def test_map_files_to_modules_root_level_file_excluded():
    """Files directly in source dir (no module subdir) are excluded."""
    files = ["src/main.py"]
    result = _map_files_to_modules(files, ["src"])
    assert result == {}


def test_map_files_to_modules_multiple_source_dirs():
    files = ["src/auth/a.py", "lib/utils/b.py"]
    result = _map_files_to_modules(files, ["src", "lib"])
    assert result == {"src/auth/a.py": "auth", "lib/utils/b.py": "utils"}


# -- Unit tests: _find_design_docs_by_modules --------------------------------


def test_find_design_docs_by_modules(tmp_path):
    project = _setup_project(tmp_path)
    config = yaml.safe_load((project / "codd" / "codd.yaml").read_text())

    docs = _find_design_docs_by_modules(
        project, config, {"auth"}, {"src/auth/service.py": "auth"},
    )

    node_ids = {d.node_id for d in docs}
    # system-design covers auth, auth-detail covers auth, requirements covers auth
    assert "design:system-design" in node_ids
    assert "design:auth-detail" in node_ids
    assert "req:taskboard-requirements" in node_ids


def test_find_design_docs_excludes_unrelated_modules(tmp_path):
    project = _setup_project(tmp_path)
    config = yaml.safe_load((project / "codd" / "codd.yaml").read_text())

    docs = _find_design_docs_by_modules(
        project, config, {"notifications"}, {"src/notifications/service.py": "notifications"},
    )

    node_ids = {d.node_id for d in docs}
    # auth-detail only covers ["auth"] → should NOT be found
    assert "design:auth-detail" not in node_ids
    # system-design covers notifications
    assert "design:system-design" in node_ids


def test_find_design_docs_returns_empty_for_unknown_module(tmp_path):
    project = _setup_project(tmp_path)
    config = yaml.safe_load((project / "codd" / "codd.yaml").read_text())

    docs = _find_design_docs_by_modules(
        project, config, {"billing"}, {"src/billing/service.py": "billing"},
    )
    assert docs == []


# -- Unit tests: _build_update_prompt ----------------------------------------


def test_build_update_prompt_contains_key_elements():
    doc = AffectedDoc(
        node_id="design:system-design",
        path="docs/design/system_design.md",
        title="System Design",
        modules=["auth", "tasks"],
        matched_modules=["auth"],
        changed_files=["src/auth/service.py"],
    )
    current = "---\ncodd:\n  node_id: design:system-design\n---\n\n# System Design\n\n## Overview\n\nOld content.\n"
    diff = "diff --git a/src/auth/service.py\n+    def new_method(self):\n"

    prompt = _build_update_prompt(doc, current, diff)

    assert "UPDATING" in prompt
    assert "design:system-design" in prompt
    assert "auth" in prompt
    assert "src/auth/service.py" in prompt
    assert "Old content" in prompt  # current doc included
    assert "new_method" in prompt  # code diff included
    assert "UNCHANGED" in prompt  # mentions leaving body unchanged
    assert "source code" in prompt.lower()  # source code path used for .py files


# -- Unit tests: doc→doc prompt -----------------------------------------------


def test_build_update_prompt_doc_to_doc():
    """When changed_files are all .md, prompt should use upstream design doc language."""
    doc = AffectedDoc(
        node_id="design:auth",
        path="docs/design/auth_design.md",
        title="Auth Design",
        modules=[],
        matched_modules=[],
        changed_files=["docs/design/system_design.md"],
    )
    current = "---\ncodd:\n  node_id: design:auth\n---\n\n# Auth Design\n\n## Overview\n\nOld content.\n"
    diff = "diff --git a/docs/design/system_design.md\n- 100 requests\n+ 200 requests\n"

    prompt = _build_update_prompt(doc, current, diff)

    assert "upstream design document" in prompt.lower()
    assert "UNCHANGED" in prompt
    assert "docs/design/system_design.md" in prompt
    assert "Old content" in prompt


# -- Unit tests: _find_changed_docs -------------------------------------------


def test_find_changed_docs(tmp_path):
    """_find_changed_docs identifies changed design docs with frontmatter."""
    from codd.propagator import _find_changed_docs

    # Setup: a doc dir with a design doc
    doc_dir = tmp_path / "docs" / "design"
    doc_dir.mkdir(parents=True)
    (doc_dir / "system_design.md").write_text(
        "---\ncodd:\n  node_id: design:system-design\n---\n\n# System Design\n"
    )
    # Non-doc file
    (tmp_path / "README.md").write_text("# Readme\n")

    config = {"scan": {"doc_dirs": ["docs"]}}
    changed_files = ["docs/design/system_design.md", "README.md"]

    result = _find_changed_docs(tmp_path, config, changed_files)

    assert len(result) == 1
    assert result[0]["node_id"] == "design:system-design"


# -- Integration test: run_propagate with mocked git -------------------------


def test_run_propagate_analysis_only(tmp_path, monkeypatch):
    """run_propagate without --update returns affected docs without calling AI."""
    project = _setup_project(tmp_path)

    monkeypatch.setattr(
        "codd.propagator.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0,
            stdout="src/auth/service.py\nsrc/auth/models.py\n", stderr="",
        ),
    )

    result = run_propagate(project, diff_target="HEAD", update=False)

    assert len(result.changed_files) == 2
    assert result.file_module_map == {
        "src/auth/service.py": "auth",
        "src/auth/models.py": "auth",
    }
    assert len(result.affected_docs) > 0
    affected_ids = {d.node_id for d in result.affected_docs}
    assert "design:auth-detail" in affected_ids
    assert "design:system-design" in affected_ids
    assert result.updated == []  # no update without flag


def test_run_propagate_with_update(tmp_path, monkeypatch):
    """run_propagate with update=True calls AI and updates docs."""
    project = _setup_project(tmp_path)
    ai_calls: list[dict] = []

    # Mock both git (propagator.subprocess) and AI (generator.subprocess)
    def patched_subprocess(command, *, capture_output=False, text=False,
                           cwd=None, check=False, input=None, **kw):
        if command[0] == "git" and "diff" in command:
            if "--name-only" in command:
                return subprocess.CompletedProcess(
                    args=command, returncode=0,
                    stdout="src/auth/service.py\n", stderr="",
                )
            return subprocess.CompletedProcess(
                args=command, returncode=0,
                stdout="diff --git a/src/auth/service.py\n+new code\n", stderr="",
            )
        # AI call
        ai_calls.append({"command": command, "input": input})
        return subprocess.CompletedProcess(
            args=command, returncode=0, stderr="",
            stdout=(
                "## 1. Overview\n\nUpdated overview.\n\n"
                "## 2. Architecture\n\nUpdated arch.\n"
            ),
        )

    monkeypatch.setattr("codd.propagator.subprocess.run", patched_subprocess)
    monkeypatch.setattr(generator_module.subprocess, "run", patched_subprocess)

    result = run_propagate(project, diff_target="HEAD", update=True)

    assert len(result.updated) > 0
    assert len(ai_calls) > 0  # AI was called

    # Verify prompt contains doc content and diff
    prompt = ai_calls[0]["input"]
    assert "UPDATING" in prompt
    assert "CODE DIFF" in prompt


# -- CLI test ----------------------------------------------------------------


def test_propagate_cli_analysis_mode(tmp_path, monkeypatch):
    """CLI 'codd propagate' shows analysis without updating."""
    from click.testing import CliRunner

    project = _setup_project(tmp_path)

    monkeypatch.setattr(
        "codd.propagator.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0,
            stdout="src/auth/service.py\n", stderr="",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["propagate", "--path", str(project)])

    assert result.exit_code == 0
    assert "auth" in result.output
    assert "design:auth-detail" in result.output
    assert "needs review" in result.output
    assert "--update" in result.output  # suggests running with --update


def test_propagate_cli_no_changes(tmp_path, monkeypatch):
    """CLI shows message when no files changed."""
    from click.testing import CliRunner

    project = _setup_project(tmp_path)

    monkeypatch.setattr(
        "codd.propagator.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0, stdout="", stderr="",
        ),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["propagate", "--path", str(project)])

    assert result.exit_code == 0
    assert "No changed files" in result.output


# -- Verify/Commit tests: band classification ----------------------------------


def _setup_graph(project: Path, config: dict) -> Path:
    """Create a graph with edges for testing band classification."""
    from codd.graph import CEG

    graph_path = project / config.get("graph", {}).get("path", "codd/scan")
    graph_path.mkdir(parents=True, exist_ok=True)
    graph = CEG(graph_path)

    # Add nodes
    graph.upsert_node("design:system-design", "design", path="docs/design/system_design.md")
    graph.upsert_node("design:auth-detail", "design", path="docs/detailed_design/auth_detail.md")
    graph.upsert_node("module:auth", "module")
    graph.upsert_node("module:tasks", "module")

    # Add edges with varying confidence
    # High confidence edge → green band
    eid1 = graph.add_edge("design:system-design", "module:auth", "depends_on", "technical")
    graph.add_evidence(eid1, "frontmatter", "frontmatter", 0.90, "modules field")
    graph.add_evidence(eid1, "human", "review", 0.85, "confirmed by architect")

    # Low confidence edge → amber band
    eid2 = graph.add_edge("design:auth-detail", "module:auth", "depends_on", "technical")
    graph.add_evidence(eid2, "frontmatter", "frontmatter", 0.60, "modules field only")

    graph.close()
    return graph_path


def test_get_doc_confidence_with_graph(tmp_path):
    """_get_doc_confidence returns max confidence from incoming edges."""
    from codd.graph import CEG

    project = _setup_project(tmp_path)
    config = yaml.safe_load((project / "codd" / "codd.yaml").read_text())
    _setup_graph(project, config)

    graph_path = project / config["graph"]["path"]
    graph = CEG(graph_path)

    doc = AffectedDoc(
        node_id="design:system-design", path="docs/design/system_design.md",
        title="System Design", modules=["auth", "tasks"],
        matched_modules=["auth"], changed_files=["src/auth/service.py"],
    )

    confidence, ev_count = _get_doc_confidence(graph, doc)
    # system-design has edge with 2 evidence pieces (0.90 + 0.85) → noisy-or > 0.90
    assert confidence > 0.90
    assert ev_count >= 2


def test_get_doc_confidence_without_graph():
    """_get_doc_confidence falls back to 0.5 when no graph."""
    doc = AffectedDoc(
        node_id="design:system-design", path="docs/design/system_design.md",
        title="System Design", modules=["auth"],
        matched_modules=["auth"], changed_files=["src/auth/service.py"],
    )
    confidence, ev_count = _get_doc_confidence(None, doc)
    assert confidence == 0.5
    assert ev_count == 0


def test_classify_docs_by_band(tmp_path):
    """Docs are classified into green/amber/gray based on graph evidence."""
    project = _setup_project(tmp_path)
    config = yaml.safe_load((project / "codd" / "codd.yaml").read_text())
    _setup_graph(project, config)

    bands_config = config.get("bands", {})

    docs = [
        AffectedDoc(
            node_id="design:system-design", path="docs/design/system_design.md",
            title="System Design", modules=["auth", "tasks"],
            matched_modules=["auth"], changed_files=["src/auth/service.py"],
        ),
        AffectedDoc(
            node_id="design:auth-detail", path="docs/detailed_design/auth_detail.md",
            title="Auth Detail", modules=["auth"],
            matched_modules=["auth"], changed_files=["src/auth/service.py"],
        ),
    ]

    verified = _classify_docs_by_band(project, config, docs, bands_config)

    assert len(verified) == 2
    bands = {v.doc.node_id: v.band for v in verified}
    # system-design has high confidence → green
    assert bands["design:system-design"] == "green"
    # auth-detail has low confidence → amber
    assert bands["design:auth-detail"] == "amber"


# -- Verify/Commit tests: state persistence ------------------------------------


def test_verify_state_save_load_clear(tmp_path):
    """Verify state is persisted and can be loaded/cleared."""
    project = _setup_project(tmp_path)

    auto = [VerifiedDoc(
        doc=AffectedDoc("design:sys", "docs/sys.md", "Sys", ["auth"], ["auth"], ["src/a.py"]),
        band="green", confidence=0.95, evidence_count=3,
    )]
    hitl = [VerifiedDoc(
        doc=AffectedDoc("design:auth", "docs/auth.md", "Auth", ["auth"], ["auth"], ["src/a.py"]),
        band="amber", confidence=0.65, evidence_count=1,
    )]

    _save_verify_state(project, auto, hitl, ["design:sys"], "HEAD")

    state = _load_verify_state(project)
    assert state is not None
    assert state["auto_node_ids"] == ["design:sys"]
    assert state["hitl_node_ids"] == ["design:auth"]
    assert len(state["auto_docs"]) == 1
    assert len(state["hitl_docs"]) == 1
    assert state["hitl_docs"][0]["band"] == "amber"

    _clear_verify_state(project)
    assert _load_verify_state(project) is None


# -- Verify/Commit tests: run_verify integration --------------------------------


def test_run_verify_splits_by_band(tmp_path, monkeypatch):
    """run_verify auto-applies green band and returns HITL list for amber/gray."""
    project = _setup_project(tmp_path)
    config = yaml.safe_load((project / "codd" / "codd.yaml").read_text())
    _setup_graph(project, config)
    ai_calls: list[dict] = []

    def patched_subprocess(command, *, capture_output=False, text=False,
                           cwd=None, check=False, input=None, **kw):
        if command[0] == "git" and "diff" in command:
            if "--name-only" in command:
                return subprocess.CompletedProcess(
                    args=command, returncode=0,
                    stdout="src/auth/service.py\n", stderr="",
                )
            return subprocess.CompletedProcess(
                args=command, returncode=0,
                stdout="diff --git a/src/auth/service.py\n+new code\n", stderr="",
            )
        ai_calls.append({"command": command, "input": input})
        return subprocess.CompletedProcess(
            args=command, returncode=0, stderr="",
            stdout="## 1. Overview\n\nUpdated.\n",
        )

    monkeypatch.setattr("codd.propagator.subprocess.run", patched_subprocess)
    monkeypatch.setattr(generator_module.subprocess, "run", patched_subprocess)

    result = run_verify(project, diff_target="HEAD")

    # Green band docs should be auto-applied
    assert len(result.auto_applied) > 0
    green_ids = {v.doc.node_id for v in result.auto_applied}
    assert "design:system-design" in green_ids

    # Amber band docs should be in HITL list
    assert len(result.needs_hitl) > 0
    amber_ids = {v.doc.node_id for v in result.needs_hitl}
    assert "design:auth-detail" in amber_ids

    # AI was called only for green band
    assert len(ai_calls) > 0
    assert len(result.updated) > 0

    # Verify state was saved
    state = _load_verify_state(project)
    assert state is not None
    assert len(state["hitl_docs"]) > 0


def test_run_verify_no_graph_all_amber(tmp_path, monkeypatch):
    """Without graph, all docs fall to amber (confidence=0.5, evidence=0)."""
    project = _setup_project(tmp_path)

    monkeypatch.setattr(
        "codd.propagator.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0,
            stdout="src/auth/service.py\n", stderr="",
        ),
    )

    result = run_verify(project, diff_target="HEAD")

    # No graph → all amber (confidence 0.5, 0 evidence → not green)
    assert len(result.auto_applied) == 0
    assert len(result.needs_hitl) > 0
    for v in result.needs_hitl:
        assert v.band == "amber"


# -- Verify/Commit tests: run_commit integration --------------------------------


def test_run_commit_records_knowledge(tmp_path, monkeypatch):
    """run_commit records HITL corrections as human evidence."""
    project = _setup_project(tmp_path)
    config = yaml.safe_load((project / "codd" / "codd.yaml").read_text())
    _setup_graph(project, config)

    # Save a fake verify state with HITL docs
    codd_dir = project / "codd"
    state = {
        "timestamp": "2026-04-07T12:00:00+00:00",
        "diff_target": "HEAD",
        "auto_node_ids": [],
        "hitl_node_ids": ["design:auth-detail"],
        "auto_docs": [],
        "hitl_docs": [
            {"node_id": "design:auth-detail",
             "path": "docs/detailed_design/auth_detail.md",
             "band": "amber", "confidence": 0.65},
        ],
    }
    (codd_dir / VERIFY_STATE_FILE).write_text(json.dumps(state), encoding="utf-8")

    # Mock git to return the HITL doc as modified
    def patched_subprocess(command, *, capture_output=False, text=False,
                           cwd=None, check=False, input=None, **kw):
        if command[0] == "git" and "diff" in command and "--name-only" in command:
            return subprocess.CompletedProcess(
                args=command, returncode=0,
                stdout="docs/detailed_design/auth_detail.md\n", stderr="",
            )
        return subprocess.CompletedProcess(
            args=command, returncode=0, stdout="", stderr="",
        )

    monkeypatch.setattr("codd.propagator.subprocess.run", patched_subprocess)

    result = run_commit(project, reason="Added archived state handling")

    assert len(result.committed_files) == 1
    assert result.knowledge_recorded > 0

    # Verify evidence was added to graph (check all edges involving the node)
    from codd.graph import CEG
    graph_path = project / config["graph"]["path"]
    graph = CEG(graph_path)
    all_edges = (graph.get_incoming_edges("design:auth-detail")
                 + graph.get_outgoing_edges("design:auth-detail"))
    human_evidence = [
        ev for edge in all_edges
        for ev in edge.get("evidence", [])
        if ev.get("source_type") == "human" and ev.get("method") == "hitl_correction"
    ]
    assert len(human_evidence) > 0
    assert "archived state" in human_evidence[0]["detail"].lower() or "archived" in human_evidence[0]["detail"]

    # State file should be cleaned up
    assert _load_verify_state(project) is None


def test_run_commit_without_verify_state_fails(tmp_path):
    """run_commit fails if no verify state exists."""
    project = _setup_project(tmp_path)

    with pytest.raises(ValueError, match="No verify state found"):
        run_commit(project)


# -- CLI tests: verify and commit modes ----------------------------------------


def test_propagate_cli_verify_mode(tmp_path, monkeypatch):
    """CLI 'codd propagate --verify' shows band classification."""
    from click.testing import CliRunner

    project = _setup_project(tmp_path)
    config = yaml.safe_load((project / "codd" / "codd.yaml").read_text())
    _setup_graph(project, config)

    def patched_subprocess(command, *, capture_output=False, text=False,
                           cwd=None, check=False, input=None, **kw):
        if command[0] == "git" and "diff" in command:
            if "--name-only" in command:
                return subprocess.CompletedProcess(
                    args=command, returncode=0,
                    stdout="src/auth/service.py\n", stderr="",
                )
            return subprocess.CompletedProcess(
                args=command, returncode=0,
                stdout="diff\n", stderr="",
            )
        return subprocess.CompletedProcess(
            args=command, returncode=0, stderr="",
            stdout="## 1. Overview\n\nUpdated.\n",
        )

    monkeypatch.setattr("codd.propagator.subprocess.run", patched_subprocess)
    monkeypatch.setattr(generator_module.subprocess, "run", patched_subprocess)

    runner = CliRunner()
    result = runner.invoke(main, ["propagate", "--verify", "--path", str(project)])

    assert result.exit_code == 0
    assert "Auto-applied" in result.output or "HITL" in result.output
    assert "confidence" in result.output


def test_propagate_cli_commit_mode(tmp_path, monkeypatch):
    """CLI 'codd propagate --commit' commits and records knowledge."""
    from click.testing import CliRunner

    project = _setup_project(tmp_path)
    config = yaml.safe_load((project / "codd" / "codd.yaml").read_text())
    _setup_graph(project, config)

    # Create verify state
    codd_dir = project / "codd"
    state = {
        "timestamp": "2026-04-07T12:00:00+00:00",
        "diff_target": "HEAD",
        "auto_node_ids": [],
        "hitl_node_ids": ["design:auth-detail"],
        "auto_docs": [],
        "hitl_docs": [
            {"node_id": "design:auth-detail",
             "path": "docs/detailed_design/auth_detail.md",
             "band": "amber", "confidence": 0.65},
        ],
    }
    (codd_dir / VERIFY_STATE_FILE).write_text(json.dumps(state), encoding="utf-8")

    def patched_subprocess(command, *, capture_output=False, text=False,
                           cwd=None, check=False, input=None, **kw):
        if command[0] == "git" and "diff" in command and "--name-only" in command:
            return subprocess.CompletedProcess(
                args=command, returncode=0,
                stdout="docs/detailed_design/auth_detail.md\n", stderr="",
            )
        return subprocess.CompletedProcess(
            args=command, returncode=0, stdout="", stderr="",
        )

    monkeypatch.setattr("codd.propagator.subprocess.run", patched_subprocess)

    runner = CliRunner()
    result = runner.invoke(main, [
        "propagate", "--commit", "--reason", "Fixed auth flow",
        "--path", str(project),
    ])

    assert result.exit_code == 0
    assert "Committed" in result.output or "committed" in result.output.lower()
    assert "Knowledge recorded" in result.output


# ---------------------------------------------------------------------------
# _sanitize_update_body: AI preamble stripping
# ---------------------------------------------------------------------------

class TestSanitizeUpdateBody:
    """AI sometimes emits analysis preamble before the document body.

    _sanitize_update_body must strip everything before the first # heading.
    """

    def test_strips_single_line_preamble(self):
        body = (
            "The code diff is an initial scaffolding that matches the design.\n"
            "\n"
            "# Key Sequence Diagrams\n"
            "\n"
            "## 1. Overview\n"
            "Content here.\n"
        )
        result = _sanitize_update_body(body)
        assert result.startswith("# Key Sequence Diagrams")

    def test_strips_multi_paragraph_preamble(self):
        body = (
            "The new code is an implementation of what is already documented.\n"
            "No design-level changes are needed.\n"
            "\n"
            "However, some minor updates are warranted.\n"
            "\n"
            "# System Design\n"
            "\n"
            "## 1. Overview\n"
        )
        result = _sanitize_update_body(body)
        assert result.startswith("# System Design")

    def test_preserves_clean_heading(self):
        body = "# ER Diagram\n\n## 1. Overview\nContent.\n"
        result = _sanitize_update_body(body)
        assert result.startswith("# ER Diagram")

    def test_strips_frontmatter_and_preamble(self):
        body = (
            "---\ncodd:\n  node_id: test\n---\n\n"
            "The design document already describes this.\n\n"
            "# ER Diagram\n\nContent.\n"
        )
        result = _sanitize_update_body(body)
        assert result.startswith("# ER Diagram")

    def test_raises_on_empty_body(self):
        with pytest.raises(ValueError, match="empty output"):
            _sanitize_update_body("")

    def test_raises_on_whitespace_only(self):
        with pytest.raises(ValueError, match="empty output"):
            _sanitize_update_body("   \n\n  ")
