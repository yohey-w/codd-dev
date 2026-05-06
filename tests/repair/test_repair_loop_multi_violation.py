from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner
import pytest
import yaml

import codd.cli as cli
from codd.cli import main
from codd.dag import DAG, Edge, Node
from codd.repair import engine as engine_registry
from codd.repair.engine import RepairEngine, register_repair_engine
from codd.repair.loop import (
    FirstViolationPicker,
    NullRepairabilityClassifier,
    RepairLoop,
    RepairLoopConfig,
    RepairabilityClassification,
)
from codd.repair.repair_result import RepairResult
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
    failures: list[object] | None = None


@pytest.fixture(autouse=True)
def isolated_repair_registry(monkeypatch):
    monkeypatch.setattr(engine_registry, "_REPAIR_ENGINES", {})


def _failure(name: str = "check_a", node: str = "node:a", message: str = "failed") -> VerificationFailureReport:
    return VerificationFailureReport(
        check_name=name,
        failed_nodes=[node],
        error_messages=[message],
        dag_snapshot={"nodes": [{"id": node}], "edges": []},
        timestamp="2026-05-06T00:00:00Z",
    )


def _dag() -> DAG:
    dag = DAG()
    dag.add_node(Node("node:a", "requirement", "docs/a.md", {}))
    dag.add_node(Node("node:b", "implementation", "src/b.py", {}))
    dag.add_edge(Edge("node:a", "node:b", "expects"))
    return dag


def _write_repair_config(project: Path) -> None:
    codd_dir = project / "codd"
    codd_dir.mkdir(exist_ok=True)
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump({"repair": {"allow_auto": {"require_explicit_optin": True}}}),
        encoding="utf-8",
    )


def _proposal(name: str = "src/generated.py") -> RepairProposal:
    return RepairProposal(
        [FilePatch(name, "full_file_replacement", "value = True\n")],
        "repair selected failure",
        0.8,
        "2026-05-06T00:00:02Z",
        "2026-05-06T00:00:01Z",
    )


def _register_engine(
    name: str,
    *,
    apply_results: list[ApplyResult] | None = None,
    propose_errors: list[Exception | None] | None = None,
    apply_errors: list[Exception | None] | None = None,
) -> type[RepairEngine]:
    results = list(apply_results or [])
    proposal_failures = list(propose_errors or [])
    apply_failures = list(apply_errors or [])

    class ScriptedEngine(RepairEngine):
        analyzed: list[VerificationFailureReport] = []
        proposals: list[RepairProposal] = []

        def __init__(self, project_root: Path | None = None):
            self.project_root = project_root
            self.apply_results = list(results)
            self.propose_errors = list(proposal_failures)
            self.apply_errors = list(apply_failures)

        def analyze(self, failure: VerificationFailureReport, dag: DAG) -> RootCauseAnalysis:
            type(self).analyzed.append(failure)
            return RootCauseAnalysis(
                "selected failure requires a patch",
                list(failure.failed_nodes),
                "full_file_replacement",
                0.8,
                "2026-05-06T00:00:01Z",
            )

        def propose_fix(self, rca: RootCauseAnalysis, file_contents: dict[str, str]) -> RepairProposal:
            if self.propose_errors:
                error = self.propose_errors.pop(0)
                if error is not None:
                    raise error
            proposal = _proposal(f"src/{len(type(self).proposals)}.py")
            type(self).proposals.append(proposal)
            return proposal

        def apply(self, proposal: RepairProposal, *, dry_run: bool = False) -> ApplyResult:
            if self.apply_errors:
                error = self.apply_errors.pop(0)
                if error is not None:
                    raise error
            if self.apply_results:
                return self.apply_results.pop(0)
            return ApplyResult(True, [patch.file_path for patch in proposal.patches], [], None)

    return register_repair_engine(name)(ScriptedEngine)


def _run_loop(
    project: Path,
    engine_name: str,
    verify_results: list[VerifyResult],
    *,
    initial_verify_result: VerifyResult | None = None,
    max_attempts: int = 10,
    classifier: object | None = None,
    picker: object | None = None,
    baseline_ref: str | None = None,
):
    _write_repair_config(project)
    if isinstance(initial_verify_result, dict):
        raw_failure = initial_verify_result.get("failure")
    else:
        raw_failure = initial_verify_result.failure if initial_verify_result and initial_verify_result.failure else None
    failure = raw_failure if isinstance(raw_failure, VerificationFailureReport) else _failure()
    loop = RepairLoop(
        RepairLoopConfig(max_attempts=max_attempts, approval_mode="auto", engine_name=engine_name),
        project,
        repairability_classifier=classifier,
        primary_picker=picker,
    )

    def verify():
        return verify_results.pop(0)

    return loop.run(
        failure,
        _dag(),
        verify_callable=verify,
        baseline_ref=baseline_ref,
        initial_verify_result=initial_verify_result,
    )


def test_config_default_max_attempts_is_ten():
    assert RepairLoopConfig().max_attempts == 10


def test_default_baseline_ref_captures_current_head(tmp_path: Path, monkeypatch):
    _register_engine("baseline-default")
    classifier = SimpleNamespace(seen=[])

    def classify(violations, *, baseline_ref=None):
        classifier.seen.append(baseline_ref)
        return RepairabilityClassification(repairable=list(violations))

    classifier.classify = classify
    _write_repair_config(tmp_path)
    loop = RepairLoop(
        RepairLoopConfig(approval_mode="auto", engine_name="baseline-default"),
        tmp_path,
        repairability_classifier=classifier,
    )
    monkeypatch.setattr(loop, "_capture_current_head", lambda: "abc123")

    outcome = loop.run(_failure(), _dag(), verify_callable=lambda: VerifyResult(True))

    assert outcome.status == "REPAIR_SUCCESS"
    assert outcome.baseline_ref == "abc123"
    assert classifier.seen == ["abc123"]


def test_explicit_baseline_ref_is_passed_to_classifier(tmp_path: Path):
    _register_engine("baseline-explicit")
    seen: list[str | None] = []

    class CapturingClassifier:
        def classify(self, violations, *, baseline_ref=None):
            seen.append(baseline_ref)
            return RepairabilityClassification(repairable=list(violations))

    _run_loop(
        tmp_path,
        "baseline-explicit",
        [VerifyResult(True)],
        classifier=CapturingClassifier(),
        baseline_ref="HEAD~2",
    )

    assert seen == ["HEAD~2"]


def test_initial_verify_failures_are_expanded_into_sequential_repairs(tmp_path: Path):
    engine_cls = _register_engine("initial-failures")
    first = _failure("check_a", "node:a")
    second = _failure("check_b", "node:b")
    initial = VerifyResult(False, first, [first, second])

    outcome = _run_loop(
        tmp_path,
        "initial-failures",
        [VerifyResult(False, second, [second]), VerifyResult(True)],
        initial_verify_result=initial,
    )

    assert outcome.status == "REPAIR_SUCCESS"
    assert [item.check_name for item in engine_cls.analyzed] == ["check_a", "check_b"]


def test_verify_result_failures_drive_next_primary_selection(tmp_path: Path):
    engine_cls = _register_engine("next-failures")
    first = _failure("check_a", "node:a")
    second = _failure("check_b", "node:b")

    outcome = _run_loop(
        tmp_path,
        "next-failures",
        [VerifyResult(False, second, [second]), VerifyResult(True)],
        initial_verify_result=VerifyResult(False, first, [first]),
    )

    assert outcome.status == "REPAIR_SUCCESS"
    assert [item.failed_nodes[0] for item in engine_cls.analyzed] == ["node:a", "node:b"]


def test_max_attempts_with_applied_patch_returns_partial_success(tmp_path: Path):
    _register_engine("partial-max")

    outcome = _run_loop(
        tmp_path,
        "partial-max",
        [VerifyResult(False, _failure("check_b", "node:b"), [_failure("check_b", "node:b")])],
        max_attempts=1,
    )

    assert outcome.status == "PARTIAL_SUCCESS"
    assert outcome.success is True
    assert outcome.remaining_violations[0].check_name == "check_b"


def test_max_attempts_without_applied_patch_returns_max_attempts_reached(tmp_path: Path):
    _register_engine("max-no-patch", apply_results=[ApplyResult(False, [], ["src/0.py"], "failed")])

    outcome = _run_loop(tmp_path, "max-no-patch", [], max_attempts=1)

    assert outcome.status == "MAX_ATTEMPTS_REACHED"
    assert outcome.success is False
    assert outcome.partial_success_patches == []


def test_run_max_attempts_argument_overrides_config(tmp_path: Path):
    _register_engine("run-max-override")
    _write_repair_config(tmp_path)
    loop = RepairLoop(
        RepairLoopConfig(max_attempts=10, approval_mode="auto", engine_name="run-max-override"),
        tmp_path,
    )

    outcome = loop.run(
        _failure(),
        _dag(),
        verify_callable=lambda: VerifyResult(False, _failure("check_b", "node:b")),
        max_attempts=1,
    )

    assert outcome.status == "PARTIAL_SUCCESS"
    assert len(outcome.attempts) == 1


def test_partial_success_maps_to_repair_result(tmp_path: Path):
    _register_engine("partial-result")

    outcome = _run_loop(tmp_path, "partial-result", [VerifyResult(False, _failure("check_b", "node:b"))], max_attempts=1)
    result = outcome.to_repair_result()

    assert isinstance(result, RepairResult)
    assert result.status == "PARTIAL_SUCCESS"
    assert result.success is True
    assert result.partial_success_patches == ["src/0.py"]


def test_final_status_persists_remaining_violations_and_patches(tmp_path: Path):
    _register_engine("partial-history")
    remaining = _failure("check_b", "node:b")

    outcome = _run_loop(tmp_path, "partial-history", [VerifyResult(False, remaining)], max_attempts=1)
    final_status = yaml.safe_load((outcome.history_session_dir / "final_status.yaml").read_text(encoding="utf-8"))

    assert final_status["outcome"] == "PARTIAL_SUCCESS"
    assert final_status["partial_success_patches"] == ["src/0.py"]
    assert final_status["remaining_violations"][0]["check_name"] == "check_b"


def test_all_pre_existing_without_patch_returns_repair_failed(tmp_path: Path):
    _register_engine("pre-existing-none")

    class PreExistingClassifier:
        def classify(self, violations, *, baseline_ref=None):
            return RepairabilityClassification(pre_existing=list(violations))

    outcome = _run_loop(tmp_path, "pre-existing-none", [], classifier=PreExistingClassifier())

    assert outcome.status == "REPAIR_FAILED"
    assert outcome.pre_existing_violations[0].check_name == "check_a"


def test_pre_existing_after_patch_returns_partial_success(tmp_path: Path):
    _register_engine("pre-existing-after")
    remaining = _failure("check_b", "node:b")

    class SequencedClassifier:
        def __init__(self):
            self.calls = 0

        def classify(self, violations, *, baseline_ref=None):
            self.calls += 1
            if self.calls == 1:
                return RepairabilityClassification(repairable=list(violations))
            return RepairabilityClassification(pre_existing=list(violations))

    outcome = _run_loop(
        tmp_path,
        "pre-existing-after",
        [VerifyResult(False, remaining, [remaining])],
        classifier=SequencedClassifier(),
    )

    assert outcome.status == "PARTIAL_SUCCESS"
    assert outcome.pre_existing_violations == [remaining]


def test_all_unrepairable_without_patch_returns_repair_failed(tmp_path: Path):
    _register_engine("unrepairable-none")

    class UnrepairableClassifier:
        def classify(self, violations, *, baseline_ref=None):
            return RepairabilityClassification(unrepairable=list(violations))

    outcome = _run_loop(tmp_path, "unrepairable-none", [], classifier=UnrepairableClassifier())

    assert outcome.status == "REPAIR_FAILED"
    assert outcome.unrepairable_violations[0].check_name == "check_a"


def test_unrepairable_after_patch_returns_partial_success(tmp_path: Path):
    _register_engine("unrepairable-after")
    remaining = _failure("check_b", "node:b")

    class SequencedClassifier:
        def __init__(self):
            self.calls = 0

        def classify(self, violations, *, baseline_ref=None):
            self.calls += 1
            if self.calls == 1:
                return RepairabilityClassification(repairable=list(violations))
            return RepairabilityClassification(unrepairable=list(violations))

    outcome = _run_loop(
        tmp_path,
        "unrepairable-after",
        [VerifyResult(False, remaining, [remaining])],
        classifier=SequencedClassifier(),
    )

    assert outcome.status == "PARTIAL_SUCCESS"
    assert outcome.unrepairable_violations == [remaining]


def test_propose_fix_exception_marks_violation_unrepairable_without_patch(tmp_path: Path):
    _register_engine("propose-exception-none", propose_errors=[RuntimeError("cannot propose")])

    outcome = _run_loop(tmp_path, "propose-exception-none", [], max_attempts=3)

    assert outcome.status == "REPAIR_FAILED"
    assert outcome.attempts == []
    assert outcome.error_message == "cannot propose"
    assert outcome.unrepairable_violations[0].check_name == "check_a"
    assert outcome.remaining_violations == outcome.unrepairable_violations
    assert outcome.reason == "ALL_REMAINING_UNREPAIRABLE_OR_PRE_EXISTING"


def test_propose_fix_exception_continues_to_next_violation(tmp_path: Path):
    engine_cls = _register_engine("propose-exception-continue", propose_errors=[RuntimeError("first cannot propose")])
    first = _failure("check_a", "node:a")
    second = _failure("check_b", "node:b")

    outcome = _run_loop(
        tmp_path,
        "propose-exception-continue",
        [VerifyResult(True)],
        initial_verify_result=VerifyResult(False, first, [first, second]),
    )

    assert outcome.status == "REPAIR_SUCCESS"
    assert [item.check_name for item in engine_cls.analyzed] == ["check_a", "check_b"]
    assert outcome.unrepairable_violations == [first]
    assert outcome.partial_success_patches == ["src/0.py"]


def test_propose_fix_exception_after_patch_returns_partial_success(tmp_path: Path):
    _register_engine("propose-exception-after-patch", propose_errors=[None, RuntimeError("second cannot propose")])
    remaining = _failure("check_b", "node:b")

    outcome = _run_loop(
        tmp_path,
        "propose-exception-after-patch",
        [VerifyResult(False, remaining, [remaining])],
    )

    assert outcome.status == "PARTIAL_SUCCESS"
    assert outcome.error_message == "second cannot propose"
    assert outcome.partial_success_patches == ["src/0.py"]
    assert outcome.unrepairable_violations == [remaining]


def test_apply_exception_marks_violation_unrepairable_and_continues(tmp_path: Path):
    engine_cls = _register_engine("apply-exception-continue", apply_errors=[RuntimeError("patch exploded")])
    first = _failure("check_a", "node:a")
    second = _failure("check_b", "node:b")

    outcome = _run_loop(
        tmp_path,
        "apply-exception-continue",
        [VerifyResult(True)],
        initial_verify_result=VerifyResult(False, first, [first, second]),
    )

    assert outcome.status == "REPAIR_SUCCESS"
    assert [item.check_name for item in engine_cls.analyzed] == ["check_a", "check_b"]
    assert outcome.attempts[0].apply_result.error_message == "patch exploded"
    assert outcome.unrepairable_violations == [first]
    assert outcome.partial_success_patches == ["src/1.py"]


def test_apply_exception_without_other_violations_returns_repair_failed(tmp_path: Path):
    _register_engine("apply-exception-none", apply_errors=[RuntimeError("patch exploded")])

    outcome = _run_loop(tmp_path, "apply-exception-none", [], max_attempts=3)

    assert outcome.status == "REPAIR_FAILED"
    assert len(outcome.attempts) == 1
    assert outcome.error_message == "patch exploded"
    assert outcome.unrepairable_violations[0].check_name == "check_a"
    assert outcome.partial_success_patches == []


def test_null_classifier_marks_all_violations_repairable():
    first = _failure("check_a")
    second = _failure("check_b")

    result = NullRepairabilityClassifier().classify([first, second], baseline_ref="HEAD")

    assert result.repairable == [first, second]
    assert result.pre_existing == []
    assert result.unrepairable == []


def test_first_violation_picker_returns_first_violation():
    first = _failure("check_a")
    second = _failure("check_b")

    assert FirstViolationPicker().pick([first, second], dag=_dag()) is first


def test_custom_picker_receives_dag_and_can_choose_later_violation(tmp_path: Path):
    engine_cls = _register_engine("custom-picker")
    first = _failure("check_a", "node:a")
    second = _failure("check_b", "node:b")

    class LastPicker:
        def __init__(self):
            self.dag = None

        def pick(self, violations, *, dag=None):
            self.dag = dag
            return violations[-1]

    picker = LastPicker()
    outcome = _run_loop(
        tmp_path,
        "custom-picker",
        [VerifyResult(True)],
        initial_verify_result=VerifyResult(False, first, [first, second]),
        picker=picker,
    )

    assert outcome.status == "REPAIR_SUCCESS"
    assert picker.dag is not None
    assert engine_cls.analyzed[0] is second


def test_picker_without_dag_keyword_is_supported(tmp_path: Path):
    engine_cls = _register_engine("picker-positional")
    first = _failure("check_a", "node:a")
    second = _failure("check_b", "node:b")

    class PositionalPicker:
        def pick(self, violations, dag):
            return violations[-1]

    _run_loop(
        tmp_path,
        "picker-positional",
        [VerifyResult(True)],
        initial_verify_result=VerifyResult(False, first, [first, second]),
        picker=PositionalPicker(),
    )

    assert engine_cls.analyzed[0] is second


def test_classifier_without_baseline_keyword_is_supported(tmp_path: Path):
    _register_engine("classifier-no-baseline")
    seen: list[int] = []

    class SimpleClassifier:
        def classify(self, violations):
            seen.append(len(violations))
            return RepairabilityClassification(repairable=list(violations))

    outcome = _run_loop(tmp_path, "classifier-no-baseline", [VerifyResult(True)], classifier=SimpleClassifier())

    assert outcome.status == "REPAIR_SUCCESS"
    assert seen == [1]


def test_mapping_verify_result_violations_are_supported(tmp_path: Path):
    engine_cls = _register_engine("mapping-violations")
    first = _failure("check_a", "node:a")
    second = _failure("check_b", "node:b")

    _run_loop(
        tmp_path,
        "mapping-violations",
        [{"passed": True}],  # type: ignore[list-item]
        initial_verify_result={"passed": False, "violations": [first, second]},  # type: ignore[arg-type]
    )

    assert engine_cls.analyzed[0] is first


def test_mapping_classifier_output_is_supported(tmp_path: Path):
    engine_cls = _register_engine("mapping-classifier")
    first = _failure("check_a", "node:a")
    second = _failure("check_b", "node:b")

    class MappingClassifier:
        def classify(self, violations, *, baseline_ref=None):
            return {"repairable": [second], "pre_existing": [first], "unrepairable": []}

    outcome = _run_loop(
        tmp_path,
        "mapping-classifier",
        [VerifyResult(True)],
        initial_verify_result=VerifyResult(False, first, [first, second]),
        classifier=MappingClassifier(),
    )

    assert outcome.status == "REPAIR_SUCCESS"
    assert engine_cls.analyzed[0] is second


def test_dict_violation_is_coerced_to_failure_report(tmp_path: Path):
    engine_cls = _register_engine("dict-violation")
    fallback = _failure("fallback", "node:fallback")

    _run_loop(
        tmp_path,
        "dict-violation",
        [VerifyResult(True)],
        initial_verify_result={"passed": False, "violations": [{"check_name": "check_dict", "message": "bad", "details": {"node_id": "node:x"}}], "failure": fallback},  # type: ignore[arg-type]
    )

    assert engine_cls.analyzed[0].check_name == "check_dict"
    assert engine_cls.analyzed[0].failed_nodes == ["node:x"]


def test_verify_cli_passes_baseline_ref_to_repair_loop(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    _write_repair_config(project)
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "_run_verify_once", lambda **kwargs: cli._CliVerificationResult(False, 1, _failure()))

    def run_loop(project_root, failure, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(status="REPAIR_SUCCESS", history_session_dir=project / ".codd" / "repair_history" / "x")

    monkeypatch.setattr(cli, "_run_repair_loop", run_loop)

    result = CliRunner().invoke(main, ["verify", "--path", str(project), "--auto-repair", "--baseline-ref", "HEAD~1"])

    assert result.exit_code == 0
    assert captured["baseline_ref"] == "HEAD~1"


def test_repair_command_passes_baseline_ref_to_repair_loop(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    _write_repair_config(project)
    report = tmp_path / "failure.yaml"
    report.write_text(yaml.safe_dump({"check_name": "check_a", "failed_nodes": ["node:a"], "error_messages": ["bad"]}), encoding="utf-8")
    captured: dict[str, object] = {}

    def run_loop(project_root, failure, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(status="REPAIR_SUCCESS", history_session_dir=project / ".codd" / "repair_history" / "x")

    monkeypatch.setattr(cli, "_run_repair_loop", run_loop)

    result = CliRunner().invoke(main, ["repair", "--from-report", str(report), "--path", str(project), "--baseline-ref", "main"])

    assert result.exit_code == 0
    assert captured["baseline_ref"] == "main"


def test_repair_max_attempts_default_and_invalid_values_are_ten():
    assert cli._repair_max_attempts({}, None) == 10
    assert cli._repair_max_attempts({"max_attempts": "bad"}, None) == 10
