"""Entry point for codd fix [PHENOMENON] — operational-feedback mode.

This module is the orchestrator. Each step is delegated to a focused
sub-module (parser, candidate_selector, design_updater, risk_classifier)
so each piece is independently unit-testable and the legacy run_fix()
path is never touched.
"""

from __future__ import annotations

import logging
import tempfile
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
from codd.fix.impact_planner import ImpactPlan, resolve_impact_plan
from codd.fix.impl_propagation import (
    CheckRunner,
    ImplPropagationResult,
    TestRunner,
    collect_propagation_targets,
    dag_has_code_nodes,
    default_check_runner,
    run_impl_propagation,
    safe_red_check_names,
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
from codd.ai_invoke import (
    force_claude_print,
    is_codex_exec_command,
    prepare_read_only_codex,
    resolve_ai_command as _resolve_ai_command,
)

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
    # Stage 4: implementation propagation (design update → impl/tests)
    strategy: str = "patch"
    propagate_impl_enabled: bool = True
    affected_impl_paths: list[str] = field(default_factory=list)
    affected_test_paths: list[str] = field(default_factory=list)
    impl_target_sources: dict[str, str] = field(default_factory=dict)
    propagation: ImplPropagationResult | None = None
    # Stage 4 impact plan (obligation-driven target resolution + coverage).
    impact_plan: ImpactPlan | None = None
    covered_obligations: dict[str, list[str]] = field(default_factory=dict)
    unresolved_obligations: list[str] = field(default_factory=list)
    # Stage 5: optional design propagation (codd propagate, Path B)
    design_propagation: Any = None
    design_propagation_error: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.applied_paths)


def _load_config_or_empty(project_root: Path) -> dict[str, Any]:
    """Load the project config, tolerating a missing/broken codd.yaml.

    Stage 4 (impl propagation) needs the local test command from config but
    must never abort the design-update pipeline just because config loading
    fails — an empty mapping degrades to the deterministic defaults.
    """
    try:
        return load_project_config(project_root)
    except Exception:  # noqa: BLE001 — config availability, not validity
        return {}


def _config_propagate_impl(config: dict[str, Any]) -> bool:
    """Resolve ``fix.phenomenon.propagate_impl`` (default on)."""
    if not isinstance(config, dict):
        return True
    fix_cfg = config.get("fix", {})
    if not isinstance(fix_cfg, dict):
        return True
    phenom_cfg = fix_cfg.get("phenomenon", {})
    if not isinstance(phenom_cfg, dict):
        return True
    return bool(phenom_cfg.get("propagate_impl", True))


def _is_design_target(target: Candidate) -> bool:
    """True only when the chosen candidate is a markdown design document.

    Defense-in-depth against the ``common`` type confusion: ``candidate_selector``
    already excludes common *code* nodes, but Stage 3 must never run
    ``update_design_doc`` on an implementation file even if a code node reaches
    here — that would patch code "as a design document" and break the
    design→impl→test north star (it produced the v3.1.0 "Attempt 1 on
    route.ts" symptom). Non-markdown targets are recorded and skipped.
    """
    return str(target.path or target.node_id or "").endswith(".md")


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
    propagate_impl: bool | None = None,
    propagate: bool = False,
    strategy: str = "patch",
    check_runner: CheckRunner | None = None,
    test_runner: TestRunner | None = None,
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
        dry_run: when True, never write any file. Diff is returned, plus the
            impl/test files that WOULD be updated by Stage 4.
        push: reserved for future use; no auto-push in MVP.
        allow_delete: pass-through to design_updater.
        include_common: include kind="common" nodes as candidates.
        ai_invoke: dependency injection point for tests.
        prompt: dependency injection point for tests.
        propagate_impl: Stage 4 (design update → impl/tests) switch. None
            resolves from config ``fix.phenomenon.propagate_impl`` (default
            on; set ``false`` in codd.yaml to stop at the design update).
        propagate: when True and Stage 4 verified, run design propagation
            (``codd propagate --update``) to reconcile dependent design docs.
        strategy: 'patch' (default) updates affected files in place;
            'regenerate' is a reserved extension point (not implemented).
        check_runner / test_runner: dependency-injection points for the
            deterministic Stage-4 verification gate.
    """
    result = PhenomenonFixResult(
        phenomenon_text=phenomenon_text,
        dry_run=dry_run,
        strategy=strategy,
    )

    if not phenomenon_text or not phenomenon_text.strip():
        result.aborted = True
        result.abort_reason = "phenomenon_text is empty"
        return result

    if strategy not in ("patch", "regenerate"):
        result.aborted = True
        result.abort_reason = (
            f"unknown strategy {strategy!r} — expected 'patch' or 'regenerate'"
        )
        return result
    if strategy == "regenerate":
        result.aborted = True
        result.abort_reason = (
            "strategy 'regenerate' is not implemented yet — only 'patch' is "
            "available (regeneration via the implementer is a planned "
            "extension point)"
        )
        return result

    config = _load_config_or_empty(project_root)
    propagate_impl_enabled = (
        propagate_impl
        if propagate_impl is not None
        else _config_propagate_impl(config)
    )
    result.propagate_impl_enabled = propagate_impl_enabled

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
    # Stage-4 baseline: the impl-propagation gate passes only when no *new*
    # red DAG check appears versus the state BEFORE any design update. Capture
    # it now, while the design docs are still untouched (real runs only).
    # ------------------------------------------------------------------
    will_propagate = propagate_impl_enabled and not dry_run
    baseline_red: set[str] | None = None
    if will_propagate:
        baseline_red = safe_red_check_names(
            check_runner or default_check_runner, project_root
        )

    # ------------------------------------------------------------------
    # Step 3: Update each target (with attempt loop)
    # ------------------------------------------------------------------
    applied_updates: list[tuple[str, DesignUpdate]] = []
    proposed_updates: list[tuple[str, DesignUpdate]] = []
    for target in targets:
        if not _is_design_target(target):
            result.attempts.append(
                PhenomenonFixAttempt(
                    attempt=1,
                    target=target,
                    update=None,
                    risk=None,
                    applied=False,
                    aborted_reason=(
                        f"refusing design update on non-design target "
                        f"(kind={target.kind}, path={target.path}): only "
                        f"markdown design documents are valid Stage-3 targets"
                    ),
                )
            )
            continue
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
            if attempt_outcome.applied_update is not None:
                applied_updates.append((target.node_id, attempt_outcome.applied_update))
        if attempt_outcome.proposed_update is not None:
            proposed_updates.append((target.node_id, attempt_outcome.proposed_update))

    # ------------------------------------------------------------------
    # Stage 4: implementation propagation (design update → impl/tests)
    # ------------------------------------------------------------------
    if propagate_impl_enabled:
        _run_stage4_propagation(
            result,
            project_root=project_root,
            phenomenon_text=phenomenon_text,
            applied_updates=applied_updates,
            proposed_updates=proposed_updates,
            dag=dag,
            ai=ai,
            config=config,
            max_attempts=max_attempts,
            dry_run=dry_run,
            baseline_red=baseline_red,
            check_runner=check_runner,
            test_runner=test_runner,
        )

    # ------------------------------------------------------------------
    # Stage 5: optional design propagation (codd propagate --update, Path B)
    # ------------------------------------------------------------------
    if propagate and not dry_run and result.applied_paths:
        _run_stage5_design_propagation(
            result,
            project_root=project_root,
            ai_command=ai_command,
        )

    return result


def _run_stage4_propagation(
    result: PhenomenonFixResult,
    *,
    project_root: Path,
    phenomenon_text: str,
    applied_updates: list[tuple[str, DesignUpdate]],
    proposed_updates: list[tuple[str, DesignUpdate]],
    dag: Any,
    ai: AiInvoke,
    config: dict[str, Any],
    max_attempts: int,
    dry_run: bool,
    baseline_red: set[str] | None,
    check_runner: CheckRunner | None,
    test_runner: TestRunner | None,
) -> None:
    """Stage 4: propagate the applied design update into impl + tests.

    When the analysis carries a change-surface decomposition (entities /
    fields / operations / surfaces, OR explicit LLM obligations), targets are
    resolved by the obligation-driven
    :func:`codd.fix.impact_planner.resolve_impact_plan` and obligation coverage
    is ENFORCED. Dry-run previews the affected files plus the obligation→file
    coverage; a real run drives the narrow LLM patch slot wrapped by the verify
    gate, using the plan as the write allowlist.

    anti-false-green: when obligations exist and the plan's status is not
    ``complete`` (an obligation is unresolved, or too many candidates are
    ambiguous), the run ABORTS with an explicit reason. A partial set of impl
    files is NEVER applied — that is exactly the semantic false green this stage
    exists to prevent.

    Back-compat: when NO obligations can be derived (no change-surface signal —
    e.g. a pure design-doc clarification), the planner has nothing to verify
    against, so the stage falls back to the legacy DAG-exact resolution. That
    path still fail-fasts via its own deterministic gates and never widens the
    write allowlist beyond hard ``expects``/module evidence.
    """
    analysis = result.analysis
    if analysis is None:  # pragma: no cover — analysis is always set by Step 1
        return

    if dry_run:
        if not proposed_updates:
            return
        node_ids = [node_id for node_id, _update in proposed_updates]
        plan = resolve_impact_plan(
            dag=dag,
            project_root=project_root,
            design_node_ids=node_ids,
            phenomenon_text=phenomenon_text,
            analysis=analysis,
            design_updates=[u for _node_id, u in proposed_updates],
            config=config,
        )
        if not plan.obligations:
            # No change surface to verify — preview via legacy resolution.
            impl_paths, test_paths, sources = collect_propagation_targets(
                dag, node_ids, project_root
            )
            result.affected_impl_paths = impl_paths
            result.affected_test_paths = test_paths
            result.impl_target_sources = sources
            return
        _record_plan(result, plan)
        if plan.status != "complete":
            result.aborted = True
            result.abort_reason = _impact_abort_reason(plan)
        return

    if not applied_updates:
        return

    node_ids = [node_id for node_id, _update in applied_updates]
    plan = resolve_impact_plan(
        dag=dag,
        project_root=project_root,
        design_node_ids=node_ids,
        phenomenon_text=phenomenon_text,
        analysis=analysis,
        design_updates=[u for _node_id, u in applied_updates],
        config=config,
    )

    if plan.obligations:
        _record_plan(result, plan)
        if plan.status != "complete":
            # Fail-fast: never apply a partial/ambiguous impact set.
            result.aborted = True
            result.abort_reason = _impact_abort_reason(plan)
            return
        propagation = run_impl_propagation(
            project_root,
            phenomenon_text=phenomenon_text,
            applied=applied_updates,
            ai_invoke=ai,
            config=config,
            max_attempts=max_attempts,
            check_runner=check_runner,
            test_runner=test_runner,
            baseline_red_checks=baseline_red,
            impl_paths=plan.impl_paths,
            test_paths=plan.test_paths,
            target_sources={nid: "impact_plan" for nid in node_ids},
            intent=analysis.intent,
        )
    else:
        # No change-surface signal: legacy DAG-exact propagation (back-compat).
        propagation = run_impl_propagation(
            project_root,
            phenomenon_text=phenomenon_text,
            applied=applied_updates,
            ai_invoke=ai,
            config=config,
            max_attempts=max_attempts,
            check_runner=check_runner,
            test_runner=test_runner,
            baseline_red_checks=baseline_red,
            intent=analysis.intent,
        )

    result.propagation = propagation
    result.affected_impl_paths = propagation.impl_paths
    result.affected_test_paths = propagation.test_paths
    result.impl_target_sources = propagation.target_sources


def _record_plan(result: PhenomenonFixResult, plan: ImpactPlan) -> None:
    """Mirror the impact plan onto the fix result (preview + audit fields)."""
    result.impact_plan = plan
    result.affected_impl_paths = list(plan.impl_paths)
    result.affected_test_paths = list(plan.test_paths)
    result.covered_obligations = dict(plan.covered_obligations)
    result.unresolved_obligations = list(plan.unresolved_obligations)


def _impact_abort_reason(plan: ImpactPlan) -> str:
    """Explicit, non-false-green abort message for an incomplete impact plan."""
    detail = "; ".join(plan.diagnostics) if plan.diagnostics else plan.status
    base = f"impact resolution {plan.status}: {detail}"
    if plan.unresolved_obligations:
        base += (
            " — refusing to apply a partial fix (unresolved: "
            + ", ".join(plan.unresolved_obligations)
            + ")"
        )
    return base


def _run_stage5_design_propagation(
    result: PhenomenonFixResult,
    *,
    project_root: Path,
    ai_command: str | None,
) -> None:
    """Stage 5: reconcile dependent design docs (codd propagate --update).

    Best-effort — a propagation failure must not undo the verified Stage-3/4
    result, so any error is captured rather than raised.
    """
    try:
        from codd.propagator import run_propagate

        result.design_propagation = run_propagate(
            project_root,
            "HEAD",
            update=True,
            ai_command=ai_command,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort reconciliation
        result.design_propagation_error = str(exc)


# ---------------------------------------------------------------------------
# Step helpers
# ---------------------------------------------------------------------------


@dataclass
class _AttemptLoopOutcome:
    attempts: list[PhenomenonFixAttempt]
    applied_path: str | None
    applied_update: DesignUpdate | None = None
    # The update the loop *would* apply but did not (dry-run): drives the
    # Stage-4 impact preview without writing anything.
    proposed_update: DesignUpdate | None = None


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
            return _AttemptLoopOutcome(
                attempts=attempts,
                applied_path=None,
                proposed_update=update,
            )

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
            applied_update=update,
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
        # `common` is overloaded (design docs AND code files); only markdown
        # docs are design context — mirror of _collect_design_nodes so the
        # parser is never seeded with implementation files as "design".
        if not str(node.path or "").endswith(".md"):
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
    force --print / -p on Claude (otherwise it runs interactively) and
    harden agentic CLIs such as `codex exec` into read-only execution
    from an empty temporary workspace.
    """
    config = load_project_config(project_root)
    resolved = _resolve_ai_command(config, ai_command, command_name="fix")
    safe_workspace = tempfile.TemporaryDirectory(prefix="codd-fix-ai-")
    safe_root = Path(safe_workspace.name)
    resolved = _prepare_plain_text_ai_command(resolved, safe_root)
    resolved = force_claude_print(resolved)

    # NOTE: ``get_ai_command`` / ``load_project_config`` stay module-level call
    # sites here (not folded into codd.ai_invoke) — tests monkeypatch them on
    # this module to fake transports.
    adapter = get_ai_command(config, project_root=safe_root, command_override=resolved)

    def invoke(prompt: str) -> str:
        _keepalive = safe_workspace
        return adapter.invoke(prompt)

    return invoke


# RF4: codex read-only hardening lives in codd.ai_invoke; the historical
# private names are kept because tests (and possibly downstream code) import
# them from this module.
_prepare_plain_text_ai_command = prepare_read_only_codex
_is_codex_exec_command = is_codex_exec_command
