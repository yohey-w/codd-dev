"""Regression cases from the independent feedback review."""

import os
import shlex
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest
import yaml

from codd.greenfield import implement_test_feedback as feedback_engine
from codd.languages import verify_executor
from codd.languages.adapters.runner_report import CTestJunitReportAdapter
from codd.languages.registry import default_adapter_registry

from .test_implement_test_feedback import APP, FIXED, HELPER, TEST_FILE, TESTS, feedback, project_factory


@pytest.fixture(autouse=True)
def _running_python_first_on_path(monkeypatch):
    # Commands here spell the interpreter as `python`; resolve it to the running one,
    # so the tests do not depend on what the host puts on PATH.
    monkeypatch.setenv("PATH", os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", ""))


_MANIFEST = (
    b'[project]\nname = "stub-app"\nversion = "0.1.0"\n\n'
    b'[tool.pytest.ini_options]\ntestpaths = ["checks"]\npythonpath = ["src"]\n'
)


def _trace(path):
    return path.read_text(encoding="utf-8").splitlines()


def _round(root):
    return feedback(root)["rounds"][0]


@pytest.mark.parametrize("interrupt", [False, True])
def test_restore_file_replaced_by_runtime_directory(project_factory, monkeypatch, interrupt):
    pipeline, root, trace, _ = project_factory(
        fixes={"case": {"files": {TEST_FILE: FIXED}, "direct": {
            "settings": None, "settings/generated.pyc": "candidate",
        }}},
    )
    settings = root / "settings"
    settings.write_bytes(b"original\r\n")
    if interrupt:
        rerun = pipeline._reimplement_tasks

        def stop_after_write(*args, **kwargs):
            rerun(*args, **kwargs)
            raise KeyboardInterrupt

        with monkeypatch.context() as patch:
            patch.setattr(pipeline, "_reimplement_tasks", stop_after_write)
            with pytest.raises(KeyboardInterrupt):
                pipeline.run(root)
        assert (settings / "generated.pyc").is_file()
        assert _round(root)["attempts"][0]["decision"] == "started"

    result = pipeline.run(root, resume=interrupt)
    assert result.status == "success", result.error
    assert settings.read_bytes() == b"original\r\n"
    [attempt] = _round(root)["attempts"]
    assert attempt["decision"] == "rejected"
    assert ("interrupted" if interrupt else "write outside declared outputs") in attempt["reason"]
    assert _trace(trace).count("feedback case") == 1


@pytest.mark.parametrize("audit_failure", [None, "coverage"])
def test_environment_write_discards_and_reprovisions(project_factory, audit_failure):
    asset = ".venv/runtime-asset.txt"
    pipeline, root, trace, _ = project_factory(
        fixes={"case": {"files": {TEST_FILE: FIXED}, "direct": {asset: "candidate"}}},
        verdict_failure=audit_failure,
    )
    runtime = root / asset
    runtime.parent.mkdir()
    runtime.write_bytes(b"original\r\n")
    provision = pipeline._provision_env
    seen = []

    def provision_clean_environment(project_root):
        seen.append(runtime.read_bytes() if runtime.exists() else None)
        if not runtime.exists():
            runtime.parent.mkdir()
            runtime.write_bytes(b"reprovisioned")
        provision(project_root)

    pipeline._provision_env = provision_clean_environment
    result = pipeline.run(root)
    assert result.status == "success", result.error
    [attempt] = _round(root)["attempts"]
    assert attempt["decision"] == "rejected"
    assert "test environment changed" in attempt["reason"]
    assert seen == [b"original\r\n", None, b"reprovisioned"]
    assert runtime.read_bytes() == b"reprovisioned"
    assert "[7][1]" in (root / TEST_FILE).read_text()
    assert _trace(trace)[-3:] == ["provision", "verify", "suite"]


def test_candidate_test_write_is_restored_before_each_run_and_round_end(project_factory):
    candidate = "from pathlib import Path\n\n" + FIXED.replace(
        "    actual = [7][0]",
        "    settings = Path('settings.txt')\n"
        "    assert settings.read_bytes() == b'original\\r\\n'\n"
        "    settings.write_text('candidate')\n"
        "    actual = [7][0]",
    )
    pipeline, root, _, _ = project_factory(fixes={"case": {"files": {TEST_FILE: candidate}}})
    settings = root / "settings.txt"
    settings.write_bytes(b"original\r\n")
    verify = pipeline.verify_runner

    def observe(project_root, **kwargs):
        assert settings.read_bytes() == b"original\r\n"
        return verify(project_root, **kwargs)

    pipeline.verify_runner = observe
    result = pipeline.run(root)
    assert result.status == "success", result.error
    [attempt] = _round(root)["attempts"]
    assert attempt["decision"] == "accepted"
    assert attempt["suite_after"] == attempt["isolated_after"] == 0
    assert "[7][0]" in (root / TEST_FILE).read_text()


def test_round_cleanup_keeps_accepted_output_and_restores_suite_side_effects(project_factory):
    side_effect = (
        "\ndef test_settings():\n"
        "    Path('settings.txt').write_bytes(b'test side effect')\n"
        "    Path('generated').mkdir(exist_ok=True)\n"
        "    Path('generated/transient.pyc').touch()\n"
        "    assert Path('settings.txt').read_bytes() == b'test side effect'\n\n"
    )
    imports = "from pathlib import Path\n\n"
    pipeline, root, _, _ = project_factory(
        initial={"case": {TEST_FILE: imports + TESTS + side_effect}},
        fixes={"case": {"files": {TEST_FILE: imports + FIXED + side_effect}}},
    )
    settings = root / "settings.txt"
    settings.write_bytes(b"original\r\n")

    def observe(project_root, **kwargs):
        assert settings.read_bytes() == b"original\r\n"
        assert not (root / "generated").exists()
        return "verified"

    pipeline.verify_runner = observe
    result = pipeline.run(root)
    assert result.status == "success", result.error
    [attempt] = _round(root)["attempts"]
    assert attempt["decision"] == "accepted"
    assert "[7][0]" in (root / TEST_FILE).read_text()


@pytest.mark.parametrize("decision", ["accepted", "rejected"])
def test_round_cleanup_preserves_provisioned_dependencies(project_factory, decision):
    pipeline, root, _, _ = project_factory(
        verdict_failure="coverage" if decision == "rejected" else None,
    )
    manifest = root / "pyproject.toml"
    prepared = _MANIFEST.replace(b'\n\n', b'\ndependencies = ["test-asset"]\n\n', 1)
    manifest.write_bytes(_MANIFEST)
    provision = pipeline._provision_env
    seen = []

    def prepare(project_root):
        provision(project_root)
        seen.append(manifest.read_bytes())
        if len(seen) == 1:
            manifest.write_bytes(prepared)

    pipeline._provision_env = prepare
    result = pipeline.run(root)

    assert result.status == "success", result.error
    [attempt] = _round(root)["attempts"]
    assert attempt["decision"] == decision
    assert seen == [_MANIFEST, prepared]
    assert manifest.read_bytes() == prepared


@pytest.mark.parametrize("resume", [False, True])
def test_reprovisioned_dependencies_survive_round_cleanup(project_factory, monkeypatch, resume):
    asset = ".venv/runtime-asset.txt"
    pipeline, root, trace, _ = project_factory(
        fixes={"case": {"files": {TEST_FILE: FIXED}, "direct": {asset: "candidate"}}},
    )
    manifest = root / "pyproject.toml"
    prepared = _MANIFEST.replace(b'\n\n', b'\ndependencies = ["test-asset"]\n\n', 1)
    reprovisioned = prepared.replace(b'"test-asset"]', b'"test-asset", "runtime-asset"]')
    manifest.write_bytes(_MANIFEST)
    runtime = root / asset
    provision = pipeline._provision_env
    seen = []

    def prepare(project_root):
        provision(project_root)
        seen.append((manifest.read_bytes(), runtime.exists()))
        if len(seen) in (1, 2):
            manifest.write_bytes(prepared if len(seen) == 1 else reprovisioned)
            runtime.parent.mkdir(exist_ok=True)
            runtime.write_text("prepared", encoding="utf-8")

    pipeline._provision_env = prepare
    if resume:
        rerun = pipeline._reimplement_tasks

        def interrupt(*args, **kwargs):
            rerun(*args, **kwargs)
            raise KeyboardInterrupt

        with monkeypatch.context() as patch:
            patch.setattr(pipeline, "_reimplement_tasks", interrupt)
            with pytest.raises(KeyboardInterrupt):
                pipeline.run(root)

    result = pipeline.run(root, resume=resume)

    assert result.status == "success", result.error
    [attempt] = _round(root)["attempts"]
    assert attempt["decision"] == "rejected"
    assert _trace(trace).count("feedback case") == 1
    assert seen == [(_MANIFEST, False), (prepared, False), (reprovisioned, True)]
    assert manifest.read_bytes() == reprovisioned
    assert runtime.read_text(encoding="utf-8") == "prepared"


@pytest.mark.parametrize("route", ["profile", "legacy"])
def test_feedback_suite_and_isolation_share_verifier_timeout(project_factory, monkeypatch, route):
    tests = "\n".join(f"def test_case_{i}():\n    actual = {i}\n    assert actual < 0\n" for i in range(3))
    profile_test = "tests/test_budget.py"
    outputs = (TEST_FILE, profile_test) if route == "profile" else (TEST_FILE,)
    pipeline, root, _, _ = project_factory(
        initial={"case": {TEST_FILE: tests}},
        owned={"case": outputs},
        timeout=29,
    )
    monkeypatch.setattr(verify_executor, "DEFAULT_VERIFY_TIMEOUT_SECONDS", 13)
    if route == "profile":
        # The built-in Python profile needs a report parser to enter the executor.
        monkeypatch.setitem(default_adapter_registry._adapters, (
            "runner_report", "pytest-junit-xml",
        ), CTestJunitReportAdapter())
        config = root / "codd/codd.yaml"
        settings = yaml.safe_load(config.read_text(encoding="utf-8"))
        del settings["verify"]["test_command"]
        settings["scan"]["test_dirs"].append("tests")
        config.write_text(yaml.safe_dump(settings), encoding="utf-8")
        (root / profile_test).write_text(tests, encoding="utf-8")

    expected = 13 if route == "profile" else 29
    # Two isolated runs exhaust the aggregate budget without wall-clock sleeps.
    elapsed = expected * 0.75
    clock = 0.0
    feedback_timeouts = []
    verify_timeouts = []
    monkeypatch.setattr(feedback_engine, "time", SimpleNamespace(monotonic=lambda: clock))
    popen = subprocess.Popen

    def launch(*args, **kwargs):
        process = popen(*args, **kwargs)
        env = kwargs.get("env") or {}
        if "CODD_TEST_FEEDBACK_CASE" in env:
            communicate = process.communicate

            def observe(timeout):
                nonlocal clock
                feedback_timeouts.append(timeout)
                result = communicate(timeout=timeout)
                if env["CODD_TEST_FEEDBACK_CASE"]:
                    clock += elapsed
                return result

            process.communicate = observe
        return process

    monkeypatch.setattr(feedback_engine, "subprocess", SimpleNamespace(
        Popen=launch, PIPE=subprocess.PIPE, TimeoutExpired=subprocess.TimeoutExpired,
    ))
    verify = pipeline.verify_runner
    run = subprocess.run

    def record_verify_timeout(project_root, **kwargs):
        def observe(*args, **kwargs):
            if str(kwargs.get("cwd")) == str(project_root):
                verify_timeouts.append(kwargs["timeout"])
            return run(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "run", observe)
            return verify(project_root, **kwargs)

    pipeline.verify_runner = record_verify_timeout
    result = pipeline.run(root)

    assert result.status == "success", result.error
    assert verify_timeouts == [expected]
    assert feedback_timeouts == [expected, expected, expected - elapsed]
    assert _round(root)["over_isolation_budget"] == 1


def test_removing_callable_assertion_rejects_candidate(project_factory):
    original = "import pytest\n\n" + TESTS.replace(
        "    actual = [7][1]", '    pytest.raises(ValueError, int, "not an integer")\n    actual = [7][1]',
    )
    pipeline, root, _, _ = project_factory(initial={"case": {TEST_FILE: original}})
    result = pipeline.run(root)
    assert result.status == "success", result.error
    [attempt] = _round(root)["attempts"]
    assert attempt["decision"] == "rejected"
    assert "assertions" in attempt["reason"]
    assert "pytest.raises" in (root / TEST_FILE).read_text(encoding="utf-8")


@pytest.mark.parametrize("uses_source", [False, True])
def test_probe_preserves_explicit_pythonpath(project_factory, uses_source):
    command = "PYTHONPATH=src python -m pytest checks -q"
    initial = {TEST_FILE: TESTS}
    fixed = FIXED
    if uses_source:
        imports = "from stub_app import subject\n\n"
        initial = {TEST_FILE: imports + TESTS, APP: "value = 7\n"}
        fixed = imports + FIXED
    pipeline, root, trace, _ = project_factory(
        command=command, initial={"case": initial},
        fixes={"case": {"files": {TEST_FILE: fixed}}},
    )
    result = pipeline.run(root)
    assert result.status == "success", result.error
    record = _round(root)
    assert "skipped" not in record
    assert record["runs"][0]["command"] == command
    assert record["runs"][0]["executed_cases"] == 2
    assert record["attempts"][0]["decision"] == "accepted"
    assert "verify" in _trace(trace)


@pytest.mark.parametrize("both_fail", [False, True])
def test_sequential_runner_launches_contribute_all_failures(project_factory, both_fail):
    command = shlex.join((sys.executable, "-m", "pytest", "-q"))
    second = "checks/test_second.py"
    pipeline, root, _, _ = project_factory(
        command=f"{command} {TEST_FILE}; {command} {second}",
        initial={"case": {TEST_FILE: TESTS if both_fail else FIXED, second: TESTS}},
        fixes={"case": {"files": {TEST_FILE: FIXED, second: FIXED}}},
        owned={"case": (TEST_FILE, second)},
    )
    result = pipeline.run(root)
    assert result.status == "success", result.error
    record = _round(root)
    assert "skipped" not in record
    assert record["runs"][0]["executed_cases"] == 4
    failure_count = 2 if both_fail else 1
    assert record["runs"][0]["failures_and_errors"] == failure_count
    [attempt] = record["attempts"]
    assert (attempt["decision"], attempt["isolated_before"], attempt["isolated_after"]) == (
        "accepted", failure_count, 0,
    )


def test_external_import_failure_in_child_skips_feedback(project_factory):
    tests = textwrap.dedent("""\
        import subprocess
        import sys

        def test_subject():
            result = subprocess.run(
                [sys.executable, "-c", "import unavailable_example_dependency"],
                check=True, capture_output=True, text=True,
            )
            assert result.returncode == 0
        """)
    pipeline, root, trace, _ = project_factory(initial={"case": {TEST_FILE: tests}}, fixes={})
    result = pipeline.run(root)
    assert result.status == "success", result.error
    assert _round(root)["attempts"] == []
    assert "external import could not be loaded" in _round(root)["skipped"]
    assert "unavailable_example_dependency" in _round(root)["skipped"]
    assert "feedback case" not in _trace(trace)


def test_wrapped_application_exception_has_no_test_owner(project_factory):
    tests = textwrap.dedent("""\
        from stub_app import subject

        def test_subject():
            try:
                result = subject.value()
            except ValueError as error:
                raise RuntimeError("could not read value") from error
            assert result == 7
        """)
    pipeline, root, trace, _ = project_factory(
        initial={"case": {TEST_FILE: tests}, "application": {APP: "def value():\n    raise ValueError('app fault')\n"}},
        owned={"case": (TEST_FILE,), "application": (APP,)},
        fixes={},
    )
    result = pipeline.run(root)
    assert result.status == "success", result.error
    assert _round(root)["runs"][0]["failures_and_errors"] == 1
    assert _round(root)["attempts"] == []
    assert "feedback case" not in _trace(trace)


def test_isolation_does_not_import_other_test_modules(project_factory):
    other_file = "checks/test_other.py"
    primary = (
        "import os\n\n"
        "def test_subject():\n"
        "    actual = 2 if 'ITFR_OTHER_TEST_COLLECTED' in os.environ else 1\n"
        "    assert actual == 1\n"
    )
    other = (
        "import os\n\n"
        "os.environ['ITFR_OTHER_TEST_COLLECTED'] = 'yes'\n\n"
        "def test_other():\n"
        "    assert 1 == 1\n"
    )
    pipeline, root, trace, _ = project_factory(
        initial={"case": {TEST_FILE: primary, other_file: other}},
        owned={"case": (TEST_FILE, other_file)}, fixes={},
    )
    result = pipeline.run(root)
    assert result.status == "success", result.error
    record = _round(root)
    assert record["runs"][0]["failures_and_errors"] == 1
    assert record["passed_in_isolation"] == 1
    assert record["attempts"] == []
    assert f"isolate {TEST_FILE}::test_subject" in _trace(trace)


def test_remaining_failure_is_isolated_after_helper(project_factory):
    tests = textwrap.dedent("""\
        from checks.support import helpers

        def test_helper():
            actual = helpers.value()
            assert actual == 7

        def test_case_depends_on_collection(request):
            actual = len(request.session.items) if helpers.FIXED else 2
            assert actual == 1
        """)
    pipeline, root, trace, _ = project_factory(
        initial={
            "case": {TEST_FILE: tests},
            "helper": {HELPER: "FIXED = False\ndef value():\n    return [7][1]\n"},
        },
        fixes={"helper": {"files": {HELPER: "FIXED = True\ndef value():\n    return 7\n"}}},
        owned={"case": (TEST_FILE,), "helper": (HELPER,)},
    )
    result = pipeline.run(root)
    assert result.status == "success", result.error
    record = _round(root)
    [helper] = record["attempts"]
    assert (helper["owner"], helper["decision"]) == ("helper", "accepted")
    events = _trace(trace)
    isolate = f"isolate {TEST_FILE}::test_case_depends_on_collection"
    assert events.count(isolate) == 1
    assert events.index("feedback helper") < events.index(isolate)
    assert [run["purpose"] for run in record["runs"][-2:]] == ["suite after helper decision", "isolate"]
    assert record["passed_in_isolation"] == 1


def test_explicit_null_uses_one_feedback_round(project_factory):
    pipeline, root, trace, _ = project_factory()
    config = root / "codd/codd.yaml"
    config.write_text(config.read_text(encoding="utf-8").replace(
        "implement_test_feedback: true", "implement_test_feedback: true\n  implement_test_feedback_rounds: null",
    ), encoding="utf-8")
    result = pipeline.run(root)
    assert result.status == "success", result.error
    assert len(feedback(root)["rounds"]) == 1
    assert _trace(trace).count("feedback case") == 1
