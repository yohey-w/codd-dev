import json

import pytest
import yaml

from codd.dag import DAG, Edge, Node
from codd.repair.history import RepairHistory
from codd.repair.llm_repair_engine import LlmRepairEngine
from codd.repair.loop import RepairLoop, RepairLoopConfig
from codd.repair.schema import VerificationFailureReport
from codd.repair.verify_runner import VerificationResult


SOURCE = "app/service.code"
TEST = "checks/behavior.contract"
DESIGN = "app/requirements.contract"
CONFIG = ".codd/codd.yaml"
UNSATISFIABLE = {"file": TEST, "assertion": "result is both A and B", "reason": "contradicts design"}


@pytest.fixture
def project(tmp_path):
    config = {
        "scan": {"source_dirs": ["app", ".codd"], "test_dirs": ["app/scripts"]},
        "repair": {"allow_auto": {"require_explicit_optin": True}},
    }
    dag = DAG()
    for identifier, kind, path, body in (
        ("impl:service", "impl_file", SOURCE, "wrong result\n"),
        ("verify:behavior", "verification_test", TEST, "observe run result\n"),
        ("design:result", "design_doc", DESIGN, "the result must be A\n"),
        ("config:project", "impl_file", CONFIG, yaml.safe_dump(config)),
        ("config:lock", "impl_file", "app/stack.lock", "pinned\n"),
        ("test:fixture", "test_file", "app/fixture.contract", "fixture input\n"),
        ("test:setup", "impl_file", "app/scripts/setup.code", "test setup\n"),
        ("impl:external", "impl_file", "outside/service.code", "outside source roots\n"),
    ):
        file = tmp_path / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(body, encoding="utf-8")
        dag.add_node(Node(identifier, kind, path))
    dag.add_edge(Edge("design:result", "verify:behavior", "expects"))
    dag.add_node(Node("impl:missing", "impl_file", "app/missing.code"))
    failure = VerificationFailureReport(
        "test_command", ["verify:behavior", TEST], ["expected A, received B"], {}, "fixture",
        failure_class="assertion_failure", code_addressable=True,
        evidence_nodes=[TEST],
    )
    return dag, failure, config


def _run(tmp_path, monkeypatch, project, answers, *, max_attempts=2, claim=False):
    dag, failure, config = project
    analysis_prompts, proposal_prompts, verifies = [], [], []
    scripted_answers = iter(answers)

    def analyze(prompt):
        analysis_prompts.append(prompt)
        return json.dumps({"probable_cause": "run returns the wrong result",
                           "affected_nodes": next(scripted_answers)})

    def propose(prompt):
        proposal_prompts.append(prompt)
        if claim:
            return json.dumps({"patches": [], "test_defect_claim": [UNSATISFIABLE]})
        return json.dumps({"patches": [{"file_path": SOURCE, "patch_mode": "full_file_replacement",
                                        "content": "correct result\n"}]})

    def verify():
        verifies.append((tmp_path / SOURCE).read_text(encoding="utf-8"))
        return VerificationResult(verifies[-1] == "correct result\n", failure=failure)

    engine = LlmRepairEngine(tmp_path, config=config, ai_command={"repair_analyze": analyze, "repair_propose": propose})
    loop = RepairLoop(RepairLoopConfig(approval_mode="auto", max_attempts=max_attempts, codd_yaml=config), tmp_path)
    monkeypatch.setattr(loop, "_new_engine", lambda: engine)
    outcome = loop.run(failure, dag, verify_callable=verify)
    history = RepairHistory().load_session(outcome.history_session_dir)["attempts"]
    return outcome, analysis_prompts, proposal_prompts, verifies, history


def _targets(prompt):
    return json.loads(prompt.split("Target file contents:\n", 1)[1].split("\n\nRules:", 1)[0])


def _stages(attempt):
    return [call["stage"] for call in attempt["attempt_evidence"]["calls"]]


def test_y1_reanalyzes_test_only_answer_and_repairs_source(tmp_path, monkeypatch, project):
    outcome, analyses, proposals, verifies, history = _run(
        tmp_path, monkeypatch, project, [["verify:behavior"], ["impl:service"]],
    )

    assert [_targets(prompt) for prompt in proposals] == [{SOURCE: "wrong result\n"}]
    assert len(analyses) == 2
    assert f"BEGIN READ-ONLY EVIDENCE {TEST}" in proposals[0]
    assert "the result must be A" in proposals[0]
    assert (tmp_path / TEST).read_text(encoding="utf-8") == "observe run result\n"
    assert verifies == ["correct result\n"]
    assert outcome.status == "REPAIR_SUCCESS"
    assert list(history) == ["attempt_0"]
    assert _stages(history["attempt_0"]) == ["repair_analyze", "repair_analyze", "repair_propose"]


def test_y2_repeated_read_only_answers_consume_one_attempt_without_proposal(tmp_path, monkeypatch, project):
    outcome, analyses, proposals, verifies, history = _run(
        tmp_path, monkeypatch, project, [[TEST], ["config:project", "design:result"], [SOURCE]],
    )

    assert _stages(history["attempt_0"]) == ["repair_analyze", "repair_analyze"]
    assert history["attempt_0"]["repair_proposal"] is None
    assert "no editable files" in history["attempt_0"]["apply_result"]["error_message"]
    assert "REPAIR ATTEMPT EVIDENCE" in analyses[2]
    assert _stages(history["attempt_1"]) == ["repair_analyze", "repair_propose"]
    assert len(proposals) == 1 and verifies == ["correct result\n"]
    assert outcome.status == "REPAIR_SUCCESS"
    assert [attempt.attempt_n for attempt in outcome.attempts] == [0, 1]


@pytest.mark.parametrize("other_nodes", [[], ["verify:behavior", DESIGN, CONFIG, "test:fixture"]])
def test_y3_source_in_first_answer_needs_no_retry(tmp_path, monkeypatch, project, other_nodes):
    outcome, analyses, proposals, verifies, _ = _run(
        tmp_path, monkeypatch, project, [[SOURCE, *other_nodes]],
    )

    assert len(analyses) == 1
    assert [_targets(prompt) for prompt in proposals] == [{SOURCE: "wrong result\n"}]
    assert outcome.status == "REPAIR_SUCCESS" and verifies == ["correct result\n"]


def test_y4_claim_only_proposal_keeps_structured_terminal(tmp_path, monkeypatch, project):
    outcome, analyses, proposals, verifies, history = _run(
        tmp_path, monkeypatch, project, [[SOURCE]], claim=True,
    )

    assert len(analyses) == len(proposals) == 1
    assert verifies == []
    assert outcome.status == "REPAIR_FAILED" and outcome.reason == "TEST_DEFECT_CLAIM"
    assert outcome.test_defect_claim == [UNSATISFIABLE]
    assert outcome.blocked_test_paths == [TEST]
    assert history["attempt_0"]["repair_proposal"]["test_defect_claim"] == [UNSATISFIABLE]


def test_y5_retry_lists_only_editable_dag_source_paths(tmp_path, monkeypatch, project):
    _, analyses, _, _, _ = _run(tmp_path, monkeypatch, project, [[TEST], [SOURCE]])

    retry = analyses[1].split("Previous affected_nodes selected no editable files", 1)[1]
    assert "read-only" in retry and TEST in retry
    paths = retry.split("Editable implementation paths from the DAG:\n", 1)[1]
    assert json.loads(paths) == [SOURCE]
