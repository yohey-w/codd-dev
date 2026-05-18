"""Entry point for codd fix [PHENOMENON] — operational-feedback mode.

This module is the orchestrator. Each step is delegated to a focused
sub-module (parser, candidate_selector, design_updater, risk_classifier)
so each piece is independently unit-testable and the legacy run_fix()
path is never touched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from codd.config import load_project_config
from codd.dag.builder import build_dag
from codd.deployment.providers.ai_command_factory import get_ai_command
from codd.fix.candidate_selector import (
    Candidate,
    CandidateSelection,
    select_candidates,
)
from codd.fix.design_updater import (
    DesignUpdate,
    DesignUpdateError,
    apply_update,
    update_design_doc,
)
from codd.fix.interactive_prompt import (
    InteractivePrompt,
    Option,
    PromptAbort,
)
from codd.fix.phenomenon_parser import (
    PhenomenonAnalysis,
    parse_phenomenon,
)
from codd.fix.risk_classifier import RiskAssessment, classify_risk
from codd.generator import _resolve_ai_command

logger = logging.getLogger("codd.fix.phenomenon_fixer")

AiInvoke = Callable[[str], str]


@dataclass
class PhenomenonFixAttempt:
    """A single attempt of the phenomenon fix loop."""

    attempt: int
    target: Candidate
    update: DesignUpdate | None
    risk: RiskAssessment | None
    applied: bool
    aborted_reason: str = ""


@dataclass
class PhenomenonFixResult:
    """Outcome of run_phenomenon_fix()."""

    phenomenon_text: str
    analysis: PhenomenonAnalysis | None = None
    selection: CandidateSelection | None = None
    attempts: list[PhenomenonFixAttempt] = field(default_factory=list)
    applied_paths: list[str] = field(default_factory=list)
    aborted: bool = False
    abort_reason: str = ""
    dry_run: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.applied_paths)


def run_phenomenon_fix(
    project_root: Path,
    phenomenon_text: str,
    *,
    ai_command: str | None = None,
    non_interactive: bool = False,
    on_ambiguity: str = "abort",
    max_attempts: int = 3,
    dry_run: bool = False,
    push: bool = False,  # noqa: ARG001 — wired by caller, no auto-push in MVP
    allow_delete: bool = False,
    include_common: bool = True,
    ai_invoke: AiInvoke | None = None,
    prompt: InteractivePrompt | None = None,
) -> PhenomenonFixResult:
    """Drive the PHENOMENON-mode fix pipeline.

    Args:
        project_root: project root (must contain a .codd dir)
        phenomenon_text: natural-language phenomenon (e.g. "ログインエラーを
            わかりやすくしたい")
        ai_command: override resolution. If ai_invoke is provided, takes
            precedence (used by tests).
        non_interactive: when True, all interactive prompts use the
            non-interactive defaults (controlled by on_ambiguity).
        on_ambiguity: 'abort' | 'default' | 'top1'.
        max_attempts: maximum design-update + risk loop iterations.
        dry_run: when True, never write any file. Diff is returned.
        push: reserved for future use; no auto-push in MVP.
        allow_delete: pass-through to design_updater.
        include_common: include kind="common" nodes as candidates.
        ai_invoke: dependency injection point for tests.
        prompt: dependency injection point for tests.
    """
    result = PhenomenonFixResult(
        phenomenon_text=phenomenon_text,
        dry_run=dry_run,
    )

    if not phenomenon_text or not phenomenon_text.strip():
        result.aborted = True
        result.abort_reason = "phenomenon_text is empty"
        return result

    ai = ai_invoke or _build_default_ai_invoke(project_root, ai_command)
    ui = prompt or InteractivePrompt(
        non_interactive=non_interactive,
        on_ambiguity=on_ambiguity,
    )

    # ------------------------------------------------------------------
    # Step 1: Parse phenomenon
    # ------------------------------------------------------------------
    dag = build_dag(project_root)
    design_summaries = _build_design_summaries(dag, project_root)
    lexicon_context = _load_lexicon_context(project_root)

    analysis = parse_phenomenon(
        phenomenon_text,
        ai_invoke=ai,
        lexicon_context=lexicon_context,
        design_summaries=design_summaries,
    )
    result.analysis = analysis

    if analysis.is_ambiguous():
        try:
            _resolve_ambiguity(analysis, ui=ui, phenomenon_text=phenomenon_text)
        except PromptAbort as exc:
            result.aborted = True
            result.abort_reason = f"clarification aborted: {exc}"
            return result

    # ------------------------------------------------------------------
    # Step 2: Select candidates
    # ------------------------------------------------------------------
    selection = select_candidates(
        analysis,
        dag=dag,
        project_root=project_root,
        ai_invoke=ai,
        include_common=include_common,
    )
    result.selection = selection

    if not selection.candidates:
        result.aborted = True
        result.abort_reason = (
            selection.fallback_reason
            or "no design_doc candidates matched the phenomenon"
        )
        return result

    try:
        targets = _choose_targets(selection, ui=ui)
    except PromptAbort as exc:
        result.aborted = True
        result.abort_reason = f"candidate selection aborted: {exc}"
        return result

    # ------------------------------------------------------------------
    # Step 3: Update each target (with attempt loop)
    # ------------------------------------------------------------------
    for target in targets:
        target_path = project_root / target.path
        if not target_path.exists():
            result.attempts.append(
                PhenomenonFixAttempt(
                    attempt=1,
                    target=target,
                    update=None,
                    risk=None,
                    applied=False,
                    aborted_reason=f"target file missing: {target.path}",
                )
            )
            continue

        attempt_outcome = _run_attempt_loop(
            target=target,
            target_path=target_path,
            phenomenon_text=phenomenon_text,
            analysis=analysis,
            ai=ai,
            ui=ui,
            max_attempts=max_attempts,
            dry_run=dry_run,
            allow_delete=allow_delete,
        )
        result.attempts.extend(attempt_outcome.attempts)
        if attempt_outcome.applied_path:
            result.applied_paths.append(attempt_outcome.applied_path)

    return result


# ---------------------------------------------------------------------------
# Step helpers
# ---------------------------------------------------------------------------


@dataclass
class _AttemptLoopOutcome:
    attempts: list[PhenomenonFixAttempt]
    applied_path: str | None


def _run_attempt_loop(
    *,
    target: Candidate,
    target_path: Path,
    phenomenon_text: str,
    analysis: PhenomenonAnalysis,
    ai: AiInvoke,
    ui: InteractivePrompt,
    max_attempts: int,
    dry_run: bool,
    allow_delete: bool,
) -> _AttemptLoopOutcome:
    attempts: list[PhenomenonFixAttempt] = []

    for attempt_num in range(1, max_attempts + 1):
        try:
            update = update_design_doc(
                target_path,
                phenomenon_text=phenomenon_text,
                analysis=analysis,
                ai_invoke=ai,
                allow_delete=allow_delete,
            )
        except DesignUpdateError as exc:
            attempts.append(
                PhenomenonFixAttempt(
                    attempt=attempt_num,
                    target=target,
                    update=None,
                    risk=None,
                    applied=False,
                    aborted_reason=f"design_updater: {exc}",
                )
            )
            continue
        except Exception as exc:  # noqa: BLE001
            attempts.append(
                PhenomenonFixAttempt(
                    attempt=attempt_num,
                    target=target,
                    update=None,
                    risk=None,
                    applied=False,
                    aborted_reason=f"design_updater error: {exc}",
                )
            )
            continue

        if update.is_no_op():
            attempts.append(
                PhenomenonFixAttempt(
                    attempt=attempt_num,
                    target=target,
                    update=update,
                    risk=None,
                    applied=False,
                    aborted_reason="LLM returned unchanged document",
                )
            )
            return _AttemptLoopOutcome(attempts=attempts, applied_path=None)

        risk = classify_risk(update.diff, ai_invoke=ai)

        if dry_run:
            attempts.append(
                PhenomenonFixAttempt(
                    attempt=attempt_num,
                    target=target,
                    update=update,
                    risk=risk,
                    applied=False,
                    aborted_reason="dry_run: not applying",
                )
            )
            return _AttemptLoopOutcome(attempts=attempts, applied_path=None)

        try:
            decision = _confirm_apply(
                ui=ui,
                target=target,
                update=update,
                risk=risk,
                dry_run=dry_run,
            )
        except PromptAbort as exc:
            attempts.append(
                PhenomenonFixAttempt(
                    attempt=attempt_num,
                    target=target,
                    update=update,
                    risk=risk,
                    applied=False,
                    aborted_reason=f"user aborted apply: {exc}",
                )
            )
            return _AttemptLoopOutcome(attempts=attempts, applied_path=None)

        if decision == "reject":
            # User said no — do not loop with the same prompt; bail out so
            # the caller can rephrase the phenomenon.
            attempts.append(
                PhenomenonFixAttempt(
                    attempt=attempt_num,
                    target=target,
                    update=update,
                    risk=risk,
                    applied=False,
                    aborted_reason="user rejected diff",
                )
            )
            return _AttemptLoopOutcome(attempts=attempts, applied_path=None)

        apply_update(update)
        attempts.append(
            PhenomenonFixAttempt(
                attempt=attempt_num,
                target=target,
                update=update,
                risk=risk,
                applied=True,
            )
        )
        return _AttemptLoopOutcome(
            attempts=attempts,
            applied_path=target.path,
        )

    return _AttemptLoopOutcome(attempts=attempts, applied_path=None)


def _resolve_ambiguity(
    analysis: PhenomenonAnalysis,
    *,
    ui: InteractivePrompt,
    phenomenon_text: str,
) -> None:
    """Ask the user a clarification question. Updates analysis in-place.

    The clarification options are *not* hardcoded; they come from the
    analysis subject_terms / lexicon_hits and from the user's free-form
    answer. The LLM is the source of truth for the question text.
    For MVP, we just ask "please clarify the phenomenon".
    """
    if ui.non_interactive:
        # In non-interactive mode the clarification can never run; the
        # caller decides what to do based on on_ambiguity.
        if ui.on_ambiguity == "abort":
            raise PromptAbort("non-interactive: phenomenon is ambiguous")
        return

    # Build options from subject_terms — these come from the LLM analysis
    # so they are not hardcoded.
    options: list[Option] = []
    seen: set[str] = set()
    for term in analysis.subject_terms[:3]:
        normalized = term.strip()
        if not normalized or normalized.lower() in seen:
            continue
        seen.add(normalized.lower())
        options.append(Option(id=normalized, label=f"focus on: {normalized}"))
    options.append(Option(id="__other__", label="Other (free text)"))

    question = (
        f"The phenomenon '{phenomenon_text}' is ambiguous "
        f"(score={analysis.ambiguity_score:.2f}). Pick a focus:"
    )
    choice_id = ui.choice(question, options, allow_abort=True)

    if choice_id == "__other__":
        clarification = ui.text("Describe the focus in your own words:")
        analysis.subject_terms = [clarification] + list(analysis.subject_terms)
    else:
        analysis.subject_terms = [choice_id] + [
            t for t in analysis.subject_terms if t.strip() != choice_id
        ]

    # Lower ambiguity since user clarified
    analysis.ambiguity_score = min(analysis.ambiguity_score, 0.3)


def _choose_targets(
    selection: CandidateSelection,
    *,
    ui: InteractivePrompt,
) -> list[Candidate]:
    candidates = selection.candidates
    if not candidates:
        return []

    if selection.is_clear_winner or len(candidates) == 1:
        return [candidates[0]]

    options = [
        Option(
            id=cand.node_id,
            label=cand.to_display(),
            is_default=(i == 0),
        )
        for i, cand in enumerate(candidates)
    ]

    question = "Multiple design documents may apply. Pick one:"
    choice_id = ui.choice(
        question,
        options,
        allow_abort=True,
        allow_all=True,
    )

    if choice_id == "__all__":
        return list(candidates)

    chosen = next((c for c in candidates if c.node_id == choice_id), None)
    return [chosen] if chosen else [candidates[0]]


def _confirm_apply(
    *,
    ui: InteractivePrompt,
    target: Candidate,
    update: DesignUpdate,
    risk: RiskAssessment,
    dry_run: bool,
) -> str:
    """Return 'accept' or 'reject'. Raises PromptAbort if user aborts."""
    header = (
        f"Proposed update for {target.node_id} "
        f"(score={target.score:.2f}, risk={'YES' if risk.risky else 'no'}):"
    )
    accept_default: bool | None
    if dry_run:
        accept_default = False
    elif risk.risky:
        accept_default = False  # safe default — require explicit yes
    else:
        accept_default = True

    return ui.show_diff(
        header + "\n" + update.diff,
        question=f"Apply update to {target.path}?",
        allow_abort=True,
        accept_default=accept_default,
    )


def _build_design_summaries(dag, project_root: Path) -> dict[str, str]:
    """Build a {node_id: description} map for parser context."""
    out: dict[str, str] = {}
    for node in dag.nodes.values():
        if node.kind not in {"design_doc", "common"}:
            continue
        fm = (node.attributes or {}).get("frontmatter") or {}
        description = ""
        if isinstance(fm, dict):
            description = str(fm.get("description") or "").strip()
        if not description and node.path:
            path = project_root / node.path
            if path.exists():
                try:
                    text = path.read_text(encoding="utf-8")
                    description = _first_meaningful_line(text)
                except (UnicodeDecodeError, OSError):
                    pass
        if description:
            out[node.id] = description[:240]
    return out


def _first_meaningful_line(text: str) -> str:
    in_fm = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "---":
            in_fm = not in_fm
            continue
        if in_fm:
            continue
        if stripped.startswith("#") or stripped.startswith("```"):
            continue
        if stripped:
            return stripped
    return ""


def _load_lexicon_context(project_root: Path) -> str:
    try:
        from codd.lexicon import load_lexicon
    except Exception:  # noqa: BLE001
        return ""
    try:
        lex = load_lexicon(project_root)
    except Exception:  # noqa: BLE001
        return ""
    if lex is None:
        return ""
    try:
        return lex.as_context_string()
    except Exception:  # noqa: BLE001
        return ""


def _build_default_ai_invoke(
    project_root: Path,
    ai_command: str | None,
) -> AiInvoke:
    """Build a stdout-oriented AI invoker for phenomenon parsing.

    PHENOMENON mode needs plain text-in / text-out: structured JSON for
    the parser, an updated document body for the design updater. So we
    force --print / -p on Claude (otherwise it runs interactively and
    writes files), and never pass project_root into the AI command
    adapter, which would route the call into the file-writing-agent path.
    """
    import shlex

    config = load_project_config(project_root)
    resolved = _resolve_ai_command(config, ai_command, command_name="fix")
    parts = shlex.split(resolved)
    if parts:
        head = parts[0].lower()
        if "claude" in head and "-p" not in parts and "--print" not in parts:
            parts.append("--print")
            resolved = shlex.join(parts)

    adapter = get_ai_command(config, project_root=None, command_override=resolved)

    def invoke(prompt: str) -> str:
        return adapter.invoke(prompt)

    return invoke
