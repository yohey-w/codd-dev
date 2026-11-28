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
TEST = "app/tests/behavior.contract"
DESIGN = "app/docs/requirements.md"
CONFIG = ".codd/codd.yaml"
UNSATISFIABLE = {"file": TEST, "assertion": "result is both A and B", "reason": "contradicts design"}


@pytest.fixture
def project(tmp_path):
    config = {
        "scan": {"source_dirs": ["app", ".codd"]},
        "repair": {"allow_auto": {"require_explicit_optin": True}},
        "stack": {"profiles": ["app/stack-contract.code"]},
    }
    dag = DAG()
    for identifier, kind, path, body in (
        (f"impl:{SOURCE}", "impl_file", SOURCE, "wrong result\n"),
        ("verify:behavior", "verification_test", TEST, "observe run result\n"),
        ("design:result", "design_doc", DESIGN, "the result must be A\n"),
        ("config:project", "impl_file", CONFIG, yaml.safe_dump(config)),
        ("stack:lock", "impl_file", "app/stack.lock", "pinned\n"),
        ("stack:profile", "impl_file", "app/stack-contract.code", "declared contract\n"),
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


def _run(tmp_path, monkeypatch, project, answers, *, claim=False):
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
    loop = RepairLoop(RepairLoopConfig(approval_mode="auto", max_attempts=1, codd_yaml=config), tmp_path)
    monkeypatch.setattr(loop, "_new_engine", lambda: engine)
    outcome = loop.run(failure, dag, verify_callable=verify)
    history = RepairHistory().load_session(outcome.history_session_dir)["attempts"]
    return outcome, analysis_prompts, proposal_prompts, verifies, history


def _targets(prompt):
    return json.loads(prompt.split("Target file contents:\n", 1)[1].split("\n\nRules:", 1)[0])


def _stages(attempt):
    return [call["stage"] for call in attempt["attempt_evidence"]["calls"]]


def test_z1_reanalyzes_test_only_answer_and_repairs_source(tmp_path, monkeypatch, project):
    outcome, analyses, proposals, verifies, history = _run(
        tmp_path, monkeypatch, project, [["verify:behavior"], [f"impl:{SOURCE}"]],
    )

    assert [_targets(prompt) for prompt in proposals] == [{SOURCE: "wrong result\n"}]
    assert len(analyses) == 2
    assert 'Previous affected_nodes: ["verify:behavior"]' in analyses[1]
    paths = analyses[1].split("Editable implementation paths from the DAG:\n", 1)[1]
    assert json.loads(paths) == [SOURCE]
    assert f"BEGIN READ-ONLY EVIDENCE {TEST}" in proposals[0]
    assert "the result must be A" in proposals[0]
    assert (tmp_path / TEST).read_text(encoding="utf-8") == "observe run result\n"
    assert verifies == ["correct result\n"]
    assert outcome.status == "REPAIR_SUCCESS"
    assert outcome.partial_success_patches == [SOURCE]
    assert list(history) == ["attempt_0"]
    assert _stages(history["attempt_0"]) == ["repair_analyze", "repair_analyze", "repair_propose"]


@pytest.mark.parametrize("retry_answer", [["config:project", "design:result"], []])
def test_z2_rejected_retry_proposes_with_first_answer_and_preserves_claim(tmp_path, monkeypatch, project, retry_answer):
    outcome, analyses, proposals, verifies, history = _run(
        tmp_path, monkeypatch, project, [["verify:behavior"], retry_answer], claim=True,
    )

    assert len(analyses) == 2
    assert [_targets(prompt) for prompt in proposals] == [{TEST: "observe run result\n"}]
    assert verifies == []
    assert outcome.status == "REPAIR_FAILED" and outcome.reason == "TEST_DEFECT_CLAIM"
    assert outcome.test_defect_claim == [UNSATISFIABLE]
    assert outcome.blocked_test_paths == [TEST]
    assert list(history) == ["attempt_0"]
    assert outcome.attempts[0].rca.affected_nodes == ["verify:behavior"]
    assert _stages(history["attempt_0"]) == ["repair_analyze", "repair_analyze", "repair_propose"]
    assert history["attempt_0"]["repair_proposal"]["test_defect_claim"] == [UNSATISFIABLE]


@pytest.mark.parametrize("other_targets", [[], [TEST, DESIGN, CONFIG]])
def test_z3_source_in_first_answer_keeps_all_proposal_targets(tmp_path, monkeypatch, project, other_targets):
    outcome, analyses, proposals, verifies, history = _run(
        tmp_path, monkeypatch, project, [[SOURCE, *other_targets]],
    )

    assert len(analyses) == 1
    assert len(proposals) == 1
    targets = _targets(proposals[0])
    assert set(targets) == {SOURCE, *other_targets}
    assert targets[SOURCE] == "wrong result\n"
    assert outcome.status == "REPAIR_SUCCESS" and verifies == ["correct result\n"]
    assert _stages(history["attempt_0"]) == ["repair_analyze", "repair_propose"]
