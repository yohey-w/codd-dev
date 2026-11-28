"""Regressions from the independent W21 review."""

import os
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from codd import implementer
from codd.greenfield import implement_test_feedback as engine, pipeline as greenfield
from codd.languages.adapters import _pytest_feedback_probe as probe
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
CACHE_TESTS = "import pytest\n\n" + TEST_SUBJECTS + """\

@pytest.mark.feedback_observer
def test_observer(cache, pytestconfig):
    assert pytestconfig.getoption('strict_markers')
    key = 'fixture/response'
    actual = response()
    # The bad response overwrites the cached value whenever the candidate runs.
    if cache.get(key, None) is None or actual == 404:
        cache.set(key, actual)
    assert cache.get(key, None) == 200
"""
PINNED_CACHE_TESTS = TEST_SUBJECTS + """\

def test_observer(cache):
    actual = response()
    expected = cache.get('fixture/response', actual)
    cache.set('fixture/response', expected)
    assert actual == expected
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
    def make(*proposals, tests=TESTS, pytest_args=None, pytest_addopts=""):
        proposals = iter(proposals)
        root = tmp_path / "project"
        source_path, test_path = root / SOURCE, root / TEST_FILE
        for path, content in ((source_path, ORIGINAL), (test_path, tests)):
            path.parent.mkdir(parents=True)
            path.write_text(content)
        env = {"PYTHONPATH": str(source_path.parent), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        pytest_command = [sys.executable, "-m", "pytest", "-q"]
        if pytest_args is not None:
            (root / "pytest.ini").write_text("[pytest]\ncache_dir = .project-cache\n")
            env["PYTEST_ADDOPTS"] = pytest_addopts
            command = [*pytest_command, *pytest_args, "checks"]
        else:
            driver = tmp_path / "run_tests.py"
            driver.write_text(FAKE_TEST_RUNNER)
            command = [sys.executable, str(driver), TEST_FILE, DIAGNOSTIC]

        def run_pytest(*args):
            return subprocess.run(
                [*pytest_command, *args], cwd=root, env={**env, "PYTHONDONTWRITEBYTECODE": "1"},
                capture_output=True, text=True,
            )

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
                               rounds=state["rounds"], run=feedback.run, pytest=run_pytest)

    return make


def test_last_failed_selection_matches_ordinary_pytest(project):
    last_failed = ("--lf", "--lfnf=none")
    sample = project((ORIGINAL, None), pytest_args=last_failed)
    seed = sample.pytest("checks")
    assert seed.returncode == 1 and "2 failed, 1 passed" in seed.stdout, seed.stdout + seed.stderr
    normal = sample.pytest(*last_failed, "checks")
    assert normal.returncode == 1, normal.stdout + normal.stderr
    failed_cases = {line.split()[1] for line in normal.stdout.splitlines() if line.startswith("FAILED ")}
    assert failed_cases == {f"{TEST_FILE}::test_first", f"{TEST_FILE}::test_second"}
    sample.run(1)
    [record] = sample.rounds
    suite = record["runs"][0]
    assert suite["executed_cases"] == suite["failures_and_errors"] == len(failed_cases)
    assert {run["case"] for run in record["runs"] if run["purpose"] == "isolate"} == failed_cases


def test_persisted_cache_value_matches_ordinary_pytest(project):
    sample = project((REGRESSION, None), tests=PINNED_CACHE_TESTS, pytest_args=())
    seed = sample.pytest(OBSERVER)
    assert seed.returncode == 0, seed.stdout + seed.stderr
    sample.run(1)
    [record] = sample.rounds
    [attempt] = record["attempts"]
    retained = sample.source.read_text()
    assert (sample.root / ".project-cache/v/fixture/response").read_text() == "200"
    sample.source.write_text(REGRESSION)
    normal = sample.pytest("checks")
    assert normal.returncode == 1 and f"FAILED {OBSERVER}" in normal.stdout, normal.stdout + normal.stderr
    assert attempt["decision"] == "rejected", attempt
    assert attempt["suite_after"] == 1 and attempt["reason"].endswith(OBSERVER)
    assert retained == ORIGINAL


def test_candidate_cache_cannot_contaminate_baseline(project):
    cache_option = ("-o", "cache_dir=.pytest_cache")
    sample = project(
        (REGRESSION, None), tests=CACHE_TESTS, pytest_args=cache_option,
        pytest_addopts="--strict-markers -o 'markers=feedback_observer: observed test'",
    )
    seed = sample.pytest(*cache_option, OBSERVER)
    assert seed.returncode == 0, seed.stdout + seed.stderr
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


@pytest.mark.parametrize("mode", ["copy-fails", "override-fails", "disabled"])
def test_pytest_cache_redirect_keeps_project_setting_on_failure_or_disable(tmp_path, monkeypatch, mode):
    project_cache = tmp_path / "project-cache"
    project_cache.mkdir()
    (project_cache / "value").write_text("200")
    run_cache = tmp_path / "run-cache"
    ini = {"cache_dir": project_cache.name}
    config = SimpleNamespace(
        rootpath=tmp_path, getini=ini.__getitem__,
        _inicache=MappingProxyType(ini) if mode == "override-fails" else ini,
        pluginmanager=SimpleNamespace(hasplugin=lambda name: name == "cacheprovider" and mode != "disabled"),
    )
    monkeypatch.setenv("CODD_TEST_FEEDBACK_CACHE_DIR", str(run_cache))

    if mode == "copy-fails":
        def fail_after_starting_copy(source, destination):
            assert source == project_cache
            destination.mkdir()
            raise OSError("copy failed")

        monkeypatch.setattr(probe.shutil, "copytree", fail_after_starting_copy)

    probe.pytest_load_initial_conftests(config)

    assert config.getini("cache_dir") == project_cache.name
    assert (project_cache / "value").read_text() == "200"
    if mode != "override-fails":
        assert not run_cache.exists()


def test_retry_omits_regression_output_for_writable_test(project):
    sample = project((REGRESSION, None), (REPAIRED_BOTH, None))
    sample.run(2)
    first, retry = (record["attempts"][0] for record in sample.rounds)
    reason = f"previously passing test now fails in isolation: {OBSERVER}"
    prompt = sample.prompts[1]
    assert first["reason"] == reason and reason in prompt
    assert DIAGNOSTIC not in prompt
    assert "rejection_output" not in first
    assert "The test named in the rejection passed before your previous change." in prompt
    assert "Fix the implementation so the test continues to pass." in prompt
    assert "Do not rewrite its expectations to match the behavior introduced by your change." in prompt
    assert retry["decision"] == "accepted", retry


def test_retry_accepts_coordinated_source_and_test_change(project):
    updated_source = REPAIRED_BOTH.replace("response", "status_code")
    updated_tests = TESTS.replace("response", "status_code")
    sample = project((REGRESSION, None), (updated_source, updated_tests))
    sample.run(2)
    _, retry = sample.rounds
    [attempt] = retry["attempts"]
    assert attempt["decision"] == "accepted", attempt
    assert all(run.get("case") != OBSERVER for run in retry["runs"])
    assert sample.source.read_text() == updated_source
    assert sample.tests.read_text() == updated_tests


def test_retry_after_acceptance_omits_stale_rejection(project):
    sample = project((REGRESSION, None), (REPAIRED, None), (REPAIRED_BOTH, UPDATED_TESTS))
    sample.run(3)
    assert [record["attempts"][0]["decision"] for record in sample.rounds] == ["rejected", "accepted", "accepted"]
    assert OBSERVER in sample.prompts[1]
    assert OBSERVER not in sample.prompts[2]
    for record in sample.rounds[1:]:
        assert all(run.get("case") != OBSERVER for run in record["runs"])
    assert sample.tests.read_text() == UPDATED_TESTS
