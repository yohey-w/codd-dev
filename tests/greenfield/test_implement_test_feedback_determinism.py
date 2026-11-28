"""W21 regression attribution and rejection feedback through a scripted runner."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from codd.greenfield import implement_test_feedback as engine, pipeline as greenfield
from codd.languages.adapters.pytest_feedback import PytestFeedbackAdapter

from .test_implement_test_feedback import DESIGN, FIXED, TEST_FILE, TESTS


REPAIRED_CASE = f"{TEST_FILE}::test_subject"
OBSERVER_FILE = "checks/test_observer.py"
OBSERVER_CASE = f"{OBSERVER_FILE}::test_observer"
NEXT_CASE = f"{OBSERVER_FILE}::test_next"

DRIVER = """\
import json
import os
import sys
from pathlib import Path

trace, test_file, repaired, observer, next_case, isolated = sys.argv[1:]
trace = Path(trace)
isolated = json.loads(isolated)
version = "candidate" if "[7][0]" in Path(test_file).read_text() else "baseline"
selected = os.environ["CODD_TEST_FEEDBACK_CASE"]
runs = json.loads(trace.read_text()) if trace.exists() else []
status = "passed" if version == "candidate" else "failed"
if not selected:
    seen = "failed" if version == "candidate" else "passed"
    cases = {repaired: status, observer: seen, next_case: seen}
elif selected == observer:
    index = runs.count([version, selected])
    outcomes = isolated[version]
    cases = {observer: outcomes[index % len(outcomes)]}
else:
    cases = {selected: status}
runs.append([version, selected])
trace.write_text(json.dumps(runs))
failures = []
for case, result in cases.items():
    if result == "failed":
        path = case.split("::")[0]
        failures.append(dict(case=case, file=path, origin=path, phase="call", detail="fake assertion"))
if selected == observer and version == "candidate" and failures:
    print("isolated output begins" + "x" * 6500 + "omitted middle" + "x" * 11000 + "isolated output ends")
status = int(bool(failures))
report = dict(cases=cases, failures=failures, environment=[], collection_errors={}, exitstatus=status)
(Path(os.environ["CODD_TEST_FEEDBACK_REPORT"]) / "report-fake.json").write_text(json.dumps(report))
sys.exit(status)
"""


@pytest.fixture
def scripted_feedback(tmp_path, monkeypatch):
    def run(*, baseline=("passed",), candidate=("failed",), rounds=1, replacement=FIXED, outside=False):
        root = tmp_path / "project"
        test_file = root / TEST_FILE
        test_file.parent.mkdir(parents=True)
        test_file.write_text(TESTS)
        (root / OBSERVER_FILE).write_text("def test_observer():\n    assert True\n")
        driver, trace = tmp_path / "driver.py", tmp_path / "runs.json"
        driver.write_text(DRIVER)
        command = [
            sys.executable, str(driver), str(trace), TEST_FILE, REPAIRED_CASE, OBSERVER_CASE, NEXT_CASE,
            json.dumps({"baseline": baseline, "candidate": candidate}),
        ]
        monkeypatch.setattr(engine.VerifyRunner, "feedback_test_command", lambda *_: (command, root, {}, 30))
        monkeypatch.setattr(greenfield, "_enforce_stage_coverage_gate", lambda *_, **__: None)
        prompts = []

        def reimplement(project_root, tasks, feedback, config, *, feedback_output_paths):
            assert feedback_output_paths.names == (TEST_FILE,)
            assert test_file.read_text() == TESTS
            prompts.append(feedback)
            test_file.write_text(replacement)
            if outside:
                (root / "undeclared.txt").write_text("outside")

        pipeline = SimpleNamespace(
            _provision_env=lambda _: None,
            _checkpoint=lambda _: None,
            echo=lambda _: None,
            _resolve_layout_profile=lambda _: None,
            _enforce_implement_oracle_gate=lambda *_, **__: None,
            _reimplement_tasks=reimplement,
        )
        task = greenfield.ImplementTaskRef("repair", DESIGN, expected_outputs=(TEST_FILE,))
        state = {"rounds": [], "ignored": [".codd/**"], "environment_paths": []}
        feedback = engine._Feedback(
            pipeline, root, [task], state, {}, {}, PytestFeedbackAdapter(), (Path("checks"),), [], (),
        )
        feedback.run(rounds)
        attempts = [attempt for record in state["rounds"] for attempt in record["attempts"]]
        runs = json.loads(trace.read_text())
        assert not (root / "undeclared.txt").exists()
        return attempts, prompts, runs, test_file.read_text()

    return run


def test_y1_baseline_failure_does_not_reject_candidate(scripted_feedback):
    attempts, _, runs, contents = scripted_feedback(baseline=("passed", "failed"))
    [attempt] = attempts
    assert attempt["decision"] == "accepted"
    assert attempt["baseline_failures"] == [{"case": OBSERVER_CASE, "passed": 1, "failed": 1}]
    assert runs[-5:] == [
        ["candidate", OBSERVER_CASE], ["candidate", OBSERVER_CASE],
        ["baseline", OBSERVER_CASE], ["baseline", OBSERVER_CASE], ["candidate", NEXT_CASE],
    ]
    assert contents == FIXED


def test_y2_repeatable_candidate_regression_is_rejected(scripted_feedback):
    attempts, _, runs, contents = scripted_feedback()
    [attempt] = attempts
    assert attempt["decision"] == "rejected"
    assert attempt["reason"] == f"previously passing test now fails in isolation: {OBSERVER_CASE}"
    assert runs[-5:] == [["candidate", OBSERVER_CASE]] * 2 + [["baseline", OBSERVER_CASE]] * 3
    assert contents == TESTS


@pytest.mark.parametrize("candidate", [("failed", "passed"), ("passed", "failed")], ids=["second-pass", "first-pass"])
def test_y3_candidate_isolation_pass_does_not_reject(scripted_feedback, candidate):
    attempts, _, runs, contents = scripted_feedback(candidate=candidate)
    [attempt] = attempts
    assert attempt["decision"] == "accepted"
    repetitions = candidate.index("passed") + 1
    assert [run for run in runs if run[1] == OBSERVER_CASE] == [["candidate", OBSERVER_CASE]] * repetitions
    assert contents == FIXED


def test_y4_prompt_includes_acceptance_rules(scripted_feedback):
    _, prompts, _, _ = scripted_feedback(outside=True)
    [prompt] = prompts
    for rule in (
        "Write only within the declared outputs",
        "Preserve test functions, assertions, and coverage markers",
        "do not add skips or expected failures",
        "Reduce the failures reported below",
        "keep currently passing tests passing when the full suite is rerun",
    ):
        assert rule in prompt


def test_y4_retry_includes_rejected_candidate_output(scripted_feedback):
    attempts, prompts, _, _ = scripted_feedback(rounds=2)
    first, second = prompts
    assert attempts[0]["reason"] in second
    assert OBSERVER_CASE in second
    assert "isolated output begins" in second and "isolated output ends" in second
    assert "omitted middle" not in second
    assert "latest rejected attempt" not in first


def test_retry_ignores_interrupted_attempt_in_rejection_history(scripted_feedback, monkeypatch):
    attempt = engine._Feedback.attempt
    interruption = "interrupted; restored before resuming"

    def attempt_with_history(self, round_record, task, failures, suite, unstable):
        if round_record["number"] == 2:
            self.state["rounds"][0]["attempts"].append({
                "owner": task.task_id, "decision": "rejected", "reason": interruption, "interrupted": True,
            })
        return attempt(self, round_record, task, failures, suite, unstable)

    monkeypatch.setattr(engine._Feedback, "attempt", attempt_with_history)
    attempts, prompts, _, _ = scripted_feedback(rounds=2)
    _, retry_prompt = prompts
    assert interruption not in retry_prompt
    assert attempts[0]["reason"] in retry_prompt
    assert "isolated output ends" in retry_prompt


@pytest.mark.parametrize(("replacement", "outside", "rejected_file"), [
    pytest.param(FIXED, True, "undeclared.txt", id="outside-scope"),
    pytest.param(FIXED.replace("    assert actual == 5\n", ""), False, TEST_FILE, id="removed-assertion"),
])
def test_y4_retry_identifies_files_in_rejection(scripted_feedback, replacement, outside, rejected_file):
    attempts, prompts, _, _ = scripted_feedback(rounds=2, replacement=replacement, outside=outside)
    reason = attempts[0]["reason"]
    assert rejected_file in reason
    assert reason in prompts[1]


def test_completed_isolation_result_is_used_after_deadline(scripted_feedback, monkeypatch):
    now = 0.0
    run_tests = engine._Feedback.run_tests

    def finish_after_deadline(self, record, purpose, case=None, timeout=None, *, target=None):
        nonlocal now
        result = run_tests(self, record, purpose, case, timeout, target=target)
        if purpose == "candidate isolate" and case == NEXT_CASE:
            assert timeout == self.timeout
            assert result.cases == {NEXT_CASE: "passed"}
            now = self.timeout + 0.001
        return result

    monkeypatch.setattr(engine, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(engine._Feedback, "run_tests", finish_after_deadline)
    attempts, _, runs, contents = scripted_feedback(candidate=("passed",))
    assert runs[-1] == ["candidate", NEXT_CASE]
    [attempt] = attempts
    assert attempt["decision"] == "accepted"
    assert contents == FIXED


def test_baseline_isolation_timeout_rejects_with_budget_reason(scripted_feedback, monkeypatch):
    run_tests = engine._Feedback.run_tests

    def timeout_on_baseline(self, record, purpose, case=None, timeout=None, *, target=None):
        if purpose == "baseline isolate":
            self.snapshot.restore(target)
            assert case == OBSERVER_CASE and (self.root / TEST_FILE).read_text() == TESTS
            assert 0 < timeout < self.timeout
            return engine._Suite(unavailable="fixture timeout", timed_out=True)
        return run_tests(self, record, purpose, case, timeout, target=target)

    monkeypatch.setattr(engine._Feedback, "run_tests", timeout_on_baseline)
    attempts, _, _, contents = scripted_feedback()
    [attempt] = attempts
    assert attempt["decision"] == "rejected"
    assert attempt["reason"] == "candidate isolation exceeded the test time budget"
    assert contents == TESTS
