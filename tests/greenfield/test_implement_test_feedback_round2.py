"""Regression cases from the second independent feedback review."""

import os
import shlex
import subprocess
import sys
import textwrap

import pytest

from codd import ai_invoke
from codd.config import load_project_config
from codd.repair.verify_runner import VerifyRunner

from .test_implement_test_feedback import APP, FIXED, TEST_FILE, TESTS, feedback, project_factory


def _run(pipeline, root):
    result = pipeline.run(root)
    assert result.status == "success", result.error
    return feedback(root)["rounds"][0]


def test_direct_writer_can_keep_fix_after_version_control_capture(project_factory, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    pipeline, root, trace, _ = project_factory(
        fixes={"case": {"direct": {TEST_FILE: "# codd: covers vb=VB-observe\n" + FIXED}}},
    )
    subprocess.run(["git", "init", "-q", "--template="], cwd=root, check=True)
    rerun = pipeline._reimplement_tasks

    def use_direct_writer(*args, **kwargs):
        with monkeypatch.context() as patch:
            patch.setattr(ai_invoke, "is_file_writing_agent", lambda argv: argv[0] == sys.executable)
            return rerun(*args, **kwargs)

    monkeypatch.setattr(pipeline, "_reimplement_tasks", use_direct_writer)
    [attempt] = _run(pipeline, root)["attempts"]
    assert trace.read_text().splitlines().count("feedback case") == 1
    assert attempt["decision"] == "accepted", attempt
    assert "[7][0]" in (root / TEST_FILE).read_text()
    assert any(path.is_file() for path in (root / ".git/objects").rglob("*"))


@pytest.mark.parametrize("during_candidate", [False, True])
def test_test_execution_discards_changed_environment(project_factory, during_candidate):
    asset = ".venv/runtime.txt"
    source = FIXED if during_candidate else TESTS
    index = 0 if during_candidate else 1
    writes_environment = "from pathlib import Path\n\n" + source.replace(
        f"    actual = [7][{index}]",
        f"    Path({asset!r}).write_bytes(b'changed')\n    actual = [7][{index}]",
    )
    pipeline, root, _, _ = project_factory(
        initial={"case": {TEST_FILE: TESTS if during_candidate else writes_environment}},
        fixes={"case": {"files": {TEST_FILE: writes_environment}}},
    )
    runtime = root / asset
    runtime.parent.mkdir()
    runtime.write_bytes(b"original\r\n")
    prepare = pipeline._provision_env
    seen = []

    def provision(project_root):
        seen.append(runtime.read_bytes() if runtime.exists() else None)
        if not runtime.exists():
            runtime.parent.mkdir()
            runtime.write_bytes(b"ready")
        prepare(project_root)

    def verify(project_root, **kwargs):
        assert runtime.read_bytes() == b"ready"
        return "verified"

    pipeline._provision_env = provision
    pipeline.verify_runner = verify
    record = _run(pipeline, root)
    if during_candidate:
        [attempt] = record["attempts"]
        assert attempt["decision"] == "rejected"
        assert "test environment changed" in attempt["reason"]
        assert seen == [b"original\r\n", None, b"ready"]
    else:
        assert record["attempts"] == []
        assert "test environment changed" in record["skipped"]
        assert seen == [b"original\r\n", None]
    assert "[7][1]" in (root / TEST_FILE).read_text()


def test_reprovision_precedes_isolation_for_next_owner(project_factory):
    second = "checks/test_zsecond.py"
    asset = ".venv/runtime.txt"
    second_case = textwrap.dedent(f"""\
        from pathlib import Path

        def test_second(request):
            actual = Path({asset!r}).read_text() if len(request.session.items) == 1 else 'broken'
            assert actual == 'ready'
        """)
    pipeline, root, trace, _ = project_factory(
        initial={"case": {TEST_FILE: TESTS}, "second": {second: second_case}},
        fixes={
            "case": {"files": {TEST_FILE: FIXED}, "direct": {asset: "candidate"}},
            "second": {"files": {second: "# Still depends on collection.\n" + second_case}},
        },
        owned={"case": (TEST_FILE,), "second": (second,)},
    )
    provision = pipeline._provision_env
    values = iter(("broken", "ready", "ready"))

    def prepare(project_root):
        runtime = project_root / asset
        runtime.parent.mkdir(exist_ok=True)
        runtime.write_text(next(values), encoding="utf-8")
        provision(project_root)

    pipeline._provision_env = prepare
    record = _run(pipeline, root)
    attempt, unstable = record["attempts"]
    assert attempt["owner"] == "case" and attempt["decision"] == "rejected"
    assert "test environment changed" in attempt["reason"]
    lines = trace.read_text().splitlines()
    case = f"isolate {second}::test_second"
    assert lines.count(case) == 1
    assert lines.index("feedback case") < lines.index(case) < lines.index("feedback second")
    assert [run["purpose"] for run in record["runs"][-3:]] == ["suite after reprovisioning", "isolate", "candidate suite"]
    assert (unstable["owner"], unstable["decision"]) == ("second", "rejected")
    assert unstable["reason"] == f"unstable test still fails: {second}::test_second"
    assert record["passed_in_isolation"] == 1


def test_candidate_cannot_pass_using_an_earlier_runs_side_effect(project_factory):
    initial = "from pathlib import Path\n\n" + TESTS.replace(
        "    actual = [7][1]", "    Path('settings.txt').write_text('dirty')\n    actual = [7][1]",
    )
    candidate = "from pathlib import Path\n\n" + FIXED.replace(
        "    assert actual == 7", "    assert actual == 7 and Path('settings.txt').read_text() == 'dirty'",
    )
    pipeline, root, _, _ = project_factory(
        initial={"case": {TEST_FILE: initial}},
        fixes={"case": {"files": {TEST_FILE: candidate}}},
    )
    settings_file = root / "settings.txt"
    settings_file.write_bytes(b"original\r\n")
    verify = pipeline.verify_runner

    def observe(project_root, **kwargs):
        assert settings_file.read_bytes() == b"original\r\n"
        return verify(project_root, **kwargs)

    pipeline.verify_runner = observe
    [attempt] = _run(pipeline, root)["attempts"]
    assert attempt["decision"] == "rejected"
    assert attempt["after"] == 1
    assert "[7][1]" in (root / TEST_FILE).read_text()


@pytest.mark.parametrize("prefix", ["cd . && PYTHONPATH=src", "env PYTHONPATH=src"])
def test_unobservable_command_reports_reason_and_leaves_verification_working(project_factory, prefix):
    command = f"{prefix} {shlex.quote(sys.executable)} -m pytest checks -q"
    pipeline, root, trace, stdout = project_factory(command=command)
    record = _run(pipeline, root)
    assert record["attempts"] == []
    reason = "No module named '_pytest_feedback_probe'"
    assert reason in record["skipped"]
    assert any("skipped" in line and "did not produce a case-level report" in line
               and reason in line for line in stdout)
    assert "feedback case" not in trace.read_text()
    settings = load_project_config(root)
    verification = VerifyRunner(root, settings)._run_test_command(settings)
    assert "1 failed, 1 passed" in verification[3].message


@pytest.mark.parametrize(("child", "reason"), [
    (["sh", "-c", "feedback_missing_test_executable"], "test executable unavailable"),
    ([sys.executable, "-c", "from json import feedback_missing_export"], "external import"),
])
def test_missing_child_prerequisite_is_not_sent(project_factory, child, reason):
    case = textwrap.dedent(f"""\
        import subprocess

        def test_subject():
            result = subprocess.run({child!r}, check=True, capture_output=True, text=True)
            assert result.returncode == 0
        """)
    pipeline, root, trace, stdout = project_factory(initial={"case": {TEST_FILE: case}}, fixes={})
    record = _run(pipeline, root)
    assert record["attempts"] == []
    assert reason in record["skipped"]
    assert any("skipped" in line and reason in line for line in stdout)
    assert "feedback case" not in trace.read_text()


def test_application_exception_nested_in_group_is_not_sent(project_factory):
    tests = textwrap.dedent("""\
        from stub_app import subject

        try:
            ExceptionGroup
        except NameError:
            class ExceptionGroup(Exception):
                def __init__(self, message, exceptions):
                    super().__init__(message)
                    self.exceptions = tuple(exceptions)

        def test_subject():
            errors = []
            value = None
            try:
                value = subject.value()
            except ValueError as exc:
                errors.append(exc)
            if errors:
                raise ExceptionGroup('parallel operations failed', [ExceptionGroup('nested', errors)])
            assert value == 7
        """)
    pipeline, root, trace, _ = project_factory(
        initial={"case": {TEST_FILE: tests}, "app": {APP: "def value():\n    raise ValueError('in application')\n"}},
        owned={"case": (TEST_FILE,), "app": (APP,)},
        fixes={},
    )
    record = _run(pipeline, root)
    assert record["runs"][0]["failures_and_errors"] == 1
    assert record["attempts"] == []
    assert "feedback " not in trace.read_text()


@pytest.mark.parametrize(("import_line", "context"), [
    ("from pytest import raises as must_raise", "must_raise"),
    ("import pytest as check", "check.raises"),
])
def test_removing_aliased_context_assertion_rejects_candidate(project_factory, import_line, context):
    original = import_line + "\n\n" + TESTS.replace(
        "    actual = [7][1]",
        f"    with {context}(ValueError):\n        int('not an integer')\n    actual = [7][1]",
    )
    pipeline, root, _, _ = project_factory(
        initial={"case": {TEST_FILE: original}},
        fixes={"case": {"files": {TEST_FILE: FIXED}}},
    )
    [attempt] = _run(pipeline, root)["attempts"]
    assert attempt["decision"] == "rejected"
    assert "assertions" in attempt["reason"]
    assert f"with {context}(ValueError)" in (root / TEST_FILE).read_text()


@pytest.mark.parametrize("missing_dependency", [False, True])
def test_other_files_collection_error_does_not_suppress_runnable_case(project_factory, missing_dependency):
    other_file = "checks/test_broken_import.py"
    command = shlex.join((sys.executable, "-m", "pytest", "--continue-on-collection-errors", "checks", "-q"))
    broken_import = (
        "import feedback_missing_collection_dependency" if missing_dependency
        else "raise RuntimeError('local test file has a defect')"
    )
    other = (
        "import sys\n"
        "if '--collect-only' not in sys.argv:\n"
        f"    {broken_import}\n"
        "def test_other():\n"
        "    assert True\n"
    )
    pipeline, root, trace, stdout = project_factory(
        command=command,
        initial={"case": {TEST_FILE: TESTS}, "other": {other_file: other}},
        owned={"case": (TEST_FILE,), "other": (other_file,)},
    )
    record = _run(pipeline, root)
    first = record["runs"][0]
    assert first["executed_cases"] == 2 and first["failures_and_errors"] == 1
    assert list(first["collection_errors"]) == [other_file]
    reason = "external import" if missing_dependency else "local test file has a defect"
    assert reason in first["collection_errors"][other_file]
    assert any(f"collection failed in {other_file}" in line for line in stdout)
    assert "skipped" not in record
    [attempt] = record["attempts"]
    assert (attempt["owner"], attempt["decision"]) == ("case", "accepted")
    assert "feedback other" not in trace.read_text()
