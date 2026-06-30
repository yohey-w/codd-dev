"""Tests for the ``source_completeness`` DAG check + its shared pure function.

Red-before-green: written before the implementation exists. They lock four
behaviours the design requires:

* the gap (on-disk source files outnumber DAG source nodes) surfaces as an
  ``amber`` finding — deploy-allowed, never a red gate (red = owner-gated);
* a complete source set is a real PASS (``checked_count`` > 0, not vacuous);
* zero source files on disk is a SKIP, never a vacuous green PASS;
* the computation is language-agnostic — it discriminates by a DATA suffix set,
  so several suffixes work with no ``language ==`` core branch;

plus that the builder's legacy ``_warn_source_completeness`` advisory still warns
(backward compatible) now that it delegates to the shared pure function.
"""

from __future__ import annotations

import warnings

from codd.dag import DAG, Node
from codd.dag.materiality import is_vacuous_pass


def _settings(*suffixes: str) -> dict:
    suffix_list = list(suffixes)
    return {"implementation_suffixes": suffix_list, "test_suffixes": suffix_list}


def _source_node(dag: DAG, rel_id: str) -> None:
    dag.add_node(Node(id=rel_id, kind="impl_file", path=rel_id))


# --- registration ------------------------------------------------------------


def test_source_completeness_registered():
    from codd.dag.checks import get_registry
    from codd.dag.checks.source_completeness import SourceCompletenessCheck

    assert get_registry()["source_completeness"] is SourceCompletenessCheck


# --- (a) gap → amber WARN ----------------------------------------------------


def test_amber_when_on_disk_source_outnumbers_nodes(tmp_path):
    from codd.dag.checks.source_completeness import SourceCompletenessCheck

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "c.py").write_text("x = 1\n", encoding="utf-8")  # outside the DAG

    dag = DAG()
    _source_node(dag, "a.py")
    _source_node(dag, "b.py")

    result = SourceCompletenessCheck(dag, tmp_path, _settings(".py")).run()

    assert result.check_name == "source_completeness"
    assert result.severity == "amber"
    assert result.status == "warn"
    assert result.passed is True
    assert result.block_deploy is False
    assert result.on_disk == 3
    assert result.node_count == 2
    assert "c.py" in result.findings
    assert "a.py" not in result.findings
    # checked_count is the number of on-disk source files examined.
    assert result.checked_count == 3
    # human-visible detail carries the count + an example.
    assert "source file(s) on disk" in result.message
    assert "c.py" in result.message


def test_warn_result_is_surfaced_by_shared_render_predicates(tmp_path):
    """The amber finding must render as WARN via the generic CLI path.

    No bespoke renderer branch: the shared ``result_status`` predicates (used by
    every verify/coverage/deploy summary) must classify the result as an
    amber-with-findings WARN.
    """
    from codd.dag import result_status
    from codd.dag.checks.source_completeness import SourceCompletenessCheck

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("x = 1\n", encoding="utf-8")

    dag = DAG()
    _source_node(dag, "a.py")

    result = SourceCompletenessCheck(dag, tmp_path, _settings(".py")).run()

    assert result_status.result_severity(result) == "amber"
    assert result_status.result_has_findings(result) is True
    assert result_status.pass_is_warn(result) is True
    assert is_vacuous_pass(result) is False


# --- (b) exact match → PASS --------------------------------------------------


def test_pass_when_every_on_disk_source_is_a_node(tmp_path):
    from codd.dag.checks.source_completeness import SourceCompletenessCheck

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("x = 1\n", encoding="utf-8")

    dag = DAG()
    _source_node(dag, "a.py")
    _source_node(dag, "b.py")

    result = SourceCompletenessCheck(dag, tmp_path, _settings(".py")).run()

    assert result.status == "pass"
    assert result.passed is True
    assert result.skipped is False
    assert result.findings == []
    assert result.checked_count == 2  # real work was done
    assert result.block_deploy is False
    assert is_vacuous_pass(result) is False


# --- (c) no source on disk → SKIP (not vacuous PASS) -------------------------


def test_skip_when_no_source_files_on_disk(tmp_path):
    from codd.dag.checks.source_completeness import SourceCompletenessCheck

    (tmp_path / "README.md").write_text("# docs only\n", encoding="utf-8")

    dag = DAG()  # no source nodes

    result = SourceCompletenessCheck(dag, tmp_path, _settings(".py")).run()

    assert result.status == "skip"
    assert result.skipped is True
    assert result.checked_count == 0
    assert result.findings == []
    assert result.passed is True
    assert is_vacuous_pass(result) is False


# --- (d) pure function unit --------------------------------------------------


def test_compute_source_completeness_pure(tmp_path):
    from codd.dag.builder import compute_source_completeness

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "c.py").write_text("x = 1\n", encoding="utf-8")

    node_paths = {(tmp_path / "a.py").resolve(), (tmp_path / "b.py").resolve()}
    report = compute_source_completeness(tmp_path, _settings(".py"), node_paths)

    assert report.on_disk == 3
    assert report.node_count == 2
    assert report.missing == ["c.py"]
    assert ".py" in report.source_suffixes


def test_compute_source_completeness_caps_missing_at_ten(tmp_path):
    from codd.dag.builder import compute_source_completeness

    for index in range(15):
        (tmp_path / f"f{index:02d}.py").write_text("x = 1\n", encoding="utf-8")

    report = compute_source_completeness(tmp_path, _settings(".py"), set())

    assert report.on_disk == 15
    assert report.node_count == 0
    assert len(report.missing) == 10  # sample is capped


# --- (e) language-agnostic: multiple suffixes work, no language literal -------


def test_language_agnostic_multiple_suffixes(tmp_path):
    from codd.dag.checks.source_completeness import SourceCompletenessCheck

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.go").write_text("package main\n", encoding="utf-8")
    (tmp_path / "c.rs").write_text("fn main() {}\n", encoding="utf-8")  # outside DAG

    dag = DAG()
    _source_node(dag, "a.py")
    _source_node(dag, "b.go")

    result = SourceCompletenessCheck(dag, tmp_path, _settings(".py", ".go", ".rs")).run()

    assert result.status == "warn"
    assert result.on_disk == 3
    # BOTH the .py and .go nodes were counted via the suffix SET — proving the
    # discrimination is data-driven, not a single-language literal.
    assert result.node_count == 2
    assert "c.rs" in result.findings


# --- backward compatibility: the builder advisory still warns ----------------


def test_warn_source_completeness_still_warns(tmp_path):
    from codd.dag.builder import _warn_source_completeness

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "c.py").write_text("x = 1\n", encoding="utf-8")

    impl_nodes = {"a.py": (tmp_path / "a.py"), "b.py": (tmp_path / "b.py")}

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _warn_source_completeness(tmp_path, _settings(".py"), impl_nodes, {})

    messages = [str(w.message) for w in caught]
    assert any("source file(s) on disk" in m for m in messages)
    assert any("c.py" in m for m in messages)


def test_warn_source_completeness_silent_when_complete(tmp_path):
    from codd.dag.builder import _warn_source_completeness

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    impl_nodes = {"a.py": (tmp_path / "a.py")}

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _warn_source_completeness(tmp_path, _settings(".py"), impl_nodes, {})

    assert not [w for w in caught if "source file(s) on disk" in str(w.message)]
