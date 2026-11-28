"""Regressions for owner scheduling and candidate assessment."""

import textwrap

import pytest

from .test_implement_test_feedback import HELPER, TEST_FILE, TESTS, feedback, project_factory


def _run(pipeline, root):
    result = pipeline.run(root)
    assert result.status == "success", result.error
    return feedback(root)["rounds"]


@pytest.mark.parametrize("case_count", [1, 20])
def test_unreproduced_owner_is_rechecked_after_acceptance(project_factory, case_count):
    other = "checks/test_zother.py"
    first = textwrap.dedent(f"""\
        import pytest
        from checks import test_zother

        @pytest.mark.parametrize("index", range({case_count}))
        def test_a(request, index):
            if len(request.session.items) == 1 and (not test_zother.FIXED or index != 0):
                request.addfinalizer(lambda: pytest.fail("isolation failed in teardown"))
                return
            actual = [7][1]
            assert actual == 7
        """)
    second = textwrap.dedent("""\
        FIXED = False

        def test_b():
            actual = [8][1]
            assert actual == 8
        """)
    pipeline, root, trace, _ = project_factory(
        initial={"a": {TEST_FILE: first}, "b": {other: second}},
        owned={"a": (TEST_FILE,), "b": (other,)},
        fixes={
            "a": {"files": {TEST_FILE: first.replace("[7][1]", "[7][0]")}},
            "b": {"files": {other: second.replace("False", "True").replace("[8][1]", "[8][0]")}},
        },
    )

    [record] = _run(pipeline, root)

    assert [(a["owner"], a["decision"]) for a in record["attempts"]] == [
        ("b", "accepted"), ("a", "accepted"),
    ]
    assert record["passed_in_isolation"] == 0
    events = trace.read_text().splitlines()
    isolated = f"isolate {TEST_FILE}::test_a[0]"
    before = events.index("feedback b")
    assert events[:before].count(isolated) == 1
    assert events[before:events.index("feedback a")].count(isolated) == 1


def test_unreproduced_owner_is_rechecked_after_reprovisioning(project_factory):
    other = "checks/test_zother.py"
    asset = ".venv/runtime.txt"
    first = textwrap.dedent(f"""\
        from pathlib import Path

        import pytest

        def test_a(request):
            if len(request.session.items) == 1 and Path({asset!r}).read_text() == "initial":
                request.addfinalizer(lambda: pytest.fail("isolation failed in teardown"))
                return
            actual = [7][1]
            assert actual == 7
        """)
    pipeline, root, trace, _ = project_factory(
        initial={"a": {TEST_FILE: first}, "b": {other: TESTS}},
        owned={"a": (TEST_FILE,), "b": (other,)},
        fixes={
            "a": {"files": {TEST_FILE: first.replace("[7][1]", "[7][0]")}},
            "b": {"direct": {asset: "modified during feedback"}},
        },
    )
    provision = pipeline._provision_env

    def prepare(project_root):
        first_provision = "provision" not in trace.read_text().splitlines()
        runtime = project_root / asset
        runtime.parent.mkdir(exist_ok=True)
        runtime.write_text("initial" if first_provision else "reprovisioned")
        provision(project_root)

    pipeline._provision_env = prepare
    [record] = _run(pipeline, root)

    assert [(a["owner"], a["decision"]) for a in record["attempts"]] == [
        ("b", "rejected"), ("a", "accepted"),
    ]
    assert record["passed_in_isolation"] == 0
    assert "discarding and reprovisioning" in record["attempts"][0]["reason"]
    assert any(run["purpose"] == "suite after reprovisioning" for run in record["runs"])


def test_reproduced_failure_is_returned_before_ending_timed_out_round(project_factory):
    later = "checks/test_zother.py"
    fast_and_slow = textwrap.dedent("""\
        import os
        import time

        def test_fast():
            actual = [7][1]
            assert actual == 7

        def test_slow():
            if os.environ.get("CODD_TEST_FEEDBACK_CASE") and [8, 0][1] != 8:
                time.sleep(10)
            actual = [8][1]
            assert actual == 8
        """)
    fixed = fast_and_slow.replace("[7][1]", "[7][0]").replace("[8, 0][1]", "[8, 0][0]").replace("[8][1]", "[8][0]")
    pipeline, root, trace, _ = project_factory(
        rounds=2, timeout=4,
        initial={"a": {TEST_FILE: fast_and_slow}, "b": {later: TESTS}},
        owned={"a": (TEST_FILE,), "b": (later,)},
        fixes={
            "a": {"files": {TEST_FILE: fixed}},
            "b": {"files": {later: TESTS.replace("[7][1]", "[7][0]")}},
        },
    )

    rounds = _run(pipeline, root)

    assert [(a["owner"], a["decision"], a["isolated_before"]) for a in rounds[0]["attempts"]] == [
        ("a", "accepted", 1),
    ]
    first, second = rounds
    assert first["isolation_time_exhausted"] and not first["stop"]
    isolates = [run for run in first["runs"] if run["purpose"] == "isolate"]
    assert [run["case"] for run in isolates] == [f"{TEST_FILE}::test_fast", f"{TEST_FILE}::test_slow"]
    assert isolates[0]["failures_and_errors"] == 1
    assert "exceeded" in isolates[1]["unavailable"]
    prompt = trace.with_name("prompt-a").read_text()
    assert "test_fast (call" in prompt
    assert "test_slow (call" not in prompt
    assert [(a["owner"], a["decision"]) for a in second["attempts"]] == [("b", "accepted")]


def test_candidate_isolation_counts_only_the_returned_owner(project_factory):
    case = textwrap.dedent("""\
        from checks.support.helpers import cleanup

        def test_shared(cleanup):
            actual = [7][1]
            assert actual == 7
        """)
    helper = textwrap.dedent("""\
        import pytest

        @pytest.fixture
        def cleanup():
            yield
            actual = [8][1]
            assert actual == 8
        """)
    pipeline, root, trace, _ = project_factory(
        initial={"case": {TEST_FILE: case}, "helper": {HELPER: helper}},
        owned={"case": (TEST_FILE,), "helper": (HELPER,)},
        fixes={
            "case": {"files": {TEST_FILE: case.replace("[7][1]", "[7][0]")}},
            "helper": {"files": {HELPER: helper.replace("[8][1]", "[8][0]")}},
        },
    )

    [record] = _run(pipeline, root)

    assert [(a["owner"], a["decision"], a["isolated_before"], a["isolated_after"])
            for a in record["attempts"]] == [
        ("helper", "accepted", 1, 0),
        ("case", "accepted", 1, 0),
    ]
    helper_attempt, case_attempt = record["attempts"]
    assert (helper_attempt["suite_before"], helper_attempt["suite_after"]) == (2, 1)
    assert (case_attempt["suite_before"], case_attempt["suite_after"]) == (1, 0)
    helper_prompt = trace.with_name("prompt-helper").read_text()
    case_prompt = trace.with_name("prompt-case").read_text()
    assert "test_shared (teardown" in helper_prompt and "test_shared (call" not in helper_prompt
    assert "test_shared (call" in case_prompt and "test_shared (teardown" not in case_prompt
