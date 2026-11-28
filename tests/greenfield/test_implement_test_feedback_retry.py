"""Regressions from the independent W21 review."""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from codd import implementer
from codd.greenfield import implement_test_feedback as engine, pipeline as greenfield
from codd.languages.adapters.pytest_feedback import PytestFeedbackAdapter


SOURCE = "src/service.py"
TEST_FILE = "checks/test_service.py"
OBSERVER = f"{TEST_FILE}::test_observer"
DIAGNOSTIC = "Observer received 404 where the test expects 200."
ORIGINAL = "def response():\n    return 200\n\nfirst, second = 1, 2\n"
REPAIRED = ORIGINAL.replace("1, 2", "2, 2")
REPAIRED_BOTH = REPAIRED.replace("2, 2", "2, 3")
REGRESSION = REPAIRED_BOTH.replace("return 200", "return 404")
TEST_SUBJECTS = """\
from service import first, response, second

def test_first():
    assert first == 2

def test_second():
    assert second == 3
"""

TESTS = TEST_SUBJECTS + """\

def test_observer():
    assert response() == 200
"""
UPDATED_TESTS = TESTS + "\n# Clarify the test file without changing expectations.\n"
CACHE_TESTS = "import os\n\nimport pytest\n\n" + TEST_SUBJECTS + """\

@pytest.mark.feedback_observer
def test_observer(cache, pytestconfig):
    assert pytestconfig.getoption('strict_markers')
    key = 'fixture/response'
    actual = response()
    # A fresh fixture or a full candidate run publishes the current value.
    if cache.get(key, None) is None or (actual == 404 and not os.environ.get('CODD_TEST_FEEDBACK_CASE')):
        cache.set(key, actual)
    assert cache.get(key, None) == 200
"""

FAKE_TEST_RUNNER = """\
import json
import os
import runpy
import sys
from pathlib import Path

test_file, diagnostic = sys.argv[1:]
selected = os.environ['CODD_TEST_FEEDBACK_CASE']
cases, failures = {}, []
tests = runpy.run_path(test_file)
for name, test in tests.items():
    case = f'{test_file}::{name}'
    if not name.startswith('test_') or (selected and selected != case):
        continue
    try:
        test()
    except AssertionError:
        cases[case] = 'failed'
        detail = diagnostic if name == 'test_observer' else 'Subject assertion failed.'
        failures.append(dict(case=case, file=test_file, origin=test_file, phase='call', detail=detail))
        print(detail)
    else:
        cases[case] = 'passed'
status = int(bool(failures))
report = dict(cases=cases, failures=failures, environment=[], collection_errors={}, exitstatus=status)
(Path(os.environ['CODD_TEST_FEEDBACK_REPORT']) / 'report-fake.json').write_text(json.dumps(report))
sys.exit(status)
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    def make(*proposals, cache=False):
        proposals = iter(proposals)
        root = tmp_path / "project"
        source_path, test_path = root / SOURCE, root / TEST_FILE
        for path, content in ((source_path, ORIGINAL), (test_path, CACHE_TESTS if cache else TESTS)):
            path.parent.mkdir(parents=True)
            path.write_text(content)
        env = {"PYTHONPATH": str(source_path.parent), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        if cache:
            env["PYTEST_ADDOPTS"] = "--strict-markers -o 'markers=feedback_observer: observed test'"
            command = [sys.executable, "-m", "pytest", "-q", "-o", "cache_dir=.pytest_cache", "checks"]
            # Seed the real cache before C. The feedback commands must leave it alone.
            seed = subprocess.run([*command[:-1], OBSERVER], cwd=root, env=env, capture_output=True, text=True)
            assert seed.returncode == 0, seed.stdout + seed.stderr
        else:
            driver = tmp_path / "run_tests.py"
            driver.write_text(FAKE_TEST_RUNNER)
            command = [sys.executable, str(driver), TEST_FILE, DIAGNOSTIC]

        prompts = []
        config = {"project": {"language": "python"}, "scan": {"test_dirs": ["checks"], "source_dirs": ["src"]}}
        task = greenfield.ImplementTaskRef("service", "design:service", expected_outputs=(SOURCE, TEST_FILE))

        def reimplement(project_root, tasks, feedback, config, *, feedback_output_paths):
            assert feedback_output_paths.names == (SOURCE, TEST_FILE)
            design = implementer.DesignContext(task.design_node, Path("design.md"), "# Contract\nReturn 200.\n")
            prompts.append(implementer._build_implementation_prompt(
                config=config, design_context=design,
                spec=implementer.ImplementSpec(task.design_node, [SOURCE, TEST_FILE]),
                dependency_documents=[], conventions=[], coding_principles=None, feedback=feedback,
            ))
            source, tests = next(proposals)
            source_path.write_text(source)
            if tests is not None:
                test_path.write_text(tests)

        monkeypatch.setattr(engine.VerifyRunner, "feedback_test_command", lambda *_: (command, root, env, 30))
        monkeypatch.setattr(greenfield, "_enforce_stage_coverage_gate", lambda *_, **__: None)
        pipeline = SimpleNamespace(
            _provision_env=lambda _: None,
            _checkpoint=lambda _: None,
            echo=lambda _: None,
            _resolve_layout_profile=lambda _: None,
            _enforce_implement_oracle_gate=lambda *_, **__: None,
            _reimplement_tasks=reimplement,
        )
        state = {"rounds": [], "ignored": [".codd/**", ".pytest_cache/**"], "environment_paths": []}
        feedback = engine._Feedback(
            pipeline, root, [task], state, {}, config, PytestFeedbackAdapter(), (Path("checks"),), ["src"], (),
        )
        return SimpleNamespace(root=root, source=source_path, tests=test_path, prompts=prompts,
                               rounds=state["rounds"], run=feedback.run)

    return make


def test_candidate_cache_cannot_contaminate_baseline(project):
    sample = project((REGRESSION, None), cache=True)
    sample.run(1)
    [record] = sample.rounds
    [attempt] = record["attempts"]
    assert attempt["decision"] == "rejected", attempt
    assert attempt["reason"].endswith(OBSERVER)
    assert [run["purpose"] for run in record["runs"] if run.get("case") == OBSERVER] == [
        "candidate isolate", "candidate isolate", "baseline isolate", "baseline isolate", "baseline isolate",
    ]
    assert (sample.root / ".pytest_cache/v/fixture/response").read_text() == "200"
    assert sample.source.read_text() == ORIGINAL


def test_retry_checks_original_assertion_against_candidate(project):
    weakened = TESTS.replace("response() == 200", "response() == 404")
    sample = project((REGRESSION, None), (REGRESSION, weakened), (REGRESSION, weakened))
    sample.run(3)
    first, second, *later = [record["attempts"][0] for record in sample.rounds]
    assert first["reason"] == f"previously passing test now fails in isolation: {OBSERVER}"
    for attempt in [second, *later]:
        assert attempt["decision"] == "rejected", attempt
        assert attempt["reason"] == f"original version of previously passing test still fails in isolation: {OBSERVER}"
    assert len(later) == 1
    for record in sample.rounds[1:]:
        assert [run["purpose"] for run in record["runs"] if run.get("case") == OBSERVER] == [
            "candidate isolate", "candidate isolate", "baseline isolate", "baseline isolate", "baseline isolate",
        ]
    for prompt in sample.prompts[1:]:
        assert DIAGNOSTIC in prompt
        assert "The test named in the rejection passed before your previous change." in prompt
        assert "The original test file will be used to check the retry. Fix the implementation." in prompt
    assert sample.tests.read_text() == TESTS
    assert sample.source.read_text() == ORIGINAL


def test_retry_keeps_candidate_test_file_when_original_passes(project):
    sample = project((REGRESSION, None), (REPAIRED_BOTH, UPDATED_TESTS))
    sample.run(2)
    _, retry = sample.rounds
    [attempt] = retry["attempts"]
    assert attempt["decision"] == "accepted", attempt
    assert [run["purpose"] for run in retry["runs"] if run.get("case") == OBSERVER] == ["candidate isolate"]
    assert sample.tests.read_text() == UPDATED_TESTS


def test_retry_after_acceptance_omits_stale_rejection(project):
    sample = project((REGRESSION, None), (REPAIRED, None), (REPAIRED_BOTH, UPDATED_TESTS))
    sample.run(3)
    assert [record["attempts"][0]["decision"] for record in sample.rounds] == ["rejected", "accepted", "accepted"]
    assert DIAGNOSTIC in sample.prompts[1]
    assert DIAGNOSTIC not in sample.prompts[2]
    assert OBSERVER not in sample.prompts[2]
    for record in sample.rounds[1:]:
        assert all(run.get("case") != OBSERVER for run in record["runs"])
    assert sample.tests.read_text() == UPDATED_TESTS
