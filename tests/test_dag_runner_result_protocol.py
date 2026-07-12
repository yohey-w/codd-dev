"""Red-before-green: the DAG check result protocol must never treat a non-pass
result as a clean PASS.

Every verify / coverage / deploy summary consumes the shared runner
(``codd/dag/runner.py``) and the shared status predicates
(``codd/dag/result_status.py``). A check that

* returns ``None`` (produced no result),
* returns a result without an explicit ``passed=True``,
* raises inside ``run()`` (including an internal ``TypeError``), or
* cannot build its DAG,

must surface as RED on the exact same result path the gate reads — not be
silently re-interpreted as a legacy call form and re-run into a green PASS.

These tests exercise the real ``run_checks`` / ``run_all_checks`` entry points
and the real predicate the gate filters use, so a green here means the gate
cannot false-green on a malfunctioning check.
"""

from __future__ import annotations

import pytest

from codd.dag import checks as dag_checks
from codd.dag.checks import CheckResult, DagCheck
from codd.dag.result_status import result_passed
from codd.dag.runner import run_checks


class _DummyDAG:
    """Minimal stand-in DAG; the fake checks below never inspect it."""

    nodes: dict = {}
    edges: list = []


@pytest.fixture
def register_check():
    """Register throwaway checks into the real registry and clean them up."""

    added: list[str] = []

    def _register(name: str, cls: type) -> type:
        dag_checks._REGISTRY[name] = cls
        added.append(name)
        return cls

    yield _register

    for name in added:
        dag_checks._REGISTRY.pop(name, None)


def test_result_passed_is_green_only_on_explicit_true():
    """``result_passed`` must be True ONLY for an explicit ``passed=True``.

    A result missing the field, or carrying ``passed=None``, verified nothing
    reliable and must not read as a clean pass (``None is not False`` was the
    false-green).
    """
    assert result_passed({"passed": True}) is True
    assert result_passed({"passed": False}) is False
    assert result_passed({"passed": None}) is False
    assert result_passed({"severity": "red", "status": "fail"}) is False
    # A real check result still reads correctly (skip/pass keep passing).
    assert result_passed(CheckResult(status="pass")) is True
    assert result_passed(CheckResult(status="skip")) is True
    assert result_passed(CheckResult(status="fail", passed=False)) is False


def test_check_returning_none_is_not_a_pass(tmp_path, register_check):
    """A check that returns ``None`` must red on the gate's result path."""

    class ReturnsNone(DagCheck):
        def run(self, dag=None, project_root=None, settings=None, codd_config=None):
            return None

    register_check("returns_none_check", ReturnsNone)
    results = run_checks(_DummyDAG(), tmp_path, {}, check_names=["returns_none_check"])

    assert len(results) == 1
    result = results[0]
    assert result_passed(result) is False
    assert str(getattr(result, "severity", "red")) == "red"


def test_check_internal_typeerror_is_not_swallowed(tmp_path, register_check):
    """An internal ``TypeError`` must red — not be mistaken for a legacy call
    form and re-run into a green PASS, and the check must run exactly once."""

    calls: list = []

    class InternalTypeError(DagCheck):
        def run(self, dag=None, project_root=None, settings=None, codd_config=None):
            calls.append(codd_config)
            if codd_config is not None:
                raise TypeError("internal bug: 'NoneType' object is not subscriptable")
            return CheckResult(check_name="internal_te", status="pass", passed=True)

    register_check("internal_te_check", InternalTypeError)
    results = run_checks(
        _DummyDAG(),
        tmp_path,
        {},
        check_names=["internal_te_check"],
        codd_config={"ci": {}},
    )

    assert len(results) == 1
    assert result_passed(results[0]) is False
    assert len(calls) == 1  # no double-run side effects


def test_check_raising_value_error_becomes_red(tmp_path, register_check):
    """Any check-internal exception (not just TypeError) must convert to red."""

    class RaisesValueError(DagCheck):
        def run(self, dag=None, project_root=None, settings=None, codd_config=None):
            raise ValueError("boom inside the check")

    register_check("raises_ve_check", RaisesValueError)
    results = run_checks(_DummyDAG(), tmp_path, {}, check_names=["raises_ve_check"])

    assert len(results) == 1
    assert result_passed(results[0]) is False


def test_build_dag_failure_becomes_red_result(tmp_path, monkeypatch):
    """A ``build_dag`` failure must surface as a red result on the same result
    path, not propagate as an uncaught crash out of ``run_all_checks``."""

    import codd.dag.runner as runner_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("dag build exploded")

    monkeypatch.setattr(runner_mod, "build_dag", _boom)

    results = runner_mod.run_all_checks(tmp_path)

    assert len(results) == 1
    assert result_passed(results[0]) is False
    assert str(getattr(results[0], "check_name", "")) == "dag_build"
