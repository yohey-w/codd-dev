"""Exercise authenticity owner-first retry through the real implement stage and writer."""

import json
from pathlib import Path
import shlex
import subprocess
import sys
from dataclasses import replace

import pytest

from tests.greenfield.owner_first_baseline import main_reference, measure_both

from codd import ai_invoke, generator, implement_oracle, implement_oracle_scope, implementer
from codd.greenfield import pipeline as pipeline_module
from codd.greenfield.pipeline import GreenfieldPipeline, ImplementTaskRef, StageError
from codd.vb_marker_authenticity import build_authenticity_report
from codd.verifiable_behavior_audit import build_vb_coverage_audit


REGISTRY = Path("docs/test/test_strategy.md")
ORPHAN_TEST = """
# codd: covers vb=VB-99
def test_orphan():
    assert score(2) == 4
"""


def _test_body(task_id):
    number = ord(task_id) - ord("a") + 1
    return (
        f"def score(n):\n    return n + {number}\n\n\n"
        f"# codd: covers vb=VB-0{number}\n"
        f"def test_{task_id}():\n    assert score(1) == {number + 1}\n"
    )


def _test_path(task_id):
    return Path(f"tests/test_{task_id}.py")


def _write(project, path, text):
    path = project / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run_stage(tmp_path, monkeypatch, mode, *, enabled=True, rounds=2, failed=("a",), oracle_mode="supported"):
    project = tmp_path / "sample"
    events_file = tmp_path / "events.jsonl"
    tmp_path.mkdir(parents=True, exist_ok=True)
    events_file.touch()
    extra = Path("tests/fixtures/extra.fixture")
    _write(project, extra, "original\n")
    _write(project, ".codd/runtime.state", "initial session state\n")
    _write(project, "src/sample/__init__.py", "")
    _write(project, "tests/__init__.py", "")
    language = "" if oracle_mode == "legacy_absent" else "  language: python\n"
    _write(
        project,
        "codd/codd.yaml",
        f"project:\n  name: sample\n{language}"
        "ai_command: test-stub\n"
        "scan:\n  source_dirs: [src]\n  test_dirs: [tests]\n"
        f"greenfield:\n  vb_rework:\n    max_rounds: {rounds}\n"
        "implement:\n  python_name_lint: off\n"
        + (f"  owner_first_retry_on_copy: {str(enabled).lower()}\n" if enabled is not None else "")
        + ("  implement_oracle: false\n" if oracle_mode == "opt_out" else ""),
    )
    registry = (
        "# Test Strategy\n\n| VB | Description | Test |\n| --- | --- | --- |\n"
        "| VB-01 | Score A | a |\n| VB-02 | Score B | b |\n| VB-03 | Score C | c |\n"
    )
    _write(project, REGISTRY, registry)
    tasks = []
    for task_id in "abc":
        design = f"docs/test/{task_id}.md"
        path = _test_path(task_id)
        _write(project, design, f"# Test design {task_id}\n")
        _write(project, path, _test_body(task_id) + (ORPHAN_TEST if task_id in failed else ""))
        outputs = (path, extra.parent) if task_id == "a" else (path,)
        tasks.append(
            ImplementTaskRef(task_id, design, tuple(output.as_posix() for output in outputs), title=f"Score {task_id}")
        )

    def note(*values):
        with events_file.open("a", encoding="utf-8") as out:
            out.write(json.dumps(values) + "\n")

    def fake_ai(_command, prompt, *, project_root):
        task_id = next(task_id for task_id in "abc" if f"# Test design {task_id}" in prompt)
        path = _test_path(task_id)
        original = project_root == project
        note("write", str(project_root), task_id, "VB-99" in (project_root / path).read_text(), prompt)
        text = _test_body(task_id)
        if not original:
            if mode == "exception":
                raise RuntimeError("stub failure")
            if mode == "timeout":
                raise subprocess.TimeoutExpired("stub", 1)
            if mode == "unrepaired":
                text += ORPHAN_TEST + "\n# Changed but still invalid.\n"
            if mode == "coverage":
                text = text.replace("# codd: covers vb=VB-01\n", "")
            if mode == "oracle":
                text = "from sample.missing import unused\n\n" + text
            if mode == "extra":
                text += f"\n=== FILE: {extra.as_posix()} ===\naltered in retry\n"
        return f"=== FILE: {path.as_posix()} ===\n{text}"

    actual_oracle = implement_oracle._run_oracle_command

    def log_native_oracle(root, profile, spec, config):
        note(
            "oracle", str(root), (root / extra).read_text(), (root / REGISTRY).read_text(),
            (root / _test_path("a")).read_text(),
        )
        result = actual_oracle(root, profile, spec, config)
        if mode == "not_executed" and root != project:
            return replace(result, executed=False)
        if mode == "registry" and root != project:
            _write(root, REGISTRY, registry + "| VB-04 | Changed | d |\n")
            _write(
                root,
                _test_path("b"),
                _test_body("b") + "\n# codd: covers vb=VB-04\ndef test_added():\n    assert score(2) == 4\n",
            )
        return result

    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setattr(generator, "_invoke_ai_command", fake_ai)
    monkeypatch.setattr(
        ai_invoke, "_invoke_with_recovery", lambda *a, **k: pytest.fail("unexpected real AI invocation"),
    )
    popen = subprocess.Popen

    def offline_popen(command, *args, **kwargs):
        argv = shlex.split(command) if isinstance(command, str) else list(command)
        if argv[:3] == [sys.executable, "-m", "pytest"] and "--collect-only" in argv:
            return popen(command, *args, **kwargs)
        if Path(argv[0]).name == "git" and not {"fetch", "pull", "push", "clone", "ls-remote"}.intersection(argv):
            return popen(command, *args, **kwargs)
        pytest.fail(f"unexpected subprocess: {argv[0]}")

    monkeypatch.setattr(subprocess, "Popen", offline_popen)
    subprocess.run(["git", "init", "--quiet", "--template="], cwd=project, check=True)
    monkeypatch.setattr(implementer._legacy, "_implementation_steps_context", lambda **_: None)
    monkeypatch.setattr(implement_oracle, "_run_oracle_command", log_native_oracle)
    if mode == "orphan_observation":
        find_orphans = implement_oracle_scope.find_orphan_artifacts

        def broken_observer(index, root, **kwargs):
            if root != project:
                raise RuntimeError("stub owner observation failure")
            return find_orphans(index, root, **kwargs)

        monkeypatch.setattr(implement_oracle_scope, "find_orphan_artifacts", broken_observer)
    log = []
    pipeline = GreenfieldPipeline(task_lister=lambda _: tasks, echo=log.append, ai_command="test-stub")
    if mode == "scope_index":
        build_scope_index = pipeline._build_oracle_scope_index

        def missing_index(root, *args, **kwargs):
            return build_scope_index(root, *args, **kwargs) if root == project else None

        monkeypatch.setattr(pipeline, "_build_oracle_scope_index", missing_index)
    for name in (
        "_enforce_api_facade_coverage",
        "_enforce_deliverable_surface_exclusion",
        "_enforce_plan_intake_grounding",
        "_enforce_vb_coverage_closure",
        "_enforce_design_doc_task_closure",
        "_order_facade_dependencies",
    ):
        monkeypatch.setattr(pipeline, name, lambda _root, refs: refs)
    record = {"units": {task.task_id: "done" for task in tasks}}
    if rounds == 0:
        with pytest.raises(StageError, match="marker-authenticity gate failed"):
            pipeline._stage_implement(project, record, {})
    else:
        pipeline._stage_implement(project, record, {})
        profile = pipeline._resolve_layout_profile(project)
        assert build_authenticity_report(project, profile=profile, strict_observability=True).passed
        assert not build_vb_coverage_audit(project).uncovered_rows
    assert (project / extra).read_text() == "original\n"
    assert (project / REGISTRY).read_text() == registry
    assert (project / ".codd/runtime.state").read_text() == "initial session state\n"
    events = [json.loads(line) for line in events_file.read_text().splitlines()]
    writes = [event[1:] for event in events if event[0] == "write"]
    oracle = [event[1:] for event in events if event[0] == "oracle"]
    return project, writes, oracle, log


def observe_auth(root, patches, mode="clean", **options):
    if "enabled" in options and options["enabled"] in (None, False):
        _require_legacy_gate_signature(patches)
    project, writes, checks, messages = _run_stage(root, patches, mode, **options)
    return {"root": str(project), "writes": writes, "checks": checks, "messages": messages}


def _require_legacy_gate_signature(monkeypatch):
    gate = pipeline_module._enforce_stage_coverage_gate

    def legacy_gate(project_root, *, coverage_gate, echo, rerun, rerun_oracle, scope_resolver, authenticity_profile):
        return gate(
            project_root,
            coverage_gate=coverage_gate,
            echo=echo,
            rerun=rerun,
            rerun_oracle=rerun_oracle,
            scope_resolver=scope_resolver,
            authenticity_profile=authenticity_profile,
        )

    monkeypatch.setattr(pipeline_module, "_enforce_stage_coverage_gate", legacy_gate)


@pytest.mark.parametrize("enabled", [None, False])
def test_auth_uses_main_round_when_option_is_unset(tmp_path, main_reference, enabled):
    current, baseline = measure_both(tmp_path, main_reference, "auth", enabled=enabled)
    for actual, expected in ((current.writes, baseline.writes), (current.checks, baseline.checks),
                             (current.messages, baseline.messages)):
        assert json.dumps(actual).replace(str(current.root), "<project>") == (
            json.dumps(expected).replace(str(baseline.root), "<project>")
        )
    assert any("authenticity rework round 1/2" in message for message in current.messages)


def test_auth_zero_rounds_skips_trial(tmp_path, main_reference):
    current, baseline = measure_both(tmp_path, main_reference, "auth", rounds=0)
    assert current.writes == baseline.writes == []
    assert [Path(at) == current.root for at, *_ in current.checks] == [
        Path(at) == baseline.root for at, *_ in baseline.checks
    ]


@pytest.mark.parametrize("failed", [("a",), ("a", "b")])
def test_auth_adopts_only_owners_after_separate_project_judgment(tmp_path, monkeypatch, failed):
    project, writes, oracle, log = _run_stage(tmp_path, monkeypatch, "clean", failed=failed)
    assert tuple(task_id for _, task_id, *_ in writes) == failed
    work_root = Path(writes[0][0])
    assert {Path(root) for root, *_ in writes} == {work_root}
    assert len(oracle) == 3
    assert Path(oracle[-1][0]) == project
    assert oracle[-1][1:] == oracle[1][1:]
    judge_root = Path(oracle[1][0])
    assert len({project, work_root, judge_root}) == 3
    assert oracle[1][1] == "original\n"
    assert oracle[1][3] == (project / _test_path("a")).read_text()
    for task_id in "abc":
        published = (project / _test_path(task_id)).read_text()
        if task_id in failed:
            assert published.startswith("# @generated-by: codd implement")
            assert published.endswith(_test_body(task_id))
        else:
            assert published == _test_body(task_id)
    assert not any("authenticity rework round" in message for message in log)


@pytest.mark.parametrize(
    ("oracle_mode", "oracle_trace"),
    [("opt_out", "opted out by config"), ("legacy_absent", "no language declared")],
)
def test_auth_can_retry_when_native_oracle_is_skipped(tmp_path, monkeypatch, oracle_mode, oracle_trace):
    project, writes, oracle, log = _run_stage(tmp_path, monkeypatch, "clean", oracle_mode=oracle_mode)
    assert [(task_id, Path(root) == project) for root, task_id, *_ in writes] == [("a", False)]
    assert not oracle
    assert sum(oracle_trace in message for message in log) == 3
    assert any("marker authenticity OK after owner-first retry" in message for message in log)


@pytest.mark.parametrize(
    "mode",
    [
        "exception", "timeout", "extra", "unrepaired", "oracle", "registry", "coverage",
        "orphan_observation", "scope_index", "not_executed",
    ],
)
def test_auth_rejection_preserves_feedback_and_normal_round(tmp_path, mode, main_reference):
    current, baseline = measure_both(tmp_path, main_reference, "auth", mode=mode)
    normal = baseline.writes
    project, writes, oracle, log = current.root, current.writes, current.checks, current.messages
    assert [task_id for _, task_id, *_ in writes] == ["a", *(row[1] for row in normal)]
    assert Path(writes[0][0]) != project
    assert writes[1][2] is True
    feedback_header = "A previous test-authoring attempt was REJECTED"
    for actual, expected in zip(writes[1:], normal, strict=True):
        assert Path(actual[0]) == project
        assert actual[3].split(feedback_header)[1] == expected[3].split(feedback_header)[1]
    assert any("authenticity rework round 1/2" in message for message in log)
    if mode in {"exception", "timeout", "extra", "scope_index"}:
        assert [Path(root) for root, *_ in oracle] == [project, project]
    else:
        assert len(oracle) == 3
        assert Path(oracle[1][0]) not in {project, Path(writes[0][0])}


def test_auth_skips_trial_if_every_test_materializer_is_an_owner(tmp_path, main_reference):
    current, baseline = measure_both(tmp_path, main_reference, "auth", failed=tuple("abc"))
    assert [(Path(at) == current.root, task) for at, task, *_ in current.writes] == [
        (Path(at) == baseline.root, task) for at, task, *_ in baseline.writes
    ]
    assert [Path(at) == current.root for at, *_ in current.checks] == [
        Path(at) == baseline.root for at, *_ in baseline.checks
    ]
    assert any("authenticity rework round 1/2" in message for message in current.messages)


def test_auth_stops_rework_if_one_target_cannot_be_restored(tmp_path, monkeypatch):
    root = tmp_path / "sample"
    gate = GreenfieldPipeline._enforce_implement_oracle_gate
    write = Path.write_bytes

    def reject_live(pipeline, at, *args, **kwargs):
        if at == root and kwargs.get("allow_rework") is False:
            def fail_one_restore(path, data):
                if path == root / _test_path("b"):
                    raise OSError("restore write failed")
                return write(path, data)
            monkeypatch.setattr(Path, "write_bytes", fail_one_restore)
            raise StageError("live recheck failed")
        return gate(pipeline, at, *args, **kwargs)

    monkeypatch.setattr(GreenfieldPipeline, "_enforce_implement_oracle_gate", reject_live)
    with pytest.raises(StageError, match="restore.*tests/test_b.py"):
        _run_stage(tmp_path, monkeypatch, "clean", failed=("a", "b"))
    assert (root / _test_path("a")).read_text() == _test_body("a") + ORPHAN_TEST
    events = [json.loads(row) for row in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert all(Path(event[1]) != root for event in events if event[0] == "write")
