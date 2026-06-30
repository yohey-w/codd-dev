"""Tests for the ``unresolved_import_residue`` DAG check + its shared measurement.

Increment 2 of the completeness-accounting gate, written red-before-green (before
the check / the ``dag.import_residue_report`` attachment exist). It mirrors the
increment-1 ``source_completeness`` discipline and locks these behaviours:

* unresolved internal-looking import residue surfaces as an ``amber`` finding —
  deploy-allowed, never a red gate (red = a NEW, owner-gated gate);
* a fully-resolved internal-import set is a real PASS (``checked_count`` > 0, not
  vacuous);
* a project with NO internal-looking import is a SKIP, never a vacuous green PASS
  (so is a DAG that never went through the import-edge pass);
* the measurement is computed once, in the single place import resolution happens
  (``_add_import_edges``), and attached to the DAG, so the builder's legacy
  ``_warn_unresolved_residue`` advisory and the check consume the SAME residue and
  can never drift;
* the residue computation is language-agnostic — ``_is_internal_looking_specifier``
  discriminates by specifier SHAPE (relative / first-party alias / C++ quote), so
  Python, JS and C++ all work with no ``language ==`` core branch.
"""

from __future__ import annotations

import warnings

import yaml

from codd.dag import DAG
from codd.dag.materiality import is_vacuous_pass


def _dag_with_report(residue, internal_import_count: int) -> DAG:
    """A DAG carrying a synthetic import-residue measurement (check-level input)."""
    from codd.dag.builder import ImportResidueReport

    dag = DAG()
    dag.import_residue_report = ImportResidueReport(
        residue=list(residue), internal_import_count=internal_import_count
    )
    return dag


def _write(path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# --- registration ------------------------------------------------------------


def test_unresolved_import_residue_registered():
    from codd.dag.checks import get_registry
    from codd.dag.checks.unresolved_import_residue import UnresolvedImportResidueCheck

    assert get_registry()["unresolved_import_residue"] is UnresolvedImportResidueCheck


# --- (a) residue → amber WARN ------------------------------------------------


def test_amber_when_residue_present():
    from codd.dag.checks.unresolved_import_residue import UnresolvedImportResidueCheck

    dag = _dag_with_report(["pkg/mod.py: .missing"], internal_import_count=3)

    result = UnresolvedImportResidueCheck(dag, ".", {}).run()

    assert result.check_name == "unresolved_import_residue"
    assert result.severity == "amber"
    assert result.status == "warn"
    assert result.passed is True
    assert result.block_deploy is False
    assert result.findings == ["pkg/mod.py: .missing"]
    # checked_count is the number of internal-looking specifiers examined.
    assert result.checked_count == 3
    # human-visible detail carries the count + an example specifier.
    assert "pkg/mod.py: .missing" in result.message
    assert "unresolved residue" in result.message


def test_findings_are_sorted_and_message_caps_examples():
    from codd.dag.checks.unresolved_import_residue import UnresolvedImportResidueCheck

    residue = [f"z{index}.py: .gone{index}" for index in range(8)]
    # deliberately shuffled input order; the check must emit deterministic output.
    dag = _dag_with_report(list(reversed(residue)), internal_import_count=12)

    result = UnresolvedImportResidueCheck(dag, ".", {}).run()

    assert result.findings == sorted(residue)  # full list, deterministic
    assert result.checked_count == 12
    # message shows a bounded example sample (first few sorted), not all eight.
    assert result.message.count(".py:") <= 5


def test_warn_result_is_surfaced_by_shared_render_predicates():
    """The amber finding must render as WARN via the generic CLI path.

    No bespoke renderer branch: the shared ``result_status`` predicates (used by
    every verify/coverage/deploy summary) must classify the result as an
    amber-with-findings WARN.
    """
    from codd.dag import result_status
    from codd.dag.checks.unresolved_import_residue import UnresolvedImportResidueCheck

    dag = _dag_with_report(["pkg/a.py: .b"], internal_import_count=2)

    result = UnresolvedImportResidueCheck(dag, ".", {}).run()

    assert result_status.result_severity(result) == "amber"
    assert result_status.result_has_findings(result) is True
    assert result_status.pass_is_warn(result) is True
    assert is_vacuous_pass(result) is False


# --- (b) all resolved → PASS -------------------------------------------------


def test_pass_when_residue_empty_but_internal_imports_examined():
    from codd.dag.checks.unresolved_import_residue import UnresolvedImportResidueCheck

    dag = _dag_with_report([], internal_import_count=2)

    result = UnresolvedImportResidueCheck(dag, ".", {}).run()

    assert result.status == "pass"
    assert result.passed is True
    assert result.skipped is False
    assert result.findings == []
    assert result.checked_count == 2  # real work was done
    assert result.block_deploy is False
    assert is_vacuous_pass(result) is False


# --- (c) no internal-looking import → SKIP (not vacuous PASS) ----------------


def test_skip_when_no_internal_looking_import():
    from codd.dag.checks.unresolved_import_residue import UnresolvedImportResidueCheck

    dag = _dag_with_report([], internal_import_count=0)

    result = UnresolvedImportResidueCheck(dag, ".", {}).run()

    assert result.status == "skip"
    assert result.skipped is True
    assert result.checked_count == 0
    assert result.findings == []
    assert result.passed is True
    assert is_vacuous_pass(result) is False


def test_skip_when_dag_never_ran_import_edge_pass():
    """A DAG with no residue measurement examined nothing → SKIP, not PASS."""
    from codd.dag.checks.unresolved_import_residue import UnresolvedImportResidueCheck

    dag = DAG()  # no import_residue_report attribute attached

    result = UnresolvedImportResidueCheck(dag, ".", {}).run()

    assert result.status == "skip"
    assert result.skipped is True
    assert result.checked_count == 0
    assert is_vacuous_pass(result) is False


# --- (d) builder end-to-end: measurement attached, denominator counts resolved


def test_build_dag_attaches_residue_and_counts_resolved_internal_imports(tmp_path):
    """Real build: one resolved + one unresolved relative import.

    Proves (1) the builder attaches the measurement, (2) the denominator counts
    a RESOLVED internal-looking import too (2 examined), and (3) the residue is
    exactly the unresolved one — the same list the legacy advisory warns about.
    """
    _write(tmp_path / "pyproject.toml", "[project]\nname='x'\n")
    _write(
        tmp_path / "codd" / "codd.yaml",
        yaml.safe_dump(
            {
                "scan": {"source_dirs": ["."], "doc_dirs": []},
                "required_artifacts": {"project_type": "generic"},
            },
            sort_keys=False,
        ),
    )
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "helper.py", "def h():\n    return 1\n")
    _write(
        tmp_path / "pkg" / "mod.py",
        "from .helper import h\nfrom .missing import gone\n\ndef f():\n    return h() + gone()\n",
    )

    from codd.dag.builder import build_dag
    from codd.dag.checks.unresolved_import_residue import UnresolvedImportResidueCheck

    dag = build_dag(tmp_path)

    report = dag.import_residue_report
    assert report.residue == ["pkg/mod.py: .missing"]
    assert report.internal_import_count == 2  # BOTH .helper (resolved) and .missing

    result = UnresolvedImportResidueCheck(dag, tmp_path, {}).run()
    assert result.status == "warn"
    assert result.severity == "amber"
    assert result.checked_count == 2
    assert result.findings == ["pkg/mod.py: .missing"]
    assert "missing" in result.message


def test_build_dag_pass_when_all_internal_imports_resolve(tmp_path):
    """Real build with only a resolvable relative import → check PASS (not SKIP)."""
    _write(tmp_path / "pyproject.toml", "[project]\nname='x'\n")
    _write(
        tmp_path / "codd" / "codd.yaml",
        yaml.safe_dump(
            {
                "scan": {"source_dirs": ["."], "doc_dirs": []},
                "required_artifacts": {"project_type": "generic"},
            },
            sort_keys=False,
        ),
    )
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "helper.py", "def h():\n    return 1\n")
    _write(tmp_path / "pkg" / "mod.py", "from .helper import h\n\ndef f():\n    return h()\n")

    from codd.dag.builder import build_dag
    from codd.dag.checks.unresolved_import_residue import UnresolvedImportResidueCheck

    dag = build_dag(tmp_path)
    assert dag.import_residue_report.residue == []
    assert dag.import_residue_report.internal_import_count == 1

    result = UnresolvedImportResidueCheck(dag, tmp_path, {}).run()
    assert result.status == "pass"
    assert result.checked_count == 1
    assert is_vacuous_pass(result) is False


# --- (e) language-agnostic ---------------------------------------------------


def test_is_internal_looking_specifier_is_language_agnostic():
    """No ``language ==``: the predicate is purely specifier-SHAPE driven."""
    from codd.dag.builder import _is_internal_looking_specifier as is_internal

    aliases = {"@app": ["src"]}

    internal = [".b", "..pkg.x", ".", "..", "./x", "../y", "@app/widget", "quote:foo.h"]
    for spec in internal:
        assert is_internal(spec, aliases) is True, spec

    # External / ambiguous shapes are deliberately NOT flagged (flagging stdlib /
    # third-party / FQN specifiers would drown the residue signal). C++ angle
    # includes are system/STL = external by construction.
    external = ["os", "lodash", "java.util.List", "org.junit.Assert", "com.example.App", "angle:vector", ""]
    for spec in external:
        assert is_internal(spec, aliases) is False, spec


def test_check_echoes_residue_regardless_of_language_flavor():
    """The CHECK has zero language logic: it reports residue of any flavor."""
    from codd.dag.checks.unresolved_import_residue import UnresolvedImportResidueCheck

    residue = ["a.py: .b", "b.ts: ./x", "c.cc: quote:foo.h"]
    dag = _dag_with_report(residue, internal_import_count=5)

    result = UnresolvedImportResidueCheck(dag, ".", {}).run()

    assert result.status == "warn"
    assert result.findings == sorted(residue)
    assert result.checked_count == 5


def test_build_dag_language_agnostic_residue_for_js(tmp_path):
    """Real JS build: unresolved ``./missing.js`` relative import → amber residue.

    The whole path (builder residue capture + check) works for JS exactly as for
    Python, proving no single-language literal anywhere in the wiring.
    """
    _write(tmp_path / "package.json", '{"name":"x"}')
    _write(
        tmp_path / "codd" / "codd.yaml",
        yaml.safe_dump(
            {
                "scan": {"source_dirs": ["src"], "doc_dirs": []},
                "required_artifacts": {"project_type": "generic"},
            },
            sort_keys=False,
        ),
    )
    _write(tmp_path / "src" / "helper.js", "export const h = 1;\n")
    _write(
        tmp_path / "src" / "app.js",
        "import {h} from './helper.js';\nimport {g} from './missing.js';\n",
    )

    from codd.dag.builder import build_dag
    from codd.dag.checks.unresolved_import_residue import UnresolvedImportResidueCheck

    dag = build_dag(tmp_path)
    report = dag.import_residue_report
    assert report.internal_import_count == 2  # ./helper.js (resolved) + ./missing.js
    assert any("missing" in entry for entry in report.residue)

    result = UnresolvedImportResidueCheck(dag, tmp_path, {}).run()
    assert result.status == "warn"
    assert result.severity == "amber"
    assert any("missing" in finding for finding in result.findings)


# --- backward compatibility: the builder advisory still warns ----------------


def test_warn_unresolved_residue_still_warns():
    """The legacy advisory is byte-identical (residue input unchanged)."""
    from codd.dag.builder import _warn_unresolved_residue

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _warn_unresolved_residue(["pkg/mod.py: .missing"])

    messages = [str(w.message) for w in caught]
    assert any("unresolved residue" in m for m in messages)
    assert any(".missing" in m for m in messages)


def test_warn_unresolved_residue_silent_when_empty():
    from codd.dag.builder import _warn_unresolved_residue

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _warn_unresolved_residue([])

    assert not [w for w in caught if "unresolved residue" in str(w.message)]
