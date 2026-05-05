from __future__ import annotations

import time
from dataclasses import asdict

import yaml

from codd.deployment.providers.ai_command import SubprocessAiCommand
from codd.llm.approval import ApprovalCache, filter_approved
from codd.repair.history import RepairHistory
from codd.repair.schema import (
    ApplyResult,
    FilePatch,
    RepairProposal,
    RootCauseAnalysis,
    VerificationFailureReport,
)


def _sample_attempt():
    failure = VerificationFailureReport(
        "user_journey_coherence",
        ["journey:login"],
        ["URL expectation failed"],
        {"nodes": [{"id": "journey:login"}], "edges": []},
        "2026-05-06T00:00:00Z",
    )
    rca = RootCauseAnalysis(
        "route guard redirects to the wrong path",
        ["journey:login"],
        "unified_diff",
        0.82,
        "2026-05-06T00:00:01Z",
    )
    proposal = RepairProposal(
        [FilePatch("src/routes.py", "unified_diff", "--- a/src/routes.py\n+++ b/src/routes.py\n")],
        "update redirect target",
        0.8,
        "2026-05-06T00:00:02Z",
        rca.analysis_timestamp,
    )
    apply_result = ApplyResult(True, ["src/routes.py"], [], None)
    post_verify = {"passed": True, "command": "codd dag verify"}
    return failure, rca, proposal, apply_result, post_verify


def test_new_session_creates_timestamped_directory(tmp_path):
    history = RepairHistory()
    session_dir = history.new_session(tmp_path / ".codd" / "repair_history")

    assert session_dir.is_dir()
    assert session_dir.parent == tmp_path / ".codd" / "repair_history"
    assert "T" in session_dir.name


def test_record_attempt_writes_five_yaml_files(tmp_path):
    history = RepairHistory()
    session_dir = history.new_session(tmp_path / ".codd" / "repair_history")

    history.record_attempt(session_dir, 0, *_sample_attempt())

    attempt_dir = session_dir / "attempt_0"
    assert sorted(path.name for path in attempt_dir.glob("*.yaml")) == [
        "apply_result.yaml",
        "failure_report.yaml",
        "post_repair_verify.yaml",
        "repair_proposal.yaml",
        "root_cause_analysis.yaml",
    ]


def test_record_attempt_serializes_dataclasses_as_plain_yaml(tmp_path):
    history = RepairHistory()
    session_dir = history.new_session(tmp_path / ".codd" / "repair_history")
    failure, rca, proposal, apply_result, post_verify = _sample_attempt()

    history.record_attempt(session_dir, 2, failure, rca, proposal, apply_result, post_verify)

    loaded = yaml.safe_load((session_dir / "attempt_2" / "repair_proposal.yaml").read_text(encoding="utf-8"))
    assert loaded == asdict(proposal)


def test_finalize_writes_final_status_yaml(tmp_path):
    history = RepairHistory()
    session_dir = history.new_session(tmp_path / ".codd" / "repair_history")

    history.finalize(session_dir, "REPAIR_SUCCESS")

    loaded = yaml.safe_load((session_dir / "final_status.yaml").read_text(encoding="utf-8"))
    assert loaded["outcome"] == "REPAIR_SUCCESS"
    assert "timestamp" in loaded


def test_load_session_restores_attempts_and_final_status(tmp_path):
    history = RepairHistory()
    session_dir = history.new_session(tmp_path / ".codd" / "repair_history")
    history.record_attempt(session_dir, 0, *_sample_attempt())
    history.finalize(session_dir, "REPAIR_EXHAUSTED")

    loaded = history.load_session(session_dir)

    assert loaded["final_status"]["outcome"] == "REPAIR_EXHAUSTED"
    assert loaded["attempts"]["attempt_0"]["failure_report"]["check_name"] == "user_journey_coherence"
    assert loaded["attempts"]["attempt_0"]["apply_result"]["success"] is True


def test_list_sessions_returns_newest_first(tmp_path):
    history = RepairHistory()
    history_dir = tmp_path / ".codd" / "repair_history"
    first = history.new_session(history_dir)
    time.sleep(0.001)
    second = history.new_session(history_dir)

    assert history.list_sessions(history_dir) == [second, first]


def test_list_sessions_returns_empty_for_missing_history_dir(tmp_path):
    assert RepairHistory().list_sessions(tmp_path / ".codd" / "repair_history") == []


def test_approval_helpers_import_from_existing_llm_module(tmp_path):
    ApprovalCache.save("repair_proposal", "approved", tmp_path)

    assert ApprovalCache.load("repair_proposal", tmp_path) == "approved"
    assert filter_approved([], "required", cache_dir=tmp_path) == []


def test_subprocess_ai_command_imports_without_reimplementation():
    assert SubprocessAiCommand.__name__ == "SubprocessAiCommand"
