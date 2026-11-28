"""Output ownership through feedback routing, rerun and acceptance."""

from pathlib import Path

import pytest

from codd import generator
from codd.config import load_project_config
from codd.greenfield import implement_test_feedback as engine, pipeline as greenfield
from codd.implementer import (
    DesignContext,
    ImplementSpec,
    _build_traceability_comment,
    _prepend_traceability_comment,
)
from codd.languages.adapters.pytest_feedback import PytestFeedbackAdapter

from .conftest import make_stub_project
from .test_implement_test_feedback import DESIGN, FIXED, TESTS, TEST_FILE


SUPPORT = "checks/support"
HELPER = f"{SUPPORT}/helper.py"
GLOB = f"{SUPPORT}/*.py"


def _task(task_id, *outputs, reference=DESIGN):
    return greenfield.ImplementTaskRef(task_id, reference, expected_outputs=outputs, title=task_id)


def _generated(path, *outputs, body=TESTS):
    spec = ImplementSpec(DESIGN, list(outputs))
    design = DesignContext("design:core", Path(DESIGN), "# Core design\n")
    return _prepend_traceability_comment(path, _build_traceability_comment(design, spec, []), body)


@pytest.fixture
def run_feedback(tmp_path, monkeypatch):
    class Pipeline:
        ai_command = "unused-fake-command"
        _reimplement_tasks = greenfield.GreenfieldPipeline._reimplement_tasks

        def _provision_env(self, root):
            pass

        def _checkpoint(self, root):
            pass

        def echo(self, message):
            pass

        def _resolve_layout_profile(self, root):
            return None

        def _enforce_implement_oracle_gate(self, root, tasks, options, *, verdict_only, **kwargs):
            assert verdict_only

    def run(tasks, origin, content, *, extra_files=None, replacement=None,
            alias=None, transient_alias=False, test_file=TEST_FILE, passing_tests=(), direct_writes=None):
        pipeline = Pipeline()
        root = make_stub_project(tmp_path, pipeline.ai_command)
        files = {DESIGN: "# Core design\n", origin: content, **(extra_files or {})}
        for name, text in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        if alias is not None:
            link = root / alias
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(root / origin)

        def audit(project_root, *, rerun, rerun_oracle, **kwargs):
            assert rerun is None and rerun_oracle is None

        monkeypatch.setattr(greenfield, "_enforce_stage_coverage_gate", audit)
        monkeypatch.setattr(engine.VerifyRunner, "feedback_test_command",
                            lambda *args: (["fake-test"], root, {}, 30))
        prompts = []

        def generate(command, prompt, **kwargs):
            assert command == pipeline.ai_command
            prompts.append(prompt)
            for name, text in (direct_writes or {}).items():
                (root / name).write_text(text, encoding="utf-8")
            return f"=== FILE: {origin} ===\n{replacement or FIXED}"

        monkeypatch.setattr(generator, "_invoke_ai_command", generate)
        case_id = f"{test_file}::test_subject"
        state = {"rounds": [], "ignored": [".codd/**"], "environment_paths": []}
        feedback = engine._Feedback(
            pipeline, root, tasks(root) if callable(tasks) else tasks, state, {}, load_project_config(root),
            PytestFeedbackAdapter(), (Path("checks"),), [], (),
        )

        def run_tests(round_record, purpose, case=None, timeout=None, *, target=None):
            feedback.snapshot.restore(feedback.target if target is None else target)
            if case is not None and case in passing_tests:
                return engine._Suite({case: "passed"})
            path = root / origin
            failed = "[7][1]" in path.read_text(encoding="utf-8")
            if failed and transient_alias:
                path.unlink()
                path.symlink_to(root / DESIGN)
            cases = {case_id: "failed" if failed else "passed"}
            if case is None:
                cases.update(dict.fromkeys(passing_tests, "passed"))
            return engine._Suite(cases, [{
                "case": case_id, "file": test_file, "phase": "call", "origin": origin,
                "detail": "the example raised an index error",
            }] if failed else [])

        monkeypatch.setattr(feedback, "run_tests", run_tests)
        feedback.run(1)
        return root, state["rounds"][0]["attempts"], prompts

    return run


def test_provenance_owner_can_rewrite_and_keep_undeclared_test(run_feedback):
    root, attempts, prompts = run_feedback(
        [_task("author", "src/subject.py")], TEST_FILE,
        _generated(TEST_FILE, "src", "checks", body="#!/usr/bin/env python\n" + TESTS),
        extra_files={"checks/unrelated.py": "# This file is outside the feedback scope.\n"},
    )
    assert [item["owner"] for item in attempts] == ["author"]
    [attempt] = attempts
    assert attempt["decision"] == "accepted", attempt["reason"]
    assert (attempt["before"], attempt["after"], attempt["isolated_after"]) == (1, 0, 0)
    [prompt] = prompts
    assert f"Output paths: src/subject.py, {TEST_FILE}\n" in prompt
    assert "[7][0]" in (root / TEST_FILE).read_text(encoding="utf-8")


def test_glob_owner_can_rewrite_and_keep_helper(run_feedback):
    root, attempts, prompts = run_feedback(
        [_task("helpers", GLOB)], HELPER, TESTS,
        extra_files={f"{SUPPORT}/unrelated.txt": "outside\n"},
    )
    assert [item["owner"] for item in attempts] == ["helpers"]
    [attempt] = attempts
    assert attempt["decision"] == "accepted", attempt["reason"]
    assert (attempt["before"], attempt["after"]) == (1, 0)
    [prompt] = prompts
    assert f"Output paths: {HELPER}\n" in prompt
    assert "[7][0]" in (root / HELPER).read_text(encoding="utf-8")


@pytest.mark.parametrize("reason", [
    "shared-design", "no-header", "outside-scope", "missing-generator", "header-in-body",
    "wrong-comment-prefix", "no-design-owner", "ambiguous-declaration",
])
def test_provenance_with_missing_conditions_does_not_route(run_feedback, reason):
    tasks = [_task("author", "src/subject.py")]
    content = _generated(TEST_FILE, "src" if reason == "outside-scope" else "checks")
    if reason == "shared-design":
        tasks.append(_task("other", "src/other.py"))
    elif reason == "no-header":
        content = TESTS
    elif reason == "missing-generator":
        content = content.replace("# @generated-by: codd implement\n", "")
    elif reason == "header-in-body":
        content = TESTS + content
    elif reason == "wrong-comment-prefix":
        content = content.replace("# @", "// @")
    elif reason == "no-design-owner":
        content = content.replace(f"@design-node: {DESIGN}", "@design-node: docs/design/other.md")
    elif reason == "ambiguous-declaration":
        tasks = [_task("author", "checks/*.py"), _task("other", TEST_FILE)]
    _, attempts, prompts = run_feedback(tasks, TEST_FILE, content)
    assert attempts == prompts == []


@pytest.mark.parametrize("origin", [
    f"{SUPPORT}/not_a_match.txt", f"{SUPPORT}/nested/helper.py", f"{SUPPORT}/dir.py/helper.txt",
])
def test_glob_does_not_own_unmatched_files_or_directory_contents(run_feedback, origin):
    _, attempts, prompts = run_feedback([_task("helpers", GLOB)], origin, TESTS)
    assert attempts == prompts == []


def test_glob_does_not_own_the_target_of_a_matching_symlink(run_feedback):
    _, attempts, prompts = run_feedback(
        [_task("helpers", GLOB)], "checks/outside.py", TESTS, alias=HELPER,
    )
    assert attempts == prompts == []


def test_provenance_scope_does_not_grant_writes_to_sibling_tests(run_feedback):
    sibling = "checks/sibling.py"
    original = "# Handwritten helper.\n"
    root, attempts, _ = run_feedback(
        [_task("author", "src/subject.py")], TEST_FILE, _generated(TEST_FILE, "checks"),
        extra_files={sibling: original},
        replacement=f"{FIXED}\n=== FILE: {sibling} ===\n# Outside the effective scope.\n",
    )
    [attempt] = attempts
    assert attempt["decision"] == "rejected"
    assert f"write outside declared outputs: {sibling}" in attempt["reason"]
    assert (root / sibling).read_text(encoding="utf-8") == original


def test_provenance_owner_is_resolved_from_the_round_baseline(run_feedback):
    root, attempts, _ = run_feedback(
        [_task("author", "src/subject.py")], TEST_FILE, _generated(TEST_FILE, "checks"),
        transient_alias=True,
    )
    [attempt] = attempts
    assert attempt["decision"] == "accepted", attempt["reason"]
    assert "[7][0]" in (root / TEST_FILE).read_text(encoding="utf-8")


def test_feedback_preserves_a_declared_directory_that_does_not_exist_yet(run_feedback):
    new_directory = "checks/generated.out/"
    child = f"{new_directory}support.py"
    root, attempts, _ = run_feedback(
        [_task("author", TEST_FILE, new_directory)], TEST_FILE, TESTS,
        replacement=f"{FIXED}\n=== FILE: {child} ===\nVALUE = 7\n",
    )
    [attempt] = attempts
    assert attempt["decision"] == "accepted", attempt["reason"]
    assert "VALUE = 7" in (root / child).read_text(encoding="utf-8")


def test_banner_owned_file_is_writable_with_a_stale_task_declaration(run_feedback):
    root, attempts, prompts = run_feedback(
        [_task("author", "artifacts/verification.log")], TEST_FILE,
        _generated(TEST_FILE, "."),
        extra_files={"src/subject.py": _generated("src/subject.py", ".", body="VALUE = 7\n")},
    )
    [attempt] = attempts
    assert attempt["decision"] == "accepted", attempt["reason"]
    [prompt] = prompts
    assert f"Output paths: artifacts/verification.log, {TEST_FILE}, src/subject.py\n" in prompt
    assert "[7][0]" in (root / TEST_FILE).read_text(encoding="utf-8")


@pytest.mark.parametrize("source", ["glob", "banner", "peer-banner", "directory", "direct-write"])
def test_feedback_rejects_weakening_a_test_with_conflicting_owners(run_feedback, source):
    subject = f"{SUPPORT}/test_subject.py"
    peer = f"{SUPPORT}/test_peer.py"
    other_design = "docs/design/peer.md"
    original = "def test_peer():\r\n    actual = 7\r\n    assert actual == 7\r\n"
    if source == "peer-banner":
        original = _generated(peer, "checks", body=original).replace(DESIGN, other_design)
    weakened = original.replace("actual == 7", "actual == actual")
    declaration = f"{SUPPORT}/" if source in {"directory", "direct-write"} else GLOB
    if source == "banner":
        subject = TEST_FILE
    content = _generated(subject, "checks") if source == "banner" else TESTS
    other_output = "src/peer.py" if source == "peer-banner" else peer
    root, attempts, prompts = run_feedback(
        [_task("author", declaration), _task("peer", other_output, reference=other_design)],
        subject, content, extra_files={peer: original, other_design: "# Peer design\n"},
        test_file=subject, passing_tests=(f"{peer}::test_peer",),
        replacement=FIXED if source == "direct-write" else f"{FIXED}\n=== FILE: {peer} ===\n{weakened}",
        direct_writes={peer: weakened} if source == "direct-write" else None,
    )
    [attempt] = attempts
    assert attempt["owner"] == "author"
    assert attempt["decision"] == "rejected", attempt["reason"]
    assert f"write outside declared outputs: {peer}" in attempt["reason"]
    assert (root / peer).read_bytes() == original.encode()
    assert (root / subject).read_bytes() == content.encode()
    [prompt] = prompts
    output_line = next(line for line in prompt.splitlines() if line.startswith("Output paths: "))
    assert peer not in output_line


@pytest.mark.parametrize("shared_design", [False, True])
def test_provenance_uses_the_resolved_design_document(run_feedback, shared_design):
    tasks = [_task("node-owner", "src/node.py", reference="design:core")]
    if shared_design:
        tasks.insert(0, _task("path-owner", "src/path.py"))
    original = _generated(TEST_FILE, "checks")
    root, attempts, prompts = run_feedback(
        tasks, TEST_FILE, original,
        extra_files={DESIGN: "---\ncodd:\n  node_id: design:core\n---\n# Core design\n"},
    )
    if shared_design:
        assert attempts == prompts == []
        assert (root / TEST_FILE).read_bytes() == original.encode()
    else:
        [attempt] = attempts
        assert (attempt["owner"], attempt["decision"]) == ("node-owner", "accepted"), attempt
        assert "[7][0]" in (root / TEST_FILE).read_text(encoding="utf-8")


@pytest.mark.parametrize("pattern", [GLOB, "checks/support/../support/*.py"], ids=["plain", "parent-segment"])
def test_glob_owner_can_rewrite_an_output_declared_by_absolute_pattern(run_feedback, pattern):
    root, attempts, prompts = run_feedback(
        lambda root: [_task("helpers", str(root / pattern))], HELPER, TESTS,
    )
    [attempt] = attempts
    assert (attempt["owner"], attempt["decision"]) == ("helpers", "accepted"), attempt
    [prompt] = prompts
    assert f"Output paths: {HELPER}\n" in prompt
    assert "[7][0]" in (root / HELPER).read_text(encoding="utf-8")
