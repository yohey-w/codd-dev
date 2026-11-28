"""Regression cases from the third independent feedback review."""

import shlex
import sys
import textwrap

import pytest

from .test_implement_test_feedback import APP, FIXED, TEST_FILE, TESTS, feedback, project_factory


def _run(pipeline, root):
    result = pipeline.run(root)
    assert result.status == "success", result.error
    [record] = feedback(root)["rounds"]
    return record


def test_runner_report_does_not_reject_fix_and_is_restored(project_factory):
    command = shlex.join((sys.executable, "-m", "pytest", "checks", "-q", "--junitxml=report.xml"))
    pipeline, root, _, _ = project_factory(command=command)
    report = root / "report.xml"
    original = b"<previous-report/>\r\n"
    report.write_bytes(original)

    def verify(project_root, **kwargs):
        assert report.read_bytes() == original
        return "verified"

    pipeline.verify_runner = verify
    [attempt] = _run(pipeline, root)["attempts"]
    assert attempt["decision"] == "accepted"
    assert attempt["suite_after"] == attempt["isolated_after"] == 0
    assert "[7][0]" in (root / TEST_FILE).read_text()
    assert report.read_bytes() == original


def test_remaining_case_is_isolated_after_own_test_fix(project_factory):
    other = "checks/test_zdependent.py"
    dependent = textwrap.dedent("""\
        from checks import test_subject

        def test_dependent(request):
            assert test_subject.FIX_ENABLED and len(request.session.items) == 1
        """)
    pipeline, root, trace, _ = project_factory(
        initial={"case": {TEST_FILE: "FIX_ENABLED = False\n" + TESTS}, "dependent": {other: dependent}},
        owned={"case": (TEST_FILE,), "dependent": (other,)},
        fixes={"case": {"files": {TEST_FILE: "FIX_ENABLED = True\n" + FIXED}}},
    )
    record = _run(pipeline, root)
    [attempt] = record["attempts"]
    assert (attempt["owner"], attempt["decision"]) == ("case", "accepted")
    assert attempt["suite_after"] == 1
    assert record["passed_in_isolation"] == 1
    timeline = trace.read_text().splitlines()
    isolate = f"isolate {other}::test_dependent"
    assert timeline.count(isolate) == 1
    assert timeline.index("feedback case") < timeline.index(isolate)
    assert "feedback dependent" not in timeline


@pytest.mark.parametrize("other_origin", ["different-owner", "application", "same-owner"])
def test_exception_group_requires_one_owner_across_branches(project_factory, other_origin):
    left = "checks/support/left.py"
    right = APP if other_origin == "application" else "checks/support/right.py"
    right_import = "from stub_app import subject as right" if right == APP else "from checks.support import right"
    tests = "from checks.support import left\n" + right_import + "\n" + textwrap.dedent("""\
        try:
            ExceptionGroup
        except NameError:
            class ExceptionGroup(Exception):
                def __init__(self, message, exceptions):
                    super().__init__(message)
                    self.exceptions = tuple(exceptions)

        def test_subject():
            errors = []
            for step in (left.run, right.run):
                try:
                    step()
                except ValueError as exc:
                    errors.append(exc)
            if errors:
                raise ExceptionGroup("operations failed", [errors[0], ExceptionGroup("nested", errors[1:])])
            assert not errors
        """)
    left_code = "def run():\n    raise ValueError('left failed')\n"
    right_code = "def run():\n    raise ValueError('right failed')\n"
    initial = {"case": {TEST_FILE: tests}, "left": {left: left_code}}
    right_owner = "left" if other_origin == "same-owner" else "right"
    initial.setdefault(right_owner, {})[right] = right_code
    pipeline, root, trace, _ = project_factory(
        initial=initial, owned={owner: tuple(files) for owner, files in initial.items()}, fixes={},
    )
    record = _run(pipeline, root)
    assert record["runs"][0]["failures_and_errors"] == 1
    if other_origin == "same-owner":
        assert [item["owner"] for item in record["attempts"]] == ["left"]
    else:
        assert record["attempts"] == []
        assert "feedback " not in trace.read_text()


@pytest.mark.parametrize("assertion", [
    "    with expect_error(ValueError):\n        int('invalid')\n",
    "    expect_error(ValueError, int, 'invalid')\n",
])
def test_local_import_cannot_hide_assertion_alias(project_factory, assertion):
    unrelated = textwrap.dedent("""\

        def unrelated():
            from contextlib import nullcontext as expect_error
            return expect_error()
        """)
    initial = "from pytest import raises as expect_error\n\n" + TESTS.replace(
        "    actual = [7][1]", assertion + "    actual = [7][1]",
    ) + unrelated
    pipeline, root, _, _ = project_factory(
        initial={"case": {TEST_FILE: initial}}, fixes={"case": {"files": {TEST_FILE: FIXED + unrelated}}},
    )
    [attempt] = _run(pipeline, root)["attempts"]
    assert attempt["decision"] == "rejected"
    assert "assertions" in attempt["reason"]
    assert assertion in (root / TEST_FILE).read_text()


@pytest.mark.parametrize(("child", "check", "message", "reason"), [
    ("feedback_missing_round3_executable", False, "result.stderr", "test executable unavailable"),
    ("./checks/worker.sh", False, "f'child failed: {result.stderr!r}'", "denied access"),
    ("./checks/worker.sh", True, "result.stderr", "exit status 126"),
])
def test_child_launch_errors_are_classified_before_feedback(project_factory, child, check, message, reason):
    tests = textwrap.dedent(f"""\
        import subprocess

        def test_subject():
            result = subprocess.run(
                ["sh", "-c", {child!r}], check={check!r}, capture_output=True, text=True,
            )
            assert result.returncode == 0, {message}
        """)
    pipeline, root, trace, _ = project_factory(initial={"case": {TEST_FILE: tests}}, fixes={})
    worker = root / "checks/worker.sh"
    worker.write_text("#!/bin/sh\nexit 0\n")
    worker.chmod(0o644)
    record = _run(pipeline, root)
    assert reason in record["skipped"]
    assert record["attempts"] == []
    assert "feedback case" not in trace.read_text()
