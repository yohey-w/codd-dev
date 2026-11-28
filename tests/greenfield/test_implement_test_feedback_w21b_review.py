"""W21b review regressions with a fake pipeline and deterministic test runs."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from codd.greenfield import implement_test_feedback as engine, pipeline as greenfield
from codd.languages.adapters.pytest_feedback import PytestFeedbackAdapter


TEST = "checks/test_observer.py"
REPAIR = "checks/test_repair.py"
THIRD = "checks/test_third.py"
HELPER = "checks/support/helper.py"
HELPER2 = "checks/support/helper2.py"
SOURCE = "src/subject.py"
RUNTIME = ".venv/runtime.txt"
B = TEST + "::test_subject"
A = REPAIR + "::test_subject"
C = THIRD + "::test_subject"
BODY = "def test_subject():\n    assert 1 == 1\n"
DESIGN = "docs/design/core.md"


def ref(owner, *outputs, design=DESIGN):
    return greenfield.ImplementTaskRef(owner, design, expected_outputs=outputs)


def write(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def revised(root, name):
    return "# revised" in (root / name).read_text()


def fake(tmp_path, monkeypatch, tasks, outcomes, *, files=None, edit=None,
         provision=None, real_audits=False):
    root = tmp_path / "project"
    for name, content in {
        TEST: BODY, REPAIR: BODY, THIRD: BODY, HELPER: "", HELPER2: "", SOURCE: "",
        DESIGN: "# Core design\n", **(files or {}),
    }.items():
        write(root, name, content)
    record = {"test_feedback": {"rounds": [], "ignored": [".codd/**"], "environment_paths": [".venv"]}}
    checkpoint_file = root / ".codd/review-session.json"
    prompts = []
    messages = []
    adapter = PytestFeedbackAdapter()

    class Process:
        def __init__(self, command, *, cwd, env, **kwargs):
            assert command == ["fake-runner"] and cwd == root
            self.env = env

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def communicate(self, *, timeout):
            selected = self.env["CODD_TEST_FEEDBACK_CASE"]
            cases, details = outcomes(root, selected)
            failures = []
            for case, status in cases.items():
                if status != "failed":
                    continue
                name = case.partition("::")[0]
                failures.append({"case": case, "file": name, "origin": name, "phase": "call",
                                 "detail": "synthetic failure", **details.get(case, {})})
            self.returncode = int(bool(failures))
            report = dict(cases=cases, failures=failures, environment=[], collection_errors={},
                          exitstatus=self.returncode)
            scratch = Path(self.env["CODD_TEST_FEEDBACK_REPORT"])
            (scratch / "report-fake.json").write_text(json.dumps(report))
            return "\n".join(item["detail"] for item in failures), ""

    def reimplement(project, selected_tasks, feedback, config, *, feedback_output_paths):
        [task] = selected_tasks
        prompts.append((task.task_id, feedback))
        if edit:
            edit(project, task.task_id, feedback_output_paths)
        else:
            [name] = feedback_output_paths.names
            write(project, name, (project / name).read_text() + "\n# revised\n")

    def checkpoint(_):
        write(root, checkpoint_file.relative_to(root), json.dumps(record))

    monkeypatch.setattr(engine, "subprocess", SimpleNamespace(Popen=Process, PIPE=subprocess.PIPE,
                                                               TimeoutExpired=subprocess.TimeoutExpired))
    monkeypatch.setattr(engine.VerifyRunner, "feedback_test_command", lambda *_: (["fake-runner"], root, {}, 30))
    if not real_audits:
        monkeypatch.setattr(greenfield, "_enforce_stage_coverage_gate", lambda *_, **__: None)
    pipeline = SimpleNamespace(
        _provision_env=provision or (lambda _: None), _checkpoint=checkpoint, echo=messages.append,
        _resolve_layout_profile=lambda _: None, _enforce_implement_oracle_gate=lambda *_, **__: None,
        _reimplement_tasks=reimplement,
    )

    def drive(count, *, resume=False):
        if resume:
            record.update(json.loads(checkpoint_file.read_text()))
            engine.recover_interrupted_feedback(root, record, pipeline.echo, checkpoint)
        state = record["test_feedback"]
        engine._Feedback(pipeline, root, tasks, state, {}, {}, adapter, (Path("checks"),), [], ()).run(count)
        return state

    return SimpleNamespace(root=root, drive=drive, prompts=prompts, messages=messages,
                           record=record, adapter=adapter)


def attempts(state):
    return [(a["owner"], a["decision"], a["reason"])
            for record in state["rounds"] for a in record["attempts"]]


@pytest.mark.parametrize("add_wait", [False, True], ids=["reject-weaker-assertion", "accept-bounded-wait"])
def test_unstable_candidate_preserves_its_assertion(tmp_path, monkeypatch, add_wait):
    original = ('# codd: covers vb=VB-ready\ndef test_subject(read_visible):\n'
                '    visible = read_visible()\n    assert visible == "ready"\n')
    if add_wait:
        candidate = original.replace("    visible =", "    wait_for_ready(read_visible, timeout=1)\n    visible =")
    else:
        candidate = original.replace('== "ready"', 'in ("ready", "pending")')

    def wait_for_ready(read_visible, *, timeout):
        assert timeout == 1
        for _ in range(2):
            if read_visible() == "ready":
                return
        raise TimeoutError("state did not become ready")

    def status(root, visible):
        namespace = {"wait_for_ready": wait_for_ready}
        exec((root / TEST).read_text(), namespace)
        samples = iter((visible,))
        try:
            namespace["test_subject"](lambda: next(samples, "ready"))
        except AssertionError:
            return "failed"
        return "passed"

    def outcomes(root, selected):
        return {B: status(root, "ready" if selected else "pending")}, {B: {"detail": "expected ready; saw pending"}}

    def edit(root, owner, scope):
        assert scope.allows(Path(TEST))
        write(root, TEST, candidate)

    case = fake(
        tmp_path, monkeypatch, [ref("author", "checks/test_observer*.py")], outcomes,
        files={TEST: original, "codd/codd.yaml": "scan:\n  test_dirs: [checks]\n",
               "docs/test/behaviors.md": "| VB | Description | Scenario |\n"
               "| --- | --- | --- |\n| VB-ready | Ready state | Observe the state |\n"},
        edit=edit, real_audits=True,
    )
    assert all(case.adapter.test_metrics(TEST, content.encode(), callable_assertions=()) == (1, 1, 0)
               for content in (original, candidate))
    state = case.drive(1)
    assert "expected ready; saw pending" in case.prompts[0][1]
    if add_wait:
        assert attempts(state) == [("author", "accepted", "unstable tests passed the suite and isolated checks")]
        assert any("marker authenticity OK" in message for message in case.messages)
        assert (case.root / TEST).read_text() == candidate and status(case.root, "pending") == "passed"
    else:
        assert attempts(state) == [("author", "rejected", f"unstable test assertions changed: {B}")]
        assert (case.root / TEST).read_text() == original and status(case.root, "pending") == "failed"
        assert "without broadening the values they accept" in case.prompts[0][1]


def baseline_script(*, origin=None, reprovision=False):
    full_after_repair = 0

    def outcomes(root, selected):
        nonlocal full_after_repair
        fixed = revised(root, HELPER)
        already_reprovisioned = reprovision and (root / RUNTIME).read_text() == "new"
        if selected == B:
            failed = not (already_reprovisioned or revised(root, TEST))
        elif not selected and fixed:
            full_after_repair += 1
            failed = full_after_repair == 1
        else:
            failed = False
        cases = {A: "passed" if fixed else "failed", B: "failed" if failed else "passed"}
        if reprovision:
            cases[C] = "passed" if already_reprovisioned else "failed"
        details = {A: {"origin": HELPER}, B: {"detail": "baseline evidence", **(origin or {})}}
        return ({selected: cases[selected]} if selected else cases), details

    return outcomes


@pytest.mark.parametrize("origin", [
    {"origin": TEST, "origins": [TEST, HELPER], "paths": [TEST, HELPER]},
    {"origin": TEST, "origins": [TEST], "paths": [TEST, SOURCE]},
], ids=["multiple-owners", "application-frame"])
def test_baseline_failure_with_ambiguous_origins_is_not_returned(tmp_path, monkeypatch, origin):
    case = fake(tmp_path, monkeypatch, [ref("repair", HELPER), ref("observer", TEST)],
                baseline_script(origin=origin))
    state = case.drive(1)
    [repair] = state["rounds"][0]["attempts"]
    assert repair["baseline_failures"] == [{"case": B, "passed": 0, "failed": 1}]
    assert attempts(state) == [("repair", "accepted", "failures decreased")]
    assert [owner for owner, _ in case.prompts] == ["repair"]
    assert state["pending_unstable"] == {}


def test_reprovisioning_invalidates_pending_observations(tmp_path, monkeypatch):
    n_provisions = 0

    def provision(root):
        nonlocal n_provisions
        n_provisions += 1
        write(root, RUNTIME, "new" if n_provisions > 1 else "old")

    def edit(root, owner, scope):
        if owner == "third":
            write(root, RUNTIME, "changed by candidate")
        else:
            [name] = scope.names
            write(root, name, (root / name).read_text() + "\n# revised\n")

    case = fake(tmp_path, monkeypatch, [ref("repair", HELPER), ref("observer", TEST), ref("third", THIRD)],
                baseline_script(reprovision=True), edit=edit, provision=provision)
    state = case.drive(4)
    assert [(a, b) for a, b, _ in attempts(state)] == [("repair", "accepted"), ("third", "rejected")]
    assert "discarding and reprovisioning" in attempts(state)[1][2]
    assert [owner for owner, _ in case.prompts] == ["repair", "third"] and n_provisions == 2
    [record] = state["rounds"]
    assert record["unstable_observations"][0]["case"] == B
    assert record["runs"][-1]["purpose"] == "suite after reprovisioning"
    assert record["runs"][-1]["failures_and_errors"] == 0
    assert state["pending_unstable"] == {} and record["stop"]


def test_resume_returns_interrupted_unstable_cases_and_drains_overflow(tmp_path, monkeypatch):
    selected_cases = [f"{TEST}::test_subject[{index}]" for index in range(21)]
    interrupted = False

    def outcomes(root, selected):
        cases = dict.fromkeys(selected_cases, "passed" if selected or interrupted else "failed")
        details = {case: {"origin": HELPER if i < 20 else HELPER2} for i, case in enumerate(selected_cases)}
        return ({selected: cases[selected]} if selected else cases), details

    def edit(root, owner, scope):
        assert owner == "observer" and scope.allows(Path(TEST))
        assert (root / TEST).read_text() == BODY
        if not interrupted:
            write(root, TEST, BODY + "\n# partial edit\n")
            raise KeyboardInterrupt

    case = fake(tmp_path, monkeypatch, [ref("helper1", HELPER), ref("helper2", HELPER2), ref("observer", TEST)],
                outcomes, edit=edit)
    with pytest.raises(KeyboardInterrupt):
        case.drive(6)
    interrupted = True
    saved = json.loads((case.root / ".codd/review-session.json").read_text())["test_feedback"]
    state = case.drive(6, resume=True)
    assert (case.root / TEST).read_text() == BODY
    assert [owner for owner, _ in case.prompts] == ["observer"] * 3
    assert list(saved["pending_unstable"]) == selected_cases
    first, second = state["rounds"]
    aborted, retried = first["attempts"]
    [overflow] = second["attempts"]
    assert aborted["interrupted"] and list(aborted["unstable_cases"]) == selected_cases[:20]
    assert retried["decision"] == "accepted" and list(retried["unstable_cases"]) == selected_cases[:20]
    assert overflow["decision"] == "accepted" and list(overflow["unstable_cases"]) == selected_cases[-1:]
    assert not first["stop"] and second["stop"] and state["pending_unstable"] == {}


def test_resume_stops_if_only_an_interrupted_ordinary_owner_remains(tmp_path, monkeypatch):
    def outcomes(root, selected):
        return {B: "failed"}, {}

    def edit(root, owner, scope):
        raise KeyboardInterrupt

    case = fake(tmp_path, monkeypatch, [ref("observer", TEST)], outcomes, edit=edit)
    with pytest.raises(KeyboardInterrupt):
        case.drive(6)
    state = case.drive(6, resume=True)
    [record] = state["rounds"]
    assert record["stop"] and record["attempts"][0]["interrupted"]
    assert [owner for owner, _ in case.prompts] == ["observer"]
