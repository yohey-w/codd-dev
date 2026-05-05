"""LLM-backed repair engine implementation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping

from codd.config import load_project_config
from codd.deployment.providers.ai_command import AiCommandError, SubprocessAiCommand
from codd.repair.engine import RepairEngine, register_repair_engine
from codd.repair.git_patcher import GitPatcher
from codd.repair.schema import (
    ApplyResult,
    FilePatch,
    RepairProposal,
    RootCauseAnalysis,
    VerificationFailureReport,
)


LOGGER = logging.getLogger(__name__)
TEMPLATE_DIR = Path(__file__).with_name("templates")


class RepairFailed(RuntimeError):
    """Raised when a repair phase cannot produce a valid result."""


@register_repair_engine("llm")
@dataclass
class LlmRepairEngine(RepairEngine):
    """Default repair engine that delegates analysis and proposal work to an AI command."""

    project_root: Path | str | None = None
    config: Mapping[str, Any] | None = None
    ai_command: Any | None = None
    git_patcher: GitPatcher = field(default_factory=GitPatcher)

    def __post_init__(self) -> None:
        if self.project_root is not None:
            self.project_root = Path(self.project_root)

    def analyze(self, failure: VerificationFailureReport, dag: Any) -> RootCauseAnalysis:
        """Analyze a verification failure and return a structured root cause."""

        prompt = _render_template(
            TEMPLATE_DIR / "analyze_meta.md",
            failure_report=_json_dumps(_to_plain_data(failure)),
            dag_context=_json_dumps(_dag_to_plain_data(dag)),
            project_context=self._project_context(),
        )
        payload = _parse_json_object(self._invoke("repair_analyze", prompt), "RootCauseAnalysis")
        try:
            return RootCauseAnalysis(
                probable_cause=str(payload["probable_cause"]).strip(),
                affected_nodes=_string_list(payload.get("affected_nodes", [])),
                repair_strategy=str(payload.get("repair_strategy") or "unified_diff"),
                confidence=float(payload.get("confidence", 0.0)),
                analysis_timestamp=str(payload.get("analysis_timestamp") or _timestamp()),
            )
        except (KeyError, TypeError, ValueError) as exc:
            LOGGER.warning("Repair analysis output did not match schema: %s", exc)
            raise RepairFailed("repair analysis output did not match schema") from exc

    def propose_fix(self, rca: RootCauseAnalysis, file_contents: dict[str, str]) -> RepairProposal:
        """Ask the AI command for patches and validate unified diffs when possible."""

        prompt = _render_template(
            TEMPLATE_DIR / "propose_meta.md",
            root_cause_analysis=_json_dumps(_to_plain_data(rca)),
            file_contents=_json_dumps(file_contents),
            project_context=self._project_context(),
        )
        payload = _parse_json_object(self._invoke("repair_propose", prompt), "RepairProposal")
        try:
            proposal = RepairProposal(
                patches=[_file_patch(item) for item in _patch_entries(payload.get("patches"))],
                rationale=str(payload.get("rationale") or "").strip(),
                confidence=float(payload.get("confidence", 0.0)),
                proposal_timestamp=str(payload.get("proposal_timestamp") or _timestamp()),
                rca_reference=str(payload.get("rca_reference") or rca.analysis_timestamp),
            )
        except (TypeError, ValueError) as exc:
            LOGGER.warning("Repair proposal output did not match schema: %s", exc)
            raise RepairFailed("repair proposal output did not match schema") from exc

        if self.project_root is not None:
            for patch in proposal.patches:
                if patch.patch_mode == "unified_diff" and not self.git_patcher.validate(patch, Path(self.project_root)):
                    LOGGER.warning("Repair proposal patch failed dry-run validation: %s", patch.file_path)
                    raise RepairFailed("repair proposal failed patch validation")
        return proposal

    def apply(self, proposal: RepairProposal, *, dry_run: bool = False) -> ApplyResult:
        """Apply all patches in a proposal and aggregate their results."""

        if self.project_root is None:
            return ApplyResult(
                False,
                [],
                [patch.file_path for patch in proposal.patches],
                "project_root is required to apply repairs",
            )

        applied: list[str] = []
        failed: list[str] = []
        errors: list[str] = []
        for patch in proposal.patches:
            result = self.git_patcher.apply(patch, Path(self.project_root), dry_run=dry_run)
            applied.extend(result.applied_patches)
            failed.extend(result.failed_patches)
            if result.error_message:
                errors.append(result.error_message)

        return ApplyResult(not failed, applied, failed, "\n".join(errors) or None)

    def _invoke(self, command_name: str, prompt: str) -> str:
        injected = _select_injected_ai_command(self.ai_command, command_name)
        if injected is not None:
            return _invoke_ai_like(injected, prompt, self.project_root, self._effective_config())

        config = self._effective_config()
        try:
            command = resolve_repair_ai_command(config, command_name)
            return SubprocessAiCommand(
                command=command,
                project_root=self.project_root,
                config=config,
            ).invoke(prompt)
        except (AiCommandError, OSError, ValueError, RepairFailed) as exc:
            LOGGER.warning("Repair AI command failed for %s: %s", command_name, exc)
            raise RepairFailed(f"repair AI command failed for {command_name}") from exc

    def _effective_config(self) -> Mapping[str, Any]:
        if self.config is not None:
            return self.config
        if self.project_root is None:
            return {}
        try:
            return load_project_config(Path(self.project_root))
        except (FileNotFoundError, ValueError):
            return {}

    def _project_context(self) -> str:
        config = self._effective_config()
        repair_config = config.get("repair") if isinstance(config, Mapping) else None
        context_path = repair_config.get("context_path") if isinstance(repair_config, Mapping) else None
        if self.project_root is None or not isinstance(context_path, str) or not context_path.strip():
            return ""
        path = Path(context_path)
        if not path.is_absolute():
            path = Path(self.project_root) / path
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""


def resolve_repair_ai_command(config: Mapping[str, Any] | None, command_name: str) -> str:
    """Resolve a repair AI command from project config, then environment."""

    mapping = config if isinstance(config, Mapping) else {}
    ai_commands = mapping.get("ai_commands")
    if isinstance(ai_commands, Mapping):
        raw_command = ai_commands.get(command_name)
        if isinstance(raw_command, str) and raw_command.strip():
            return raw_command.strip()

    raw_command = mapping.get("ai_command")
    if isinstance(raw_command, str) and raw_command.strip():
        return raw_command.strip()

    env_command = os.environ.get("CODD_AI_COMMAND")
    if env_command and env_command.strip():
        return env_command.strip()

    raise RepairFailed(f"AI command is not configured for {command_name}")


def _render_template(path: Path, **values: str) -> str:
    rendered = path.read_text(encoding="utf-8")
    for name, value in values.items():
        rendered = rendered.replace("{" + name + "}", value)
    return rendered


def _parse_json_object(raw_output: str, label: str) -> Mapping[str, Any]:
    text = _strip_json_fence(raw_output)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            LOGGER.warning("%s output was not valid JSON", label)
            raise RepairFailed(f"{label} output was not valid JSON")
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            LOGGER.warning("%s output was not valid JSON: %s", label, exc)
            raise RepairFailed(f"{label} output was not valid JSON") from exc
    if not isinstance(payload, Mapping):
        LOGGER.warning("%s output must be a JSON object", label)
        raise RepairFailed(f"{label} output must be a JSON object")
    return payload


def _strip_json_fence(raw_output: str) -> str:
    text = raw_output.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _select_injected_ai_command(ai_command: Any | None, command_name: str) -> Any | None:
    if ai_command is None:
        return None
    if isinstance(ai_command, Mapping):
        return ai_command.get(command_name) or ai_command.get("default")
    return ai_command


def _invoke_ai_like(
    ai_command: Any,
    prompt: str,
    project_root: Path | str | None,
    config: Mapping[str, Any],
) -> str:
    try:
        if isinstance(ai_command, str):
            return SubprocessAiCommand(command=ai_command, project_root=project_root, config=config).invoke(prompt)
        if hasattr(ai_command, "invoke"):
            return str(ai_command.invoke(prompt))
        if callable(ai_command):
            return str(ai_command(prompt))
    except (AiCommandError, OSError, ValueError) as exc:
        LOGGER.warning("Injected repair AI command failed: %s", exc)
        raise RepairFailed("injected repair AI command failed") from exc
    raise RepairFailed("ai_command must be a string, callable, or expose invoke()")


def _file_patch(payload: Any) -> FilePatch:
    if not isinstance(payload, Mapping):
        raise TypeError("patch entries must be objects")
    return FilePatch(
        file_path=str(payload["file_path"]).strip(),
        patch_mode=str(payload.get("patch_mode") or "unified_diff"),
        content=str(payload.get("content") or ""),
    )


def _patch_entries(payload: Any) -> list[Any]:
    if not isinstance(payload, list):
        raise TypeError("patches must be a list")
    return payload


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _dag_to_plain_data(dag: Any) -> dict[str, Any]:
    nodes = getattr(dag, "nodes", {})
    edges = getattr(dag, "edges", [])
    return {
        "nodes": [_to_plain_data(node) for node in nodes.values()] if isinstance(nodes, Mapping) else [],
        "edges": [_to_plain_data(edge) for edge in edges] if isinstance(edges, list) else [],
    }


def _to_plain_data(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Mapping):
        return {str(key): _to_plain_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_plain_data(item) for item in value]
    return value


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "LlmRepairEngine",
    "RepairFailed",
    "resolve_repair_ai_command",
]
