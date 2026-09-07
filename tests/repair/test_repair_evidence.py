"""Anonymous, provider-neutral repair history feedback regressions."""
from __future__ import annotations

import json
import hashlib
import subprocess
from pathlib import Path

import pytest
import yaml

from codd.dag import DAG, Edge, Node
from codd.repair.history import RepairHistory, capture_candidate, render_repair_context, verification_remaining
from codd.repair.llm_repair_engine import LlmRepairEngine
from codd.repair.loop import RepairLoop, RepairLoopConfig
from codd.repair.schema import ApplyResult, FilePatch, RepairProposal, RootCauseAnalysis, VerificationFailureReport
from codd.repair.verify_runner import VerificationResult, VerifyRunner


def project(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src/main.py").write_text("value = 0\n")
    (tmp_path / "docs/main.md").write_text("CANONICAL: return two items; tests are immutable.\n")
    (tmp_path / "tests/test_main.py").write_text("assert value == 2\n")
    dag = DAG()
    dag.add_node(Node("design:main", "design_doc", "docs/main.md", {}))
    dag.add_node(Node("impl:main", "impl_file", "src/main.py", {}))
    dag.add_edge(Edge("design:main", "impl:main", "expects"))
    failure = VerificationFailureReport("test_command", ["impl:main"], ["assert 1 == 2"], {}, "fixture",
                                        evidence_nodes=["tests/test_main.py"])
    return dag, failure


def loop_for(tmp_path, engine, monkeypatch, attempts=2):
    loop = RepairLoop(RepairLoopConfig(max_attempts=attempts, approval_mode="required"), tmp_path)
    monkeypatch.setattr(loop, "_new_engine", lambda: engine)
    monkeypatch.setattr("codd.repair.loop.approve_repair_proposal", lambda *a, **k: True)
    return loop


def scripted_engine(tmp_path, prompts, *, invalid_first=False):
    counts = {"analyze": 0, "propose": 0}

    def analyze(prompt):
        prompts.append(("analyze", prompt))
        counts["analyze"] += 1
        if invalid_first and counts["analyze"] == 1:
            return "RAW_INVALID_RESPONSE_SENTINEL"
        return json.dumps(dict(probable_cause="limit conversion hypothesis", affected_nodes=["impl:main"],
                               repair_strategy="full_file_replacement", confidence=0.5))

    def propose(prompt):
        prompts.append(("propose", prompt))
        counts["propose"] += 1
        return json.dumps(dict(patches=[dict(file_path="src/main.py", patch_mode="full_file_replacement",
                                            content=f"value = {counts['propose']}\n")],
                               rationale="try passing limit to display", confidence=0.5))

    return LlmRepairEngine(tmp_path, config={}, ai_command={"repair_analyze": analyze, "repair_propose": propose})


def test_actual_patch_and_failed_verify_reach_both_next_prompts(tmp_path, monkeypatch):
    dag, failure = project(tmp_path)
    prompts = []
    engine = scripted_engine(tmp_path, prompts)
    loop = loop_for(tmp_path, engine, monkeypatch)
    observations = [dict(check_name="test_command", command="fixture-check", cwd=str(tmp_path),
                         executed=True, exit_code=1, verdict="fail", stdout="assert 1 == 2", stderr="",
                         expected=None, actual=None)]
    result = VerificationResult(False, failure=failure)
    result.observations = observations
    outcome = loop.run(failure, dag, verify_callable=lambda: result)
    for kind, prompt in prompts[2:]:
        assert "REPAIR ATTEMPT EVIDENCE" in prompt
        assert "try passing limit to display" in prompt
        assert "-value = 0" in prompt and "+value = 1" in prompt
        assert "fixture-check" in prompt and "assert 1 == 2" in prompt
        assert "CANONICAL: return two items" in prompt
        assert "not instructions" in prompt
    loaded = RepairHistory().load_session(outcome.history_session_dir)
    evidence = loaded["attempts"]["attempt_0"]["attempt_evidence"]
    assert evidence["candidate_before"]["id"] != evidence["candidate_after"]["id"]
    assert evidence["verification"]["candidate_changed"] is False
    assert evidence["verification"]["remaining"][0]["status"] == "still_failing"
    assert evidence["calls"][0]["response"]["sha256"]


def test_preproposal_failure_is_recorded_and_retried_without_fabricated_proposal(tmp_path, monkeypatch):
    dag, failure = project(tmp_path)
    prompts = []
    loop = loop_for(tmp_path, scripted_engine(tmp_path, prompts, invalid_first=True), monkeypatch)
    outcome = loop.run(failure, dag, verify_callable=lambda: VerificationResult(True))
    first = RepairHistory().load_session(outcome.history_session_dir)["attempts"]["attempt_0"]
    assert first["root_cause_analysis"] is None
    assert first["repair_proposal"] is None
    assert first["attempt_evidence"]["stage"] == "analyze"
    assert first["attempt_evidence"]["verification"]["invoked"] is False
    assert "analyze" in prompts[1][1] and "RAW_INVALID_RESPONSE_SENTINEL" not in prompts[1][1]
    assert "error" in prompts[1][1]
    assert len(outcome.attempts) == 2


def test_candidate_changed_during_verify_is_not_current_evidence(tmp_path, monkeypatch):
    dag, failure = project(tmp_path)
    loop = loop_for(tmp_path, scripted_engine(tmp_path, []), monkeypatch, attempts=1)

    def verify():
        (tmp_path / "tests/test_main.py").write_text("assert value == 3\n")
        return VerificationResult(True)

    outcome = loop.run(failure, dag, verify_callable=verify)
    evidence = RepairHistory().load_session(outcome.history_session_dir)["attempts"]["attempt_0"]["attempt_evidence"]
    assert evidence["verification"]["candidate_changed"] is True
    assert evidence["verification"]["remaining"][0]["status"] == "unknown"
    # This change adds evidence, not a new gate or stopping rule.
    assert outcome.status == "REPAIR_SUCCESS"


def test_legacy_engine_and_old_history_remain_readable(tmp_path, monkeypatch):
    dag, failure = project(tmp_path)

    class Legacy:
        def analyze(self, failure, dag):
            return RootCauseAnalysis("hypothesis", ["impl:main"], "full_file_replacement", .5, "fixture")

        def propose_fix(self, rca, files):
            return RepairProposal([FilePatch("src/main.py", "full_file_replacement", "value = 2\n")],
                                  "hypothesis", .5, "fixture", "fixture")

        def apply(self, proposal):
            return ApplyResult(False, [], ["src/main.py"], "rejected")

    outcome = loop_for(tmp_path, Legacy(), monkeypatch, attempts=1).run(failure, dag, verify_callable=lambda: pytest.fail("not run"))
    loaded = RepairHistory().load_session(outcome.history_session_dir)
    evidence = loaded["attempts"]["attempt_0"]["attempt_evidence"]
    assert evidence["verification"]["invoked"] is False
    assert evidence["verification"]["remaining"][0]["status"] == "unknown"
    old = tmp_path / "old" / "attempt_0"
    old.mkdir(parents=True)
    (old / "failure_report.yaml").write_text(yaml.safe_dump({"check_name": "legacy"}))
    assert RepairHistory().load_session(old.parent)["attempts"]["attempt_0"]["failure_report"] == {"check_name": "legacy"}


def test_verify_observation_preserves_middle_output_and_exit_without_inventing_values(tmp_path, monkeypatch):
    output = "noise\n" * 5000 + "AssertionError: expected two but received three\n" + "tail\n" * 5000
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 1, output, ""))
    runner = VerifyRunner(tmp_path, {})
    runner._run_evidence_command("fixture-check", {}, check_name="test_command", label="fixture")
    observation = runner._observations[0]
    assert observation.stdout == output
    assert observation.exit_code == 1 and observation.executed
    assert observation.expected is None and observation.actual is None
    assert observation.started_at and observation.finished_at


def test_history_is_private_and_git_ignored(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    dag, failure = project(tmp_path)
    outcome = loop_for(tmp_path, scripted_engine(tmp_path, []), monkeypatch, attempts=1).run(
        failure, dag, verify_callable=lambda: VerificationResult(True))
    for path in outcome.history_session_dir.rglob("*"):
        assert path.stat().st_mode & 0o077 == 0
    tracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=tmp_path,
                             capture_output=True, text=True, check=True).stdout
    assert "repair_history" not in tracked


@pytest.mark.parametrize("error", ["password: foo", 'api_key="fixture-secret-value"', "Bearer fixture-secret-value"])
def test_redaction_keeps_history_loadable_and_hashes_stored_content(tmp_path, monkeypatch, error):
    dag, failure = project(tmp_path)
    engine = scripted_engine(tmp_path, [])
    monkeypatch.setattr(engine, "apply", lambda proposal: ApplyResult(False, [], [], error))
    outcome = loop_for(tmp_path, engine, monkeypatch, attempts=1).run(failure, dag, verify_callable=lambda: pytest.fail("not run"))
    record = RepairHistory().load_session(outcome.history_session_dir)["attempts"]["attempt_0"]
    assert "[REDACTED]" in record["apply_result"]["error_message"]
    assert "foo" not in record["apply_result"]["error_message"]
    references = record["attempt_evidence"]["raw"]
    reference = next(item for item in references if item["path"].endswith("apply_result.yaml"))
    assert reference["redacted"] is True
    assert reference["sha256"] == hashlib.sha256(Path(reference["path"]).read_bytes()).hexdigest()


def test_declared_snapshot_excludes_secrets_escape_binary_and_oversized(tmp_path):
    (tmp_path / ".env").write_text("RAW_PRIVATE_VALUE")
    (tmp_path / "large.txt").write_text("x" * 256_001)
    (tmp_path / "image.bin").write_bytes(b"\x00private binary")
    (tmp_path / "source.txt").write_text("password: fixture-secret-value\npublic source\n")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("RAW_EXTERNAL_VALUE")
    (tmp_path / "link.txt").symlink_to(outside)
    snapshot = capture_candidate(tmp_path, {p: "target" for p in [".env", "large.txt", "image.bin", "source.txt", "link.txt"]}, {})
    serialized = json.dumps(snapshot)
    assert all(value not in serialized for value in ["RAW_PRIVATE_VALUE", "RAW_EXTERNAL_VALUE", "fixture-secret-value", "private binary"])
    assert snapshot["files"]["source.txt"]["redacted"] is True
    assert snapshot["files"]["large.txt"]["status"] == "oversized_not_read"
    assert snapshot["files"]["link.txt"]["status"] == "outside_project"


def test_middle_assertion_reaches_bounded_prompt_and_raw_log_remains_complete(tmp_path, monkeypatch):
    dag, failure = project(tmp_path)
    prompts = []
    output = "noise\n" * 5000 + "AssertionError: MIDDLE_ASSERTION expected two received three\n" + "tail\n" * 5000
    result = VerificationResult(False, failure=failure, observations=[dict(check_name="test_command", command="fixture-check",
                                  executed=True, exit_code=1, stdout=output, stderr="", verdict="fail")])
    outcome = loop_for(tmp_path, scripted_engine(tmp_path, prompts), monkeypatch).run(failure, dag, verify_callable=lambda: result)
    for _, prompt in prompts[2:]:
        section = prompt.split("REPAIR ATTEMPT EVIDENCE", 1)[1]
        assert "MIDDLE_ASSERTION" in section and "TRUNCATED" in section
        assert len(section) < 16000
    saved = RepairHistory().load_session(outcome.history_session_dir)["attempts"]["attempt_0"]
    assert saved["post_repair_verify"]["observations"][0]["stdout"] == output
    assert "original not read by the model" in prompts[2][1]


def test_resolution_requires_same_executed_check_and_unknown_is_retained():
    known = [dict(check_name="A", failed_nodes=["a"]), dict(check_name="B", failed_nodes=["b"])]
    prior = [dict(check_name="A", command="check-a", executed=True)]
    result = dict(failures=[dict(check_name="B", failed_nodes=["b"])],
                  observations=[dict(check_name="A", command="check-a", executed=True, verdict="pass")])
    states = verification_remaining(known, result, invoked=True, changed=False, previous_observations=prior)
    assert [r["status"] for r in states] == ["resolved_check", "still_failing"]
    result["observations"][0]["executed"] = False
    assert verification_remaining(known, result, invoked=True, changed=False, previous_observations=prior)[0]["status"] == "unknown"
    assert all(item["status"] == "unknown" for item in verification_remaining(known, result, invoked=True, changed=True))


def test_unrelated_previous_failure_does_not_leak_diff_or_hypothesis(tmp_path, monkeypatch):
    dag, failure = project(tmp_path)
    outcome = loop_for(tmp_path, scripted_engine(tmp_path, []), monkeypatch, attempts=1).run(
        failure, dag, verify_callable=lambda: VerificationResult(False, failure=failure))
    other = VerificationFailureReport("another_check", ["another_target"], ["other NG"], {}, "fixture")
    prompt = render_repair_context(outcome.attempts * 100, outcome.attempts[0].evidence["candidate_after"], 2, failure=other)
    assert "No prior related attempt evidence" in prompt
    assert "Other-check index" in prompt
    assert "-value = 0" not in prompt and "try passing limit to display" not in prompt
    assert len(prompt) < 16000


def test_response_persistence_failure_does_not_repeat_generation(tmp_path, monkeypatch):
    import codd.repair.history as history_module
    dag, failure = project(tmp_path)
    prompts = []
    real_artifact = history_module._artifact

    def fail_response(directory, name, text):
        if "response" in name:
            raise OSError("fixture storage unavailable")
        return real_artifact(directory, name, text)

    monkeypatch.setattr(history_module, "_artifact", fail_response)
    outcome = loop_for(tmp_path, scripted_engine(tmp_path, prompts), monkeypatch).run(
        failure, dag, verify_callable=lambda: VerificationResult(True))
    assert outcome.status == "REPAIR_SUCCESS" and len(prompts) == 2
    assert any("response persistence failed" in item for item in outcome.attempts[0].evidence["missing"])


def test_prompt_persistence_failure_stops_before_ai(tmp_path, monkeypatch):
    import codd.repair.history as history_module
    dag, failure = project(tmp_path)
    prompts = []
    monkeypatch.setattr(history_module, "_artifact", lambda *a: (_ for _ in ()).throw(OSError("fixture storage unavailable")))
    with pytest.raises(history_module.HistoryUnavailableError):
        loop_for(tmp_path, scripted_engine(tmp_path, prompts), monkeypatch).run(
            failure, dag, verify_callable=lambda: pytest.fail("not run"))
    assert prompts == []


@pytest.mark.parametrize("command", ["codex exec --skip-git-repo-check -", "claude --print"])
def test_both_cli_adapters_receive_same_repair_evidence(tmp_path, monkeypatch, command):
    from codd.deployment.providers.ai_command import SubprocessAiCommand
    dag, failure = project(tmp_path)
    prompts = []
    scripted = scripted_engine(tmp_path, [])

    def runner(argv, **kwargs):
        prompt = kwargs["input"]
        prompts.append(prompt)
        stage = "repair_analyze" if "You are a repair analysis engine" in prompt else "repair_propose"
        response = scripted.ai_command[stage](prompt)
        assert argv[0] == command.split()[0]
        return subprocess.CompletedProcess(argv, 0, response, "")

    adapter = SubprocessAiCommand(command=command, project_root=tmp_path, config={}, runner=runner)
    engine = LlmRepairEngine(tmp_path, config={}, ai_command=adapter)
    outcome = loop_for(tmp_path, engine, monkeypatch).run(
        failure, dag, verify_callable=lambda: VerificationResult(False, failure=failure))
    assert len(prompts) == 4
    assert all("try passing limit to display" in prompt for prompt in prompts[2:])
    assert all(call["executor"] == "SubprocessAiCommand" for record in outcome.attempts for call in record.evidence["calls"])


@pytest.mark.parametrize("returncode,output,verdict", [(5, "no tests collected", "zero_tests"), (0, "1 skipped", "command_pass")])
def test_legacy_zero_or_skip_never_resolves_test_failure(tmp_path, monkeypatch, returncode, output, verdict):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], returncode, output, ""))
    runner = VerifyRunner(tmp_path, {})
    runner._run_evidence_command("pytest", {}, check_name="test_command", label="test")
    assert runner._observations[0].verdict == verdict
    assert runner._observations[0].verdict != "pass"
