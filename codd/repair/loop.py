"""Repair loop orchestration."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
import subprocess
from typing import Any, Callable, Literal, Mapping

from codd.config import load_project_config
from codd.dag import DAG
from codd.repair.approval_repair import (
    RepairApprovalError,
    RepairApprovalMode,
    approve_repair_proposal,
)
from codd.repair.engine import get_repair_engine
from codd.repair.history import RepairHistory
from codd.repair.repair_result import RepairResult
from codd.repair.schema import (
    ApplyResult,
    RepairProposal,
    RootCauseAnalysis,
    VerificationFailureReport,
)


RepairLoopStatus = Literal[
    "REPAIR_SUCCESS",
    "PARTIAL_SUCCESS",
    "MAX_ATTEMPTS_REACHED",
    "REPAIR_EXHAUSTED",
    "REPAIR_REJECTED_BY_HITL",
    "REPAIR_FAILED",
]


@dataclass
class RepairLoopConfig:
    max_attempts: int = 10
    approval_mode: RepairApprovalMode = "required"
    history_dir: Path = field(default_factory=lambda: Path(".codd/repair_history"))
    engine_name: str = "llm"
    notify_callable: Callable[[str], None] | None = None


@dataclass
class RepairAttemptRecord:
    attempt_n: int
    failure_report: VerificationFailureReport
    rca: RootCauseAnalysis
    proposal: RepairProposal
    apply_result: ApplyResult
    post_verify_passed: bool | None


@dataclass
class RepairabilityClassification:
    repairable: list[VerificationFailureReport] = field(default_factory=list)
    pre_existing: list[VerificationFailureReport] = field(default_factory=list)
    unrepairable: list[VerificationFailureReport] = field(default_factory=list)


class NullRepairabilityClassifier:
    def classify(
        self,
        violations: list[VerificationFailureReport],
        *,
        baseline_ref: str | None = None,
    ) -> RepairabilityClassification:
        return RepairabilityClassification(repairable=list(violations))


class FirstViolationPicker:
    def pick(self, violations: list[VerificationFailureReport], *, dag: DAG | None = None) -> VerificationFailureReport:
        if not violations:
            raise ValueError("at least one repairable violation is required")
        return violations[0]


@dataclass
class RepairLoopOutcome:
    status: RepairLoopStatus
    attempts: list[RepairAttemptRecord]
    history_session_dir: Path
    error_message: str | None = None
    pre_existing_violations: list[VerificationFailureReport] = field(default_factory=list)
    unrepairable_violations: list[VerificationFailureReport] = field(default_factory=list)
    remaining_violations: list[VerificationFailureReport] = field(default_factory=list)
    partial_success_patches: list[str] = field(default_factory=list)
    baseline_ref: str | None = None
    reason: str = ""

    @property
    def success(self) -> bool:
        return self.status in {"REPAIR_SUCCESS", "PARTIAL_SUCCESS"}

    def to_repair_result(self) -> RepairResult:
        status = {
            "REPAIR_SUCCESS": "SUCCESS",
            "PARTIAL_SUCCESS": "PARTIAL_SUCCESS",
            "MAX_ATTEMPTS_REACHED": "MAX_ATTEMPTS_REACHED",
        }.get(self.status, "REPAIR_FAILED")
        return RepairResult(
            success=self.success,
            status=status,  # type: ignore[arg-type]
            attempts=len(self.attempts),
            applied_patches=list(self.partial_success_patches),
            pre_existing_violations=list(self.pre_existing_violations),
            unrepairable_violations=list(self.unrepairable_violations),
            remaining_violations=list(self.remaining_violations),
            partial_success_patches=list(self.partial_success_patches),
            reason=self.reason or self.status,
        )


class RepairLoop:
    def __init__(
        self,
        config: RepairLoopConfig,
        project_root: Path,
        *,
        repairability_classifier: Any | None = None,
        primary_picker: Any | None = None,
    ):
        self.config = config
        self.project_root = Path(project_root)
        self.history = RepairHistory()
        self.repairability_classifier = repairability_classifier or _default_repairability_classifier()
        self.primary_picker = primary_picker or _default_primary_picker()

    def run(
        self,
        failure: VerificationFailureReport,
        dag: DAG,
        *,
        verify_callable: Callable[[], Any],
        max_attempts: int | None = None,
        baseline_ref: str | None = None,
        initial_verify_result: Any | None = None,
    ) -> RepairLoopOutcome:
        """Run repair attempts until verification passes or policy stops the loop."""

        session_dir = self.history.new_session(self._history_dir())
        attempts: list[RepairAttemptRecord] = []
        codd_yaml = self._load_codd_yaml()
        resolved_baseline_ref = baseline_ref or self._capture_current_head()
        current_violations = _violations_from_verify_result(initial_verify_result, fallback=failure)
        applied_patch_files: list[str] = []
        pre_existing: list[VerificationFailureReport] = []
        unrepairable: list[VerificationFailureReport] = []

        try:
            engine = self._new_engine()
        except (KeyError, TypeError, ValueError) as exc:
            return self._finalize(
                session_dir,
                "REPAIR_FAILED",
                attempts,
                str(exc),
                baseline_ref=resolved_baseline_ref,
                reason=str(exc),
            )

        current_failure = failure
        effective_max_attempts = _positive_attempts(max_attempts if max_attempts is not None else self.config.max_attempts)
        for attempt_n in range(effective_max_attempts):
            classification = self._classify_violations(current_violations, resolved_baseline_ref)
            pre_existing = classification.pre_existing
            unrepairable = classification.unrepairable
            if not classification.repairable:
                status: RepairLoopStatus = "PARTIAL_SUCCESS" if applied_patch_files else "REPAIR_FAILED"
                return self._finalize(
                    session_dir,
                    status,
                    attempts,
                    None,
                    pre_existing_violations=pre_existing,
                    unrepairable_violations=unrepairable,
                    remaining_violations=pre_existing + unrepairable,
                    partial_success_patches=applied_patch_files,
                    baseline_ref=resolved_baseline_ref,
                    reason="ALL_REMAINING_UNREPAIRABLE_OR_PRE_EXISTING",
                )

            current_failure = self._pick_primary_violation(classification.repairable, dag)
            try:
                rca = engine.analyze(current_failure, dag)
                file_contents = self._load_affected_file_contents(rca, dag)
                proposal = engine.propose_fix(rca, file_contents)
            except Exception as exc:  # noqa: BLE001 - repair engines are plug-ins.
                return self._finalize(
                    session_dir,
                    "REPAIR_FAILED",
                    attempts,
                    str(exc),
                    remaining_violations=current_violations,
                    baseline_ref=resolved_baseline_ref,
                    reason=str(exc),
                )

            try:
                approved = approve_repair_proposal(
                    proposal,
                    approval_mode=self.config.approval_mode,
                    codd_yaml=codd_yaml,
                    notify_callable=self.config.notify_callable,
                )
            except RepairApprovalError as exc:
                apply_result = ApplyResult(False, [], _proposal_files(proposal), str(exc))
                attempts.append(
                    self._record_attempt(
                        session_dir,
                        attempt_n,
                        current_failure,
                        rca,
                        proposal,
                        apply_result,
                        None,
                    )
                )
                return self._finalize(
                    session_dir,
                    "REPAIR_FAILED",
                    attempts,
                    str(exc),
                    remaining_violations=current_violations,
                    partial_success_patches=applied_patch_files,
                    baseline_ref=resolved_baseline_ref,
                    reason=str(exc),
                )

            if not approved:
                apply_result = ApplyResult(False, [], _proposal_files(proposal), "repair proposal rejected")
                attempts.append(
                    self._record_attempt(
                        session_dir,
                        attempt_n,
                        current_failure,
                        rca,
                        proposal,
                        apply_result,
                        None,
                    )
                )
                return self._finalize(
                    session_dir,
                    "REPAIR_REJECTED_BY_HITL",
                    attempts,
                    None,
                    remaining_violations=current_violations,
                    partial_success_patches=applied_patch_files,
                    baseline_ref=resolved_baseline_ref,
                    reason="REPAIR_REJECTED_BY_HITL",
                )

            try:
                apply_result = engine.apply(proposal)
            except Exception as exc:  # noqa: BLE001 - repair engines are plug-ins.
                apply_result = ApplyResult(False, [], _proposal_files(proposal), str(exc))

            verify_result = None
            post_verify_passed: bool | None = None
            if apply_result.success:
                applied_patch_files.extend(_applied_patch_files(apply_result, proposal))
                verify_result = verify_callable()
                post_verify_passed = _verification_passed(verify_result)

            attempts.append(
                self._record_attempt(
                    session_dir,
                    attempt_n,
                    current_failure,
                    rca,
                    proposal,
                    apply_result,
                    verify_result,
                )
            )

            if not apply_result.success:
                continue
            if post_verify_passed:
                return self._finalize(
                    session_dir,
                    "REPAIR_SUCCESS",
                    attempts,
                    None,
                    partial_success_patches=applied_patch_files,
                    baseline_ref=resolved_baseline_ref,
                    reason="REPAIR_SUCCESS",
                )

            current_violations = _violations_from_verify_result(verify_result, fallback=current_failure)
            current_failure = current_violations[0]

        status = "PARTIAL_SUCCESS" if applied_patch_files else "MAX_ATTEMPTS_REACHED"
        return self._finalize(
            session_dir,
            status,
            attempts,
            None,
            pre_existing_violations=pre_existing,
            unrepairable_violations=unrepairable,
            remaining_violations=current_violations,
            partial_success_patches=applied_patch_files,
            baseline_ref=resolved_baseline_ref,
            reason="MAX_ATTEMPTS_REACHED",
        )

    def _new_engine(self) -> Any:
        engine_cls = get_repair_engine(self.config.engine_name)
        try:
            return engine_cls(project_root=self.project_root)
        except TypeError:
            return engine_cls()

    def _record_attempt(
        self,
        session_dir: Path,
        attempt_n: int,
        failure: VerificationFailureReport,
        rca: RootCauseAnalysis,
        proposal: RepairProposal,
        apply_result: ApplyResult,
        verify_result: Any,
    ) -> RepairAttemptRecord:
        post_verify_passed = _verification_passed(verify_result) if verify_result is not None else None
        self.history.record_attempt(
            session_dir,
            attempt_n,
            failure,
            rca,
            proposal,
            apply_result,
            _to_plain_data(verify_result) if verify_result is not None else None,
        )
        return RepairAttemptRecord(attempt_n, failure, rca, proposal, apply_result, post_verify_passed)

    def _finalize(
        self,
        session_dir: Path,
        status: RepairLoopStatus,
        attempts: list[RepairAttemptRecord],
        error_message: str | None,
        *,
        pre_existing_violations: list[VerificationFailureReport] | None = None,
        unrepairable_violations: list[VerificationFailureReport] | None = None,
        remaining_violations: list[VerificationFailureReport] | None = None,
        partial_success_patches: list[str] | None = None,
        baseline_ref: str | None = None,
        reason: str = "",
    ) -> RepairLoopOutcome:
        pre_existing = list(pre_existing_violations or [])
        unrepairable = list(unrepairable_violations or [])
        remaining = list(remaining_violations or [])
        patches = list(partial_success_patches or [])
        self.history.finalize(
            session_dir,
            status,
            {
                "reason": reason or status,
                "baseline_ref": baseline_ref,
                "partial_success_patches": patches,
                "pre_existing_violations": pre_existing,
                "unrepairable_violations": unrepairable,
                "remaining_violations": remaining,
            },
        )
        return RepairLoopOutcome(
            status,
            attempts,
            session_dir,
            error_message,
            pre_existing,
            unrepairable,
            remaining,
            patches,
            baseline_ref,
            reason or status,
        )

    def _history_dir(self) -> Path:
        history_dir = Path(self.config.history_dir)
        if history_dir.is_absolute():
            return history_dir
        return self.project_root / history_dir

    def _load_codd_yaml(self) -> dict:
        try:
            return load_project_config(self.project_root)
        except (FileNotFoundError, ValueError):
            return {}

    def _load_affected_file_contents(self, rca: RootCauseAnalysis, dag: DAG) -> dict[str, str]:
        contents: dict[str, str] = {}
        for raw_path in self._candidate_paths(rca, dag):
            resolved = self._resolve_project_file(raw_path)
            if resolved is None or not resolved.is_file():
                continue
            relative = resolved.relative_to(self.project_root.resolve(strict=False))
            contents[str(relative)] = resolved.read_text(encoding="utf-8")
        return contents

    def _candidate_paths(self, rca: RootCauseAnalysis, dag: DAG) -> list[str]:
        paths: list[str] = []
        for item in rca.affected_nodes:
            node = dag.nodes.get(item)
            if node is not None:
                path = node.path or node.attributes.get("path")
                if path:
                    paths.append(str(path))
                    continue
            paths.append(item)
        return paths

    def _resolve_project_file(self, raw_path: str) -> Path | None:
        text = str(raw_path or "").strip()
        if not text:
            return None
        root = self.project_root.resolve(strict=False)
        candidate = Path(text)
        if candidate.is_absolute():
            resolved = candidate.resolve(strict=False)
        else:
            if any(part == ".." for part in candidate.parts):
                return None
            resolved = (root / candidate).resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError:
            return None
        return resolved

    def _capture_current_head(self) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.project_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            value = completed.stdout.strip()
            if value:
                return value
        return "HEAD"

    def _classify_violations(
        self,
        violations: list[VerificationFailureReport],
        baseline_ref: str | None,
    ) -> RepairabilityClassification:
        try:
            raw = self.repairability_classifier.classify(violations, baseline_ref=baseline_ref)
        except TypeError:
            raw = self.repairability_classifier.classify(violations)
        return _coerce_classification(raw)

    def _pick_primary_violation(self, violations: list[VerificationFailureReport], dag: DAG) -> VerificationFailureReport:
        try:
            picked = self.primary_picker.pick(violations, dag=dag)
        except TypeError:
            try:
                picked = self.primary_picker.pick(violations, dag)
            except TypeError:
                picked = self.primary_picker.pick(violations)
        return _coerce_violation_report(picked, violations[0])


def _proposal_files(proposal: RepairProposal) -> list[str]:
    return [patch.file_path for patch in proposal.patches]


def _applied_patch_files(apply_result: ApplyResult, proposal: RepairProposal) -> list[str]:
    if apply_result.applied_patches:
        return list(apply_result.applied_patches)
    return _proposal_files(proposal)


def _default_repairability_classifier() -> Any:
    try:
        from codd.repair.repairability_classifier import NullClassifier
    except (ImportError, AttributeError):
        return NullRepairabilityClassifier()
    return NullClassifier()


def _default_primary_picker() -> Any:
    try:
        from codd.repair.primary_picker import PrimaryPicker
    except (ImportError, AttributeError):
        return FirstViolationPicker()
    return PrimaryPicker()


def _verification_passed(verify_result: Any) -> bool:
    if isinstance(verify_result, Mapping):
        return bool(verify_result.get("passed"))
    if hasattr(verify_result, "passed"):
        return bool(getattr(verify_result, "passed"))
    return bool(verify_result)


def _verification_failure(verify_result: Any) -> VerificationFailureReport | None:
    failure = verify_result.get("failure") if isinstance(verify_result, Mapping) else getattr(verify_result, "failure", None)
    return failure if isinstance(failure, VerificationFailureReport) else None


def _violations_from_verify_result(
    verify_result: Any,
    *,
    fallback: VerificationFailureReport,
) -> list[VerificationFailureReport]:
    if verify_result is None:
        return [fallback]
    raw_violations = _value(verify_result, "violations")
    if isinstance(raw_violations, list):
        converted = [_coerce_violation_report(item, fallback) for item in raw_violations]
        if converted:
            return converted
    raw_failures = _value(verify_result, "failures")
    if isinstance(raw_failures, list) and raw_failures:
        return [_coerce_violation_report(item, fallback) for item in raw_failures]
    failure = _verification_failure(verify_result)
    return [failure or fallback]


def _coerce_violation_report(value: Any, fallback: VerificationFailureReport) -> VerificationFailureReport:
    if isinstance(value, VerificationFailureReport):
        return value
    if isinstance(value, Mapping):
        check_name = str(value.get("check_name") or fallback.check_name)
        message = str(value.get("message") or value.get("error_message") or "")
        messages = value.get("error_messages")
        error_messages = [str(item) for item in messages] if isinstance(messages, list) else [message or _fallback_message(fallback)]
        failed_nodes = _string_list(value.get("failed_nodes")) or _node_refs(value.get("details")) or list(fallback.failed_nodes)
        snapshot = value.get("dag_snapshot") if isinstance(value.get("dag_snapshot"), dict) else fallback.dag_snapshot
        timestamp = str(value.get("timestamp") or fallback.timestamp or _timestamp())
        return VerificationFailureReport(check_name, failed_nodes, error_messages, snapshot, timestamp)
    check_name = str(getattr(value, "check_name", fallback.check_name) or fallback.check_name)
    message = str(getattr(value, "message", "") or "")
    messages = getattr(value, "error_messages", None)
    error_messages = [str(item) for item in messages] if isinstance(messages, list) else [message or _fallback_message(fallback)]
    details = getattr(value, "details", None)
    failed_nodes = _string_list(getattr(value, "failed_nodes", None)) or _node_refs(details) or list(fallback.failed_nodes)
    snapshot = getattr(value, "dag_snapshot", None)
    dag_snapshot = snapshot if isinstance(snapshot, dict) else fallback.dag_snapshot
    timestamp = str(getattr(value, "timestamp", None) or fallback.timestamp or _timestamp())
    return VerificationFailureReport(check_name, failed_nodes, error_messages, dag_snapshot, timestamp)


def _coerce_classification(value: Any) -> RepairabilityClassification:
    if isinstance(value, RepairabilityClassification):
        return value
    if isinstance(value, Mapping):
        return RepairabilityClassification(
            repairable=list(value.get("repairable") or []),
            pre_existing=list(value.get("pre_existing") or []),
            unrepairable=list(value.get("unrepairable") or []),
        )
    repairable = getattr(value, "repairable", None)
    pre_existing = getattr(value, "pre_existing", None)
    unrepairable = getattr(value, "unrepairable", None)
    if repairable is None and pre_existing is None and unrepairable is None:
        return RepairabilityClassification(repairable=list(value or []))
    return RepairabilityClassification(
        repairable=list(repairable or []),
        pre_existing=list(pre_existing or []),
        unrepairable=list(unrepairable or []),
    )


def _value(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item]


def _fallback_message(fallback: VerificationFailureReport) -> str:
    return str(fallback.error_messages[0]) if fallback.error_messages else "verification failed"


def _node_refs(value: Any) -> list[str]:
    refs: list[str] = []
    _collect_node_refs(value, refs)
    return _dedupe(refs)


def _collect_node_refs(value: Any, refs: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"node", "node_id", "from_id", "to_id", "from_node", "to_node", "design_doc"}:
                if isinstance(item, str) and item:
                    refs.append(item)
            elif key in {"missing_impl_files", "dangling_refs", "unreachable_nodes", "failed_nodes"}:
                if isinstance(item, list):
                    refs.extend(str(entry) for entry in item if isinstance(entry, str) and entry)
            else:
                _collect_node_refs(item, refs)
    elif isinstance(value, list):
        for item in value:
            _collect_node_refs(item, refs)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _positive_attempts(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 10


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_plain_data(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Mapping):
        return {str(key): _to_plain_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_plain_data(item) for item in value]
    if isinstance(value, tuple):
        return [_to_plain_data(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "__dict__"):
        return {str(key): _to_plain_data(item) for key, item in vars(value).items()}
    return value


__all__ = [
    "FirstViolationPicker",
    "NullRepairabilityClassifier",
    "RepairAttemptRecord",
    "RepairabilityClassification",
    "RepairLoop",
    "RepairLoopConfig",
    "RepairLoopOutcome",
    "RepairLoopStatus",
]
