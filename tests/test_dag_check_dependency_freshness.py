"""Tests for the doc-to-doc dependency_freshness check + reconciliation ledger.

All scenarios use a temporary git repository and synthetic design docs; no
project-specific paths or vocabulary.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from codd.dag import DAG, Edge, Node
from codd.dag.checks import get_registry
from codd.dag.checks.dependency_freshness import (
    DependencyFreshnessCheck,
    DependencyFreshnessResult,
)
from codd.reconciliation_ledger import (
    edge_key,
    ledger_path,
    load_ledger,
    record_reconciliation,
)


DOWNSTREAM = "docs/design/downstream.md"
UPSTREAM = "docs/design/upstream.md"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "codd").mkdir()
    (repo / "codd" / "codd.yaml").write_text("project:\n  frameworks: []\n", encoding="utf-8")
    docs = repo / "docs" / "design"
    docs.mkdir(parents=True)
    (repo / UPSTREAM).write_text("---\nnode_id: upstream\n---\n# Upstream\nshared rule v1\n", encoding="utf-8")
    (repo / DOWNSTREAM).write_text(
        "---\nnode_id: downstream\ndepends_on:\n  - upstream.md\n---\n# Downstream\nuses shared rule v1\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial docs")
    return repo


def _doc_dag() -> DAG:
    dag = DAG()
    dag.add_node(Node(id=DOWNSTREAM, kind="design_doc", path=DOWNSTREAM, attributes={}))
    dag.add_node(Node(id=UPSTREAM, kind="design_doc", path=UPSTREAM, attributes={}))
    dag.add_edge(Edge(from_id=DOWNSTREAM, to_id=UPSTREAM, kind="depends_on"))
    return dag


def _touch_upstream(repo: Path, content: str) -> None:
    (repo / UPSTREAM).write_text(
        f"---\nnode_id: upstream\n---\n# Upstream\n{content}\n", encoding="utf-8"
    )
    _git(repo, "add", UPSTREAM)
    # Force a strictly newer commit timestamp so recency comparison is stable.
    env_date = "2030-01-02T03:04:05"
    subprocess.run(
        ["git", "commit", "-q", "-m", "update upstream"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        env={
            **_clean_env(),
            "GIT_AUTHOR_DATE": env_date,
            "GIT_COMMITTER_DATE": env_date,
        },
    )


def _clean_env() -> dict[str, str]:
    import os

    return dict(os.environ)


def _commit_all(repo: Path, message: str, iso_date: str) -> None:
    """Stage everything and commit with a deterministic timestamp."""

    _git(repo, "add", "-A")
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        env={
            **_clean_env(),
            "GIT_AUTHOR_DATE": iso_date,
            "GIT_COMMITTER_DATE": iso_date,
        },
    )


def _write_doc(repo: Path, rel_path: str, body: str) -> None:
    (repo / rel_path).write_text(body, encoding="utf-8")


def test_dependency_freshness_registered():
    assert "dependency_freshness" in get_registry()


def test_no_ledger_no_upstream_change_explicit_baseline_note(tmp_path):
    """① ledger absent and nothing stale → pass, but the missing baseline is explicit."""
    repo = _init_repo(tmp_path)
    result = DependencyFreshnessCheck(_doc_dag(), repo, {}).run()
    assert isinstance(result, DependencyFreshnessResult)
    assert result.passed is True
    assert result.violations == []
    assert result.skipped is False
    assert any("baseline not created" in warning for warning in result.warnings)
    assert "checked" in result.message


def test_no_violation_with_warnings_surfaces_amber(tmp_path):
    """No-violation but warning-bearing path is amber/warn, not a green info/pass.

    Ledger absent + nothing stale → zero violations but a 'baseline not created'
    warning. Returning severity=info/status=pass hid that amber warning behind a
    green PASS row (the CLI only renders WARN + counts the finding when
    severity=='amber'). Deploy stays allowed (passed=True, block_deploy=False).
    Mirrors the resource_flow_coherence round-1 #2 false-green fix.
    """
    repo = _init_repo(tmp_path)
    result = DependencyFreshnessCheck(_doc_dag(), repo, {}).run()
    assert result.violations == []
    assert result.warnings  # the missing-baseline advisory is present
    assert result.severity == "amber"  # was "info"
    assert result.status == "warn"  # was "pass"
    # Deploy remains allowed: amber advisory, not a red gate.
    assert result.passed is True
    assert result.block_deploy is False


def test_no_violation_no_warnings_stays_info_pass(tmp_path):
    """Regression: zero violations AND zero warnings → unchanged info/pass.

    Acknowledging the edge via the ledger clears both the violation and the
    missing-baseline warning, so the clean path must still be a green info/pass.
    """
    repo = _init_repo(tmp_path)
    assert record_reconciliation(repo, DOWNSTREAM, UPSTREAM) is True
    result = DependencyFreshnessCheck(_doc_dag(), repo, {}).run()
    assert result.violations == []
    assert result.warnings == []
    assert result.severity == "info"
    assert result.status == "pass"
    assert result.passed is True
    assert result.block_deploy is False


def test_unacked_upstream_change_is_amber(tmp_path):
    """② upstream commit newer than downstream, never reconciled → amber warning."""
    repo = _init_repo(tmp_path)
    _touch_upstream(repo, "shared rule v2")
    result = DependencyFreshnessCheck(_doc_dag(), repo, {}).run()
    assert result.passed is True  # amber: advisory, deploy allowed
    assert result.severity == "amber"
    assert result.status == "warn"
    assert len(result.violations) == 1
    violation = result.violations[0]
    assert violation["kind"] == "never_reconciled"
    assert violation["upstream"] == UPSTREAM
    assert violation["downstream"] == DOWNSTREAM


def test_ledger_unacked_change_after_previous_ack(tmp_path):
    """② (ledger form) acked once, then upstream changed again → amber."""
    repo = _init_repo(tmp_path)
    assert record_reconciliation(repo, DOWNSTREAM, UPSTREAM) is True
    _touch_upstream(repo, "shared rule v2")
    result = DependencyFreshnessCheck(_doc_dag(), repo, {}).run()
    assert result.severity == "amber"
    assert len(result.violations) == 1
    assert result.violations[0]["kind"] == "unacked_upstream_change"


def test_ack_clears_violation(tmp_path):
    """③ after acknowledging the upstream change the check passes."""
    repo = _init_repo(tmp_path)
    _touch_upstream(repo, "shared rule v2")
    assert record_reconciliation(repo, DOWNSTREAM, UPSTREAM) is True
    result = DependencyFreshnessCheck(_doc_dag(), repo, {}).run()
    assert result.passed is True
    assert result.violations == []
    assert result.severity == "info"
    assert result.status == "pass"


def test_severity_red_opt_in(tmp_path):
    """④ severity: red opt-in turns the stale edge into a failing red check."""
    repo = _init_repo(tmp_path)
    _touch_upstream(repo, "shared rule v2")
    result = DependencyFreshnessCheck(
        _doc_dag(), repo, {"dependency_freshness": {"severity": "red"}}
    ).run()
    assert result.severity == "red"
    assert result.status == "fail"
    assert result.passed is False


def test_severity_red_via_codd_config(tmp_path):
    repo = _init_repo(tmp_path)
    _touch_upstream(repo, "shared rule v2")
    check = DependencyFreshnessCheck(_doc_dag(), repo, {})
    result = check.run(codd_config={"dependency_freshness": {"severity": "red"}})
    assert result.severity == "red"
    assert result.passed is False


def test_disabled_via_config_skips(tmp_path):
    repo = _init_repo(tmp_path)
    _touch_upstream(repo, "shared rule v2")
    result = DependencyFreshnessCheck(
        _doc_dag(), repo, {"dependency_freshness": {"enabled": False}}
    ).run()
    assert result.skipped is True
    assert result.passed is True
    assert result.violations == []


def test_no_doc_edges_skips_explicitly(tmp_path):
    repo = _init_repo(tmp_path)
    dag = DAG()
    dag.add_node(Node(id="src/a.py", kind="impl_file", path="src/a.py", attributes={}))
    result = DependencyFreshnessCheck(dag, repo, {}).run()
    assert result.skipped is True
    assert "no doc-to-doc depends_on edges" in result.message


def test_non_git_directory_skips(tmp_path):
    plain = tmp_path / "plain"
    (plain / "docs" / "design").mkdir(parents=True)
    (plain / DOWNSTREAM).write_text("x", encoding="utf-8")
    (plain / UPSTREAM).write_text("y", encoding="utf-8")
    result = DependencyFreshnessCheck(_doc_dag(), plain, {}).run()
    assert result.skipped is True
    assert result.passed is True


def test_impl_edges_are_ignored(tmp_path):
    """depends_on edges that touch non-doc nodes are out of scope."""
    repo = _init_repo(tmp_path)
    dag = _doc_dag()
    dag.add_node(Node(id="src/a.py", kind="impl_file", path="src/a.py", attributes={}))
    dag.add_edge(Edge(from_id=DOWNSTREAM, to_id="src/a.py", kind="depends_on"))
    _touch_upstream(repo, "shared rule v2")
    result = DependencyFreshnessCheck(dag, repo, {}).run()
    assert len(result.violations) == 1  # only the doc->doc edge
    assert result.violations[0]["upstream"] == UPSTREAM


def test_common_code_nodes_are_ignored(tmp_path):
    """kind="common" nodes with non-markdown paths are code, not docs.

    ``common_node_patterns`` assigns kind="common" to shared source files
    (for the transitive-closure exemption); their code->code depends_on edges
    must not enter the doc freshness check — the reconciliation ledger never
    acknowledges non-md paths, so they would be permanent false positives.
    """
    repo = _init_repo(tmp_path)
    dag = _doc_dag()
    # Shared code files classified common via common_node_patterns.
    dag.add_node(Node(id="src/lib/auth.ext", kind="common", path="src/lib/auth.ext", attributes={}))
    dag.add_node(
        Node(id="src/shared/config.ext", kind="common", path="src/shared/config.ext", attributes={})
    )
    # code->code edge between two common code nodes (expected_extraction origin).
    dag.add_edge(Edge(from_id="src/lib/auth.ext", to_id="src/shared/config.ext", kind="depends_on"))
    # doc->code and code->doc edges must be out of scope too.
    dag.add_edge(Edge(from_id=DOWNSTREAM, to_id="src/shared/config.ext", kind="depends_on"))
    dag.add_edge(Edge(from_id="src/lib/auth.ext", to_id=UPSTREAM, kind="depends_on"))
    (repo / "src" / "lib").mkdir(parents=True)
    (repo / "src" / "shared").mkdir(parents=True)
    (repo / "src" / "lib" / "auth.ext").write_text("code v1\n", encoding="utf-8")
    (repo / "src" / "shared" / "config.ext").write_text("code v1\n", encoding="utf-8")
    _commit_all(repo, "add shared code", "2030-01-01T00:00:00")
    # Upstream code changes alone — would trip the recency heuristic if included.
    (repo / "src" / "shared" / "config.ext").write_text("code v2\n", encoding="utf-8")
    _commit_all(repo, "update shared code", "2030-01-02T00:00:00")

    result = DependencyFreshnessCheck(dag, repo, {}).run()
    assert result.violations == []
    assert result.edges_checked == 1  # only the doc->doc edge


def test_common_markdown_docs_remain_in_scope(tmp_path):
    """kind="common" nodes with .md paths (frontmatter type: common) stay checked."""
    repo = _init_repo(tmp_path)
    common_doc = "docs/design/_common/conventions.md"
    (repo / common_doc).parent.mkdir(parents=True)
    (repo / common_doc).write_text(
        "---\nnode_id: conventions\ntype: common\n---\n# Conventions\nrule v1\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add common doc")
    dag = DAG()
    dag.add_node(Node(id=DOWNSTREAM, kind="design_doc", path=DOWNSTREAM, attributes={}))
    dag.add_node(Node(id=common_doc, kind="common", path=common_doc, attributes={}))
    dag.add_edge(Edge(from_id=DOWNSTREAM, to_id=common_doc, kind="depends_on"))
    # Common doc drifts after the downstream's last commit.
    (repo / common_doc).write_text(
        "---\nnode_id: conventions\ntype: common\n---\n# Conventions\nrule v2\n",
        encoding="utf-8",
    )
    _commit_all(repo, "update common doc", "2030-01-02T00:00:00")

    result = DependencyFreshnessCheck(dag, repo, {}).run()
    assert result.edges_checked == 1
    assert len(result.violations) == 1
    assert result.violations[0]["upstream"] == common_doc
    assert result.violations[0]["kind"] == "never_reconciled"


def test_record_reconciliation_writes_ledger(tmp_path):
    repo = _init_repo(tmp_path)
    assert record_reconciliation(repo, DOWNSTREAM, UPSTREAM) is True
    ledger = load_ledger(repo)
    assert ledger is not None
    entry = ledger["edges"][edge_key(DOWNSTREAM, UPSTREAM)]
    assert len(entry["upstream_commit"]) == 40
    assert entry["method"] == "propagate_commit"
    assert ledger_path(repo).is_file()


def test_record_reconciliation_without_git_history(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert record_reconciliation(plain, DOWNSTREAM, UPSTREAM) is False


def test_propagator_commit_acks_ledger(tmp_path, monkeypatch):
    """run_commit acknowledges doc->upstream-doc pairs from the verify state."""
    from codd.propagator import _record_reconciliation_acks

    repo = _init_repo(tmp_path)
    state = {
        "auto_docs": [],
        "hitl_docs": [
            {
                "node_id": "downstream",
                "path": DOWNSTREAM,
                "band": "amber",
                "confidence": 0.6,
                "upstream_paths": [UPSTREAM, "src/code.ts"],
            }
        ],
    }
    count = _record_reconciliation_acks(repo, state)
    assert count == 1  # the .ts source path is filtered out
    ledger = load_ledger(repo)
    assert edge_key(DOWNSTREAM, UPSTREAM) in ledger["edges"]

    # And the check passes afterwards even though upstream changed earlier.
    result = DependencyFreshnessCheck(_doc_dag(), repo, {}).run()
    assert result.violations == []


def test_runner_includes_dependency_freshness():
    from codd.dag.runner import CHECK_MODULES

    assert "codd.dag.checks.dependency_freshness" in CHECK_MODULES


def test_corrupt_ledger_treated_as_missing(tmp_path):
    repo = _init_repo(tmp_path)
    ledger_file = ledger_path(repo)
    ledger_file.parent.mkdir(parents=True, exist_ok=True)
    ledger_file.write_text("{not json", encoding="utf-8")
    result = DependencyFreshnessCheck(_doc_dag(), repo, {}).run()
    assert any("baseline not created" in warning for warning in result.warnings)


def test_depends_on_consistency_empty_input_visibility(tmp_path):
    """T2: empty propagation output is reported as '0 records compared'."""
    from codd.dag.checks.depends_on_consistency import DependsOnConsistencyCheck

    repo = _init_repo(tmp_path)
    output = repo / ".codd"
    output.mkdir(exist_ok=True)
    (output / "propagation_results.json").write_text(
        json.dumps({"generated_by": "baseline", "results": [], "values": []}),
        encoding="utf-8",
    )
    result = DependsOnConsistencyCheck(_doc_dag(), repo, {}).run()
    # Behaviour unchanged: still a pass, not skipped.
    assert result.passed is True
    assert result.skipped is False
    # Visibility added: the empty comparison is now explicit.
    assert result.records_compared == 0
    assert "0 records compared" in result.message


def test_depends_on_consistency_counts_real_records(tmp_path):
    from codd.dag.checks.depends_on_consistency import DependsOnConsistencyCheck

    repo = _init_repo(tmp_path)
    output = repo / ".codd"
    output.mkdir(exist_ok=True)
    payload = {
        "results": [
            {
                "from_node": DOWNSTREAM,
                "to_node": UPSTREAM,
                "edge_kind": "depends_on",
                "value_type": "url",
                "from_value": "/a",
                "to_value": "/a",
            }
        ]
    }
    (output / "propagation_results.json").write_text(json.dumps(payload), encoding="utf-8")
    result = DependsOnConsistencyCheck(_doc_dag(), repo, {}).run()
    assert result.passed is True
    assert result.records_compared == 1
    assert result.message == ""


# --- Joint-tip disambiguation (fallback false-negative fix) -----------------
#
# Scenario reproduced from a real incident: upstream drifted via exclusive
# commits, then a later bulk commit touched BOTH documents. Tip recency became
# equal, so the plain "upstream newer than downstream" heuristic went silent
# even though the downstream was never actually reconciled.


def _bare_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "codd").mkdir()
    (repo / "codd" / "codd.yaml").write_text("project:\n  frameworks: []\n", encoding="utf-8")
    (repo / "docs" / "design").mkdir(parents=True)
    return repo


def test_joint_tip_commit_masking_is_detected(tmp_path):
    """Upstream-exclusive drift hidden behind a joint bulk commit is flagged."""
    repo = _bare_repo(tmp_path)
    _write_doc(repo, UPSTREAM, "# Upstream\nrule v1\n")
    _write_doc(repo, DOWNSTREAM, "# Downstream\nuses rule v1\n")
    _commit_all(repo, "initial docs (joint)", "2030-01-01T00:00:00")
    # Upstream drifts on its own; downstream is never updated to match.
    _write_doc(repo, UPSTREAM, "# Upstream\nrule v2 BREAKING\n")
    _commit_all(repo, "upstream only", "2030-01-02T00:00:00")
    # Bulk commit touches both files (formatting/mass touch) — equal tips.
    _write_doc(repo, UPSTREAM, "# Upstream\nrule v2 BREAKING\n<!-- touched -->\n")
    _write_doc(repo, DOWNSTREAM, "# Downstream\nuses rule v1\n<!-- touched -->\n")
    _commit_all(repo, "bulk touch (joint tip)", "2030-01-03T00:00:00")

    result = DependencyFreshnessCheck(_doc_dag(), repo, {}).run()
    assert result.severity == "amber"
    assert len(result.violations) == 1
    violation = result.violations[0]
    assert violation["kind"] == "never_reconciled"
    assert violation["upstream"] == UPSTREAM
    assert violation["downstream"] == DOWNSTREAM
    assert "joint_tip_commit" in violation
    assert "does not prove" in violation["detail"]


def test_joint_tip_without_exclusive_upstream_drift_passes(tmp_path):
    """Docs only ever move together → no ordering evidence → no flag."""
    repo = _bare_repo(tmp_path)
    _write_doc(repo, UPSTREAM, "# Upstream\nrule v1\n")
    _write_doc(repo, DOWNSTREAM, "# Downstream\nuses rule v1\n")
    _commit_all(repo, "initial docs (joint)", "2030-01-01T00:00:00")
    _write_doc(repo, UPSTREAM, "# Upstream\nrule v2\n")
    _write_doc(repo, DOWNSTREAM, "# Downstream\nuses rule v2\n")
    _commit_all(repo, "co-update (joint tip)", "2030-01-02T00:00:00")

    result = DependencyFreshnessCheck(_doc_dag(), repo, {}).run()
    assert result.violations == []


def test_joint_tip_only_single_commit_history_passes(tmp_path):
    """One joint commit total: no pre-tip downstream state to drift from."""
    repo = _bare_repo(tmp_path)
    _write_doc(repo, UPSTREAM, "# Upstream\nrule v1\n")
    _write_doc(repo, DOWNSTREAM, "# Downstream\nuses rule v1\n")
    _commit_all(repo, "single joint commit", "2030-01-01T00:00:00")

    result = DependencyFreshnessCheck(_doc_dag(), repo, {}).run()
    assert result.violations == []


def test_joint_tip_after_downstream_reconciliation_passes(tmp_path):
    """Exclusive downstream update AFTER the upstream drift → considered current."""
    repo = _bare_repo(tmp_path)
    _write_doc(repo, UPSTREAM, "# Upstream\nrule v1\n")
    _write_doc(repo, DOWNSTREAM, "# Downstream\nuses rule v1\n")
    _commit_all(repo, "initial docs (joint)", "2030-01-01T00:00:00")
    _write_doc(repo, UPSTREAM, "# Upstream\nrule v2\n")
    _commit_all(repo, "upstream only", "2030-01-02T00:00:00")
    _write_doc(repo, DOWNSTREAM, "# Downstream\nuses rule v2\n")
    _commit_all(repo, "downstream reconciled", "2030-01-03T00:00:00")
    _write_doc(repo, UPSTREAM, "# Upstream\nrule v2\n<!-- touched -->\n")
    _write_doc(repo, DOWNSTREAM, "# Downstream\nuses rule v2\n<!-- touched -->\n")
    _commit_all(repo, "bulk touch (joint tip)", "2030-01-04T00:00:00")

    result = DependencyFreshnessCheck(_doc_dag(), repo, {}).run()
    assert result.violations == []


def test_joint_tip_masking_silenced_by_ledger_ack(tmp_path):
    """Ledger entry is authoritative: an acked edge skips the heuristic."""
    repo = _bare_repo(tmp_path)
    _write_doc(repo, UPSTREAM, "# Upstream\nrule v1\n")
    _write_doc(repo, DOWNSTREAM, "# Downstream\nuses rule v1\n")
    _commit_all(repo, "initial docs (joint)", "2030-01-01T00:00:00")
    _write_doc(repo, UPSTREAM, "# Upstream\nrule v2\n")
    _commit_all(repo, "upstream only", "2030-01-02T00:00:00")
    _write_doc(repo, UPSTREAM, "# Upstream\nrule v2\n<!-- touched -->\n")
    _write_doc(repo, DOWNSTREAM, "# Downstream\nuses rule v1\n<!-- touched -->\n")
    _commit_all(repo, "bulk touch (joint tip)", "2030-01-03T00:00:00")
    assert record_reconciliation(repo, DOWNSTREAM, UPSTREAM) is True

    result = DependencyFreshnessCheck(_doc_dag(), repo, {}).run()
    assert result.violations == []
    assert result.status == "pass"


def test_no_ledger_warning_mentions_edge_count_and_triage(tmp_path):
    repo = _init_repo(tmp_path)
    result = DependencyFreshnessCheck(_doc_dag(), repo, {}).run()
    warning = next(w for w in result.warnings if "baseline not created" in w)
    assert "1 doc-to-doc depends_on edge(s)" in warning
    assert "best-effort" in warning
    assert "codd propagate --verify" in warning


def test_commit_history_for_path_orders_newest_first(tmp_path):
    from codd.reconciliation_ledger import commit_history_for_path

    repo = _bare_repo(tmp_path)
    _write_doc(repo, UPSTREAM, "v1\n")
    _commit_all(repo, "first", "2030-01-01T00:00:00")
    _write_doc(repo, UPSTREAM, "v2\n")
    _commit_all(repo, "second", "2030-01-02T00:00:00")

    history = commit_history_for_path(repo, UPSTREAM)
    assert len(history) == 2
    assert history[0][1] > history[1][1]
    assert all(len(commit) == 40 for commit, _ in history)


def test_commit_history_for_path_non_git(tmp_path):
    from codd.reconciliation_ledger import commit_history_for_path

    plain = tmp_path / "plain"
    plain.mkdir()
    assert commit_history_for_path(plain, UPSTREAM) == []


@pytest.mark.parametrize("severity_value", ["RED", " red "])
def test_severity_value_normalized(tmp_path, severity_value):
    repo = _init_repo(tmp_path)
    _touch_upstream(repo, "v2")
    result = DependencyFreshnessCheck(
        _doc_dag(), repo, {"dependency_freshness": {"severity": severity_value}}
    ).run()
    assert result.severity == "red"
