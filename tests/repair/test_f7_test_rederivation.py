"""F7 — impl-blind test re-derivation: repair-layer DoD (T1/T2 triggers, schema,
propose_meta, F7b evidence). Fable5 ruling
``dogfood/fable5_reply_2026-07-10_js-repair-direction.md`` §③–§⑤.

Red-first: every symbol under test (``RepairProposal.test_defect_claim``,
``RepairLoopOutcome.blocked_test_paths`` / ``.test_defect_claim``, the propose_meta
claim rule, the F7b contract-evidence path) does not exist at HEAD.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from codd.dag import DAG, Edge, Node
from codd.repair import engine as engine_registry
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


_TEST_A = "tests/evaluator.test.js"
_TEST_B = "tests/tokenizer.test.js"


def _contract_failure() -> VerificationFailureReport:
    """The 現物 shape: a red test-command contract run (verify_contract_not_green)."""
    return VerificationFailureReport(
        check_name="test_command",
        failed_nodes=[],
        error_messages=["report shows failed/skipped test file(s)"],
        dag_snapshot={"nodes": [], "edges": []},
        timestamp="2026-07-09T16:14:56Z",
        failure_class="verify_contract_not_green",
        code_addressable=False,
    )


def _rca(affected: list[str]) -> RootCauseAnalysis:
    return RootCauseAnalysis(
        probable_cause="test transcription defect",
        affected_nodes=affected,
        repair_strategy="full_file_replacement",
        confidence=0.8,
        analysis_timestamp="2026-07-09T16:15:00Z",
    )


def _dag() -> DAG:
    dag = DAG()
    dag.add_node(Node("test:evaluator", "test_doc", _TEST_A, {}))
    dag.add_node(Node("test:tokenizer", "test_doc", _TEST_B, {}))
    return dag


def _auto_codd_yaml() -> dict:
    return {"repair": {"allow_auto": {"require_explicit_optin": True, "max_files_per_proposal": 10}}}


def _register(name: str, *, proposal: RepairProposal, rca: RootCauseAnalysis) -> None:
    class ScriptedEngine(RepairEngine):
        def __init__(self, project_root: Path | None = None):
            self.project_root = project_root

        def analyze(self, failure, dag):
            return rca

        def propose_fix(self, rca, file_contents, **kwargs):
            return proposal

        def apply(self, proposal, *, dry_run: bool = False):
            return ApplyResult(True, [p.file_path for p in proposal.patches], [], None)

    register_repair_engine(name)(ScriptedEngine)


def _run_auto(tmp_path: Path, name: str, proposal: RepairProposal, rca: RootCauseAnalysis):
    _register(name, proposal=proposal, rca=rca)
    config = RepairLoopConfig(
        max_attempts=2, approval_mode="auto", engine_name=name, codd_yaml=_auto_codd_yaml()
    )
    failure = _contract_failure()
    return RepairLoop(config, tmp_path).run(
        failure, _dag(), verify_callable=lambda: VerifyResult(False, failure)
    )


def _final_status(outcome) -> dict:
    return yaml.safe_load((outcome.history_session_dir / "final_status.yaml").read_text())


# ── DoD (1): guard rejection, ALL offenders under the test-file rule ──────────

def test_t1_all_test_offenders_thread_blocked_test_paths(tmp_path: Path):
    """A scope rejection whose offenders are ALL test files + verify_contract_not_green
    → structured ``blocked_test_paths`` on the outcome AND in final_status.yaml."""
    proposal = RepairProposal(
        [FilePatch(_TEST_A, "full_file_replacement", "x\n"), FilePatch(_TEST_B, "full_file_replacement", "y\n")],
        "patch the tests", 0.8, "t", "t",
    )
    outcome = _run_auto(tmp_path, "t1", proposal, _rca([_TEST_A, _TEST_B]))

    assert outcome.status == "REPAIR_FAILED"
    assert sorted(outcome.blocked_test_paths) == sorted([_TEST_A, _TEST_B])
    assert sorted(_final_status(outcome)["blocked_test_paths"]) == sorted([_TEST_A, _TEST_B])


def test_t1_mixed_offenders_do_not_qualify(tmp_path: Path):
    """A MIXED proposal (a test file + an oracle/gate-control offender) stays a hard
    terminal — NO blocked_test_paths (the false-green vector stays fully blocked)."""
    proposal = RepairProposal(
        [FilePatch(_TEST_A, "full_file_replacement", "x\n"), FilePatch("codd.yaml", "full_file_replacement", "z\n")],
        "patch a test and the oracle", 0.8, "t", "t",
    )
    outcome = _run_auto(tmp_path, "t1mixed", proposal, _rca([_TEST_A]))

    assert outcome.status == "REPAIR_FAILED"
    assert outcome.blocked_test_paths == []
    assert _final_status(outcome)["blocked_test_paths"] == []


# ── DoD (2): test_defect_claim round-trip; claim-only ≠ engine exception ──────

def test_t2_claim_only_proposal_round_trips_to_final_status(tmp_path: Path):
    """A CLAIM-ONLY proposal (no patches, a test_defect_claim) is a STRUCTURED
    terminal — the claim reaches the outcome and final_status.yaml, and the loop
    does NOT treat it as an F2 engine-failure strike."""
    claim = [{"file": _TEST_A, "assertion": "expect(false).toBe(true)", "reason": "tautology"}]
    proposal = RepairProposal([], "the test is unsatisfiable", 0.9, "t", "t", test_defect_claim=claim)
    outcome = _run_auto(tmp_path, "t2", proposal, _rca([_TEST_A]))

    assert outcome.status == "REPAIR_FAILED"
    assert outcome.reason == "TEST_DEFECT_CLAIM"
    assert outcome.test_defect_claim == claim
    assert outcome.blocked_test_paths == [_TEST_A]
    fs = _final_status(outcome)
    assert fs["test_defect_claim"] == claim
    # ONE attempt recorded (structured terminal), not 3 strike attempts.
    assert len(outcome.attempts) == 1


def test_llm_engine_returns_claim_only_proposal_instead_of_raising():
    """The LLM engine surfaces a claim-only proposal rather than raising
    RepairFailed('selected no-patch') — the legal channel exists."""
    from codd.repair.llm_repair_engine import LlmRepairEngine

    payload = (
        '{"patches": [], "rationale": "unsatisfiable", "confidence": 0.9, '
        '"test_defect_claim": [{"file": "tests/x.test.js", "assertion": "a", "reason": "tautology"}]}'
    )
    engine = LlmRepairEngine(ai_command=lambda _prompt: payload, project_root=None)
    proposal = engine.propose_fix(_rca([_TEST_A]), {})
    assert proposal.patches == []
    assert proposal.test_defect_claim == [
        {"file": "tests/x.test.js", "assertion": "a", "reason": "tautology"}
    ]


# ── DoD (3): propose_meta contract test asserts the claim rule text ──────────

def test_propose_meta_carries_the_claim_rule():
    from codd.repair import llm_repair_engine

    template = (Path(llm_repair_engine.__file__).parent / "templates" / "propose_meta.md").read_text()
    assert "test_defect_claim" in template
    assert "cannot be satisfied by ANY implementation conforming to the design" in template
    assert "do NOT patch the test" in template


# ── DoD (7): F7b — contract-path failure carries per-test evidence + attribution ──

def test_f7b_contract_failure_carries_per_test_evidence_and_attribution(tmp_path: Path):
    """``_tuple_from_execution`` on a contract FAIL surfaces the failed test file(s)
    and the executor's captured assertion text into the failure evidence, runs B0
    attribution (evidence_nodes + per-path attribution), and KEEPS the routing class
    ``verify_contract_not_green`` (so T1 still fires)."""
    from codd.languages.adapters.runner_report import RunnerExecution
    from codd.languages.profile import VerifyObservationPolicy
    from codd.languages.verify_executor import VerifyExecutionResult
    from codd.languages.verify_plan import VerifyClass, VerifyRunPlan
    from codd.repair.verify_runner import VerifyRunner

    # A real failing vitest test file so B0 can infer its source + attribute it.
    (tmp_path / "tests").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "evaluator.js").write_text("export const x = 1;\n", encoding="utf-8")
    (tmp_path / "tests" / "evaluator.test.js").write_text(
        "import { x } from '../src/evaluator.js';\nit('x', () => expect(x).toBe(2));\n", encoding="utf-8"
    )

    plan = VerifyRunPlan(
        language_id="javascript",
        argv=("npx", "vitest", "run", "--reporter=json"),
        cwd=None,
        env={},
        report_path=None,
        report_adapter="vitest-json",
        report_required=True,
        must_include_test_sets=(),
        required_test_sets=(),
        observation=VerifyObservationPolicy(),
    )
    execution = RunnerExecution(
        executed_failed_files=frozenset({"tests/evaluator.test.js"}),
        total_cases=1,
        passed_cases=0,
    )
    captured = "FAIL tests/evaluator.test.js\nAssertionError: expected 1 to be 2\n  expect(x).toBe(2)\n"
    result = VerifyExecutionResult(
        verify_class=VerifyClass.FAIL,
        returncode=1,
        execution=execution,
        detail="report shows failed test file(s)",
        stdout=captured,
        stderr="",
    )

    runner = VerifyRunner(tmp_path, {})
    executed, command, detail, failure = runner._tuple_from_execution(plan, result)

    assert executed is True
    assert failure is not None
    details = failure.details
    # Routing class preserved (T1 depends on it).
    assert details["failure_class"] == "verify_contract_not_green"
    # Per-test failure entry surfaced.
    assert details["failed_test_files"] == ["tests/evaluator.test.js"]
    # The actual assertion text (from captured stdout) reached the evidence.
    assert "expected 1 to be 2" in failure.message
    assert "expected 1 to be 2" in details.get("output", "")
    # B0 attribution ran: the failing test is READ-ONLY evidence + attribution present.
    assert details.get("evidence_nodes") == ["tests/evaluator.test.js"]
    assert any(a["path"] == "tests/evaluator.test.js" for a in details.get("attribution", []))
