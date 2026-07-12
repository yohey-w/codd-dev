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
from codd.deployment.providers.ai_command import AiCommandError
from codd.deployment.providers.ai_command_factory import get_ai_command
from codd.repair.design_context import render_design_context
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
DEFAULT_MAX_STRATEGY_ATTEMPTS = 2

#: Prompt rule for the F3 read-only failure-evidence block. Mirrors the Inc3
#: PART-A ``DESIGN_CONTEXT_RULE``: the evidence is context to localize the bug,
#: never an edit target.
IMMUTABLE_EVIDENCE_RULE = (
    "The following FAILURE EVIDENCE is READ-ONLY / IMMUTABLE. The test expectations "
    "and their observed (expected-vs-received) output localize the defect — align "
    "the IMPLEMENTATION toward them. NEVER edit these evidence files to make the "
    "failure pass."
)

#: Header for the v3.35.0 regeneration-parity block. States the monotonicity
#: invariant — a repair regeneration prompt carries the SAME mechanical contract
#: that pinned the first-pass implementation (namespace / module-specifier /
#: runtime-dependency conventions), plus, for a SOURCE file under repair, the
#: SIGNATURE surface of its distance-1 producers — so a single repair cannot undo
#: v3.33.0's signature convergence and re-open Root A (TS2835) / Root C (TS2307).
#: The producer surface is signature-FLOOR-and-CEILING: never a producer body.
REGENERATION_CONTRACT_RULE = (
    "MECHANICAL CONTRACT (regeneration parity — this is the SAME contract that "
    "pinned the first-pass implementation). Keep every import specifier, namespace, "
    "and dependency-manifest declaration your patch (re)generates conformant to the "
    "conventions below; a repair that reintroduces a specifier / namespace / "
    "manifest drift is a FAILED repair. Where a distance-1 producer SIGNATURE "
    "surface is shown, it is READ-ONLY context for binding your call shapes — align "
    "to it, and NEVER emit a producer's body."
)


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
    max_strategy_attempts: int | None = None

    def __post_init__(self) -> None:
        if self.project_root is not None:
            self.project_root = Path(self.project_root)
        #: Design-doc closure prose resolved for the failing nodes during
        #: :meth:`analyze`, reused by :meth:`propose_fix` (which has no DAG). See
        #: :func:`codd.repair.design_context.render_design_context`.
        self._design_context_cache: str = ""

    def analyze(self, failure: VerificationFailureReport, dag: Any) -> RootCauseAnalysis:
        """Analyze a verification failure and return a structured root cause."""

        self._design_context_cache = render_design_context(
            dag, list(failure.failed_nodes), self.project_root
        )
        prompt = _render_template(
            TEMPLATE_DIR / "analyze_meta.md",
            failure_report=_json_dumps(_to_plain_data(failure)),
            dag_context=_json_dumps(_dag_to_plain_data(dag)),
            project_context=self._composed_project_context(),
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

    def propose_fix(
        self,
        rca: RootCauseAnalysis,
        file_contents: dict[str, str],
        *,
        error_messages: list[str] | None = None,
        evidence: dict[str, str] | None = None,
    ) -> RepairProposal:
        """Ask the AI command for patches and retry with validation feedback.

        F3: the picked failure's ``error_messages`` (expected-vs-received + call
        shape) and the read-only ``evidence`` files (attribution ``evidence_nodes``)
        are threaded into the prompt as an explicitly IMMUTABLE section — the
        localization signal for a missing-``return`` facade — mirroring the Inc3
        PART-A design-context threading. Both are optional and default to empty, so
        a caller that supplies neither renders the prompt unchanged.
        """

        prompt_values = {
            "root_cause_analysis": _json_dumps(_to_plain_data(rca)),
            "file_contents": _json_dumps(file_contents),
            "project_context": self._composed_project_context(),
            "failure_evidence": _render_failure_evidence(error_messages, evidence),
            "mechanical_contract": self._regeneration_contract_context(file_contents),
        }
        prompt = _render_template(TEMPLATE_DIR / "propose_meta.md", **prompt_values)
        last_error: str | None = None

        for attempt in range(self._max_strategy_attempts()):
            payload = _parse_json_object(self._invoke("repair_propose", prompt), "RepairProposal")
            proposal = _repair_proposal(payload, rca)
            if not proposal.patches:
                # F7 (T2): a CLAIM-ONLY proposal (no patches, but a test_defect_claim)
                # is a legal structured terminal — the model reported an unsatisfiable
                # test transcription instead of attempting a forbidden test edit. Let
                # it surface so the loop can thread it into the outcome; the claim is
                # checked by re-derivation, never trusted.
                if proposal.test_defect_claim:
                    return proposal
                raise RepairFailed("repair proposal selected no-patch")

            last_error = self._proposal_validation_error(proposal)
            if last_error is None:
                return proposal

            LOGGER.warning("Repair proposal patch failed dry-run validation: %s", last_error)
            if attempt + 1 < self._max_strategy_attempts():
                prompt = _render_template(
                    TEMPLATE_DIR / "repair_strategy_meta.md",
                    error_message=last_error,
                    previous_proposal=_json_dumps(_to_plain_data(proposal)),
                    **prompt_values,
                )

        raise RepairFailed(f"repair proposal failed patch validation: {last_error or 'unknown validation error'}")

    def apply(self, proposal: RepairProposal, *, dry_run: bool = False) -> ApplyResult:
        """Apply all patches in a proposal and aggregate their results."""

        if self.project_root is None:
            return ApplyResult(
                False,
                [],
                [patch.file_path for patch in proposal.patches],
                "project_root is required to apply repairs",
            )

        if not proposal.patches:
            return ApplyResult(False, [], [], "no patch proposed")

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

    def _proposal_validation_error(self, proposal: RepairProposal) -> str | None:
        if self.project_root is None:
            return None

        root = Path(self.project_root)
        errors: list[str] = []
        for patch in proposal.patches:
            if patch.patch_mode != "unified_diff":
                continue
            result = self.git_patcher.validate_result(patch, root)
            if result.success:
                continue
            message = result.error_message or "unified diff failed validation"
            errors.append(f"{patch.file_path}: {message}")
        return "\n".join(errors) or None

    def _max_strategy_attempts(self) -> int:
        if self.max_strategy_attempts is not None:
            return _positive_int(self.max_strategy_attempts, DEFAULT_MAX_STRATEGY_ATTEMPTS)

        config = self._effective_config()
        repair_config = config.get("repair") if isinstance(config, Mapping) else None
        if isinstance(repair_config, Mapping):
            for key in ("max_strategy_attempts", "max_repair_strategy_attempts"):
                if key in repair_config:
                    return _positive_int(repair_config.get(key), DEFAULT_MAX_STRATEGY_ATTEMPTS)
        return DEFAULT_MAX_STRATEGY_ATTEMPTS

    def _invoke(self, command_name: str, prompt: str) -> str:
        injected = _select_injected_ai_command(self.ai_command, command_name)
        if injected is not None:
            return _invoke_ai_like(injected, prompt, self.project_root, self._effective_config())

        config = self._effective_config()
        try:
            command = resolve_repair_ai_command(config, command_name)
            adapter = get_ai_command(config, self.project_root, command_override=command)
            return adapter.invoke(prompt)
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

    def _composed_project_context(self) -> str:
        """Base project context (config ``context_path``) plus the design-doc
        closure prose resolved for the failing nodes during :meth:`analyze`.

        The design section is what steers a repair toward the CANONICAL design
        pins / producer declarations instead of the test text; it is empty when
        the failure does not map to any design doc, so the prompt is unchanged in
        that case.
        """
        parts = [
            part
            for part in (self._project_context(), getattr(self, "_design_context_cache", ""))
            if part and part.strip()
        ]
        return "\n\n".join(parts)

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

    def _regeneration_contract_context(self, file_contents: Mapping[str, str]) -> str:
        """v3.35.0 regeneration parity: the mechanical-contract context a repair
        regeneration prompt must carry so it is ⊇ the first-pass implement prompt's
        (at signature granularity) — the monotonicity invariant that keeps a single
        repair from undoing v3.33.0's signature convergence (re-opening Root A /
        Root C). Two parts, both profile-DATA / language-blind:

          (a) the three mechanical blocks (namespace / module-specifier / runtime-
              dependency) via ``build_mechanical_contract_context`` — IDENTICAL to
              the first-pass implement prompt (the shared builder is the one seam);
          (b) for each SOURCE (non-test) file under repair, its DISK-MEASURED
              distance-1 producer imports, rendered at SIGNATURE granularity
              (``render_public_surface`` → names → path; NEVER a body).

        Test-unit repair stays SUT-blind: part (b) resolves producers ONLY from the
        non-test consumer files, so a pure test-file repair carries the mechanical
        blocks alone — a test's distance-1 producer is its SUT, and steering an
        impl-blind test toward the SUT's on-disk signature is a false-green vector.
        No global cache: distance-1 differs per repair unit, so this is recomputed
        per proposal (never co-located with ``_design_context_cache``). Empty string
        when nothing resolves, so the prompt is byte-unchanged in that case.
        """
        if self.project_root is None:
            return ""
        project_root = Path(self.project_root)
        config = self._effective_config()

        # (a) the three profile-DATA mechanical blocks — the first-pass floor.
        mechanical = ""
        try:
            from codd.implementer import build_mechanical_contract_context

            mechanical = build_mechanical_contract_context(project_root, dict(config)) or ""
        except Exception:  # noqa: BLE001 — a projection failure must never block repair.
            mechanical = ""

        # (b) distance-1 producer SIGNATURE slices for the SOURCE files under repair.
        producer_surface = self._distance1_producer_surface(file_contents, project_root, config)

        parts = [part for part in (mechanical, producer_surface) if part and part.strip()]
        if not parts:
            return ""
        return "\n\n".join([REGENERATION_CONTRACT_RULE, *parts])

    def _distance1_producer_surface(
        self,
        file_contents: Mapping[str, str],
        project_root: Path,
        config: Mapping[str, Any],
    ) -> str:
        """SIGNATURE-granularity surface of the distance-1 producers imported by the
        SOURCE (non-test) files under repair — the repair-side of the Root B floor.

        SUT-blind by construction: producers are resolved ONLY from non-test
        consumers (a pure test repair → ``""``), and any resolved producer that is
        itself a test file or is already under repair is skipped. Signature is FLOOR
        and CEILING (``render_public_surface`` → names → path; never a body). Pure +
        best-effort + language-blind (dispatch on file extension, TS today; a
        renderer-less language degrades to names, then paths — the implement ladder).
        """
        try:
            from codd.implement_oracle_scope import (
                extract_public_surface,
                render_public_surface,
                resolve_local_import_targets,
            )
            from codd.verifiable_behavior_audit import is_test_related_implement
        except Exception:  # noqa: BLE001 — a missing seam must never block repair.
            return ""

        cfg = dict(config) if isinstance(config, Mapping) else {}

        def _is_test(path: str) -> bool:
            try:
                return is_test_related_implement(cfg, design_node=None, output_paths=[path])
            except Exception:  # noqa: BLE001 — classification failure ⇒ treat as source.
                return False

        source_consumers = [path for path in file_contents if not _is_test(path)]
        if not source_consumers:
            # Pure test-unit repair — SUT-blind: mechanical blocks only.
            return ""

        try:
            producers = resolve_local_import_targets(source_consumers, project_root)
        except Exception:  # noqa: BLE001 — a resolution failure must never block repair.
            return ""

        under_repair = set(file_contents)
        sections: list[str] = []
        for producer in producers:
            if producer in under_repair or _is_test(producer):
                continue
            entry = _render_producer_signature(
                producer, project_root, render_public_surface, extract_public_surface
            )
            if entry:
                sections.append(entry)
        return "\n\n".join(sections)


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


def _render_failure_evidence(
    error_messages: list[str] | None,
    evidence: dict[str, str] | None,
) -> str:
    """Render the F3 IMMUTABLE failure-evidence block, or "" when there is none.

    Empty-string when neither the failing output nor any evidence file is present,
    so the prompt is byte-identical to the pre-F3 prompt in that case (matching the
    design-context threading's empty-on-nothing contract).
    """
    messages = [str(message).strip() for message in (error_messages or []) if str(message).strip()]
    files = {str(path): str(body) for path, body in (evidence or {}).items() if str(path).strip()}
    if not messages and not files:
        return ""
    sections = [IMMUTABLE_EVIDENCE_RULE]
    if messages:
        sections.append("Observed failure output (expected vs received):\n" + "\n".join(messages))
    for path, body in files.items():
        sections.append(
            f"--- BEGIN READ-ONLY EVIDENCE {path} ---\n{body.rstrip()}\n--- END READ-ONLY EVIDENCE {path} ---"
        )
    return "\n\n".join(sections)


def _render_producer_signature(
    producer: str,
    project_root: Path,
    render_public_surface: Any,
    extract_public_surface: Any,
) -> str:
    """One distance-1 producer at the SIGNATURE floor: signature → names → path.

    Never full content, never a body (repair is post-verify; a producer body is a
    false-green vector). ``render_public_surface`` yields the signature slice
    (interface/type bodies, function param+return signatures with bodies stripped);
    on ``None`` (no signature renderer for this file kind) it degrades to the
    name-level surface, then to a path-only line — the same graceful-degradation
    ladder the implement Tier-1 floor uses, so a renderer-less language keeps the
    legacy names→paths behaviour (generality).
    """
    signature = None
    names = None
    try:
        signature = render_public_surface(producer, project_root)
        if not signature:
            names = extract_public_surface(producer, project_root)
    except Exception:  # noqa: BLE001 — never fail the repair prompt build.
        signature = None
    if signature:
        return (
            f"--- DISTANCE-1 PRODUCER {producer} "
            f"(signature surface — READ-ONLY, never emit its body) ---\n"
            f"{signature.rstrip()}"
        )
    if names:
        joined = ", ".join(str(name) for name in names)
        return (
            f"--- DISTANCE-1 PRODUCER {producer} (public surface names — READ-ONLY) ---\n"
            f"Exported symbols: {joined}"
        )
    return (
        f"--- DISTANCE-1 PRODUCER {producer} "
        f"(path only — no surface extractor for this file kind) ---"
    )


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
            return get_ai_command(config, project_root, command_override=ai_command).invoke(prompt)
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


def _repair_proposal(payload: Mapping[str, Any], rca: RootCauseAnalysis) -> RepairProposal:
    try:
        patches: list[FilePatch]
        if _selects_no_patch(payload):
            patches = []
        else:
            patches = [_file_patch(item) for item in _patch_entries(payload.get("patches"))]
        return RepairProposal(
            patches=patches,
            rationale=str(payload.get("rationale") or "").strip(),
            confidence=float(payload.get("confidence", 0.0)),
            proposal_timestamp=str(payload.get("proposal_timestamp") or _timestamp()),
            rca_reference=str(payload.get("rca_reference") or rca.analysis_timestamp),
            test_defect_claim=_test_defect_claims(payload.get("test_defect_claim")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        # ``KeyError`` mirrors the sibling ``analyze`` handler: a malformed proposal
        # (e.g. a patch entry missing the required ``file_path`` key, accessed in
        # ``_file_patch``) becomes the module's retriable ``RepairFailed`` (schema
        # mismatch) instead of a raw ``KeyError`` that escapes and crashes the loop.
        LOGGER.warning("Repair proposal output did not match schema: %s", exc)
        raise RepairFailed("repair proposal output did not match schema") from exc


def _test_defect_claims(payload: Any) -> list[dict]:
    """Parse the optional ``test_defect_claim`` array (F7 T2), best-effort.

    Each entry is normalized to ``{"file", "assertion", "reason"}`` strings. A
    non-list, or an entry with no ``file``, contributes nothing — the claim only
    unlocks bounded re-derivation, and a malformed claim must never abort propose.
    """
    if not isinstance(payload, list):
        return []
    claims: list[dict] = []
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        file_path = str(item.get("file") or item.get("file_path") or "").strip()
        if not file_path:
            continue
        claims.append(
            {
                "file": file_path,
                "assertion": str(item.get("assertion") or "").strip(),
                "reason": str(item.get("reason") or "").strip(),
            }
        )
    return claims


def _selects_no_patch(payload: Mapping[str, Any]) -> bool:
    for key in ("patch_mode", "repair_strategy", "strategy"):
        if _is_no_patch(payload.get(key)):
            return True

    patches = payload.get("patches")
    if not isinstance(patches, list):
        return False
    return any(isinstance(item, Mapping) and _is_no_patch(item.get("patch_mode")) for item in patches)


def _is_no_patch(value: Any) -> bool:
    return str(value or "").strip().lower().replace("_", "-") == "no-patch"


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


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, parsed)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "LlmRepairEngine",
    "RepairFailed",
    "resolve_repair_ai_command",
]
