from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from codd.dag import DAG, Edge, Node
from codd.repair import engine as engine_registry
from codd.repair.approval_repair import approve_repair_proposal
import codd.repair.loop as loop_module
from codd.repair.engine import RepairEngine, register_repair_engine
from codd.repair.loop import RepairLoop, RepairLoopConfig
from codd.repair.schema import (
    ApplyResult,
    FilePatch,
    RepairProposal,
    RootCauseAnalysis,
    VerificationFailureReport,
)


@dataclass
class VerifyResult:
    passed: bool
    failure: VerificationFailureReport | None = None


@pytest.fixture(autouse=True)
def isolated_repair_registry(monkeypatch):
    monkeypatch.setattr(engine_registry, "_REPAIR_ENGINES", {})


def _failure(message: str = "initial failure") -> VerificationFailureReport:
    return VerificationFailureReport(
        check_name="node_completeness",
        failed_nodes=["impl:main"],
        error_messages=[message],
        dag_snapshot={"nodes": [{"id": "impl:main"}], "edges": []},
        timestamp="2026-05-06T00:00:00Z",
    )


def _rca(affected_nodes: list[str] | None = None) -> RootCauseAnalysis:
    return RootCauseAnalysis(
        probable_cause="implementation artifact drift",
        affected_nodes=affected_nodes or ["impl:main"],
        repair_strategy="full_file_replacement",
        confidence=0.8,
        analysis_timestamp="2026-05-06T00:00:01Z",
    )


def _proposal(file_count: int = 1) -> RepairProposal:
    return RepairProposal(
        [
            FilePatch(f"src/file_{index}.py", "full_file_replacement", f"value_{index} = True\n")
            for index in range(file_count)
        ],
        "replace drifted files",
        0.8,
        "2026-05-06T00:00:02Z",
        "2026-05-06T00:00:01Z",
    )


def _dag() -> DAG:
    dag = DAG()
    dag.add_node(Node("design:main", "design_doc", "docs/main.md", {}))
    dag.add_node(Node("impl:main", "impl_file", "src/main.py", {}))
    dag.add_edge(Edge("design:main", "impl:main", "expects"))
    return dag


def _write_repair_config(tmp_path: Path, repair: dict) -> None:
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(yaml.safe_dump({"repair": repair}), encoding="utf-8")


def _register_engine(
    name: str,
    *,
    proposal: RepairProposal | None = None,
    rca: RootCauseAnalysis | None = None,
    apply_results: list[ApplyResult] | None = None,
) -> type[RepairEngine]:
    proposal = proposal or _proposal()
    rca = rca or _rca()
    apply_sequence = list(apply_results or [])

    class ScriptedRepairEngine(RepairEngine):
        instances: list["ScriptedRepairEngine"] = []
        analyze_inputs: list[VerificationFailureReport] = []
        propose_inputs: list[dict[str, str]] = []

        def __init__(self, project_root: Path | None = None):
            self.project_root = project_root
            self.apply_results = list(apply_sequence)
            self.apply_calls = 0
            type(self).instances.append(self)

        def analyze(self, failure: VerificationFailureReport, dag: DAG) -> RootCauseAnalysis:
            type(self).analyze_inputs.append(failure)
            return rca

        def propose_fix(self, rca: RootCauseAnalysis, file_contents: dict[str, str]) -> RepairProposal:
            type(self).propose_inputs.append(file_contents)
            return proposal

        def apply(self, proposal: RepairProposal, *, dry_run: bool = False) -> ApplyResult:
            self.apply_calls += 1
            if self.apply_results:
                return self.apply_results.pop(0)
            return ApplyResult(True, [patch.file_path for patch in proposal.patches], [], None)

    return register_repair_engine(name)(ScriptedRepairEngine)


def _run_loop(
    tmp_path: Path,
    engine_name: str,
    verify_callable,
    *,
    max_attempts: int = 3,
    approval_mode: str = "auto",
):
    return RepairLoop(
        RepairLoopConfig(
            max_attempts=max_attempts,
            approval_mode=approval_mode,  # type: ignore[arg-type]
            engine_name=engine_name,
        ),
        tmp_path,
    ).run(_failure(), _dag(), verify_callable=verify_callable)


def test_repair_loop_config_defaults():
    config = RepairLoopConfig()

    assert config.max_attempts == 3
    assert config.approval_mode == "required"
    assert config.history_dir == Path(".codd/repair_history")
    assert config.engine_name == "llm"


def test_unknown_engine_returns_repair_failed(tmp_path: Path):
    outcome = _run_loop(tmp_path, "missing", lambda: VerifyResult(True))

    assert outcome.status == "REPAIR_FAILED"
    assert outcome.attempts == []
    assert yaml.safe_load((outcome.history_session_dir / "final_status.yaml").read_text())["outcome"] == "REPAIR_FAILED"


def test_repair_loop_exhausts_after_max_attempts(tmp_path: Path):
    _write_repair_config(tmp_path, {"allow_auto": {"require_explicit_optin": True}})
    engine_cls = _register_engine("exhaust")
    verify_calls = 0

    def verify():
        nonlocal verify_calls
        verify_calls += 1
        return VerifyResult(False, _failure(f"verify failure {verify_calls}"))

    outcome = _run_loop(tmp_path, "exhaust", verify, max_attempts=3)

    assert outcome.status == "REPAIR_EXHAUSTED"
    assert len(outcome.attempts) == 3
    assert verify_calls == 3
    assert len(engine_cls.analyze_inputs) == 3


def test_success_on_first_attempt_stops_remaining_attempts(tmp_path: Path):
    _write_repair_config(tmp_path, {"allow_auto": {"require_explicit_optin": True}})
    engine_cls = _register_engine("success-first")

    outcome = _run_loop(tmp_path, "success-first", lambda: VerifyResult(True), max_attempts=3)

    assert outcome.status == "REPAIR_SUCCESS"
    assert outcome.success is True
    assert len(outcome.attempts) == 1
    assert engine_cls.instances[0].apply_calls == 1


def test_required_approval_gate_allows_continuation_when_approved(tmp_path: Path, monkeypatch):
    _register_engine("required-approved")
    approvals: list[str] = []

    def approve(proposal, *, approval_mode, codd_yaml, notify_callable=None):
        approvals.append(approval_mode)
        return True

    monkeypatch.setattr(loop_module, "approve_repair_proposal", approve)

    outcome = _run_loop(tmp_path, "required-approved", lambda: VerifyResult(True), approval_mode="required")

    assert outcome.status == "REPAIR_SUCCESS"
    assert approvals == ["required"]


def test_required_approval_rejection_returns_rejected_by_hitl(tmp_path: Path, monkeypatch):
    engine_cls = _register_engine("required-rejected")
    monkeypatch.setattr(loop_module, "approve_repair_proposal", lambda *args, **kwargs: False)

    outcome = _run_loop(tmp_path, "required-rejected", lambda: VerifyResult(True), approval_mode="required")

    assert outcome.status == "REPAIR_REJECTED_BY_HITL"
    assert len(outcome.attempts) == 1
    assert engine_cls.instances[0].apply_calls == 0


def test_auto_approval_without_explicit_optin_returns_repair_failed(tmp_path: Path):
    engine_cls = _register_engine("auto-no-optin")

    outcome = _run_loop(tmp_path, "auto-no-optin", lambda: VerifyResult(True), approval_mode="auto")

    assert outcome.status == "REPAIR_FAILED"
    assert "require_explicit_optin" in outcome.error_message
    assert engine_cls.instances[0].apply_calls == 0


def test_auto_approval_with_explicit_optin_allows_small_proposal(tmp_path: Path):
    _write_repair_config(tmp_path, {"allow_auto": {"require_explicit_optin": True}})
    _register_engine("auto-optin")

    outcome = _run_loop(tmp_path, "auto-optin", lambda: VerifyResult(True), approval_mode="auto")

    assert outcome.status == "REPAIR_SUCCESS"


def test_auto_approval_escalates_when_proposal_touches_too_many_files(tmp_path: Path):
    _write_repair_config(
        tmp_path,
        {
            "allow_auto": {"require_explicit_optin": True, "max_files_per_proposal": 1},
            "approval_decision": "approved",
        },
    )
    _register_engine("auto-escalate", proposal=_proposal(2))

    with pytest.warns(RuntimeWarning, match="max_files_per_proposal"):
        outcome = _run_loop(tmp_path, "auto-escalate", lambda: VerifyResult(True), approval_mode="auto")

    assert outcome.status == "REPAIR_SUCCESS"


def test_per_attempt_approval_runs_on_each_attempt(tmp_path: Path, monkeypatch):
    _register_engine("per-attempt")
    approvals: list[int] = []
    verify_results = [
        VerifyResult(False, _failure("first verify failure")),
        VerifyResult(False, _failure("second verify failure")),
        VerifyResult(True),
    ]

    def approve(*args, **kwargs):
        approvals.append(len(approvals))
        return True

    monkeypatch.setattr(loop_module, "approve_repair_proposal", approve)

    outcome = _run_loop(
        tmp_path,
        "per-attempt",
        lambda: verify_results.pop(0),
        max_attempts=3,
        approval_mode="per_attempt",
    )

    assert outcome.status == "REPAIR_SUCCESS"
    assert approvals == [0, 1, 2]


def test_history_writes_attempt_files_and_final_status(tmp_path: Path):
    _write_repair_config(tmp_path, {"allow_auto": {"require_explicit_optin": True}})
    _register_engine("history-files")

    outcome = _run_loop(tmp_path, "history-files", lambda: VerifyResult(True), max_attempts=1)

    assert sorted(path.name for path in (outcome.history_session_dir / "attempt_0").glob("*.yaml")) == [
        "apply_result.yaml",
        "failure_report.yaml",
        "post_repair_verify.yaml",
        "repair_proposal.yaml",
        "root_cause_analysis.yaml",
    ]
    final_status = yaml.safe_load((outcome.history_session_dir / "final_status.yaml").read_text())
    assert final_status["outcome"] == "REPAIR_SUCCESS"


def test_history_attempt_directories_are_sequential(tmp_path: Path):
    _write_repair_config(tmp_path, {"allow_auto": {"require_explicit_optin": True}})
    _register_engine("history-sequence")

    outcome = _run_loop(
        tmp_path,
        "history-sequence",
        lambda: VerifyResult(False, _failure("still failing")),
        max_attempts=3,
    )

    assert [path.name for path in sorted(outcome.history_session_dir.glob("attempt_*"))] == [
        "attempt_0",
        "attempt_1",
        "attempt_2",
    ]


def test_second_verify_pass_returns_success_at_attempt_one(tmp_path: Path):
    _write_repair_config(tmp_path, {"allow_auto": {"require_explicit_optin": True}})
    _register_engine("second-pass")
    results = [VerifyResult(False, _failure("first verify failure")), VerifyResult(True)]

    outcome = _run_loop(tmp_path, "second-pass", lambda: results.pop(0), max_attempts=3)

    assert outcome.status == "REPAIR_SUCCESS"
    assert [attempt.attempt_n for attempt in outcome.attempts] == [0, 1]


def test_required_approval_calls_notify_callable():
    messages: list[str] = []

    approved = approve_repair_proposal(
        _proposal(),
        approval_mode="required",
        codd_yaml={"repair": {"approval_decision": "approved"}},
        notify_callable=messages.append,
    )

    assert approved is True
    assert "approval required" in messages[0]
    assert "src/file_0.py" in messages[0]


def test_required_approval_stdout_fallback_does_not_raise(capsys):
    approved = approve_repair_proposal(
        _proposal(),
        approval_mode="required",
        codd_yaml={},
        notify_callable=None,
    )

    assert approved is False
    assert "approval required" in capsys.readouterr().out


def test_apply_failure_records_attempt_and_continues(tmp_path: Path):
    _write_repair_config(tmp_path, {"allow_auto": {"require_explicit_optin": True}})
    _register_engine(
        "apply-failure",
        apply_results=[
            ApplyResult(False, [], ["src/file_0.py"], "patch failed"),
            ApplyResult(True, ["src/file_0.py"], [], None),
        ],
    )
    verify_calls = 0

    def verify():
        nonlocal verify_calls
        verify_calls += 1
        return VerifyResult(True)

    outcome = _run_loop(tmp_path, "apply-failure", verify, max_attempts=3)

    assert outcome.status == "REPAIR_SUCCESS"
    assert len(outcome.attempts) == 2
    assert outcome.attempts[0].apply_result.success is False
    assert verify_calls == 1


def test_failed_verify_result_becomes_next_attempt_input(tmp_path: Path):
    _write_repair_config(tmp_path, {"allow_auto": {"require_explicit_optin": True}})
    engine_cls = _register_engine("next-failure")
    next_failure = _failure("second failure input")
    results = [VerifyResult(False, next_failure), VerifyResult(False, _failure("final failure"))]

    outcome = _run_loop(tmp_path, "next-failure", lambda: results.pop(0), max_attempts=2)

    assert outcome.status == "REPAIR_EXHAUSTED"
    assert engine_cls.analyze_inputs[1] is next_failure


def test_outcome_attempts_preserve_attempt_records(tmp_path: Path):
    _write_repair_config(tmp_path, {"allow_auto": {"require_explicit_optin": True}})
    _register_engine("attempt-records")

    outcome = _run_loop(tmp_path, "attempt-records", lambda: VerifyResult(True), max_attempts=1)

    assert outcome.attempts[0].attempt_n == 0
    assert outcome.attempts[0].failure_report.check_name == "node_completeness"
    assert outcome.attempts[0].rca.probable_cause
    assert outcome.attempts[0].proposal.patches
    assert outcome.attempts[0].post_verify_passed is True


def test_affected_file_contents_are_loaded_from_dag_node_paths(tmp_path: Path):
    _write_repair_config(tmp_path, {"allow_auto": {"require_explicit_optin": True}})
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("existing = True\n", encoding="utf-8")
    engine_cls = _register_engine("file-contents", rca=_rca(["impl:main"]))

    outcome = _run_loop(tmp_path, "file-contents", lambda: VerifyResult(True), max_attempts=1)

    assert outcome.status == "REPAIR_SUCCESS"
    assert engine_cls.propose_inputs[0] == {"src/main.py": "existing = True\n"}


def test_auto_approval_max_files_zero_escalates_to_required():
    with pytest.warns(RuntimeWarning, match="max_files_per_proposal"):
        approved = approve_repair_proposal(
            _proposal(),
            approval_mode="auto",
            codd_yaml={
                "repair": {
                    "allow_auto": {"require_explicit_optin": True, "max_files_per_proposal": 0},
                    "approval_decision": "approved",
                }
            },
        )

    assert approved is True


def test_engine_is_initialized_with_project_root(tmp_path: Path):
    _write_repair_config(tmp_path, {"allow_auto": {"require_explicit_optin": True}})
    engine_cls = _register_engine("project-root")

    outcome = _run_loop(tmp_path, "project-root", lambda: VerifyResult(True), max_attempts=1)

    assert outcome.status == "REPAIR_SUCCESS"
    assert engine_cls.instances[0].project_root == tmp_path


def test_post_verify_yaml_serializes_verify_result(tmp_path: Path):
    _write_repair_config(tmp_path, {"allow_auto": {"require_explicit_optin": True}})
    _register_engine("post-verify")
    verify_failure = _failure("persisted verify failure")

    outcome = _run_loop(
        tmp_path,
        "post-verify",
        lambda: VerifyResult(False, verify_failure),
        max_attempts=1,
    )

    post_verify = yaml.safe_load(
        (outcome.history_session_dir / "attempt_0" / "post_repair_verify.yaml").read_text()
    )
    assert post_verify["passed"] is False
    assert post_verify["failure"]["error_messages"] == ["persisted verify failure"]
