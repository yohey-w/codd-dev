"""Core implementation for ``codd fixup-drift``.

The command re-runs drift and validation detectors to rebuild DriftEvents in
the current process, filters them, dispatches to registered fix strategies, and
keeps writes behind an explicit ``--apply`` flag.
"""

from __future__ import annotations

import subprocess
import tempfile
import uuid
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from typing import Any

from codd.coherence_engine import EventBus
from codd.config import find_codd_dir
from codd.fixup_drift_strategies import FixProposal, get_strategy

KNOWN_FIX_KINDS = {"url_drift", "design_token_drift", "lexicon_violation", "screen_flow_drift"}


def run_fixup_drift(
    project_root: Path,
    dry_run: bool = True,
    severity_filter: str = "red",
    kind_filter: str = "all",
) -> dict[str, Any]:
    """Run fixup-drift and return proposals or application counts."""
    project_root = Path(project_root).resolve()
    bus = EventBus()

    events = _collect_events(project_root, bus)
    filtered = _filter_events(events, severity_filter, kind_filter)
    proposals = _build_proposals(filtered, project_root)

    if dry_run:
        return {
            "events": filtered,
            "proposals": proposals,
            "applied": 0,
            "hitl_logged": 0,
            "errors": [],
        }

    result = _apply_with_worktree(proposals, project_root)
    result["events"] = filtered
    return result


def _collect_events(project_root: Path, bus: EventBus) -> list[Any]:
    """Re-run detectors and collect DriftEvents published to the bus."""
    from codd.drift import run_drift, set_coherence_bus as drift_set_bus
    from codd.validator import (
        run_validate,
        set_coherence_bus as validator_set_bus,
        validate_design_tokens,
        validate_with_lexicon,
    )

    codd_dir = find_codd_dir(project_root)
    if codd_dir is None:
        raise FileNotFoundError(
            f"CoDD config dir not found in {project_root} (looked for codd/ and .codd/)"
        )

    drift_set_bus(bus)
    validator_set_bus(bus)
    try:
        _run_detector(run_drift, project_root, codd_dir)
        _run_detector(run_validate, project_root, codd_dir)
        _run_detector(validate_with_lexicon, project_root)
        _run_detector(validate_design_tokens, project_root)
    finally:
        drift_set_bus(None)
        validator_set_bus(None)

    return bus.published_events()


def _run_detector(func, *args) -> None:
    try:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            func(*args)
    except Exception:
        return


def _filter_events(events: list[Any], severity_filter: str, kind_filter: str) -> list[Any]:
    """Filter DriftEvents by severity and canonical fix kind."""
    filtered = []
    for event in events:
        if severity_filter != "all" and getattr(event, "severity", None) != severity_filter:
            continue
        if kind_filter != "all" and _canonical_kind(event) != kind_filter:
            continue
        filtered.append(_with_canonical_kind(event))
    return filtered


def _build_proposals(events: list[Any], project_root: Path) -> list[FixProposal]:
    """Convert DriftEvents into proposals via the strategy registry."""
    proposals: list[FixProposal] = []
    for event in events:
        strategy = get_strategy(getattr(event, "kind", ""), project_root)
        if strategy is None:
            continue
        try:
            proposals.extend(strategy.propose(event))
        except Exception:
            continue
    return proposals


def _canonical_kind(event: Any) -> str:
    kind = str(getattr(event, "kind", ""))
    if kind in KNOWN_FIX_KINDS:
        return kind

    payload = getattr(event, "payload", {}) or {}
    drift_type = str(payload.get("drift_type", payload.get("kind", "")))
    if kind == "drift" and drift_type in {"design-only", "impl-only", "url_drift"}:
        return "url_drift"
    if kind == "drift" and drift_type in {"design_token", "design_token_drift"}:
        return "design_token_drift"
    if kind == "drift" and drift_type in {"screen_flow", "screen_flow_drift"}:
        return "screen_flow_drift"
    return kind


def _with_canonical_kind(event: Any) -> Any:
    canonical = _canonical_kind(event)
    if getattr(event, "kind", None) == canonical:
        return event
    try:
        return replace(event, kind=canonical)
    except Exception:
        event.kind = canonical
        return event


def _apply_with_worktree(proposals: list[FixProposal], project_root: Path) -> dict[str, Any]:
    """Apply proposals in a temporary git worktree, then apply the verified diff."""
    worktree_path = Path(tempfile.gettempdir()) / f"codd-fixup-{uuid.uuid4().hex[:8]}"
    applied = 0
    hitl_logged = 0
    errors: list[str] = []

    if not proposals:
        return {
            "proposals": proposals,
            "applied": 0,
            "hitl_logged": 0,
            "errors": errors,
        }

    try:
        _git(project_root, "rev-parse", "--show-toplevel")
        _git(project_root, "diff", "--quiet")
        _git(project_root, "diff", "--cached", "--quiet")
        _git(project_root, "worktree", "add", "--detach", str(worktree_path), "HEAD")

        for proposal in proposals:
            if not proposal.can_auto_apply:
                _log_hitl(proposal, project_root)
                hitl_logged += 1
                continue

            strategy = get_strategy(proposal.kind, worktree_path)
            if strategy is None:
                continue
            try:
                if strategy.apply(proposal):
                    applied += 1
            except Exception as exc:
                errors.append(f"{proposal.kind}:{proposal.file_path}: {exc}")

        if errors:
            raise RuntimeError("; ".join(errors))

        if applied:
            _apply_worktree_diff_to_main(worktree_path, project_root)
    except Exception as exc:
        errors.append(str(exc))
        applied = 0
    finally:
        if worktree_path.exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree_path)],
                cwd=str(project_root),
                capture_output=True,
                text=True,
                check=False,
            )

    return {
        "proposals": proposals,
        "applied": applied,
        "hitl_logged": hitl_logged,
        "errors": errors,
    }


def _log_hitl(proposal: FixProposal, project_root: Path) -> None:
    """Append a non-auto proposal to the pending HITL review file."""
    hitl_path = project_root / "docs" / "coherence" / "pending_hitl.md"
    hitl_path.parent.mkdir(parents=True, exist_ok=True)

    should_write_heading = not hitl_path.exists() or hitl_path.stat().st_size == 0
    with hitl_path.open("a", encoding="utf-8") as handle:
        if should_write_heading:
            handle.write("# Pending HITL Fix Proposals\n")
        handle.write(f"\n## [{proposal.kind}] {proposal.description}\n")
        handle.write(f"- File: {proposal.file_path}\n")
        handle.write(f"- Severity: {proposal.severity}\n")
        handle.write("- Status: [ ] accepted  [ ] rejected\n")
        if proposal.diff:
            handle.write(f"```diff\n{proposal.diff.rstrip()}\n```\n")


def _apply_worktree_diff_to_main(worktree_path: Path, project_root: Path) -> None:
    diff = subprocess.run(
        ["git", "diff", "--binary"],
        cwd=str(worktree_path),
        capture_output=True,
        text=False,
        check=True,
    ).stdout
    if not diff:
        return

    subprocess.run(
        ["git", "apply", "--check", "--binary"],
        cwd=str(project_root),
        input=diff,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "apply", "--binary"],
        cwd=str(project_root),
        input=diff,
        capture_output=True,
        check=True,
    )


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
