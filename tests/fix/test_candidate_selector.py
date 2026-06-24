"""Unit tests for codd.fix.candidate_selector."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codd.dag import DAG, Node
from codd.fix.candidate_selector import select_candidates
from codd.fix.phenomenon_parser import PhenomenonAnalysis


def _make_dag(tmp_path: Path, docs: dict[str, tuple[str, str]]) -> DAG:
    """Create a DAG with design_doc nodes whose files we materialize."""
    dag = DAG()
    for node_id, (kind, body) in docs.items():
        rel_path = node_id
        (tmp_path / rel_path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel_path).write_text(body, encoding="utf-8")
        dag.add_node(Node(id=node_id, kind=kind, path=rel_path, attributes={}))
    return dag


def test_no_design_docs_yields_empty_selection(tmp_path):
    dag = DAG()
    analysis = PhenomenonAnalysis(intent="improvement", subject_terms=["x"])
    result = select_candidates(analysis, dag=dag, project_root=tmp_path)
    assert result.candidates == []
    assert "no design_doc" in result.fallback_reason


def test_tier1_lexicon_match_scores_highest(tmp_path):
    dag = _make_dag(
        tmp_path,
        {
            "auth/login.md": ("design_doc", "# Login\nlogin error wording"),
            "billing/pay.md": ("design_doc", "# Pay\nstripe checkout flow"),
        },
    )
    analysis = PhenomenonAnalysis(
        intent="improvement",
        subject_terms=["login"],
        lexicon_hits=["login"],
    )
    result = select_candidates(analysis, dag=dag, project_root=tmp_path)
    assert result.candidates
    top = result.candidates[0]
    assert top.node_id == "auth/login.md"
    assert top.tier_1_score >= 1.0


def test_common_kind_included_by_default(tmp_path):
    dag = _make_dag(
        tmp_path,
        {
            "shared/errors.md": ("common", "# Errors\nlogin failure messages"),
        },
    )
    analysis = PhenomenonAnalysis(subject_terms=["login"], lexicon_hits=["login"])
    result = select_candidates(analysis, dag=dag, project_root=tmp_path)
    assert any(c.node_id == "shared/errors.md" for c in result.candidates)


def test_common_code_file_excluded_from_design_candidates(tmp_path):
    """Regression (v3.1.0 'Attempt 1 on route.ts'): kind='common' is overloaded
    for common design *docs* and common *code* files. Only markdown docs may be
    Stage-3 design-update candidates — a common code file must never be selected
    as a design target even when it matches the phenomenon terms."""
    dag = _make_dag(
        tmp_path,
        {
            "docs/design/courses.md": (
                "design_doc",
                "# Courses\nvideo lesson contentBody handling",
            ),
            # overloaded common: an implementation file matching common_node_patterns
            "src/app/api/v1/lessons/route.ts": (
                "common",
                "video lesson contentBody create handler",
            ),
        },
    )
    analysis = PhenomenonAnalysis(
        intent="new_feature",
        subject_terms=["video", "lesson", "contentBody"],
        lexicon_hits=["contentBody"],
    )
    result = select_candidates(
        analysis, dag=dag, project_root=tmp_path, include_common=True
    )
    paths = {c.path for c in result.candidates}
    assert "src/app/api/v1/lessons/route.ts" not in paths
    assert all(c.path.endswith(".md") for c in result.candidates)
    # the genuine markdown design doc remains selectable
    assert "docs/design/courses.md" in paths


def test_common_kind_excluded_when_disabled(tmp_path):
    dag = _make_dag(
        tmp_path,
        {"shared/errors.md": ("common", "login failure messages")},
    )
    analysis = PhenomenonAnalysis(subject_terms=["login"], lexicon_hits=["login"])
    result = select_candidates(
        analysis,
        dag=dag,
        project_root=tmp_path,
        include_common=False,
    )
    assert not result.candidates


def test_clear_winner_flag(tmp_path):
    dag = _make_dag(
        tmp_path,
        {
            "auth/login.md": ("design_doc", "login login login"),
            "billing/pay.md": ("design_doc", "stripe payment"),
        },
    )
    analysis = PhenomenonAnalysis(subject_terms=["login"], lexicon_hits=["login"])
    result = select_candidates(analysis, dag=dag, project_root=tmp_path)
    assert result.candidates[0].node_id == "auth/login.md"
    assert result.is_clear_winner


def test_tier2_llm_invoked_when_provided(tmp_path):
    dag = _make_dag(
        tmp_path,
        {
            "auth/login.md": ("design_doc", "login flow"),
            "auth/oauth.md": ("design_doc", "external IdP integration"),
        },
    )
    analysis = PhenomenonAnalysis(
        intent="improvement",
        subject_terms=["external login"],
        lexicon_hits=[],
    )

    def fake_ai(_prompt: str) -> str:
        return json.dumps({
            "scores": {
                "auth/oauth.md": {"score": 0.85, "reason": "external IdP matches"},
            }
        })

    result = select_candidates(
        analysis,
        dag=dag,
        project_root=tmp_path,
        ai_invoke=fake_ai,
    )
    ids = [c.node_id for c in result.candidates]
    assert "auth/oauth.md" in ids


def test_tier2_llm_failure_does_not_crash(tmp_path):
    dag = _make_dag(
        tmp_path,
        {"auth/login.md": ("design_doc", "login flow")},
    )
    analysis = PhenomenonAnalysis(subject_terms=["login"], lexicon_hits=["login"])

    def bad_ai(_prompt: str) -> str:
        raise RuntimeError("ai unavailable")

    result = select_candidates(
        analysis,
        dag=dag,
        project_root=tmp_path,
        ai_invoke=bad_ai,
    )
    # Tier 1 still produced a candidate even though tier 2 errored.
    assert result.candidates
    assert result.candidates[0].node_id == "auth/login.md"


def test_max_candidates_cap(tmp_path):
    docs = {f"d/{i}.md": ("design_doc", "login") for i in range(10)}
    dag = _make_dag(tmp_path, docs)
    analysis = PhenomenonAnalysis(subject_terms=["login"], lexicon_hits=["login"])
    result = select_candidates(
        analysis,
        dag=dag,
        project_root=tmp_path,
        max_candidates=3,
    )
    assert len(result.candidates) == 3


def test_node_path_traversal_does_not_read_outside_root(tmp_path):
    """Path-escape jail: a DAG ``node.path`` is user-controllable (it can be a
    ``../../`` traversal or absolute path). Its text must NEVER be read from
    outside the project root and fed into tier-1 scoring / the tier-2 AI prompt —
    that would be a path-escape false-green (an off-root file 'matching' the
    phenomenon). An out-of-root node contributes no score, no evidence."""
    project = tmp_path / "project"
    project.mkdir()
    # A secret file outside the project root whose body would score on the lexicon.
    secret = tmp_path / "secret.md"
    secret.write_text("# Secret\nlogin login login secret password", encoding="utf-8")

    # A genuine in-root design doc that legitimately matches.
    (project / "auth").mkdir()
    (project / "auth" / "login.md").write_text(
        "# Login\nlogin error wording", encoding="utf-8"
    )

    dag = DAG()
    dag.add_node(Node(id="auth/login.md", kind="design_doc", path="auth/login.md", attributes={}))
    # node.path escapes the project root via ``../``.
    dag.add_node(
        Node(id="escape", kind="design_doc", path="../secret.md", attributes={})
    )

    analysis = PhenomenonAnalysis(
        intent="improvement",
        subject_terms=["login"],
        lexicon_hits=["login", "password"],
    )
    result = select_candidates(analysis, dag=dag, project_root=project)

    node_ids = {c.node_id for c in result.candidates}
    # The traversal node must NOT appear as a scored candidate (its out-of-root
    # text was never read → no lexicon hits → score 0).
    assert "escape" not in node_ids
    # The genuine in-root doc is unaffected (anti-false-red: in-root still works).
    assert "auth/login.md" in node_ids


def test_node_path_absolute_out_of_root_not_read(tmp_path):
    """An absolute ``node.path`` pointing outside the root is jailed too."""
    project = tmp_path / "project"
    project.mkdir()
    secret = tmp_path / "outside.md"
    secret.write_text("# Outside\npassword password password", encoding="utf-8")

    dag = DAG()
    dag.add_node(
        Node(id="abs-escape", kind="design_doc", path=str(secret), attributes={})
    )

    analysis = PhenomenonAnalysis(
        intent="improvement",
        subject_terms=["password"],
        lexicon_hits=["password"],
    )
    result = select_candidates(analysis, dag=dag, project_root=project)
    assert all(c.node_id != "abs-escape" for c in result.candidates)


def test_node_path_symlink_escape_not_read(tmp_path):
    """An in-root path that is a symlink whose target escapes the root is jailed."""
    project = tmp_path / "project"
    project.mkdir()
    secret = tmp_path / "real_secret.md"
    secret.write_text("# Secret\nlogin login login", encoding="utf-8")
    link = project / "linked.md"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")

    dag = DAG()
    dag.add_node(Node(id="linked.md", kind="design_doc", path="linked.md", attributes={}))

    analysis = PhenomenonAnalysis(
        intent="improvement",
        subject_terms=["login"],
        lexicon_hits=["login"],
    )
    result = select_candidates(analysis, dag=dag, project_root=project)
    assert all(c.node_id != "linked.md" for c in result.candidates)


def test_in_root_node_text_still_read(tmp_path):
    """Anti-false-red: a normal in-root node.path is read and scored as before."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "d").mkdir()
    (project / "d" / "spec.md").write_text("# Spec\nlogin handling", encoding="utf-8")

    dag = DAG()
    dag.add_node(Node(id="d/spec.md", kind="design_doc", path="d/spec.md", attributes={}))

    analysis = PhenomenonAnalysis(subject_terms=["login"], lexicon_hits=["login"])
    result = select_candidates(analysis, dag=dag, project_root=project)
    assert any(c.node_id == "d/spec.md" for c in result.candidates)
