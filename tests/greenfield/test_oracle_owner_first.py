"""Greenfield oracle retry sequencing with a fixed implementation response."""

from pathlib import Path
import re
import shlex
import subprocess
import sys

import pytest

from tests.greenfield.owner_first_baseline import doctor_and_check, main_reference

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
    original = subprocess.Popen.__init__
    collection = shlex.join([sys.executable, '-m', 'pytest', 'tests', '--collect-only', '-q', '-p', 'no:cacheprovider'])

    def checked_popen(process, args, *positional, **kwargs):
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


def oracle_scenario(tmp_path, monkeypatch, scenario, *, enabled):
    project = OracleProject(tmp_path, monkeypatch, enabled=enabled,
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


def _normalized_feedback(text, root):
    return re.sub(r"\d+\.\d+s\b", "<duration>", text.replace(str(root), "<project>"))


@pytest.mark.parametrize("enabled", [None, False])
def test_oracle_owner_first_default_matches_main(tmp_path, monkeypatch, main_reference, enabled):
    project = OracleProject(tmp_path, monkeypatch, enabled=enabled)
    project.run()
    baseline = main_reference["oracle"][str(enabled)]
    assert project.events == baseline["events"]
    assert project.echo == baseline["echo"]
    assert [_normalized_feedback(f, project.root) for f in project.feedback] == [
        _normalized_feedback(f, baseline["root"]) for f in baseline["feedback"]
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
def test_oracle_owner_first_falls_back_with_original_feedback(tmp_path, monkeypatch, main_reference, response):
    project = OracleProject(tmp_path, monkeypatch, enabled=True, response=response)
    project.run()

    judge = [('certify', 'judge'), ('oracle', 'judge')] if response == 'unfixed' else []
    baseline = main_reference["oracle"]["None"]
    ordinary = baseline["events"]
    assert project.events == [
        *ordinary[:2], ('copy', 'docs/design/target.md'), *judge, *ordinary[2:],
    ], project.echo
    assert project.feedback[1] == project.feedback[2]
    assert [_normalized_feedback(f, project.root) for f in project.feedback[1:]] == [
        _normalized_feedback(f, baseline["root"]) for f in baseline["feedback"]
    ]
    assert 'pytest_collect_exit_2' in project.feedback[1]
    reruns = [line for line in project.echo if 're-running implementation' in line]
    assert len(reruns) == 1 and '(attempt 1/1, scope=broad)' in reruns[0]
    if response == 'timeout':
        assert any('rejected (P5: TimeoutExpired)' in line for line in project.echo)
    assert (project.root / TARGET).read_text().endswith(FIXED)
    assert (project.root / OTHER).read_text().endswith('VALUE = 2\n')


def test_oracle_owner_first_refuses_multiple_collection_failures(tmp_path, monkeypatch, main_reference):
    project = oracle_scenario(tmp_path, monkeypatch, "multiple", enabled=True)
    with pytest.raises(StageError):
        project.run()
    assert project.events == main_reference["oracle"]["multiple"]["events"]


def test_oracle_owner_first_refuses_mixed_source_and_collection_failures(tmp_path, monkeypatch, main_reference):
    project = oracle_scenario(tmp_path, monkeypatch, "mixed", enabled=True)
    project.run()
    assert project.events == main_reference["oracle"]["mixed"]["events"]


def test_oracle_owner_first_judge_enforces_unowned_artifacts(tmp_path, monkeypatch, main_reference):
    project = oracle_scenario(tmp_path, monkeypatch, "orphan", enabled=True)
    with pytest.raises(StageError, match='orphan-artifact gate'):
        project.run()

    ordinary = main_reference["oracle"]["orphan"]["events"]
    assert project.events == [
        *ordinary[:2], ('copy', 'docs/design/target.md'),
        ('certify', 'judge'), ('oracle', 'judge'), *ordinary[2:],
    ], project.echo


def test_oracle_owner_first_does_not_preempt_narrow_or_later_broad(tmp_path, monkeypatch, main_reference):
    project = oracle_scenario(tmp_path, monkeypatch, "narrow", enabled=True)
    project.run()

    assert project.events == main_reference["oracle"]["narrow"]["events"]


def test_oracle_owner_first_respects_single_attempt_budget(tmp_path, monkeypatch, main_reference):
    project = oracle_scenario(tmp_path, monkeypatch, "budget", enabled=True)
    with pytest.raises(StageError, match='test_not_collected'):
        project.run()

    assert project.events == main_reference["oracle"]["budget"]["events"]
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
    assert doctor_and_check(tmp_path, enabled) == main_reference["doctor"][str(enabled)]


def test_oracle_skips_owner_when_collection_also_reports_global_error(tmp_path, monkeypatch, main_reference):
    project = oracle_scenario(tmp_path, monkeypatch, "global_error", enabled=True)
    project.run()
    assert project.events == main_reference["oracle"]["global_error"]["events"]
    assert not any("owner-first retry: trying" in line for line in project.echo)
