"""Post-implementation typecheck repair loop."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
import shlex
import subprocess
from typing import Any, Callable, Literal, Mapping

from codd.dag import DAG, Node
from codd.repair import RepairLoop, RepairLoopConfig, RepairLoopOutcome
from codd.repair.schema import VerificationFailureReport


TypecheckLoopStatus = Literal["PASS", "REPAIR_SUCCESS", "REPAIR_EXHAUSTED", "DISABLED"]
RunCommand = Callable[..., subprocess.CompletedProcess[str]]
RepairLoopFactory = Callable[[RepairLoopConfig, Path], RepairLoop]


@dataclass
class TypecheckLoopResult:
    status: TypecheckLoopStatus
    attempts: list[dict[str, Any]]
    final_typecheck_output: str


@dataclass
class _TypecheckRun:
    passed: bool
    output: str
    return_code: int


class TypecheckRepairLoop:
    def __init__(
        self,
        typecheck_command: str | None,
        max_attempts: int = 3,
        *,
        enabled: bool = True,
        runner: RunCommand = subprocess.run,
        repair_loop_factory: RepairLoopFactory | None = None,
        engine_name: str = "llm",
    ) -> None:
        self.typecheck_command = typecheck_command
        self.max_attempts = int(max_attempts)
        self.enabled = bool(enabled)
        self.runner = runner
        self.repair_loop_factory = repair_loop_factory or RepairLoop
        self.engine_name = engine_name

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any] | None,
        *,
        force_enabled: bool = False,
        runner: RunCommand = subprocess.run,
        repair_loop_factory: RepairLoopFactory | None = None,
    ) -> "TypecheckRepairLoop":
        typecheck = config.get("typecheck") if isinstance(config, Mapping) else None
        values = typecheck if isinstance(typecheck, Mapping) else {}
        enabled = bool(values.get("enabled", False)) or force_enabled
        return cls(
            _string_or_none(values.get("command")),
            max_attempts=_positive_int(values.get("max_repair_attempts"), 3),
            enabled=enabled,
            runner=runner,
            repair_loop_factory=repair_loop_factory,
            engine_name=str(values.get("engine_name") or values.get("engine") or "llm"),
        )

    def run_after_implement(
        self,
        project_root: Path,
        modified_files: list[Path],
        ai_command: str,
    ) -> TypecheckLoopResult:
        if not self.enabled:
            return TypecheckLoopResult("DISABLED", [], "")
        if not self.typecheck_command or not self.typecheck_command.strip():
            raise ValueError("codd.yaml [typecheck.command] is required when typecheck loop is enabled")
        if self.max_attempts < 1:
            raise ValueError("typecheck.max_repair_attempts must be at least 1")

        project_root = Path(project_root).resolve()
        scoped_files = _relative_project_files(project_root, modified_files)
        initial = self._run_typecheck(project_root)
        if initial.passed:
            return TypecheckLoopResult("PASS", [], initial.output)

        dag = _modified_files_dag(scoped_files)
        failure = _typecheck_failure(initial, scoped_files, dag)
        final_run = initial

        def verify_callable() -> dict[str, Any]:
            nonlocal final_run
            final_run = self._run_typecheck(project_root)
            return {
                "passed": final_run.passed,
                "failure": None if final_run.passed else _typecheck_failure(final_run, scoped_files, dag),
            }

        from codd.deployment.providers.ai_command import SubprocessAiCommand

        config = RepairLoopConfig(
            max_attempts=self.max_attempts,
            engine_name=self.engine_name,
            llm_client=SubprocessAiCommand(command=ai_command, project_root=project_root),
            repo_path=project_root,
        )
        repair_loop = self.repair_loop_factory(config, project_root)
        outcome = repair_loop.run(failure, dag, verify_callable=verify_callable)
        status: TypecheckLoopStatus = (
            "REPAIR_SUCCESS"
            if getattr(outcome, "status", None) == "REPAIR_SUCCESS"
            or (getattr(outcome, "status", None) is None and outcome.success)
            else "REPAIR_EXHAUSTED"
        )
        return TypecheckLoopResult(status, _attempts_to_dicts(outcome), final_run.output)

    def _run_typecheck(self, project_root: Path) -> _TypecheckRun:
        command = shlex.split(str(self.typecheck_command).strip())
        if not command:
            raise ValueError("typecheck command must not be empty")
        try:
            completed = self.runner(
                command,
                cwd=str(project_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        except FileNotFoundError:
            return _TypecheckRun(False, f"typecheck command not found: {command[0]}", 127)
        return _TypecheckRun(
            passed=int(completed.returncode) == 0,
            output=_joined_output(completed.stdout, completed.stderr),
            return_code=int(completed.returncode),
        )


def _typecheck_failure(run: _TypecheckRun, modified_files: list[str], dag: DAG) -> VerificationFailureReport:
    return VerificationFailureReport(
        check_name="typecheck",
        failed_nodes=list(modified_files),
        error_messages=[run.output],
        dag_snapshot=_dag_snapshot(dag),
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z"),
    )


def _modified_files_dag(modified_files: list[str]) -> DAG:
    dag = DAG()
    for file_path in modified_files:
        dag.add_node(Node(id=file_path, kind="implementation_file", path=file_path))
    return dag


def _dag_snapshot(dag: DAG) -> dict[str, Any]:
    return {
        "nodes": [
            {"id": node.id, "kind": node.kind, "path": node.path, "attributes": dict(node.attributes)}
            for node in dag.nodes.values()
        ],
        "edges": [
            {"from_id": edge.from_id, "to_id": edge.to_id, "kind": edge.kind, "attributes": edge.attributes or {}}
            for edge in dag.edges
        ],
    }


def _relative_project_files(project_root: Path, paths: list[Path]) -> list[str]:
    root = project_root.resolve(strict=False)
    seen: set[str] = set()
    scoped: list[str] = []
    for raw_path in paths:
        path = Path(raw_path)
        resolved = path.resolve(strict=False) if path.is_absolute() else (root / path).resolve(strict=False)
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            continue
        normalized = relative.as_posix()
        if normalized not in seen:
            seen.add(normalized)
            scoped.append(normalized)
    return scoped


def _attempts_to_dicts(outcome: RepairLoopOutcome) -> list[dict[str, Any]]:
    attempts = [_attempt_to_dict(attempt) for attempt in outcome.attempts]
    if outcome.error_message:
        attempts.append({"error_message": outcome.error_message})
    return attempts


def _attempt_to_dict(attempt: Any) -> dict[str, Any]:
    payload = _to_plain_data(attempt)
    if isinstance(payload, dict):
        failure = payload.get("failure_report")
        if isinstance(failure, dict):
            messages = failure.get("error_messages")
            if isinstance(messages, list):
                payload["typecheck_output"] = "\n".join(str(message) for message in messages)
        return payload
    return {"attempt": payload}


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
        return value.as_posix()
    if hasattr(value, "__dict__"):
        return {str(key): _to_plain_data(item) for key, item in vars(value).items()}
    return value


def _joined_output(stdout: str | None, stderr: str | None) -> str:
    parts = [part for part in (stdout or "", stderr or "") if part]
    return "".join(parts)


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


__all__ = [
    "TypecheckLoopResult",
    "TypecheckLoopStatus",
    "TypecheckRepairLoop",
]
