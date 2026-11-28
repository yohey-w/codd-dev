import errno
import json

import pytest

from codd.dag import DAG, Node
from codd.repair import history
from codd.repair.llm_repair_engine import LlmRepairEngine
from codd.repair.loop import NullRepairabilityClassifier, RepairLoop, RepairLoopConfig
from codd.repair.schema import VerificationFailureReport
from codd.repair.verify_runner import VerificationResult


SOURCE = "src/service.py"
TEST = "tests/test_behavior.py"
DESIGN = "docs/design/behavior.md"
FIXED = "fixed\n"
CLAIM = {"file": TEST, "assertion": "A must equal B", "reason": "the design requires A"}
RETRY_MENU = "Editable implementation paths from the DAG:\n"
AUTO_SCOPE_RULE = "keep design documents and gate-control configuration read-only"


def _write(root, path, content):
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(content, encoding="utf-8")


def _analysis(nodes):
    return {"probable_cause": "scripted", "affected_nodes": nodes}


def _replacement_if_target(path, targets, *, otherwise):
    if path not in targets:
        return otherwise
    return {"patches": [{"file_path": path, "patch_mode": "full_file_replacement", "content": FIXED}]}


def _claim_if_test_target(targets):
    return {"patches": [], "test_defect_claim": [CLAIM] if TEST in targets else []}


@pytest.fixture
def repair(tmp_path, monkeypatch):
    _write(tmp_path, SOURCE, "wrong result\n")
    _write(tmp_path, TEST, "expected assertion\n")
    dag = DAG()
    config = {"scan": {"source_dirs": ["src"]}, "repair": {"allow_auto": {"require_explicit_optin": True}}}
    analysis_prompts, proposal_targets, approvals = [], [], []

    def run(analyze, propose, *, failed=TEST, kind="assertion_failure", code_addressable=True,
            mode="auto", verify_target=SOURCE):
        def fake_analyze(prompt):
            analysis_prompts.append(prompt)
            return json.dumps(analyze(prompt))

        def fake_propose(prompt):
            block = prompt.split("Target file contents:\n", 1)[1].split("\n\nRules:", 1)[0]
            targets = json.loads(block)
            proposal_targets.append(targets)
            return json.dumps(propose(targets))

        failure = VerificationFailureReport(
            "test_command", [failed], ["expected A"], {}, "fixture",
            failure_class=kind, code_addressable=code_addressable, evidence_nodes=[TEST],
        )
        engine = LlmRepairEngine(tmp_path, config=config,
                                 ai_command={"repair_analyze": fake_analyze, "repair_propose": fake_propose})
        loop = RepairLoop(
            RepairLoopConfig(max_attempts=1, approval_mode=mode, codd_yaml=config, notify_callable=approvals.append),
            tmp_path, repairability_classifier=NullRepairabilityClassifier(),
        )
        monkeypatch.setattr(loop, "_new_engine", lambda: engine)
        return loop.run(failure, dag, verify_callable=lambda: VerificationResult(
            (tmp_path / verify_target).read_text(encoding="utf-8") == FIXED))

    return dag, config, run, analysis_prompts, proposal_targets, approvals


def test_q7_absolute_dag_target_matches_relative_attribution(tmp_path, repair):
    dag, _, run, analyses, proposals, _ = repair
    other = "src/other.py"
    _write(tmp_path, other, "other\n")
    dag.add_node(Node("impl:service", "impl_file", str(tmp_path / SOURCE)))
    dag.add_node(Node(other, "impl_file", other))

    outcome = run(
        lambda prompt: _analysis([other] if RETRY_MENU in prompt else ["impl:service"]),
        lambda targets: _replacement_if_target(SOURCE, targets, otherwise={"patches": []}),
        failed=SOURCE, kind="runtime_exception",
    )

    assert outcome.status == "REPAIR_SUCCESS"
    assert outcome.partial_success_patches == [SOURCE]
    assert len(analyses) == 1 and AUTO_SCOPE_RULE in analyses[0]
    assert proposals == [{SOURCE: "wrong result\n"}]


def test_q8_outside_retry_target_preserves_first_answer_and_claim(tmp_path, repair):
    dag, _, run, analyses, proposals, _ = repair
    dag.add_node(Node(SOURCE, "impl_file", SOURCE))
    outside = str(tmp_path.parent / "outside-project-does-not-exist.code")

    outcome = run(lambda prompt: _analysis([outside] if RETRY_MENU in prompt else [TEST]), _claim_if_test_target)

    assert outcome.reason == "TEST_DEFECT_CLAIM"
    assert outcome.test_defect_claim == [CLAIM]
    assert len(analyses) == 2
    assert proposals == [{TEST: "expected assertion\n"}]
    assert outcome.attempts[0].rca.affected_nodes == [TEST]


@pytest.mark.parametrize("kind,code_addressable", [("assertion_failure", True), ("verify_contract_not_green", False)])
def test_q9_retry_menu_lists_only_implementation_files(tmp_path, repair, kind, code_addressable):
    dag, config, run, analyses, _, _ = repair
    config["scan"]["source_dirs"] = ["."]
    stack_profile = "src/stack-contract.yaml"
    config["stack"] = {"profiles": [stack_profile]}
    for path, node_kind in (
        (SOURCE, "impl_file"), (TEST, "test_file"), ("src/spec/behavior.js", "test_file"),
        (DESIGN, "design_doc"), ("pytest.ini", "config_file"), (".codd/codd.yaml", "config_file"),
        (stack_profile, "impl_file"),
    ):
        if not (tmp_path / path).exists():
            _write(tmp_path, path, "fixture\n")
        dag.add_node(Node(path, node_kind, path))
    shared_design = "src/architecture.md"
    _write(tmp_path, shared_design, "shared design\n")
    dag.add_node(Node(shared_design, "common", shared_design, attributes={"frontmatter": {}}))

    outcome = run(lambda _: _analysis([TEST]), _claim_if_test_target, kind=kind, code_addressable=code_addressable)

    assert outcome.reason == "TEST_DEFECT_CLAIM"
    assert len(analyses) == 2
    assert json.loads(analyses[1].split(RETRY_MENU, 1)[1]) == [SOURCE]


@pytest.mark.parametrize("mode", ["required", "per_attempt"])
def test_q10_human_approval_analysis_can_target_design(tmp_path, repair, mode):
    dag, config, run, analyses, proposals, approvals = repair
    config["repair"]["approval_decision"] = "yes"
    _write(tmp_path, DESIGN, "stale pin\n")
    dag.add_node(Node(DESIGN, "design_doc", DESIGN))

    outcome = run(
        lambda prompt: _analysis([] if AUTO_SCOPE_RULE in prompt else [DESIGN]),
        lambda targets: _replacement_if_target(DESIGN, targets, otherwise={"patches": []}),
        failed=SOURCE, mode=mode, verify_target=DESIGN,
    )

    assert outcome.status == "REPAIR_SUCCESS"
    assert outcome.partial_success_patches == [DESIGN]
    assert len(analyses) == 1 and AUTO_SCOPE_RULE not in analyses[0]
    assert proposals == [{DESIGN: "stale pin\n"}]
    assert len(approvals) == 1 and DESIGN in approvals[0]


@pytest.mark.parametrize("artifact", ["prompt", "response", "evidence"])
def test_q11_retry_history_write_failure_preserves_first_answer(monkeypatch, repair, artifact):
    dag, _, run, analyses, proposals, _ = repair
    dag.add_node(Node(SOURCE, "impl_file", SOURCE))
    write_artifact = history._artifact
    write_yaml = history._write_yaml

    def write_call_artifact(directory, name, body):
        if name == f"call_1_{artifact}.txt" and (artifact != "prompt" or RETRY_MENU in body):
            raise OSError(errno.EFBIG, "fake retry artifact storage limit")
        return write_artifact(directory, name, body)

    def write_attempt_evidence(path, body):
        if (artifact == "evidence" and path.name == "attempt_evidence.yaml"
                and body["stage"] == "analyze" and len(body["calls"]) == 2):
            raise OSError(errno.EFBIG, "fake retry evidence storage limit")
        return write_yaml(path, body)

    monkeypatch.setattr(history, "_artifact", write_call_artifact)
    monkeypatch.setattr(history, "_write_yaml", write_attempt_evidence)
    outcome = run(lambda prompt: _analysis([SOURCE] if RETRY_MENU in prompt else [TEST]), _claim_if_test_target)

    assert outcome.reason == "TEST_DEFECT_CLAIM"
    assert outcome.test_defect_claim == [CLAIM]
    assert proposals == [{TEST: "expected assertion\n"}]
    assert len(analyses) == (2 if artifact == "response" else 1)
    attempt = outcome.attempts[0]
    assert attempt.rca.affected_nodes == [TEST]
    assert attempt.evidence["analysis_retry_error"] == "HistoryUnavailableError"
    assert attempt.evidence["calls"][-1]["stage"] == "repair_propose"
