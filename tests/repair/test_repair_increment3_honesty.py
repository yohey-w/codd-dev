"""Increment 3 (v3.22.0) — repair honesty: design-context injection + the
deterministic ``TEST_CONTRACT_OVERREACH`` terminal label.

PART A: the analyze / propose prompt CONTAINS the failing nodes' design-doc body
(over the transitive ``depends_on`` closure) when the failure maps to design
docs, and is UNCHANGED (no injected design section) when it does not.

PART B: repair finalize emits ``TEST_CONTRACT_OVERREACH`` UNDER the deterministic
absence condition (failing-assertion surface tokens provably absent from design +
producer corpus) and NEVER otherwise (a control where the tokens ARE present keeps
the old ``ALL_REMAINING_UNREPAIRABLE_OR_PRE_EXISTING`` reason).

Anti-false-green: the label rides an already-RED terminal — no test edit, no
patch-scope change, deterministic red-only.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from codd.dag import DAG, Edge, Node
from codd.repair.design_context import (
    DESIGN_CONTEXT_RULE,
    TEST_CONTRACT_OVERREACH_REASON,
)
from codd.repair.llm_repair_engine import LlmRepairEngine
from codd.repair.loop import RepairabilityClassification, RepairLoop, RepairLoopConfig
from codd.repair.schema import RootCauseAnalysis, VerificationFailureReport


DESIGN_BODY_MARKER = "CANONICAL_SESSION_TOKEN_SURFACE"


class FakeAiCommand:
    def __init__(self, *responses: str):
        self.responses = list(responses)
        self.prompts: list[str] = []

    def invoke(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.responses.pop(0)


class UnrepairableClassifier:
    """Classify every violation as unrepairable so the loop terminates RED at
    the ``ALL_REMAINING_UNREPAIRABLE_OR_PRE_EXISTING`` branch (no green path)."""

    def classify(self, violations, *, baseline_ref=None):
        return RepairabilityClassification(unrepairable=list(violations))


def _analyze_response() -> str:
    return (
        '{"probable_cause":"impl drift","affected_nodes":["src/login.py"],'
        '"repair_strategy":"full_file_replacement","confidence":0.6}'
    )


def _propose_response() -> str:
    return (
        '{"patches":[{"file_path":"src/login.py","patch_mode":"full_file_replacement",'
        '"content":"x"}],"rationale":"align impl","confidence":0.6}'
    )


def _rca() -> RootCauseAnalysis:
    return RootCauseAnalysis(
        probable_cause="impl drift",
        affected_nodes=["src/login.py"],
        repair_strategy="full_file_replacement",
        confidence=0.6,
        analysis_timestamp="2026-07-09T00:00:00Z",
    )


def _write_design_doc(root: Path, rel: str = "docs/login.md") -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Login\n\n"
        f"The login flow authenticates a user and returns a session_token. "
        f"{DESIGN_BODY_MARKER} pins the session_token field on the response.\n",
        encoding="utf-8",
    )
    return rel


def _design_dag(doc_id: str = "docs/login.md", impl_id: str = "src/login.py") -> DAG:
    dag = DAG()
    dag.add_node(Node(doc_id, "design_doc", doc_id, {"depends_on": []}))
    dag.add_node(Node(impl_id, "implementation", impl_id, {}))
    dag.add_edge(Edge(doc_id, impl_id, "expects"))
    return dag


def _orphan_dag(impl_id: str = "src/orphan.py") -> DAG:
    """A failure whose node maps to NO design doc."""
    dag = DAG()
    dag.add_node(Node(impl_id, "implementation", impl_id, {}))
    return dag


def _failure(node: str, message: str) -> VerificationFailureReport:
    return VerificationFailureReport(
        check_name="verification_test",
        failed_nodes=[node],
        error_messages=[message],
        dag_snapshot={"nodes": [{"id": node}], "edges": []},
        timestamp="2026-07-09T00:00:00Z",
    )


# ── PART A: design-context injection into the analyze / propose prompt ──────────


def test_analyze_prompt_contains_design_body_when_nodes_map_to_docs(tmp_path: Path):
    doc_id = _write_design_doc(tmp_path)
    ai = FakeAiCommand(_analyze_response())
    engine = LlmRepairEngine(project_root=tmp_path, ai_command={"repair_analyze": ai})

    engine.analyze(_failure(doc_id, "impl missing session_token"), _design_dag(doc_id))
    prompt = ai.prompts[0]

    assert DESIGN_BODY_MARKER in prompt
    assert DESIGN_CONTEXT_RULE in prompt


def test_analyze_prompt_unchanged_when_nodes_do_not_map_to_docs(tmp_path: Path):
    ai = FakeAiCommand(_analyze_response())
    engine = LlmRepairEngine(project_root=tmp_path, ai_command={"repair_analyze": ai})

    engine.analyze(_failure("src/orphan.py", "impl broken"), _orphan_dag())
    prompt = ai.prompts[0]

    assert DESIGN_BODY_MARKER not in prompt
    assert DESIGN_CONTEXT_RULE not in prompt


def test_propose_prompt_contains_design_body_after_analyze(tmp_path: Path):
    doc_id = _write_design_doc(tmp_path)
    analyze_ai = FakeAiCommand(_analyze_response())
    propose_ai = FakeAiCommand(_propose_response())
    engine = LlmRepairEngine(
        project_root=tmp_path,
        ai_command={"repair_analyze": analyze_ai, "repair_propose": propose_ai},
        max_strategy_attempts=1,
    )

    engine.analyze(_failure(doc_id, "impl missing session_token"), _design_dag(doc_id))
    engine.propose_fix(_rca(), {"src/login.py": "x"})
    prompt = propose_ai.prompts[0]

    assert DESIGN_BODY_MARKER in prompt
    assert DESIGN_CONTEXT_RULE in prompt


# ── PART B: deterministic TEST_CONTRACT_OVERREACH terminal label ───────────────


def _run_terminal(tmp_path: Path, failure: VerificationFailureReport, dag: DAG):
    loop = RepairLoop(
        RepairLoopConfig(
            max_attempts=1,
            approval_mode="auto",
            engine_name="llm",
            history_dir=tmp_path / ".codd" / "repair_history",
        ),
        tmp_path,
        repairability_classifier=UnrepairableClassifier(),
    )
    return loop.run(
        failure,
        dag,
        verify_callable=lambda: SimpleNamespace(passed=False),
    )


def test_finalize_emits_overreach_when_tokens_absent_from_design_and_producer(tmp_path: Path):
    doc_id = _write_design_doc(tmp_path)
    # The assertion demands a key that appears in NEITHER the design nor producer.
    failure = _failure(doc_id, "KeyError: 'phantom_capability_xyz'")

    outcome = _run_terminal(tmp_path, failure, _design_dag(doc_id))

    assert outcome.reason == TEST_CONTRACT_OVERREACH_REASON


def test_finalize_keeps_default_reason_when_tokens_present(tmp_path: Path):
    doc_id = _write_design_doc(tmp_path)
    # The assertion binds to 'session_token', which the design DOES pin.
    failure = _failure(doc_id, "KeyError: 'session_token'")

    outcome = _run_terminal(tmp_path, failure, _design_dag(doc_id))

    assert outcome.reason == "ALL_REMAINING_UNREPAIRABLE_OR_PRE_EXISTING"


def test_finalize_keeps_default_reason_when_no_design_corpus(tmp_path: Path):
    # No design doc resolves -> absence is unprovable -> default reason (never
    # a spurious overreach label).
    failure = _failure("src/orphan.py", "KeyError: 'phantom_capability_xyz'")

    outcome = _run_terminal(tmp_path, failure, _orphan_dag())

    assert outcome.reason == "ALL_REMAINING_UNREPAIRABLE_OR_PRE_EXISTING"
