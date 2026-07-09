"""Fable5-authorized F1-F6 repair-loop fixes (dogfood/fable5_reply_2026-07-09_js-verify-repair.md).

Root principle (Inc1-revert): open-world questions get STEERED, never JUDGED. The
repair loop must not abandon its clearest, broadest bugs one round-local strike at
a time while attempt budget remains. These are the §⑤ red-first DoD tests:

1. F2 — engine raises RepairFailed once then succeeds -> loop reaches REPAIR_SUCCESS.
2. F2 — 3 consecutive engine failures -> unrepairable (strike bound holds).
3. F1 — observed test_command violation, no attribution, llm=None -> repairable.
4. F1/D3 — environment_build_error still -> unrepairable (D3 preserved).
5. F3 — propose prompt carries error_messages + evidence file marked read-only.
6. F4 — post-diff-failure retry instructs full_file_replacement; a retry no-patch
   is a strike (F2), not a terminal exception.
7. F5 — repair report carries head+tail beyond a 4000-char tail window.
8. F6 — zero patches + all-unrepairable is NOT PARTIAL_SUCCESS.
9. tests/test_language_free_core_ratchet.py stays green (run separately).

Anti-false-green (Fable5 verified, must stay untouched): GREEN is decided ONLY by
the post-repair verify; F1-F6 grant more attempts + more evidence, never a green
path and never a test edit.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from codd.dag import DAG, Edge, Node
from codd.repair import engine as engine_registry
from codd.repair.engine import RepairEngine, register_repair_engine
from codd.repair.llm_repair_engine import LlmRepairEngine, RepairFailed
from codd.repair.loop import RepairLoop, RepairLoopConfig, RepairabilityClassification
from codd.repair.repairability_classifier import RepairabilityClassifier
from codd.repair.schema import (
    ApplyResult,
    FilePatch,
    RepairProposal,
    RootCauseAnalysis,
    VerificationFailureReport,
)
from codd.repair.verify_runner import _command_output_tail


@dataclass
class VerifyResult:
    passed: bool
    failure: VerificationFailureReport | None = None
    failures: list[object] | None = None


@pytest.fixture(autouse=True)
def isolated_repair_registry(monkeypatch):
    monkeypatch.setattr(engine_registry, "_REPAIR_ENGINES", {})


def _failure(message: str = "evaluate() returned undefined") -> VerificationFailureReport:
    return VerificationFailureReport(
        check_name="test_command",
        failed_nodes=["src/calc.py"],
        error_messages=[message],
        dag_snapshot={"nodes": [{"id": "src/calc.py"}], "edges": []},
        timestamp="2026-07-09T00:00:00Z",
    )


def _rca() -> RootCauseAnalysis:
    return RootCauseAnalysis(
        probable_cause="facade returns undefined",
        affected_nodes=["src/calc.py"],
        repair_strategy="full_file_replacement",
        confidence=0.8,
        analysis_timestamp="2026-07-09T00:00:01Z",
    )


def _proposal() -> RepairProposal:
    return RepairProposal(
        [FilePatch("src/calc.py", "full_file_replacement", "def evaluate(x):\n    return x\n")],
        "return the computed value",
        0.8,
        "2026-07-09T00:00:02Z",
        "2026-07-09T00:00:01Z",
    )


def _dag() -> DAG:
    dag = DAG()
    dag.add_node(Node("design:calc", "design_doc", "docs/calc.md", {}))
    dag.add_node(Node("src/calc.py", "impl_file", "src/calc.py", {}))
    dag.add_edge(Edge("design:calc", "src/calc.py", "expects"))
    return dag


def _write_optin(project: Path, extra: dict | None = None) -> None:
    codd_dir = project / "codd"
    codd_dir.mkdir(exist_ok=True)
    repair = {"allow_auto": {"require_explicit_optin": True}}
    if extra:
        repair.update(extra)
    (codd_dir / "codd.yaml").write_text(yaml.safe_dump({"repair": repair}), encoding="utf-8")


def _register_scripted(
    name: str,
    *,
    propose_errors: list[Exception | None] | None = None,
    apply_errors: list[Exception | None] | None = None,
) -> type[RepairEngine]:
    """Scripted engine: analyze always OK; propose/apply raise from their queues.

    When a queue is exhausted the phase succeeds. Records how many times each
    phase ran so the strike bound (F2) can be asserted.
    """
    proposal = _proposal()

    class ScriptedEngine(RepairEngine):
        propose_calls: int = 0
        apply_calls: int = 0

        def __init__(self, project_root: Path | None = None):
            self.project_root = project_root
            self.propose_errors = list(propose_errors or [])
            self.apply_errors = list(apply_errors or [])

        def analyze(self, failure, dag):
            return _rca()

        def propose_fix(self, rca, file_contents, **kwargs):
            type(self).propose_calls += 1
            if self.propose_errors:
                error = self.propose_errors.pop(0)
                if error is not None:
                    raise error
            return proposal

        def apply(self, proposal, *, dry_run: bool = False):
            type(self).apply_calls += 1
            if self.apply_errors:
                error = self.apply_errors.pop(0)
                if error is not None:
                    raise error
            return ApplyResult(True, [patch.file_path for patch in proposal.patches], [], None)

    ScriptedEngine.propose_calls = 0
    ScriptedEngine.apply_calls = 0
    return register_repair_engine(name)(ScriptedEngine)


def _run(project: Path, name: str, verify_results, *, max_attempts=6, classifier=None):
    loop = RepairLoop(
        RepairLoopConfig(max_attempts=max_attempts, approval_mode="auto", engine_name=name),
        project,
        repairability_classifier=classifier,
    )
    results = list(verify_results)

    def verify():
        return results.pop(0) if results else VerifyResult(True)

    return loop.run(_failure(), _dag(), verify_callable=verify)


# ── Test 1 (F2): one RepairFailed then success -> REPAIR_SUCCESS ────────────────


def test_f2_engine_failure_once_then_success_reaches_repair_success(tmp_path: Path):
    _write_optin(tmp_path)
    _register_scripted("f2-recover", propose_errors=[RepairFailed("bad JSON")])

    outcome = _run(tmp_path, "f2-recover", [VerifyResult(True)], max_attempts=5)

    # Today (HEAD): the single propose exception is a permanent verdict -> the
    # violation is dropped and the loop ends PARTIAL_SUCCESS with nothing fixed.
    assert outcome.status == "REPAIR_SUCCESS"


# ── Test 2 (F2): 3 consecutive engine failures -> unrepairable, strike-bounded ──


def test_f2_three_consecutive_engine_failures_become_unrepairable(tmp_path: Path):
    _write_optin(tmp_path)
    engine_cls = _register_scripted(
        "f2-strikeout",
        propose_errors=[RepairFailed("x"), RepairFailed("x"), RepairFailed("x"), RepairFailed("x")],
    )

    outcome = _run(tmp_path, "f2-strikeout", [], max_attempts=6)

    # Strike bound: exactly engine_failure_strikes (3) attempts on the same
    # violation key, NOT the full budget of 6 (which would prove no bound).
    assert engine_cls.propose_calls == 3
    assert [v.check_name for v in outcome.unrepairable_violations] == ["test_command"]
    # Zero patches + all-unrepairable is a failure status (F6), never partial.
    assert outcome.status == "REPAIR_FAILED"


# ── Test 3 (F1): observed test failure, no attribution, llm=None -> repairable ──


def test_f1_observed_test_failure_without_attribution_is_repairable(tmp_path: Path):
    violation = VerificationFailureReport(
        check_name="test_command",
        failed_nodes=[],  # no attribution fields at all (mocha / node:test hole)
        error_messages=["1) evaluate returns undefined"],
        dag_snapshot={},
        timestamp="2026-07-09T00:00:00Z",
    )
    classifier = RepairabilityClassifier(llm=None, repo_path=tmp_path)

    result = classifier.classify([violation], baseline_ref=None)

    # Today (HEAD): code_addressable False + empty paths -> pending -> llm=None
    # -> unrepairable. F1: an observed test failure is ALWAYS repairable.
    assert result.repairable == [violation]
    assert result.unrepairable == []
    assert result.pre_existing == []


# ── Test 4 (F1/D3): environment_build_error stays unrepairable ──────────────────


def test_d3_environment_build_error_stays_unrepairable(tmp_path: Path):
    env = VerificationFailureReport(
        check_name="test_command",
        failed_nodes=[],
        error_messages=["/bin/sh: node: not found"],
        dag_snapshot={},
        timestamp="2026-07-09T00:00:00Z",
        failure_class="environment_build_error",
        code_addressable=False,
    )
    classifier = RepairabilityClassifier(llm=None, repo_path=tmp_path)

    result = classifier.classify([env], baseline_ref=None)

    assert result.unrepairable == [env]
    assert result.repairable == []


# ── Test 5 (F3): propose prompt carries error_messages + read-only evidence ─────


def test_f3_propose_prompt_contains_error_messages_and_readonly_evidence(tmp_path: Path):
    class CapturingAi:
        def __init__(self):
            self.prompts: list[str] = []

        def invoke(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return (
                '{"patches":[{"file_path":"src/calc.py","patch_mode":"full_file_replacement",'
                '"content":"x"}],"rationale":"fix","confidence":0.6}'
            )

    ai = CapturingAi()
    engine = LlmRepairEngine(project_root=tmp_path, ai_command=ai)

    engine.propose_fix(
        _rca(),
        {"src/calc.py": "def evaluate(x):\n    pass\n"},
        error_messages=["AssertionError: expected undefined to be 14"],
        evidence={"tests/calc.test.js": "expect(evaluate('7*2')).toBe(14)"},
    )
    prompt = ai.prompts[0]

    # The expected-vs-received signal reaches the prompt.
    assert "expected undefined to be 14" in prompt
    # The failing test is present AS READ-ONLY evidence (never an edit target).
    assert "tests/calc.test.js" in prompt
    assert "expect(evaluate('7*2')).toBe(14)" in prompt
    assert "READ-ONLY" in prompt or "IMMUTABLE" in prompt


# ── Test 6 (F4): retry requires full_file_replacement; no-patch is a strike ─────


def test_f4_retry_template_requires_full_file_and_drops_no_patch(tmp_path: Path):
    root = tmp_path
    (root / "sample.txt").write_text("one\n", encoding="utf-8")
    import subprocess

    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)

    class CapturingAi:
        def __init__(self, *responses):
            self.responses = list(responses)
            self.prompts: list[str] = []

        def invoke(self, prompt: str) -> str:
            self.prompts.append(prompt)
            return self.responses.pop(0)

    ai = CapturingAi(
        '{"patches":[{"file_path":"sample.txt","patch_mode":"unified_diff","content":"not a diff"}],'
        '"rationale":"bad","confidence":0.8}',
        '{"patches":[{"file_path":"sample.txt","patch_mode":"full_file_replacement","content":"two\\n"}],'
        '"rationale":"replace","confidence":0.8}',
    )
    rca = RootCauseAnalysis("drift", ["sample.txt"], "unified_diff", 0.8, "2026-07-09T00:00:01Z")

    LlmRepairEngine(project_root=root, ai_command=ai).propose_fix(rca, {"sample.txt": "one\n"})

    retry_prompt = ai.prompts[1]
    assert "full_file_replacement" in retry_prompt
    # F4: the no-patch trapdoor is removed from the retry menu.
    assert "no-patch" not in retry_prompt


def test_f4_retry_no_patch_is_a_strike_not_terminal(tmp_path: Path):
    _write_optin(tmp_path)
    # The engine 'selects no-patch' once (RepairFailed), then proposes a real fix.
    _register_scripted("f4-nopatch", propose_errors=[RepairFailed("repair proposal selected no-patch")])

    outcome = _run(tmp_path, "f4-nopatch", [VerifyResult(True)], max_attempts=5)

    # A no-patch is a strike (F2): the loop retries and reaches GREEN, it does not
    # terminate on the no-patch exception.
    assert outcome.status == "REPAIR_SUCCESS"


# ── Test 7 (F5): repair report carries head+tail beyond 4000 ────────────────────


def test_f5_command_output_report_keeps_head_and_tail(tmp_path: Path):
    stdout = "HEAD_MARKER_START\n" + ("filler line\n" * 4000) + "TAIL_MARKER_END"

    out = _command_output_tail(stdout, "")

    # Today (HEAD): only the last 4000 chars survive -> the head is gone.
    assert "HEAD_MARKER_START" in out
    assert "TAIL_MARKER_END" in out
    assert len(out) > 4000


# ── Test 8 (F6): zero patches + all-unrepairable is NOT partial success ─────────


def test_f6_zero_patches_all_unrepairable_is_not_partial_success(tmp_path: Path):
    _write_optin(tmp_path)
    _register_scripted("f6-noop")

    class UnrepairableClassifier:
        def classify(self, violations, *, baseline_ref=None):
            return RepairabilityClassification(unrepairable=list(violations))

    outcome = _run(tmp_path, "f6-noop", [], classifier=UnrepairableClassifier())

    assert outcome.status != "PARTIAL_SUCCESS"
    assert outcome.status == "REPAIR_FAILED"
    assert outcome.partial_success_patches == []
