"""W21b feedback through a fake pipeline and scripted test outcomes."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from codd.greenfield import implement_test_feedback as engine, pipeline as greenfield
from codd.languages.adapters.pytest_feedback import PytestFeedbackAdapter


TEST_FILE = "checks/test_subject.py"
OBSERVER_FILE = "checks/test_observer.py"
HELPER_FILE = "checks/support/observer.py"
SUBJECT = f"{TEST_FILE}::test_example"
OBSERVER = f"{OBSERVER_FILE}::test_example"
OTHER = f"{OBSERVER_FILE}::test_other"
INITIAL = "def test_example():\n    assert True\n\ndef test_other():\n    assert True\n"


@pytest.fixture
def run_feedback(tmp_path, monkeypatch):
    def run(outcomes, owners, *, rounds=1, origins=None):
        root = tmp_path / "project"
        files = (TEST_FILE, OBSERVER_FILE, HELPER_FILE)
        for name in files:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(INITIAL)
        observed = []
        prompts = []

        class FakeProcess:
            def __init__(self, command, *, cwd, env, **kwargs):
                assert command == ["scripted-runner"] and cwd == root
                self.env = env

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                pass

            def communicate(self, *, timeout):
                version = frozenset(
                    line.removeprefix("# revised by ") for name in files
                    for line in (root / name).read_text().splitlines() if line.startswith("# revised by ")
                )
                selected = self.env["CODD_TEST_FEEDBACK_CASE"]
                run = (version, selected)
                cases, output = outcomes(version, selected, observed.count(run))
                observed.append(run)
                failures = []
                for case, status in cases.items():
                    if status == "failed":
                        name = case.partition("::")[0]
                        failures.append({
                            "case": case, "file": name, "origin": (origins or {}).get(case, name),
                            "phase": "call", "detail": "scripted failure",
                        })
                self.returncode = int(bool(failures))
                report = dict(cases=cases, failures=failures, environment=[], collection_errors={},
                              exitstatus=self.returncode)
                scratch = Path(self.env["CODD_TEST_FEEDBACK_REPORT"])
                (scratch / "report-fake.json").write_text(json.dumps(report))
                return output, ""

        def reimplement(project_root, tasks, feedback, config, *, feedback_output_paths):
            [task] = tasks
            prompts.append((task.task_id, feedback))
            [name] = feedback_output_paths.names
            with (project_root / name).open("a") as source:
                number = sum(owner == task.task_id for owner, _ in prompts)
                source.write(f"# revised by {task.task_id}:{number}\n")

        monkeypatch.setattr(engine, "subprocess", SimpleNamespace(
            Popen=FakeProcess, PIPE=subprocess.PIPE, TimeoutExpired=subprocess.TimeoutExpired,
        ))
        monkeypatch.setattr(engine.VerifyRunner, "feedback_test_command",
                            lambda *_: (["scripted-runner"], root, {}, 30))
        monkeypatch.setattr(greenfield, "_enforce_stage_coverage_gate", lambda *_, **__: None)
        pipeline = SimpleNamespace(
            _provision_env=lambda _: None,
            _checkpoint=lambda _: None,
            echo=lambda _: None,
            _resolve_layout_profile=lambda _: None,
            _enforce_implement_oracle_gate=lambda *_, **__: None,
            _reimplement_tasks=reimplement,
        )
        tasks = [greenfield.ImplementTaskRef(owner, "design.md", expected_outputs=(name,))
                 for owner, name in owners.items()]
        state = {"rounds": [], "ignored": [".codd/**"], "environment_paths": []}
        engine._Feedback(
            pipeline, root, tasks, state, {}, {}, PytestFeedbackAdapter(), (Path("checks"),), [], (),
        ).run(rounds)
        return state["rounds"], prompts, observed, root

    return run


def test_y8_returns_suite_failure_to_test_file_owner(run_feedback):
    failure_output = "suite output starts" + "x" * 6500 + "omitted middle" + "x" * 11000 + "suite output ends"

    def outcomes(version, selected, _):
        if selected or version:
            return {OBSERVER: "passed"}, ""
        return {OBSERVER: "failed"}, failure_output

    rounds, prompts, _, _ = run_feedback(
        outcomes, {"helper": HELPER_FILE, "observer": OBSERVER_FILE}, origins={OBSERVER: HELPER_FILE},
    )
    assert [owner for owner, _ in prompts] == ["observer"]
    _, prompt = prompts[0]
    assert "\n\nUnstable tests\n" in prompt and OBSERVER in prompt
    assert "(isolate). Failed in " in prompt and "(round 1 suite)" in prompt
    assert "suite output starts" in prompt and "suite output ends" in prompt
    assert "omitted middle" not in prompt
    for rule in ("wait with a timeout for the expected state", "request completion",
                 "test order or state left by other tests", "Preserve the assertions"):
        assert rule in prompt
    [observation] = rounds[0]["unstable_observations"]
    assert observation["case"] == OBSERVER and "suite output ends" in observation["output"]
    [attempt] = rounds[0]["attempts"]
    assert (attempt["decision"], attempt["isolated_before"]) == ("accepted", 0)


@pytest.mark.parametrize("already_handled", [False, True], ids=["same-round", "next-round"])
def test_y9_returns_baseline_failure_on_the_next_selection(run_feedback, already_handled):
    def outcomes(version, selected, count):
        repaired = "repair:1" in version
        observer_fixed = f"observer:{2 if already_handled else 1}" in version
        other = "passed" if "observer:1" in version or not already_handled else "failed"
        if selected == OBSERVER:
            # The first two candidate runs fail. A later screen passes, so the
            # old flow has no reproducible failure to send to the observer.
            failed = not observer_fixed and (not repaired or count < 2)
            output = "candidate observer failure" if repaired else "baseline observer failure"
            return {selected: "failed" if failed else "passed"}, output if failed else ""
        statuses = {
            SUBJECT: "passed" if repaired else "failed",
            OTHER: other,
            OBSERVER: "failed" if repaired and not observer_fixed and count == 0 else "passed",
        }
        return ({selected: statuses[selected]} if selected else statuses), "suite output"

    rounds, prompts, _, _ = run_feedback(
        outcomes, {"observer": OBSERVER_FILE, "repair": TEST_FILE}, rounds=2,
    )
    [repair] = [attempt for attempt in rounds[0]["attempts"] if attempt["owner"] == "repair"]
    assert repair["baseline_failures"] == [{"case": OBSERVER, "passed": 0, "failed": 1}]
    assert repair["decision"] == "accepted"
    assert prompts[-1][0] == "observer"
    prompt = prompts[-1][1]
    assert "\n\nUnstable tests\n" in prompt and OBSERVER in prompt
    assert "baseline observer failure" in prompt and "candidate observer failure" not in prompt
    assert "(baseline isolate)" in prompt
    assert len(rounds) == (2 if already_handled else 1)
    observer = rounds[-1]["attempts"][-1]
    assert (observer["owner"], observer["decision"], observer["isolated_before"]) == ("observer", "accepted", 0)
    if already_handled:
        assert rounds[1]["runs"][0]["failures_and_errors"] == 0


@pytest.mark.parametrize("mode", ["isolated-failure", "suite-failure", "all-pass"])
def test_y10_requires_full_suite_and_three_isolated_passes(run_feedback, mode):
    flaky = f"{TEST_FILE}::test_other"

    def outcomes(version, selected, count):
        if selected == flaky:
            fails = bool(version) and mode == "isolated-failure" and count == 2
            return {flaky: "failed" if fails else "passed"}, ""
        cases = {SUBJECT: "passed" if version else "failed"}
        if not selected:
            cases[flaky] = "failed" if not version or mode == "suite-failure" else "passed"
        return cases, ""

    [record], _, observed, root = run_feedback(outcomes, {"repair": TEST_FILE})
    [attempt] = record["attempts"]
    if mode == "all-pass":
        assert attempt["decision"] == "accepted"
        assert "revised by repair" in (root / TEST_FILE).read_text()
    else:
        assert attempt["decision"] == "rejected"
        assert attempt["reason"] == f"unstable test still fails: {flaky}"
        assert (root / TEST_FILE).read_text() == INITIAL
    assert attempt["isolated_before"] == attempt["before"] == 1
    assert observed.count((frozenset({"repair:1"}), flaky)) == (0 if mode == "suite-failure" else 3)


def test_y11_does_not_return_unstable_test_with_conflicting_owners(run_feedback):
    def outcomes(_version, selected, _count):
        return {OBSERVER: "passed" if selected else "failed"}, "suite output"

    [record], prompts, observed, _ = run_feedback(
        outcomes, {"helper": HELPER_FILE, "first": OBSERVER_FILE, "second": OBSERVER_FILE},
        origins={OBSERVER: HELPER_FILE},
    )
    assert (frozenset(), OBSERVER) in observed
    assert record["passed_in_isolation"] == 1
    assert prompts == record["attempts"] == []


def test_unstable_return_limit_defers_remaining_evidence_to_next_round(run_feedback):
    cases = [f"{OBSERVER_FILE}::test_example[{index}]" for index in range(21)]

    def outcomes(version, selected, _count):
        if selected:
            return {selected: "passed"}, ""
        return dict.fromkeys(cases, "passed" if version else "failed"), "suite output"

    rounds, prompts, _, _ = run_feedback(
        outcomes, {"helper": HELPER_FILE, "other_helper": TEST_FILE, "observer": OBSERVER_FILE},
        origins={**dict.fromkeys(cases[:-1], HELPER_FILE), cases[-1]: TEST_FILE}, rounds=2,
    )
    first, second = [record["attempts"][0] for record in rounds]
    assert first["decision"] == second["decision"] == "accepted"
    assert [owner for owner, _ in prompts] == ["observer", "observer"]
    assert list(first["unstable_cases"]) == cases[:-1]
    assert list(second["unstable_cases"]) == cases[-1:]
    assert rounds[1]["runs"][0]["failures_and_errors"] == 0
