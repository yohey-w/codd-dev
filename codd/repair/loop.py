"""Repair loop orchestration."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
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
from codd.repair.schema import (
    ApplyResult,
    RepairProposal,
    RootCauseAnalysis,
    VerificationFailureReport,
)


RepairLoopStatus = Literal[
    "REPAIR_SUCCESS",
    "REPAIR_EXHAUSTED",
    "REPAIR_REJECTED_BY_HITL",
    "REPAIR_FAILED",
]


@dataclass
class RepairLoopConfig:
    max_attempts: int = 3
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
class RepairLoopOutcome:
    status: RepairLoopStatus
    attempts: list[RepairAttemptRecord]
    history_session_dir: Path
    error_message: str | None = None

    @property
    def success(self) -> bool:
        return self.status == "REPAIR_SUCCESS"


class RepairLoop:
    def __init__(self, config: RepairLoopConfig, project_root: Path):
        self.config = config
        self.project_root = Path(project_root)
        self.history = RepairHistory()

    def run(
        self,
        failure: VerificationFailureReport,
        dag: DAG,
        *,
        verify_callable: Callable[[], Any],
    ) -> RepairLoopOutcome:
        """Run repair attempts until verification passes or policy stops the loop."""

        session_dir = self.history.new_session(self._history_dir())
        attempts: list[RepairAttemptRecord] = []
        codd_yaml = self._load_codd_yaml()

        try:
            engine = self._new_engine()
        except (KeyError, TypeError, ValueError) as exc:
            return self._finalize(session_dir, "REPAIR_FAILED", attempts, str(exc))

        current_failure = failure
        for attempt_n in range(max(0, int(self.config.max_attempts))):
            try:
                rca = engine.analyze(current_failure, dag)
                file_contents = self._load_affected_file_contents(rca, dag)
                proposal = engine.propose_fix(rca, file_contents)
            except Exception as exc:  # noqa: BLE001 - repair engines are plug-ins.
                return self._finalize(session_dir, "REPAIR_FAILED", attempts, str(exc))

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
                return self._finalize(session_dir, "REPAIR_FAILED", attempts, str(exc))

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
                return self._finalize(session_dir, "REPAIR_REJECTED_BY_HITL", attempts, None)

            try:
                apply_result = engine.apply(proposal)
            except Exception as exc:  # noqa: BLE001 - repair engines are plug-ins.
                apply_result = ApplyResult(False, [], _proposal_files(proposal), str(exc))

            verify_result = None
            post_verify_passed: bool | None = None
            if apply_result.success:
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
                return self._finalize(session_dir, "REPAIR_SUCCESS", attempts, None)

            current_failure = _verification_failure(verify_result) or current_failure

        return self._finalize(session_dir, "REPAIR_EXHAUSTED", attempts, None)

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
    ) -> RepairLoopOutcome:
        self.history.finalize(session_dir, status)
        return RepairLoopOutcome(status, attempts, session_dir, error_message)

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


def _proposal_files(proposal: RepairProposal) -> list[str]:
    return [patch.file_path for patch in proposal.patches]


def _verification_passed(verify_result: Any) -> bool:
    if isinstance(verify_result, Mapping):
        return bool(verify_result.get("passed"))
    if hasattr(verify_result, "passed"):
        return bool(getattr(verify_result, "passed"))
    return bool(verify_result)


def _verification_failure(verify_result: Any) -> VerificationFailureReport | None:
    failure = verify_result.get("failure") if isinstance(verify_result, Mapping) else getattr(verify_result, "failure", None)
    return failure if isinstance(failure, VerificationFailureReport) else None


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
    "RepairAttemptRecord",
    "RepairLoop",
    "RepairLoopConfig",
    "RepairLoopOutcome",
    "RepairLoopStatus",
]
