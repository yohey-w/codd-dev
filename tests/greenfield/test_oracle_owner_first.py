"""Greenfield oracle retry sequencing with a fixed implementation response."""

from pathlib import Path
import re
import shlex
import subprocess
import sys

import pytest

from tests.greenfield import owner_first_baseline
from tests.greenfield.owner_first_baseline import main_reference, measure_both

from codd import implement_oracle, implementer
from codd.deployment.providers.ai_command import SubprocessAiCommand
from codd.greenfield.pipeline import GreenfieldPipeline, ImplementTaskRef, StageError
from codd.implement_oracle_scope import OracleRerunScope


BROKEN = 'raise ValueError("broken on collection")\n'
FIXED = 'def test_fixed():\n    assert True\n'
TARGET = 'tests/test_broken.py'
OTHER = 'src/app/core.py'


@pytest.fixture(autouse=True)
def guard_subprocesses(monkeypatch):
    _guard_oracle_processes(monkeypatch, allow_comparison=True)


def _guard_oracle_processes(monkeypatch, allow_comparison=False):
    original = subprocess.Popen.__init__
    collection = shlex.join([sys.executable, '-m', 'pytest', 'tests', '--collect-only', '-q', '-p', 'no:cacheprovider'])

    def checked_popen(process, args, *positional, **kwargs):
        if (allow_comparison and not kwargs.get("shell") and isinstance(args, list)
                and args[:4] == [sys.executable, "-I", "-B", owner_first_baseline.__file__]):
            return original(process, args, *positional, **kwargs)
        if args != collection:
            assert not kwargs.get('shell') and isinstance(args, (tuple, list)), args
            assert args and Path(args[0]).name == 'git', args
            command = list(args[1:])
            assert (
                (len(command) == 3 and command[:2] == ['init', '--quiet'])
                or command == ['rev-parse', '--show-toplevel']
                or (len(command) == 5 and command[:4] == ['config', '--includes', '--name-only', '--get-regexp'])
            ), args
        return original(process, args, *positional, **kwargs)

    monkeypatch.setattr(subprocess.Popen, '__init__', checked_popen)


def _write(root: Path, path: str, content: str) -> None:
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding='utf-8')


class OracleProject:
    def __init__(self, tmp_path, monkeypatch, *, enabled=None, response='fix', orphan_mode='warn', max_attempts=2):
        self.root = tmp_path / 'app'
        self.events = []
        self.echo = []
        self.feedback = []
        self.results = []
        flag = '' if enabled is None else f'  owner_first_retry_on_copy: {str(enabled).lower()}\n'
        _write(self.root, 'codd/codd.yaml',
               'project:\n  name: app\n  language: python\n'
               'ai_command: fixed-response\nai_commands:\n  implement: fixed-response\n'
               f'implement:\n  oracle_max_attempts: {max_attempts}\n  no_usable_file_retries: 0\n'
               f'  orphan_artifact_gate: {orphan_mode}\n{flag}')
        _write(self.root, 'docs/design/target.md', '# Target design\n\nRepair the test.\n')
        _write(self.root, 'docs/design/other.md', '# Other design\n\nKeep the source.\n')
        _write(self.root, TARGET, BROKEN)
        _write(self.root, OTHER, 'VALUE = 1\n')
        subprocess.run(['git', 'init', '--quiet', str(self.root)], check=True)
        self.tasks = [
            ImplementTaskRef('target', 'docs/design/target.md', (TARGET,), expected_outputs=(TARGET,)),
            ImplementTaskRef('other', 'docs/design/other.md', (OTHER,), expected_outputs=(OTHER,)),
        ]
        self.pipeline = GreenfieldPipeline(echo=self.echo.append)

        original_invoke = implementer.implement_tasks
        original_run = implement_oracle._run_oracle_command
        original_certify = implement_oracle.certify_oracle_scope
        original_gate = implement_oracle.run_implement_oracle_gate

        def implement(root, **kwargs):
            location = 'real' if root == self.root else 'copy'
            self.events.append((location, kwargs['design']))
            self.feedback.append(kwargs['feedback'])
            assert (self.root / TARGET).read_text() == BROKEN or location == 'real'
            return original_invoke(root, **kwargs)

        def fake_ai(_command, prompt, *, project_root):
            path = TARGET if 'Repair the test.' in prompt else OTHER
            if path == OTHER:
                return f'=== FILE: {OTHER} ===\nVALUE = 2\n'
            if project_root != self.root:
                assert str(self.root) not in prompt
                if response == 'exception':
                    raise RuntimeError('attempt failed')
                if response == 'timeout':
                    raise subprocess.TimeoutExpired('fixed-response', timeout=1)
                if response == 'extra':
                    _write(project_root, 'pyproject.toml', '[tool.pytest.ini_options]\naddopts = "--ignore=tests/test_broken.py"\n')
                if response == 'unfixed':
                    return f'=== FILE: {TARGET} ===\n{BROKEN}# still broken\n'
            return f'=== FILE: {TARGET} ===\n{FIXED}'

        def unexpected_ai(*_args, **_kwargs):
            raise AssertionError('Only the fixed implementation response may be invoked')

        def run(root, profile, spec, config):
            self.events.append(('oracle', 'real' if root == self.root else 'judge'))
            return original_run(root, profile, spec, config)

        def certify(root, profile, spec):
            self.events.append(('certify', 'real' if root == self.root else 'judge'))
            return original_certify(root, profile, spec)

        def gate(*args, **kwargs):
            result = original_gate(*args, **kwargs)
            self.results.append(result)
            return result

        monkeypatch.setenv('PYTHONDONTWRITEBYTECODE', '1')
        monkeypatch.setenv('PYTEST_DISABLE_PLUGIN_AUTOLOAD', '1')
        monkeypatch.setenv('PYTEST_ADDOPTS', '')
        monkeypatch.setattr(SubprocessAiCommand, 'invoke', unexpected_ai)
        monkeypatch.setitem(original_invoke.__globals__, '_load_or_derive_implementation_steps', lambda *_: [])
        monkeypatch.setattr(implementer.generator_module, '_invoke_ai_command', fake_ai)
        monkeypatch.setattr(implementer, 'implement_tasks', implement)
        monkeypatch.setattr(implement_oracle, '_run_oracle_command', run)
        monkeypatch.setattr(implement_oracle, 'certify_oracle_scope', certify)
        monkeypatch.setattr(implement_oracle, 'run_implement_oracle_gate', gate)

    def run(self):
        self.pipeline._enforce_implement_oracle_gate(self.root, self.tasks, {})


def oracle_scenario(tmp_path, monkeypatch, scenario, *, enabled, response="fix"):
    project = OracleProject(tmp_path, monkeypatch, enabled=enabled, response=response,
                            max_attempts={"narrow": 4, "budget": 1}.get(scenario, 2),
                            orphan_mode="enforce" if scenario == "orphan" else "warn")
    if scenario == "multiple":
        _write(project.root, 'tests/test_notes.txt', '>>> raise ValueError("doctest failed collection")\n')
        _write(project.root, 'tests/conftest.py',
               'def pytest_collect_file(file_path, parent):\n'
               '    if file_path.suffix == ".txt":\n'
               '        import pytest\n'
               '        class BadCollector(pytest.File):\n'
               '            def collect(self):\n'
               '                raise ValueError("doctest failed collection")\n'
               '        return BadCollector.from_parent(parent, path=file_path)\n')
    elif scenario == "mixed":
        _write(project.root, OTHER, 'from .missing import name\n')
    elif scenario == "orphan":
        _write(project.root, 'web/orphan.js', 'const value = 3;\n')
    elif scenario == "narrow":
        scope = OracleRerunScope(
            rung='narrow', task_ids=('other',), allowed_paths=(OTHER,),
        )
        outcomes = iter(((scope, False), (None, True)))
        monkeypatch.setattr(implement_oracle, '_derive_scope_for_rung', lambda **_kwargs: next(outcomes))
    elif scenario == "global_error":
        _write(project.root, "tests/conftest.py",
               "from pathlib import Path\n"
               "def pytest_sessionfinish(session, exitstatus):\n"
               "    if 'VALUE = 2' not in Path('src/app/core.py').read_text():\n"
               "        term = session.config.pluginmanager.get_plugin('terminalreporter')\n"
               "        term.write_line('ERROR: global fixture schema is inconsistent')\n"
               "        session.exitstatus = 1\n")
    return project


def observe_oracle(root, patches, scenario="default", **options):
    _guard_oracle_processes(patches)
    project = oracle_scenario(root, patches, scenario, **options)
    failures = {"multiple": None, "orphan": "orphan-artifact gate", "budget": "test_not_collected"}
    if scenario in failures:
        with pytest.raises(StageError, match=failures[scenario]):
            project.run()
    else:
        project.run()
    return {"root": str(project.root), "events": project.events, "echo": project.echo, "feedback": project.feedback}


def _normalized_feedback(text, root):
    return re.sub(r"\d+\.\d+s\b", "<duration>", text.replace(str(root), "<project>"))


@pytest.mark.parametrize("enabled", [None, False])
def test_oracle_owner_first_default_matches_main(tmp_path, main_reference, enabled):
    project, baseline = measure_both(tmp_path, main_reference, "oracle", enabled=enabled)
    assert project.events == baseline.events
    assert project.echo == baseline.echo
    assert [_normalized_feedback(f, project.root) for f in project.feedback] == [
        _normalized_feedback(f, baseline.root) for f in baseline.feedback
    ]
    assert project.feedback[0] == project.feedback[1]
    assert (project.root / TARGET).read_text().endswith(FIXED)
    assert (project.root / OTHER).read_text().endswith('VALUE = 2\n')


def test_oracle_owner_first_accepts_one_file_with_original_manifest_semantics(tmp_path, monkeypatch):
    project = OracleProject(tmp_path, monkeypatch, enabled=True)
    _write(project.root, 'web/orphan.js', 'const value = 3;\n')
    _write(project.root, 'harness.js', 'const value = 4;\n')
    manifests = project.pipeline._oracle_manifest_paths
    monkeypatch.setattr(project.pipeline, '_oracle_manifest_paths', lambda root: (*manifests(root), 'harness.js'))
    project.run()

    assert project.events == [
        ('certify', 'real'), ('oracle', 'real'), ('copy', 'docs/design/target.md'),
        ('certify', 'judge'), ('oracle', 'judge'), ('certify', 'real'), ('oracle', 'real'),
    ], project.echo
    assert project.results[-1].passed
    assert project.results[-1].orphan_artifacts == ['web/orphan.js']
    assert (project.root / TARGET).read_text().endswith(FIXED)
    assert (project.root / OTHER).read_text() == 'VALUE = 1\n'
    assert not (project.root / '.codd/audit/reference_resolution.jsonl').exists()


@pytest.mark.parametrize('response', ['unfixed', 'exception', 'timeout', 'extra'])
def test_oracle_owner_first_falls_back_with_original_feedback(tmp_path, main_reference, response):
    project, baseline = measure_both(tmp_path, main_reference, "oracle", enabled=True, response=response)

    judge = [('certify', 'judge'), ('oracle', 'judge')] if response == 'unfixed' else []
    ordinary = baseline.events
    assert project.events == [
        *ordinary[:2], ('copy', 'docs/design/target.md'), *judge, *ordinary[2:],
    ], project.echo
    assert project.feedback[1] == project.feedback[2]
    assert [_normalized_feedback(f, project.root) for f in project.feedback[1:]] == [
        _normalized_feedback(f, baseline.root) for f in baseline.feedback
    ]
    assert 'pytest_collect_exit_2' in project.feedback[1]
    reruns = [line for line in project.echo if 're-running implementation' in line]
    assert len(reruns) == 1 and '(attempt 1/1, scope=broad)' in reruns[0]
    if response == 'timeout':
        assert any('rejected (P5: TimeoutExpired)' in line for line in project.echo)
    assert (project.root / TARGET).read_text().endswith(FIXED)
    assert (project.root / OTHER).read_text().endswith('VALUE = 2\n')


def test_oracle_owner_first_refuses_multiple_collection_failures(tmp_path, main_reference):
    project, baseline = measure_both(tmp_path, main_reference, "oracle", scenario="multiple", enabled=True)
    assert project.events == baseline.events


def test_oracle_owner_first_refuses_mixed_source_and_collection_failures(tmp_path, main_reference):
    project, baseline = measure_both(tmp_path, main_reference, "oracle", scenario="mixed", enabled=True)
    assert project.events == baseline.events


def test_oracle_owner_first_judge_enforces_unowned_artifacts(tmp_path, main_reference):
    project, baseline = measure_both(tmp_path, main_reference, "oracle", scenario="orphan", enabled=True)

    ordinary = baseline.events
    assert project.events == [
        *ordinary[:2], ('copy', 'docs/design/target.md'),
        ('certify', 'judge'), ('oracle', 'judge'), *ordinary[2:],
    ], project.echo


def test_oracle_owner_first_does_not_preempt_narrow_or_later_broad(tmp_path, main_reference):
    project, baseline = measure_both(tmp_path, main_reference, "oracle", scenario="narrow", enabled=True)

    assert project.events == baseline.events


def test_oracle_owner_first_respects_single_attempt_budget(tmp_path, main_reference):
    project, baseline = measure_both(tmp_path, main_reference, "oracle", scenario="budget", enabled=True)

    assert project.events == baseline.events
    assert any('FAILED after 1 attempt(s)' in line for line in project.echo)
    assert (project.root / TARGET).read_text() == BROKEN


def test_oracle_refuses_two_findings_in_the_same_file(tmp_path):
    target = 'src/unit.data'
    _write(tmp_path, target, 'broken')
    findings = [
        implement_oracle.ImplementOracleFinding(
            implement_oracle.EVIDENCE_MISSING_SYMBOL, name, f'Missing {name}', target,
        )
        for name in ('first', 'second')
    ]
    result = implement_oracle.ImplementOracleResult(
        passed=False, executed=True, command='check', findings=findings,
    )
    assert implement_oracle._owner_first_failure_path(result, tmp_path) is None


@pytest.mark.parametrize("enabled", [None, False])
def test_doctor_and_check_preserve_open_implement_settings(tmp_path, main_reference, enabled):
    current, baseline = measure_both(tmp_path, main_reference, "doctor", enabled=enabled)
    assert current.results == baseline.results


def test_oracle_skips_owner_when_collection_also_reports_global_error(tmp_path, main_reference):
    project, baseline = measure_both(tmp_path, main_reference, "oracle", scenario="global_error", enabled=True)
    assert project.events == baseline.events
    assert not any("owner-first retry: trying" in line for line in project.echo)


@pytest.mark.parametrize("report", ["UsageError", "unknown", "no_total", "two_errors", "extra_summary"])
def test_oracle_requires_a_complete_single_collection_failure(tmp_path, monkeypatch, report):
    project = OracleProject(tmp_path, monkeypatch, enabled=True)
    run = subprocess.run

    def collect(command, *args, **kwargs):
        result = run(command, *args, **kwargs)
        if isinstance(command, str) and "--collect-only" in command and result.returncode:
            lines = result.stdout.rstrip().splitlines()
            if report in {"UsageError", "unknown"}:
                lines.insert(0, f"{report}: global fixture schema is inconsistent")
            elif report == "no_total":
                lines.pop()
            elif report == "two_errors":
                lines[-1] = lines[-1].replace("1 error", "2 errors")
            else:
                lines.extend(["", "another check did not finish"])
            result.stdout = "\n".join(lines) + "\n"
        return result

    monkeypatch.setattr(subprocess, "run", collect)
    project.run()
    assert [where for where, _ in project.events if where in {"copy", "real"}] == ["real", "real"]
    assert not any("owner-first retry: trying" in line for line in project.echo)


def test_oracle_requires_failure_count_even_with_a_located_finding(tmp_path):
    _write(tmp_path, TARGET, BROKEN)
    result = implement_oracle.ImplementOracleResult(
        passed=False, executed=True, command="check", failed_paths=[TARGET],
        findings=[implement_oracle.ImplementOracleFinding(
            implement_oracle.EVIDENCE_OTHER, "invalid", "Invalid input", TARGET,
        )],
    )
    assert implement_oracle._owner_first_failure_path(result, tmp_path) is None


@pytest.mark.parametrize("timeout", [False, True])
def test_oracle_skips_a_counted_error_if_another_tool_failed_without_a_finding(tmp_path, monkeypatch, timeout):
    from codd.languages.adapters import oracle_python

    project = OracleProject(tmp_path, monkeypatch, enabled=True)
    run = subprocess.run

    def commands(command, *args, **kwargs):
        if isinstance(command, str) and command.startswith("ruff check"):
            if timeout:
                raise subprocess.TimeoutExpired(command, 1)
            return subprocess.CompletedProcess(command, 1, stdout="invalid configuration", stderr="")
        return run(command, *args, **kwargs)

    monkeypatch.setattr(oracle_python, "_which", lambda name: "ruff" if name == "ruff" else None)
    monkeypatch.setattr(subprocess, "run", commands)
    project.run()
    assert [where for where, _ in project.events if where in {"copy", "real"}] == ["real", "real"]
