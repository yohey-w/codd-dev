"""Root-jail for config-derived filesystem paths (path-escape closure class).

Config values that name filesystem paths may be absolute, and several extractors
historically read/enumerated them with no root check. An absolute config path that
points outside the project root then leaked file contents (or crashed) — a
path-escape false-green.

Each test pins: a config path pointing OUTSIDE the project root must NOT be
read/enumerated (the out-of-root file's content must never appear in the result),
and must not crash. In-root config paths keep their existing behaviour (regression).

Five closure sites:
  * codd/e2e_extractor.py  _configured_doc_files (scan.doc_dirs rglob+read)
  * codd/dag/builder.py    _project_path read sites (plan_task_file / lexicon_file)
  * codd/deployment/extractor.py  extract_deployment_docs (deployment.documents[*].path)
  * codd/dag/checks/depends_on_consistency.py  propagation_output_path read
  * codd/cli.py  _configured_text_files (doctor scan.source_dirs / scan.test_dirs rglob+read)
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from codd.cli import _configured_text_files
from codd.dag import DAG, Edge, Node
from codd.dag.builder import build_dag
from codd.dag.checks.depends_on_consistency import DependsOnConsistencyCheck
from codd.deployment.extractor import extract_deployment_docs
from codd.e2e_extractor import _operation_flows_from_project


# ---------------------------------------------------------------------------
# 1. e2e_extractor._configured_doc_files — scan.doc_dirs
# ---------------------------------------------------------------------------

_OPERATION_FLOW_DOC = """---
operation_flow:
  operations:
    - id: leaked_op
      actor: attacker
      verb: read
      target: secret
      ui_pattern: single_form
---
# leaked
"""


def _write_codd_yaml(project_root: Path, body: str) -> None:
    cfg = project_root / "codd" / "codd.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(body, encoding="utf-8")


def test_doc_dirs_outside_root_not_enumerated(tmp_path):
    """An absolute scan.doc_dir outside the project root must not be walked/read."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.md").write_text(_OPERATION_FLOW_DOC, encoding="utf-8")

    _write_codd_yaml(
        project_root,
        "project:\n  type: web\nscan:\n  doc_dirs:\n    - " + str(outside) + "\n",
    )

    flows = _operation_flows_from_project(project_root)
    sources = " ".join(source for source, _flow in flows)
    assert "leak" not in sources, "doc_dir outside project root was enumerated/read"


def test_doc_dirs_inside_root_still_read(tmp_path):
    """Regression: an in-root doc dir still produces flows (anti-false-red)."""
    project_root = tmp_path / "project"
    docs = project_root / "docs"
    docs.mkdir(parents=True)
    (docs / "flow.md").write_text(_OPERATION_FLOW_DOC, encoding="utf-8")

    _write_codd_yaml(
        project_root,
        "project:\n  type: web\nscan:\n  doc_dirs:\n    - docs/\n",
    )

    flows = _operation_flows_from_project(project_root)
    sources = " ".join(source for source, _flow in flows)
    assert "flow.md" in sources, "in-root doc dir must still be read"


# ---------------------------------------------------------------------------
# 2. dag/builder._project_path — plan_task_file / lexicon_file
# ---------------------------------------------------------------------------

# Plan-task node ⇔ the plan file was parsed. Lexicon `expected` node ⇔ lexicon parsed.
_PLAN_DOC = "## 1-1 Build feature\noutputs:\n  - src/feature.py\n"

_LEXICON_DOC = "required_artifacts:\n  - id: leaked_artifact\n    path: src/feature.py\n"


def _build_settings(**overrides):
    settings = {
        "design_doc_patterns": ["docs/design/*.md"],
        "impl_file_patterns": ["src/**/*.py"],
        "test_file_patterns": ["tests/**/*.py"],
        "plan_task_file": "docs/design/implementation_plan.md",
        "lexicon_file": "project_lexicon.yaml",
    }
    settings.update(overrides)
    return settings


def _has_kind(dag, kind: str) -> bool:
    return any(node.kind == kind for node in dag.nodes.values())


def test_plan_task_file_outside_root_not_read(tmp_path):
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True)
    (project_root / "src" / "feature.py").write_text("feature = True\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    plan = outside / "implementation_plan.md"
    plan.write_text(_PLAN_DOC, encoding="utf-8")

    dag = build_dag(project_root, _build_settings(plan_task_file=str(plan)))
    assert not _has_kind(dag, "plan_task"), "plan_task_file outside root was read"


def test_lexicon_file_outside_root_not_read(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    lexicon = outside / "lex.yaml"
    lexicon.write_text(_LEXICON_DOC, encoding="utf-8")

    dag = build_dag(project_root, _build_settings(lexicon_file=str(lexicon)))
    ids = " ".join(dag.nodes)
    assert "leaked_artifact" not in ids, "lexicon_file outside root was read"
    assert not _has_kind(dag, "expected"), "lexicon_file outside root was read"


def test_plan_and_lexicon_inside_root_still_read(tmp_path):
    """Regression: in-root plan/lexicon paths keep producing nodes."""
    project_root = tmp_path / "project"
    (project_root / "docs" / "design").mkdir(parents=True)
    (project_root / "src").mkdir(parents=True)
    (project_root / "src" / "feature.py").write_text("feature = True\n", encoding="utf-8")
    (project_root / "docs" / "design" / "implementation_plan.md").write_text(_PLAN_DOC, encoding="utf-8")
    (project_root / "project_lexicon.yaml").write_text(_LEXICON_DOC, encoding="utf-8")

    dag = build_dag(project_root, _build_settings())
    ids = " ".join(dag.nodes)
    assert _has_kind(dag, "plan_task"), "in-root plan_task_file must still be read"
    assert "leaked_artifact" in ids, "in-root lexicon_file must still be read"


# ---------------------------------------------------------------------------
# 3. deployment/extractor.extract_deployment_docs — deployment.documents[*].path
# ---------------------------------------------------------------------------


def test_deployment_documents_outside_root_not_read(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret_release.md").write_text("## LeakedDeploySection\n", encoding="utf-8")

    config = {
        "project": {"type": "web"},
        "deployment": {"documents": [{"path": str(outside / "secret_release.md")}]},
    }
    docs = extract_deployment_docs(project_root, config)
    paths = " ".join(doc.path for doc in docs)
    assert "secret_release" not in paths, "deployment document outside root was read"


def test_deployment_documents_inside_root_still_read(tmp_path):
    """Regression: in-root deployment document still extracted."""
    project_root = tmp_path / "project"
    (project_root / "ops").mkdir(parents=True)
    (project_root / "ops" / "release.md").write_text("## Start\n", encoding="utf-8")

    config = {"project": {"type": "web"}, "deployment": {"documents": [{"path": "ops/release.md"}]}}
    docs = extract_deployment_docs(project_root, config)
    assert [doc.path for doc in docs] == ["ops/release.md"]


# ---------------------------------------------------------------------------
# 4. depends_on_consistency — propagation_output_path
# ---------------------------------------------------------------------------


def _dag_with_depends_on() -> DAG:
    dag = DAG()
    dag.add_node(Node(id="docs/design/ux.md", kind="design_doc", path="docs/design/ux.md"))
    dag.add_node(Node(id="docs/design/api.md", kind="design_doc", path="docs/design/api.md"))
    dag.add_edge(Edge(from_id="docs/design/ux.md", to_id="docs/design/api.md", kind="depends_on"))
    return dag


# A payload that, if read, yields a value-comparison that would FAIL (red) — proving
# the out-of-root file was actually consumed. Jailed → file ignored → SKIP, not red.
_MISMATCH_PAYLOAD = {
    "values": [
        {"node_id": "docs/design/ux.md", "value_type": "url", "name": "dash", "value": "/a"},
        {"node_id": "docs/design/api.md", "value_type": "url", "name": "dash", "value": "/DIFFERENT"},
    ]
}


def test_propagation_output_outside_root_not_read(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    out_file = outside / "propagation_results.json"
    out_file.write_text(json.dumps(_MISMATCH_PAYLOAD), encoding="utf-8")

    settings = {"propagation_output_path": str(out_file)}
    result = DependsOnConsistencyCheck().run(_dag_with_depends_on(), project_root, settings)

    # If the out-of-root file were read, the mismatch would surface as a violation.
    assert result.violations == [], "propagation_output_path outside root was read"
    assert result.skipped is True


def test_propagation_output_inside_root_still_read(tmp_path):
    """Regression: in-root configured propagation output is still consumed."""
    out_file = tmp_path / ".codd" / "custom_results.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(_MISMATCH_PAYLOAD), encoding="utf-8")

    settings = {"propagation_output_path": ".codd/custom_results.json"}
    result = DependsOnConsistencyCheck().run(_dag_with_depends_on(), tmp_path, settings)

    # In-root mismatch must still be detected (anti-false-red).
    assert result.violations, "in-root propagation output must still be read/compared"


# ---------------------------------------------------------------------------
# 5. cli._configured_text_files — doctor scan.source_dirs / scan.test_dirs
# ---------------------------------------------------------------------------

# Marker filename whose presence in the returned paths proves the dir was walked.
_LEAK_MARKER = "leaked_source.py"


def test_source_dirs_outside_root_not_enumerated(tmp_path):
    """An absolute scan.source_dir outside the project root must not be walked."""
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / _LEAK_MARKER).write_text("app.post('/x')\n", encoding="utf-8")

    config = {"scan": {"source_dirs": [str(outside)]}}
    files = _configured_text_files(project_root, config, "source_dirs", ["src/"])

    assert all(_LEAK_MARKER not in p.name for p in files), (
        "scan.source_dir outside project root was enumerated/read"
    )


def test_source_dirs_inside_root_still_enumerated(tmp_path):
    """Regression: an in-root scan.source_dir is still walked (anti-false-red)."""
    project_root = tmp_path / "project"
    src = project_root / "src"
    src.mkdir(parents=True)
    (src / "real.py").write_text("app.post('/x')\n", encoding="utf-8")

    config = {"scan": {"source_dirs": ["src/"]}}
    files = _configured_text_files(project_root, config, "source_dirs", ["src/"])

    assert any(p.name == "real.py" for p in files), "in-root source dir must still be enumerated"


def test_test_dirs_outside_root_not_enumerated(tmp_path):
    """An absolute scan.test_dir outside the project root must not be walked."""
    project_root = tmp_path / "project"
    (project_root / "tests").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / _LEAK_MARKER).write_text("page.reload()\n", encoding="utf-8")

    config = {"scan": {"test_dirs": [str(outside)]}}
    files = _configured_text_files(project_root, config, "test_dirs", ["tests/"])

    assert all(_LEAK_MARKER not in p.name for p in files), (
        "scan.test_dir outside project root was enumerated/read"
    )


def test_source_dirs_outside_root_direct_file_not_read(tmp_path):
    """An absolute scan.source_dir naming a single out-of-root file is excluded."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    leak = outside / _LEAK_MARKER
    leak.write_text("app.post('/x')\n", encoding="utf-8")

    config = {"scan": {"source_dirs": [str(leak)]}}
    files = _configured_text_files(project_root, config, "source_dirs", ["src/"])

    assert all(_LEAK_MARKER not in p.name for p in files), (
        "out-of-root single source file was read"
    )


def test_source_dirs_in_root_symlink_escaping_not_enumerated(tmp_path):
    """An in-root symlink whose target escapes the root must not smuggle files."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / _LEAK_MARKER).write_text("app.post('/x')\n", encoding="utf-8")
    link = project_root / "linked_src"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        return  # platform without symlink support — nothing to assert

    config = {"scan": {"source_dirs": ["linked_src"]}}
    files = _configured_text_files(project_root, config, "source_dirs", ["src/"])

    assert all(_LEAK_MARKER not in p.name for p in files), (
        "in-root symlink escaping the root smuggled an out-of-root file"
    )
