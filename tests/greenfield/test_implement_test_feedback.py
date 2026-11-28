"""Exercise greenfield feedback with real test runs and a scripted AI."""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

import pytest
import yaml

from codd.config import load_project_config
from codd.greenfield import pipeline as greenfield
from codd.implementer import implement_tasks
from codd.repair.verify_runner import VerifyRunner
from codd.repair.verify_runner import _EXEC_ENV_STATE_RELPATH

from .conftest import make_stub_project
from .test_pipeline import _fake_runners

TEST_FILE = "checks/test_subject.py"
HELPER = "checks/support/helpers.py"
APP = "src/stub_app/subject.py"
DESIGN = "docs/design/core_design.md"
TESTS = """\
def test_subject():
    actual = [7][1]
    assert actual == 7

def test_passing():
    actual = 5
    assert actual == 5
"""
FIXED = TESTS.replace("[7][1]", "[7][0]")

AI = """\
import json
import re
import sys
from pathlib import Path

prompt = sys.stdin.read()
payloads = json.loads(Path(sys.argv[1]).read_text())
log = Path(sys.argv[2])
part = "feedback" if "--- REVIEW FEEDBACK" in prompt else "initial"
match = re.search(r"^- Task: (.+)$", prompt, re.M)
owner = match[1] if match else "intake"
with log.open("a") as stream:
    stream.write(f"{part} {owner}\\n")
if part == "feedback":
    log.with_name(f"prompt-{owner}").write_text(prompt)
result = payloads[part].get(owner, {})
for path, content in result.get("direct", {}).items():
    dest = Path(path)
    if content is None:
        dest.unlink()
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
for path, content in result.get("files", {}).items():
    print(f"=== FILE: {path} ===\\n{content}")
"""

DRIVER = """\
import os
import sys
from pathlib import Path

import pytest

assert os.environ["FEEDBACK_TEST_ASSET"] == "ready"
assert Path(os.environ["PATH"].split(os.pathsep)[0]) == Path(sys.argv[1]).parent / "tools"
sys.path[:0] = [str(Path.cwd()), str(Path.cwd() / "src")]
case = os.environ.get("CODD_TEST_FEEDBACK_CASE", "")
with Path(sys.argv[1]).open("a") as stream:
    stream.write(("isolate " + case if case else "suite") + "\\n")
sys.exit(pytest.main(["checks", "--tb=short", "-q"]))
"""


@pytest.fixture
def project_factory(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    monkeypatch.delenv("PYTEST_PLUGINS", raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("CODD_AI_USAGE_LOG", raising=False)
    real_audit = greenfield._enforce_stage_coverage_gate

    def make(
        name="case", *, enabled=True, rounds=None, initial=None, fixes=None, owned=None,
        provision_error=False, command=None, timeout=30, verdict_failure=None, language="python",
    ):
        sandbox = tmp_path / name
        sandbox.mkdir()
        script = sandbox / "ai.py"
        script.write_text(AI, encoding="utf-8")
        driver = sandbox / "run_suite.py"
        driver.write_text(DRIVER, encoding="utf-8")
        trace = sandbox / "trace"
        tools = sandbox / "tools"
        tools.mkdir()
        payloads = sandbox / "payloads.json"
        initial = initial or {"case": {TEST_FILE: TESTS}}
        fixes = fixes if fixes is not None else {"case": {"files": {TEST_FILE: FIXED}}}

        def marked(files):
            return {
                path: re.sub(r"^def test_", "# codd: covers vb=VB-observe\ndef test_", content, count=1, flags=re.M)
                if path == TEST_FILE else content
                for path, content in files.items()
            }

        payloads.write_text(json.dumps({
            "initial": {
                owner: {"files": marked(files)} for owner, files in initial.items()
            },
            "feedback": {
                owner: {**data, "files": marked(data.get("files", {}))}
                for owner, data in fixes.items()
            },
        }), encoding="utf-8")
        ai = shlex.join((sys.executable, str(script), str(payloads), str(trace)))
        config = {"elicit": False}
        if enabled is not None:
            config["implement_test_feedback"] = enabled
        if rounds is not None:
            config["implement_test_feedback_rounds"] = rounds
        root = make_stub_project(sandbox, ai, greenfield_config=config, coverage_gate=True)
        config_file = root / "codd/codd.yaml"
        config = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        config["project"]["language"] = language
        config["scan"]["test_dirs"] = ["checks"]
        config["verify"] = {
            "test_command": command or (
                "FEEDBACK_TEST_ASSET=ready " + shlex.join((sys.executable, str(driver), str(trace)))
            ),
            "test_timeout_seconds": timeout,
        }
        config_file.write_text(yaml.safe_dump(config), encoding="utf-8")
        design = root / DESIGN
        design.parent.mkdir(parents=True, exist_ok=True)
        design.write_text("# Subject design\n\nThe test examples evaluate literal values.\n", encoding="utf-8")
        behavior = root / "docs/test/behaviors.md"
        behavior.parent.mkdir()
        behavior.write_text(
            "| VB | Description | Scenario |\n| --- | --- | --- |\n"
            "| VB-observe | observes the result | evaluate the example |\n", encoding="utf-8",
        )
        stdout = []

        def log(event):
            with trace.open("a", encoding="utf-8") as stream:
                stream.write(event + "\n")

        tasks = [
            greenfield.ImplementTaskRef(name, DESIGN, tuple(paths), expected_outputs=tuple(paths), title=name)
            for name, paths in (owned or {"case": (TEST_FILE,)}).items()
        ]
        for task in tasks:
            for path in task.output_paths:
                target = root / path
                if path.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)

        def implement(project_root, task, **kwargs):
            files = list(initial.get(task.task_id, {}))
            if files:
                implement_tasks(
                    project_root, design=task.design_node, ai_command=ai,
                    output_paths=files, expected_outputs=task.expected_outputs, task_title=task.title,
                    use_derived_steps=False,
                )
            return "implemented"

        def verify(project_root, **kwargs):
            log("verify")
            settings = load_project_config(project_root)
            verdict = VerifyRunner(project_root, settings)._run_test_command(settings)
            return verdict[2]

        class Pipeline(greenfield.GreenfieldPipeline):
            def _provision_env(self, project_root):
                log("provision")
                if provision_error and trace.read_text().splitlines().count("provision") == 1:
                    raise greenfield.EnvironmentProvisionError("missing fixture asset", "prepare it")
                state = project_root / _EXEC_ENV_STATE_RELPATH
                state.parent.mkdir(parents=True, exist_ok=True)
                state.write_text(json.dumps({"path_prepend_dirs": [str(tools)]}), encoding="utf-8")

            def _enforce_implement_oracle_gate(self, project_root, refs, options, **kwargs):
                if kwargs.get("verdict_only"):
                    log("oracle verdict")
                    if verdict_failure == "oracle":
                        raise greenfield.StageError("candidate oracle failed")
                else:
                    log("oracle")

        def audit(project_root, **kwargs):
            kind = "both audits" if kwargs.get("rerun") is not None else "both audits verdict"
            log(kind)
            if kwargs.get("rerun") is None and verdict_failure in {"coverage", "authenticity"}:
                assert kwargs["rerun_oracle"] is None
                raise greenfield.StageError(f"candidate {verdict_failure} failed")
            return real_audit(project_root, **kwargs)

        monkeypatch.setattr(greenfield, "_enforce_stage_coverage_gate", audit)
        fake_runners = _fake_runners([])
        fake_runners.update(
            task_lister=lambda _: tasks, implement_task_runner=implement, verify_runner=verify,
        )
        return Pipeline(ai_command=ai, echo=stdout.append, **fake_runners), root, trace, stdout

    return make


def feedback(root):
    return greenfield.load_session(root)["stages"]["implement"]["test_feedback"]


def test_disabled_matches_main_commands_and_artifacts(project_factory):
    # Main's observed order and artifact bytes are also checked with the same
    # fixture run against the pre-feature source in the baseline comparison.
    snapshots = []
    for enabled in (None, False):
        pipeline, root, trace, stdout = project_factory(f"disabled-{enabled}", enabled=enabled)
        result = pipeline.run(root)
        assert result.status == "success", result.error
        session = greenfield.load_session(root)
        assert "test_feedback" not in session["stages"]["implement"]
        assert "implement_test_feedback" not in session["options"]
        assert not any("implement test feedback" in line for line in stdout)
        calls = trace.read_text().splitlines()
        assert calls == ["initial intake", "initial case", "oracle", "both audits", "provision", "verify", "suite"]
        artifacts = {
            path.relative_to(root).as_posix(): path.read_bytes()
            for base in ("checks", "src", "docs") for path in (root / base).rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        snapshots.append(artifacts)
    assert snapshots[0] == snapshots[1]


def test_helper_first_and_owned_reproducible_and_unstable_tests_are_sent(project_factory):
    tests = """\
from checks.support import helpers
from stub_app import subject

def test_helper():
    assert helpers.value() == 7

def test_app():
    assert subject.value() == 7

def test_flaky(request):
    actual = len(request.session.items)
    assert actual == 1

""" + TESTS
    fixed_tests = tests.replace(TESTS, FIXED).replace("len(request.session.items)", "1")
    pipeline, root, trace, stdout = project_factory(
        initial={
            "case": {TEST_FILE: tests},
            "helper": {HELPER: "def value():\n    return [7][1]\n"},
            "application": {APP: "def value():\n    return [7][1]\n"},
        },
        fixes={
            "case": {"files": {TEST_FILE: fixed_tests}},
            "helper": {"files": {HELPER: "def value():\n    return 7\n"}},
        },
        owned={"case": (TEST_FILE,), "helper": ("checks/support/",), "application": (APP,)},
    )
    result = pipeline.run(root)
    assert result.status == "success", result.error
    calls = trace.read_text().splitlines()
    assert [x for x in calls if x.startswith("feedback ")] == ["feedback helper", "feedback case"]
    assert calls.index("both audits") < calls.index("provision") < calls.index("suite")
    between = calls[calls.index("feedback helper"):calls.index("feedback case")]
    assert between[:4] == ["feedback helper", "oracle verdict", "both audits verdict", "suite"]
    assert calls[-3:] == ["provision", "verify", "suite"]

    [round_record] = feedback(root)["rounds"]
    assert round_record["passed_in_isolation"] == 1
    assert any(run["purpose"] == "suite after helper decision" for run in round_record["runs"])
    assert [item["decision"] for item in round_record["attempts"]] == ["accepted", "accepted"]
    assert [(item["before"], item["after"]) for item in round_record["attempts"]] == [(1, 0), (2, 1)]
    case_prompt = trace.with_name("prompt-case").read_text()
    reproduced, unstable = case_prompt.split("\n\nUnstable tests\n", 1)
    assert "test_subject (call" in reproduced
    assert f"{TEST_FILE}::test_flaky\nPassed in " in unstable
    helper_prompt = trace.with_name("prompt-helper").read_text()
    review = helper_prompt.split("--- REVIEW FEEDBACK", 1)[1].split("--- END REVIEW FEEDBACK", 1)[0]
    assert HELPER in review and "test_helper" in review
    assert "test_app" not in review and "test_flaky" not in review
    assert "Do not rewrite expectations" in review
    assert "feedback application" not in trace.read_text()
    assert any("accepted" in line and "suite" in line for line in stdout)


@pytest.mark.parametrize("mode", [
    "oracle", "coverage", "authenticity", "outside", "outside-output", "function", "assertion",
    "marker", "skip", "xfail", "no-improvement", "regression", "unusable",
])
def test_candidate_rejection_restores_bytes(project_factory, mode):
    original = TESTS
    candidate = FIXED
    reason = mode
    direct = {}
    files = {}
    if mode == "outside":
        direct = {"settings.txt": "bad", "old-data.bin": None, "new/data.txt": "unowned"}
        reason = "outside declared outputs"
    elif mode == "outside-output":
        files["settings.txt"] = "bad"
        reason = "outside declared outputs"
    elif mode == "function":
        candidate = FIXED.split("\ndef test_passing")[0]
        reason = "test functions"
    elif mode == "assertion":
        original = TESTS.replace("    assert actual == 7", "    assert actual > 0\n    assert actual == 7")
        reason = "assertions"
    elif mode == "marker":
        original = "# codd: covers operation=read axis=success\n" + TESTS
        reason = "coverage markers"
    elif mode in {"skip", "xfail"}:
        candidate = "import pytest\n\n@pytest.mark." + mode + "\n" + FIXED
        reason = "skip/xfail"
    elif mode == "no-improvement":
        candidate = TESTS.replace("actual = [7][1]", "actual = [7][2]")
        reason = "did not decrease"
    elif mode == "regression":
        extra = "\ndef test_second():\n    actual = [8][1]\n    assert actual == 8\n"
        original += extra
        candidate = FIXED.replace("actual = 5", "actual = 0") + extra.replace("[8][1]", "[8][0]")
        reason = "now fails in isolation"
    elif mode == "unusable":
        candidate = ""
        reason = "could not evaluate"
    if candidate:
        files[TEST_FILE] = candidate
    pipeline, root, trace, _ = project_factory(
        initial={"case": {TEST_FILE: original}}, fixes={"case": {"files": files, "direct": direct}},
        verdict_failure=mode,
    )
    settings = root / "settings.txt"
    settings.write_bytes(b"good\r\n")
    binary = root / "old-data.bin"
    binary.write_bytes(b"\xff\x00\r\n")
    initial = pipeline.implement_task_runner

    def record_bytes(*args, **kwargs):
        detail = initial(*args, **kwargs)
        before[TEST_FILE] = (root / TEST_FILE).read_bytes()
        return detail

    before = {}
    pipeline.implement_task_runner = record_bytes
    result = pipeline.run(root)
    assert result.status == "success", result.error
    [attempt] = feedback(root)["rounds"][0]["attempts"]
    assert attempt["decision"] == "rejected"
    assert reason in attempt["reason"]
    assert (root / TEST_FILE).read_bytes() == before[TEST_FILE]
    assert settings.read_bytes() == b"good\r\n"
    assert binary.read_bytes() == b"\xff\x00\r\n"
    assert not (root / "new").exists()
    assert trace.read_text().splitlines().count("feedback case") == 1


@pytest.mark.parametrize(("change", "reason"), [
    ("missing", "external import"),
    ("permission", "denied access"),
    ("connection", "denied access"),
    ("executable", "test executable unavailable"),
    ("provision", "missing fixture asset"),
    ("timeout", "exceeded"),
    ("unsupported", "no test feedback adapter"),
])
def test_environment_failure_skips_feedback(project_factory, change, reason):
    trailers = {
        "missing": "def test_environment():\n    import no_such_feedback_fixture_dependency\n",
        "permission": "def test_environment():\n    raise PermissionError('fixture blocked')\n",
        "connection": "def test_environment():\n    raise ConnectionRefusedError('fixture offline')\n",
        "executable": "import subprocess\ndef test_environment():\n    subprocess.run(['/fixture-not-present'])\n",
    }
    command = shlex.join((sys.executable, "-c", "import time; time.sleep(2)")) if change == "timeout" else None
    pipeline, root, trace, stdout = project_factory(
        initial={"case": {TEST_FILE: TESTS + "\n" + trailers.get(change, "")}},
        provision_error=change == "provision", language="go" if change == "unsupported" else "python",
        command=command, timeout=0.05 if change == "timeout" else 30,
    )
    result = pipeline.run(root)
    assert result.status == "success", result.error
    state = feedback(root)
    skip = state.get("skipped") or state["rounds"][0]["skipped"]
    assert reason in skip
    assert "feedback case" not in trace.read_text()
    assert any("skipped" in line and reason in line for line in stdout)
    assert "verify" in trace.read_text().splitlines()


def test_resume_restores_interrupted_candidate_without_returning_it_again(project_factory, monkeypatch):
    pipeline, root, trace, _ = project_factory()
    rerun = pipeline._reimplement_tasks

    def interrupt(*args, **kwargs):
        rerun(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(pipeline, "_reimplement_tasks", interrupt)
    with pytest.raises(KeyboardInterrupt):
        pipeline.run(root)
    assert b"[7][0]" in (root / TEST_FILE).read_bytes()
    assert feedback(root)["rounds"][0]["attempts"][0]["decision"] == "started"

    monkeypatch.setattr(pipeline, "_reimplement_tasks", rerun)
    result = pipeline.run(root, resume=True)
    assert result.status == "success", result.error
    assert b"[7][1]" in (root / TEST_FILE).read_bytes()
    assert trace.read_text().splitlines().count("feedback case") == 1
    [decision] = feedback(root)["rounds"][0]["attempts"]
    assert decision["decision"] == "rejected"
    assert "interrupted" in decision["reason"]


def test_second_round_can_return_remaining_failures_to_same_owner(project_factory):
    tests = TESTS + "\ndef test_second():\n    actual = [6][1]\n    assert actual == 6\n"
    pipeline, root, trace, _ = project_factory(
        rounds=2, initial={"case": {TEST_FILE: tests}},
        fixes={"case": {"files": {TEST_FILE: tests.replace("[7][1]", "[7][0]")}}},
    )
    result = pipeline.run(root)
    assert result.status == "success", result.error
    first, second = feedback(root)["rounds"]
    assert first["attempts"][0]["decision"] == "accepted"
    assert second["attempts"][0]["decision"] == "rejected"
    assert trace.read_text().splitlines().count("feedback case") == 2


def test_missing_or_ambiguous_owner_is_never_called(project_factory):
    pipeline, root, trace, _ = project_factory(
        owned={"case": (TEST_FILE,), "duplicate": ("checks/",)},
        initial={"case": {TEST_FILE: TESTS}},
    )
    result = pipeline.run(root)
    assert result.status == "success", result.error
    [round_record] = feedback(root)["rounds"]
    assert round_record["attempts"] == []
    assert "feedback case" not in trace.read_text()


def test_feedback_isolation_has_a_twenty_case_limit(project_factory):
    tests = "\n".join(f"def test_case_{i}():\n    actual = {i}\n    assert actual == {i + 1}\n" for i in range(22))
    pipeline, root, _, _ = project_factory(
        initial={"case": {TEST_FILE: tests}},
        fixes={"case": {"files": {TEST_FILE: tests}}},
    )
    result = pipeline.run(root)
    assert result.status == "success", result.error
    [round_record] = feedback(root)["rounds"]
    assert round_record["isolated"] == 20
    assert round_record["over_isolation_budget"] == 2
    assert len(round_record["attempts"]) == 1
    assert round_record["attempts"][0]["isolated_before"] == 20


@pytest.mark.parametrize("rounds", [0, -1, True, 1.5, "2"])
def test_rounds_require_a_positive_integer(project_factory, rounds):
    pipeline, root, trace, _ = project_factory(rounds=rounds)
    result = pipeline.run(root)
    assert result.failed_stage == "implement"
    assert "integer >= 1" in result.error
    assert "feedback case" not in trace.read_text()
