"""PHENOMENON-mode implementation propagation (Stage 4 of codd fix [PHENOMENON]).

After the design document update is applied, this module completes the
north-star loop — "CoDD updates the design doc, implementation, and tests
in one shot":

1. **Impact (deterministic)** — affected implementation/test files are
   resolved from the project DAG (:mod:`codd.dag.impact`): ``expects``
   edges forward, then ``tested_by`` edges, with a frontmatter ``modules``
   filesystem fallback.
2. **Patch (the only LLM slot)** — the AI receives the applied design diff
   plus the current content of the affected files and returns complete file
   bodies in the shared fenced-block contract (:mod:`codd.ai_patch`).
   Writes are restricted to an explicit allowlist (affected files + test
   files), never the whole project.
3. **Gate (deterministic)** — ``codd.dag.runner.run_all_checks`` (no *new*
   red findings vs. the pre-update baseline) plus the project's local test
   command (``codd.fixer._run_local_tests``). A red gate retries with
   accumulated session state (``codd.fixer._SessionState``), max attempts.
4. **Rollback (deterministic, targeted)** — on final failure ONLY the files
   this run wrote are restored to their pre-run content (created files are
   removed). This is the targeted equivalent of ``git restore -- <paths>``
   but also safe for untracked files and for pre-existing uncommitted
   edits, which a git-based restore would silently destroy. A repo-wide
   restore is never performed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from codd.ai_patch import apply_fix_blocks, parse_fix_blocks
from codd.dag.impact import affected_impl_targets, is_test_path
from codd.fix.design_updater import DesignUpdate
from codd.fix.templates_loader import load_template, render_template
from codd.fixer import (
    FailureInfo,
    _extract_diagnosis,
    _guess_lang,
    _run_local_tests,
    _SessionState,
)

logger = logging.getLogger("codd.fix.impl_propagation")

AiInvoke = Callable[[str], str]
CheckRunner = Callable[[Path], list[Any]]
TestRunner = Callable[[Path], "list[FailureInfo] | None"]

_MAX_PROMPT_FILE_CHARS = 50000


@dataclass
class ImplPatchAttempt:
    """A single patch + gate iteration."""

    attempt: int
    written_paths: list[str] = field(default_factory=list)
    verified: bool = False
    failure_summary: str = ""
    new_red_checks: list[str] = field(default_factory=list)


@dataclass
class ImplPropagationResult:
    """Outcome of run_impl_propagation()."""

    enabled: bool = True
    impl_paths: list[str] = field(default_factory=list)
    test_paths: list[str] = field(default_factory=list)
    target_sources: dict[str, str] = field(default_factory=dict)
    attempts: list[ImplPatchAttempt] = field(default_factory=list)
    written_paths: list[str] = field(default_factory=list)
    verified: bool = False
    checks_unavailable: bool = False
    tests_unavailable: bool = False
    rolled_back: bool = False
    rolled_back_paths: list[str] = field(default_factory=list)
    skipped_reason: str = ""
    # Red-green witness (anti-false-green): proves the patched tests actually
    # DETECT this run's change. ``witness_applicable`` is True only for
    # behaviour-changing intents (new_feature / bugfix); ``witness_passed`` is
    # True when the patched tests fail against the baseline impl (so they would
    # have caught the missing behaviour) and pass against the patched impl.
    witness_applicable: bool = False
    witness_passed: bool = False
    witness_reason: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.written_paths)


# ---------------------------------------------------------------------------
# Public helpers (also used by phenomenon_fixer for dry-run display)
# ---------------------------------------------------------------------------


def collect_propagation_targets(
    dag: Any,
    design_node_ids: list[str],
    project_root: Path,
) -> tuple[list[str], list[str], dict[str, str]]:
    """Union the affected impl/test files of several design documents."""
    impl_paths: list[str] = []
    test_paths: list[str] = []
    sources: dict[str, str] = {}
    seen_impl: set[str] = set()
    seen_tests: set[str] = set()

    for node_id in design_node_ids:
        targets = affected_impl_targets(dag, node_id, project_root=project_root)
        sources[node_id] = targets.source
        for path in targets.impl_paths:
            if path not in seen_impl:
                seen_impl.add(path)
                impl_paths.append(path)
        for path in targets.test_paths:
            if path not in seen_tests:
                seen_tests.add(path)
                test_paths.append(path)

    return impl_paths, test_paths, sources


def dag_has_code_nodes(dag: Any) -> bool:
    """True when the DAG contains at least one implementation code node."""
    for node in (getattr(dag, "nodes", {}) or {}).values():
        node_path = str(getattr(node, "path", "") or node.id)
        if node.kind == "impl_file":
            return True
        if node.kind == "common" and not node_path.endswith(".md"):
            return True
    return False


def red_check_names(results: list[Any]) -> set[str]:
    """Names of red, failed, non-opt-out check results (codd dag verify gate)."""
    names: set[str] = set()
    for result in results:
        severity = str(_result_value(result, "severity") or "red")
        passed = _result_value(result, "passed") is not False
        status = str(_result_value(result, "status") or "")
        if severity == "red" and not passed and status != "opt_out":
            names.add(str(_result_value(result, "check_name") or result.__class__.__name__))
    return names


def safe_red_check_names(
    check_runner: CheckRunner,
    project_root: Path,
) -> set[str] | None:
    """Run DAG checks; return red names, or None when checks cannot run."""
    try:
        return red_check_names(check_runner(project_root))
    except Exception as exc:  # noqa: BLE001 — gate availability, not validity
        logger.warning("DAG checks unavailable for gating: %s", exc)
        return None


def default_check_runner(project_root: Path) -> list[Any]:
    from codd.dag.runner import run_all_checks

    return run_all_checks(project_root)


def _result_value(result: Any, key: str) -> Any:
    if isinstance(result, dict):
        return result.get(key)
    return getattr(result, key, None)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_impl_propagation(
    project_root: Path,
    *,
    phenomenon_text: str,
    applied: list[tuple[str, DesignUpdate]],
    ai_invoke: AiInvoke,
    config: dict[str, Any],
    dag: Any | None = None,
    max_attempts: int = 3,
    check_runner: CheckRunner | None = None,
    test_runner: TestRunner | None = None,
    baseline_red_checks: set[str] | None = None,
    impl_paths: list[str] | None = None,
    test_paths: list[str] | None = None,
    target_sources: dict[str, str] | None = None,
    intent: str = "",
) -> ImplPropagationResult:
    """Propagate applied design updates into implementation and test files.

    Args:
        project_root: project root.
        phenomenon_text: the original phenomenon (context for the patch AI).
        applied: ``(design_node_id, DesignUpdate)`` pairs that were applied.
        ai_invoke: text-in/text-out LLM call (the narrow LLM slot).
        config: loaded project config (for the local test command).
        dag: pre-built DAG; when None a fresh one is built (post design update).
        max_attempts: patch + gate iterations before targeted rollback.
        check_runner / test_runner: dependency-injection points for tests.
        baseline_red_checks: red check names captured BEFORE the design update
            was applied (the gate passes only when no *new* red appears).
            When None, the baseline is captured now (post design update).
        impl_paths / test_paths / target_sources: pre-resolved impact targets
            (from :func:`codd.fix.impact_planner.resolve_impact_plan`). When
            provided, they are the SOURCE OF TRUTH and the legacy
            ``collect_propagation_targets`` DAG re-resolution is skipped — the
            planner has already proven obligation coverage, so re-deriving here
            would only risk reintroducing the under-reach this design fixes.
        intent: the phenomenon intent (``analysis.intent``). For
            behaviour-changing intents (``new_feature`` / ``bugfix``) a
            red-green witness runs after the gate goes green: it proves the
            patched tests actually DETECT this run's change (they fail against
            the baseline impl). A failed witness blocks the verified success and
            triggers the targeted rollback — a green that no test would have
            caught is a semantic false green. Other intents skip the witness.
    """
    result = ImplPropagationResult()
    project_root = Path(project_root).resolve()
    checks = check_runner or default_check_runner
    tests: TestRunner = test_runner or (lambda root: _run_local_tests(root, config))

    if not applied:
        result.skipped_reason = "no applied design updates to propagate"
        return result

    # ------------------------------------------------------------------
    # Impact: use the pre-resolved plan when given, else fall back to the
    # legacy DAG-based resolution (back-compat for callers without a plan).
    # ------------------------------------------------------------------
    if impl_paths is not None:
        result.impl_paths = list(impl_paths)
        result.test_paths = list(test_paths or [])
        result.target_sources = dict(target_sources or {})
    else:
        if dag is None:
            try:
                from codd.dag.builder import build_dag

                dag = build_dag(project_root)
            except Exception as exc:  # noqa: BLE001
                result.skipped_reason = f"DAG build failed: {exc}"
                return result

        design_node_ids = [node_id for node_id, _update in applied]
        resolved_impl, resolved_test, sources = collect_propagation_targets(
            dag, design_node_ids, project_root
        )
        result.impl_paths = resolved_impl
        result.test_paths = resolved_test
        result.target_sources = sources

    impl_paths = result.impl_paths
    test_paths = result.test_paths

    if not impl_paths:
        result.skipped_reason = (
            "no affected implementation files resolved from the DAG "
            "(no `expects` edges and no frontmatter `modules` fallback hits)"
        )
        return result

    # ------------------------------------------------------------------
    # Gate preflight: both gates must be assessed before any LLM write
    # ------------------------------------------------------------------
    baseline_failures = _safe_run_tests(tests, project_root)
    if baseline_failures:
        result.skipped_reason = (
            "pre-existing local test failures — bring tests green first "
            "(e.g. `codd fix` failure mode), then re-run the phenomenon fix"
        )
        return result
    result.tests_unavailable = baseline_failures is None

    baseline_red = baseline_red_checks
    if baseline_red is None:
        baseline_red = safe_red_check_names(checks, project_root)
    result.checks_unavailable = baseline_red is None

    if result.checks_unavailable and result.tests_unavailable:
        result.skipped_reason = (
            "no verification gate available (DAG checks and local tests are "
            "both unusable) — refusing to apply unverifiable AI patches"
        )
        return result
    baseline_red = baseline_red or set()

    # ------------------------------------------------------------------
    # Patch loop: narrow LLM slot wrapped by deterministic gates
    # ------------------------------------------------------------------
    allowed_set = set(impl_paths) | set(test_paths)
    snapshot: dict[str, str | None] = {}
    all_written: list[str] = []
    seen_written: set[str] = set()
    session = _SessionState()
    prev_failures: list[FailureInfo] = [
        FailureInfo(
            source="local",
            category="design",
            summary="implementation not yet aligned with the updated design",
            log="",
        )
    ]

    for attempt_num in range(1, max_attempts + 1):
        prompt = _build_impl_update_prompt(
            project_root,
            phenomenon_text=phenomenon_text,
            applied=applied,
            file_paths=[*impl_paths, *test_paths],
            allowed_set=allowed_set,
            session=session,
        )
        raw = ai_invoke(prompt)

        permitted_blocks = _filter_blocks(
            parse_fix_blocks(raw), project_root, allowed_set
        )
        _snapshot_before_write(snapshot, project_root, permitted_blocks)
        application = apply_fix_blocks(permitted_blocks, project_root)
        for path in application.applied_paths:
            if path not in seen_written:
                seen_written.add(path)
                all_written.append(path)

        gate = _run_gate(result, checks, tests, project_root, baseline_red)
        attempt = ImplPatchAttempt(
            attempt=attempt_num,
            written_paths=list(application.applied_paths),
            verified=gate.verified,
            failure_summary=gate.failure_summary,
            new_red_checks=gate.new_red_checks,
        )
        result.attempts.append(attempt)

        if gate.verified:
            # Anti-false-green: before claiming a verified fix, prove the
            # patched tests actually DETECT this run's change. A green that no
            # test would have caught is a semantic false green.
            witness = _run_red_green_witness(
                result,
                project_root=project_root,
                snapshot=snapshot,
                written_paths=all_written,
                tests=tests,
                intent=intent,
            )
            if witness.passed:
                result.verified = True
                result.written_paths = sorted(all_written)
                return result
            # Witness FAILED — roll back this run's writes and do NOT return a
            # verified success. Fall through to the targeted-rollback tail so no
            # partial/unwitnessed change is left applied.
            break

        session.record_attempt(
            attempt=attempt_num,
            diagnosis=_extract_diagnosis(raw),
            failures=prev_failures,
            new_failures=gate.failures,
            ai_output=raw,
        )
        prev_failures = gate.failures

    # ------------------------------------------------------------------
    # Final failure: targeted rollback — ONLY files this run wrote
    # ------------------------------------------------------------------
    result.rolled_back_paths = _restore_snapshot(project_root, snapshot)
    result.rolled_back = bool(snapshot)
    result.written_paths = []
    return result


# ---------------------------------------------------------------------------
# Red-green witness (anti-false-green)
# ---------------------------------------------------------------------------

_WITNESS_INTENTS = frozenset({"new_feature", "bugfix"})


@dataclass
class _WitnessOutcome:
    """Result of the red-green witness check."""

    passed: bool
    reason: str = ""


def _write_or_remove(project_root: Path, rel_path: str, content: str | None) -> None:
    """Set ``rel_path`` to ``content``; ``None`` removes the file (created this run)."""
    target = project_root / rel_path
    if content is None:
        if target.exists():
            target.unlink()
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _run_red_green_witness(
    result: ImplPropagationResult,
    *,
    project_root: Path,
    snapshot: dict[str, str | None],
    written_paths: list[str],
    tests: TestRunner,
    intent: str,
) -> _WitnessOutcome:
    """Prove the patched tests DETECT this run's change (anti-false-green).

    Runs only after the deterministic gate is already green. Procedure
    (snapshot-based, generic — no language/framework/project literals):

    1. Identify the impl files (non-test) and test files this run wrote.
    2. Capture the PATCHED impl content now on disk.
    3. Restore those impl files to their PRE-RUN baseline (from ``snapshot``;
       ``None`` baseline means the file was created this run → remove it).
       LEAVE the patched test files in place.
    4. Run the local tests. The result MUST be RED — the patched tests should
       fail without the new behaviour. GREEN means the tests do not exercise
       the change → semantic false green → witness FAILS.
    5. ALWAYS restore the patched impl content (``finally``), so a witness
       failure leaves the patched state intact for the caller's normal
       targeted rollback.

    A test command that cannot run (returns ``None``) is INCONCLUSIVE and
    treated as a FAIL — the module refuses to claim a verified fix it cannot
    witness. Behaviour-changing intents with no changed/created test file fail
    for the same reason: a behaviour change with no behaviour test is not
    provably covered.

    Non behaviour-changing intents (anything outside ``new_feature`` /
    ``bugfix``) are NOT APPLICABLE: the witness is skipped and the run proceeds
    as a normal success.
    """
    normalized_intent = (intent or "").strip().lower()
    if normalized_intent not in _WITNESS_INTENTS:
        result.witness_applicable = False
        result.witness_passed = True
        result.witness_reason = (
            f"not applicable for intent {normalized_intent or 'unknown'!r} "
            "(witness runs only for new_feature / bugfix)"
        )
        return _WitnessOutcome(passed=True, reason=result.witness_reason)

    result.witness_applicable = True

    test_written = [p for p in written_paths if is_test_path(p)]
    impl_written = [p for p in written_paths if not is_test_path(p)]

    if not test_written:
        result.witness_passed = False
        result.witness_reason = (
            "no behaviour-witness test was created or changed — a behaviour "
            "change with no test cannot be proven to detect the change"
        )
        return _WitnessOutcome(passed=False, reason=result.witness_reason)

    # Capture the verified-green PATCHED impl content before perturbing it.
    patched_impl: dict[str, str | None] = {}
    for rel_path in impl_written:
        target = project_root / rel_path
        if target.is_file():
            try:
                patched_impl[rel_path] = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                patched_impl[rel_path] = None
        else:
            patched_impl[rel_path] = None

    try:
        # Restore impl to PRE-RUN baseline; leave patched tests in place.
        for rel_path in impl_written:
            _write_or_remove(project_root, rel_path, snapshot.get(rel_path))

        baseline_failures = _safe_run_tests(tests, project_root)
        if baseline_failures is None:
            result.witness_passed = False
            result.witness_reason = (
                "red-green witness inconclusive — the test command is "
                "unavailable, so the patched tests cannot be witnessed; "
                "refusing to claim a verified fix that cannot be proven"
            )
            return _WitnessOutcome(passed=False, reason=result.witness_reason)
        if not baseline_failures:
            result.witness_passed = False
            result.witness_reason = (
                "red-green witness failed — the patched tests still pass "
                "against the baseline implementation, so they do not detect "
                "this change (semantic false green)"
            )
            return _WitnessOutcome(passed=False, reason=result.witness_reason)

        result.witness_passed = True
        result.witness_reason = (
            "red-green witness passed — patched tests fail against the "
            "baseline implementation and pass against the patched one"
        )
        return _WitnessOutcome(passed=True, reason=result.witness_reason)
    finally:
        # ALWAYS restore the patched impl so the caller's rollback (or success)
        # sees the post-patch state, never the perturbed baseline.
        for rel_path in impl_written:
            _write_or_remove(project_root, rel_path, patched_impl.get(rel_path))


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


@dataclass
class _GateOutcome:
    verified: bool
    failure_summary: str = ""
    new_red_checks: list[str] = field(default_factory=list)
    failures: list[FailureInfo] = field(default_factory=list)


def _run_gate(
    result: ImplPropagationResult,
    checks: CheckRunner,
    tests: TestRunner,
    project_root: Path,
    baseline_red: set[str],
) -> _GateOutcome:
    new_reds: list[str] = []
    checks_ok = True
    if not result.checks_unavailable:
        red_now = safe_red_check_names(checks, project_root)
        if red_now is None:
            result.checks_unavailable = True
        else:
            new_reds = sorted(red_now - baseline_red)
            checks_ok = not new_reds

    test_failures: list[FailureInfo] = []
    tests_ok = True
    if not result.tests_unavailable:
        run_failures = _safe_run_tests(tests, project_root)
        if run_failures is None:
            result.tests_unavailable = True
        else:
            test_failures = run_failures
            tests_ok = not test_failures

    if result.checks_unavailable and result.tests_unavailable:
        return _GateOutcome(
            verified=False,
            failure_summary="verification gate became unavailable mid-run",
        )

    gate_failures = [
        FailureInfo(
            source="local",
            category="check",
            summary=f"DAG check red: {name}",
            log=f"`codd dag verify` reports new red finding: {name}",
        )
        for name in new_reds
    ] + test_failures

    if checks_ok and tests_ok:
        return _GateOutcome(verified=True)

    summary_parts = [f"new red DAG check(s): {', '.join(new_reds)}"] if new_reds else []
    if test_failures:
        summary_parts.append(
            "test failure(s): " + "; ".join(f.summary for f in test_failures)[:300]
        )
    return _GateOutcome(
        verified=False,
        failure_summary="; ".join(summary_parts),
        new_red_checks=new_reds,
        failures=gate_failures,
    )


def _safe_run_tests(
    tests: TestRunner,
    project_root: Path,
) -> list[FailureInfo] | None:
    try:
        return tests(project_root)
    except Exception as exc:  # noqa: BLE001 — gate availability, not validity
        logger.warning("local tests unavailable for gating: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Write filtering / snapshot / rollback
# ---------------------------------------------------------------------------


def _filter_blocks(
    blocks: list[tuple[str, str]],
    project_root: Path,
    allowed_set: set[str],
) -> list[tuple[str, str]]:
    """Keep blocks inside the project that hit the allowlist or are tests.

    New test files (paths the project's test conventions recognize) are
    permitted so the AI can add coverage for newly designed behavior.
    """
    permitted: list[tuple[str, str]] = []
    for path, content in blocks:
        target = project_root / path
        try:
            inside = target.resolve().is_relative_to(project_root.resolve())
        except OSError:
            inside = False
        if not inside:
            logger.warning("Skipping file outside project: %s", path)
            continue
        if path in allowed_set or is_test_path(path):
            permitted.append((path, content))
        else:
            logger.warning(
                "Skipping file outside the permitted write set: %s", path
            )
    return permitted


def _snapshot_before_write(
    snapshot: dict[str, str | None],
    project_root: Path,
    blocks: list[tuple[str, str]],
) -> None:
    """Record the pre-run content of every path about to be written.

    ``None`` marks a file that did not exist before this run (rollback
    removes it). A path already snapshotted keeps its FIRST recorded
    content — the pre-run state, not an intermediate attempt.
    """
    for path, _content in blocks:
        if path in snapshot:
            continue
        target = project_root / path
        if target.is_file():
            try:
                snapshot[path] = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                snapshot[path] = None
        else:
            snapshot[path] = None


def _restore_snapshot(
    project_root: Path,
    snapshot: dict[str, str | None],
) -> list[str]:
    """Targeted rollback: restore ONLY the files this run wrote.

    Equivalent in effect to ``git restore -- <paths>`` limited to this run's
    write set, but content-snapshot based so it also rolls back files git
    does not track and preserves pre-run uncommitted edits. Never touches
    any path outside the snapshot — a repo-wide restore is forbidden.
    """
    restored: list[str] = []
    for rel_path in sorted(snapshot):
        original = snapshot[rel_path]
        target = project_root / rel_path
        if original is None:
            if target.exists():
                target.unlink()
                restored.append(rel_path)
            continue
        current: str | None = None
        if target.is_file():
            try:
                current = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                current = None
        if current != original:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(original, encoding="utf-8")
            restored.append(rel_path)
    return restored


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


def _build_impl_update_prompt(
    project_root: Path,
    *,
    phenomenon_text: str,
    applied: list[tuple[str, DesignUpdate]],
    file_paths: list[str],
    allowed_set: set[str],
    session: _SessionState,
) -> str:
    design_diff = "\n".join(
        update.diff for _node_id, update in applied if update.diff
    ).strip() or "(no diff available)"

    file_sections: list[str] = []
    total_chars = 0
    for rel_path in file_paths:
        target = project_root / rel_path
        if not target.is_file():
            continue
        try:
            content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if total_chars + len(content) > _MAX_PROMPT_FILE_CHARS:
            remaining = _MAX_PROMPT_FILE_CHARS - total_chars
            if remaining > 500:
                file_sections.append(
                    f"```{_guess_lang(target)} {rel_path}\n{content[:remaining]}\n```\n(truncated)"
                )
            break
        file_sections.append(f"```{_guess_lang(target)} {rel_path}\n{content}\n```")
        total_chars += len(content)

    session_block = session.format_for_prompt()
    if session_block:
        session_block += "\n"

    template = load_template("impl_update.txt")
    return render_template(
        template,
        phenomenon_text=phenomenon_text.strip(),
        design_diff=design_diff,
        allowed_files="\n".join(f"- {path}" for path in sorted(allowed_set)) or "(none)",
        current_files="\n\n".join(file_sections) or "(none)",
        session_state=session_block,
    )
