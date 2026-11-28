"""Exercise owner scheduling with a scripted pipeline and fixture tests."""

import pytest

from codd.greenfield import implement_test_feedback as feedback_engine

from .test_implement_test_feedback import FIXED, HELPER, TEST_FILE, TESTS, feedback, project_factory


def _failing_tests(count):
    return "\n".join(
        f"def test_case_{index:02d}():\n    actual = [7][1]\n    assert actual == 7\n"
        for index in range(count)
    )


def _run(pipeline, root, **kwargs):
    result = pipeline.run(root, **kwargs)
    assert result.status == "success", result.error
    return feedback(root)["rounds"]


def test_accepted_helper_leaves_room_for_other_owners_in_the_same_round(project_factory):
    other = "checks/test_zother.py"
    tests = (
        "from checks.support import helpers\n\n" + _failing_tests(19)
        + "\ndef test_helper():\n    assert helpers.value() == 7\n"
        + "\ndef test_tail():\n    actual = [7][1]\n    assert actual == 7\n"
    )
    pipeline, root, _, _ = project_factory(
        rounds=2,
        initial={
            "case": {TEST_FILE: tests},
            "helper": {HELPER: "def value():\n    return [7][1]\n"},
            "other": {other: _failing_tests(5)},
        },
        owned={"case": (TEST_FILE,), "other": (other,), "helper": (HELPER,)},
        fixes={
            "helper": {"files": {HELPER: "def value():\n    return 7\n"}},
            "case": {"files": {TEST_FILE: tests.replace("[7][1]", "[7][0]")}},
            "other": {"files": {other: _failing_tests(5).replace("[7][1]", "[7][0]")}},
        },
    )
    [record] = _run(pipeline, root)
    assert record["runs"][0]["failures_and_errors"] == 26
    assert [(attempt["owner"], attempt["decision"]) for attempt in record["attempts"]] == [
        ("helper", "accepted"), ("case", "accepted"), ("other", "accepted"),
    ]
    assert record["stop"]


def test_owner_beyond_first_twenty_suite_failures_is_returned_in_round_one(project_factory):
    other = "checks/test_zlate.py"
    pipeline, root, _, _ = project_factory(
        initial={"z_early": {TEST_FILE: _failing_tests(21)}, "a_late": {other: _failing_tests(2)}},
        owned={"z_early": (TEST_FILE,), "a_late": (other,)},
        fixes={},
    )
    [record] = _run(pipeline, root)
    assert record["runs"][0]["failures_and_errors"] == 23
    assert [attempt["owner"] for attempt in record["attempts"]] == ["a_late", "z_early"]
    isolated = [run["case"] for run in record["runs"] if run["purpose"] == "isolate"]
    assert isolated[:2] == [f"{other}::test_case_{index:02d}" for index in range(2)]
    assert isolated[2:] == [f"{TEST_FILE}::test_case_{index:02d}" for index in range(20)]
    assert record["over_isolation_budget"] == 1


def test_exhausted_isolation_time_allows_next_round(project_factory, monkeypatch):
    pipeline, root, _, _ = project_factory(rounds=2)
    run_tests = feedback_engine._Feedback.run_tests

    def exhaust_first_round(self, record, purpose, case=None, timeout=None, **kwargs):
        if record["number"] == 1 and purpose == "isolate":
            record["isolated_seconds"] += timeout
            return feedback_engine._Suite(unavailable="fixture timeout", timed_out=True)
        return run_tests(self, record, purpose, case, timeout, **kwargs)

    monkeypatch.setattr(feedback_engine._Feedback, "run_tests", exhaust_first_round)
    rounds = _run(pipeline, root)
    assert len(rounds) == 2
    first, second = rounds
    assert first["isolation_time_exhausted"] and not first["stop"]
    assert first["attempts"] == []
    assert second["runs"][0]["purpose"] == "round 2 suite"
    assert [(attempt["owner"], attempt["decision"]) for attempt in second["attempts"]] == [
        ("case", "accepted"),
    ]


@pytest.mark.parametrize("failing", [False, True])
def test_rounds_stop_when_the_suite_has_no_uniquely_owned_failure(project_factory, failing):
    pipeline, root, _, _ = project_factory(
        rounds=3,
        initial={"case": {TEST_FILE: TESTS if failing else FIXED}},
        owned={"case": (TEST_FILE,), "overlap": ("checks/",)},
    )
    [record] = _run(pipeline, root)
    assert record["runs"][0]["failures_and_errors"] == int(failing)
    assert record["stop"] and not record["attempts"]
    assert [run["purpose"] for run in record["runs"]] == ["round 1 suite"]


def test_resumed_round_returns_only_to_owners_not_already_attempted(project_factory, monkeypatch):
    paths = {"a": TEST_FILE, "b": "checks/test_b.py", "c": "checks/test_c.py"}
    pipeline, root, trace, _ = project_factory(
        initial={owner: {path: TESTS} for owner, path in paths.items()},
        owned={owner: (path,) for owner, path in paths.items()},
        fixes={owner: {"files": {path: FIXED}} for owner, path in paths.items()},
    )
    rerun = pipeline._reimplement_tasks
    original = None

    def interrupt(project_root, tasks, *args, **kwargs):
        nonlocal original
        if tasks[0].task_id != "b":
            return rerun(project_root, tasks, *args, **kwargs)
        original = (root / paths["b"]).read_bytes()
        rerun(project_root, tasks, *args, **kwargs)
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(pipeline, "_reimplement_tasks", interrupt)
        with pytest.raises(KeyboardInterrupt):
            pipeline.run(root)

    [record] = _run(pipeline, root, resume=True)
    assert [(attempt["owner"], attempt["decision"]) for attempt in record["attempts"]] == [
        ("a", "accepted"), ("b", "rejected"), ("c", "accepted"),
    ]
    assert record["attempts"][1]["interrupted"]
    assert [line for line in trace.read_text().splitlines() if line.startswith("feedback ")] == [
        "feedback a", "feedback b", "feedback c",
    ]
    assert "[7][0]" in (root / paths["a"]).read_text()
    assert (root / paths["b"]).read_bytes() == original
