"""CoDD propagate — reverse-propagate source code changes to design documents."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from codd._git_helper import _diff_files, _resolve_base_ref
from codd.config import find_codd_dir, load_project_config
from codd.coherence_engine import DriftEvent, EventBus, Orchestrator, use_coherence_bus
from codd.fixup_drift_strategies import FixProposal, get_strategy
from codd.generator import (
    _invoke_ai_command,
    _resolve_ai_command,
    MARKDOWN_FENCE_RE,
)
from codd.scanner import _extract_frontmatter


@dataclass
class AffectedDoc:
    """A design document affected by source code changes."""

    node_id: str
    path: str  # relative to project root
    title: str
    modules: list[str]
    matched_modules: list[str]  # modules that triggered the match
    changed_files: list[str]  # source files that changed in those modules


@dataclass
class PropagationResult:
    """Result of propagate analysis."""

    changed_files: list[str]
    file_module_map: dict[str, str]  # file -> module name
    affected_docs: list[AffectedDoc]
    updated: list[str]  # node_ids of docs actually updated


@dataclass
class VerifiedDoc:
    """A design doc with verification band classification."""

    doc: AffectedDoc
    band: str  # "green", "amber", "gray"
    confidence: float
    evidence_count: int


@dataclass
class VerifyResult:
    """Result of propagate --verify."""

    changed_files: list[str]
    file_module_map: dict[str, str]
    auto_applied: list[VerifiedDoc]  # green band — AI updated
    needs_hitl: list[VerifiedDoc]  # amber/gray — waiting for human
    updated: list[str]  # node_ids of auto-applied docs


@dataclass
class CommitResult:
    """Result of propagate --commit."""

    committed_files: list[str]
    knowledge_recorded: int  # number of HITL evidence entries added


@dataclass
class ReversePropagationResult:
    """Result of propagate --reverse."""

    source: str
    base_ref: str
    changes: list[dict[str, str]]
    events: list[DriftEvent]
    proposals: list[FixProposal]
    applied_files: list[str]


# Path for verify state persistence (relative to codd dir)
VERIFY_STATE_FILE = "propagate_state.json"
DESIGN_MD_DIFF_PATHS = ["DESIGN.md", "docs/design/DESIGN.md"]
LEXICON_DIFF_PATHS = ["project_lexicon.yaml", "docs/lexicon/project_lexicon.yaml"]
_YAML_KEY_VALUE_RE = re.compile(r"^(?P<indent>\s*)(?:-\s*)?(?P<key>[A-Za-z0-9_.$-]+)\s*:\s*(?P<value>.*)$")
_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.*?) b/(.*)$")
_IMPLEMENTATION_EXTENSIONS = {
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".vue",
    ".svelte",
    ".swift",
    ".kt",
    ".dart",
}
_SKIPPED_REVERSE_DIRS = {
    ".codd",
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    "node_modules",
}


def run_propagate(
    project_root: Path,
    diff_target: str = "HEAD",
    update: bool = False,
    ai_command: str | None = None,
    feedback: str | None = None,
    coherence_context: dict[str, Any] | None = None,
) -> PropagationResult:
    """Detect source code changes and find affected design documents.

    When update=True, calls AI to update each affected design doc.
    Supports two propagation paths:
      1. Source code change → design docs (via module mapping)
      2. Design doc change → dependent design docs (via CEG graph)
    """
    config = load_project_config(project_root)
    source_dirs = config.get("scan", {}).get("source_dirs", [])

    # Step 1: Get changed files
    changed_files = _get_changed_files(project_root, diff_target)
    if not changed_files:
        return PropagationResult([], {}, [], [])

    # Step 2: Filter to source files and map to modules
    file_module_map = _map_files_to_modules(changed_files, source_dirs)

    affected_docs: list[AffectedDoc] = []
    diff_text = ""

    if file_module_map:
        # Path A: source code → design docs (existing logic)
        changed_modules = set(file_module_map.values())
        affected_docs = _find_design_docs_by_modules(
            project_root, config, changed_modules, file_module_map,
        )
        diff_text = _get_code_diff(project_root, diff_target, list(file_module_map.keys()))

    if not affected_docs:
        # Path B: design doc → dependent design docs (via graph)
        changed_docs = _find_changed_docs(project_root, config, changed_files)
        if changed_docs:
            affected_docs = _find_docs_depending_on(
                project_root, config, changed_docs,
            )
            diff_text = _get_code_diff(project_root, diff_target, changed_files)

    if not affected_docs:
        return PropagationResult(changed_files, file_module_map, [], [])

    # Step 4: Optionally update via AI
    updated: list[str] = []
    if update and affected_docs:
        resolved_ai_command = _resolve_ai_command(config, ai_command, command_name="propagate")

        for doc in affected_docs:
            doc_path = project_root / doc.path
            if not doc_path.exists():
                continue
            current_content = doc_path.read_text(encoding="utf-8")
            prompt = _build_update_prompt(
                doc,
                current_content,
                diff_text,
                feedback=feedback,
                coherence_context=coherence_context,
            )
            raw_body = _invoke_ai_command(resolved_ai_command, prompt)
            body = _sanitize_update_body(raw_body)

            _write_updated_doc(doc_path, current_content, body)
            updated.append(doc.node_id)

    return PropagationResult(changed_files, file_module_map, affected_docs, updated)


def run_verify(
    project_root: Path,
    diff_target: str = "HEAD",
    ai_command: str | None = None,
    feedback: str | None = None,
    coherence_context: dict[str, Any] | None = None,
) -> VerifyResult:
    """Verify propagation: auto-apply green band, return HITL list for amber/gray.

    1. Detect affected docs (same as run_propagate)
    2. Load graph, classify each doc by confidence band
    3. Green band → auto-update via AI
    4. Amber/Gray → return as HITL candidates
    5. Save state for subsequent --commit
    """
    config = load_project_config(project_root)
    source_dirs = config.get("scan", {}).get("source_dirs", [])
    bands_config = config.get("bands", {})

    # Step 1: Get affected docs (source→doc or doc→doc)
    changed_files = _get_changed_files(project_root, diff_target)
    if not changed_files:
        return VerifyResult([], {}, [], [], [])

    file_module_map = _map_files_to_modules(changed_files, source_dirs)

    affected_docs: list[AffectedDoc] = []
    diff_text = ""

    if file_module_map:
        # Path A: source code → design docs
        changed_modules = set(file_module_map.values())
        affected_docs = _find_design_docs_by_modules(
            project_root, config, changed_modules, file_module_map,
        )
        diff_text = _get_code_diff(project_root, diff_target, list(file_module_map.keys()))

    if not affected_docs:
        # Path B: design doc → dependent design docs (via graph)
        changed_docs = _find_changed_docs(project_root, config, changed_files)
        if changed_docs:
            affected_docs = _find_docs_depending_on(
                project_root, config, changed_docs,
            )
            diff_text = _get_code_diff(project_root, diff_target, changed_files)

    if not affected_docs:
        return VerifyResult(changed_files, file_module_map, [], [], [])

    # Step 2: Classify by confidence band
    verified = _classify_docs_by_band(project_root, config, affected_docs, bands_config)

    auto_apply = [v for v in verified if v.band == "green"]
    hitl = [v for v in verified if v.band != "green"]

    # Step 3: Auto-apply green band via AI
    updated: list[str] = []
    if auto_apply:
        resolved_ai_command = _resolve_ai_command(config, ai_command, command_name="propagate")

        for vdoc in auto_apply:
            doc = vdoc.doc
            doc_path = project_root / doc.path
            if not doc_path.exists():
                continue
            current_content = doc_path.read_text(encoding="utf-8")
            prompt = _build_update_prompt(
                doc,
                current_content,
                diff_text,
                feedback=feedback,
                coherence_context=coherence_context,
            )
            raw_body = _invoke_ai_command(resolved_ai_command, prompt)
            body = _sanitize_update_body(raw_body)
            _write_updated_doc(doc_path, current_content, body)
            updated.append(doc.node_id)

    # Step 4: Save state for --commit
    _save_verify_state(project_root, auto_apply, hitl, updated, diff_target)

    return VerifyResult(changed_files, file_module_map, auto_apply, hitl, updated)


def run_commit(
    project_root: Path,
    reason: str | None = None,
    reason_map: dict[str, str] | None = None,
) -> CommitResult:
    """Commit after HITL review: record knowledge and git commit.

    Args:
        reason: Default reason for all HITL corrections.
        reason_map: Per-file reasons as {path: reason}. Overrides default.

    1. Load verify state (saved by --verify)
    2. Detect which HITL docs were modified by human
    3. Record HITL corrections as evidence (source_type="human")
    4. Git commit all propagation changes
    """
    state = _load_verify_state(project_root)
    if state is None:
        raise ValueError(
            "No verify state found. Run 'codd propagate --verify' first."
        )

    hitl_node_ids = set(state.get("hitl_node_ids", []))
    auto_node_ids = set(state.get("auto_node_ids", []))
    diff_target = state.get("diff_target", "HEAD")

    # Find which HITL docs were actually modified
    modified_files = _get_changed_files(project_root, diff_target)
    hitl_paths = {item["path"] for item in state.get("hitl_docs", [])}
    committed_files = [f for f in modified_files if f in hitl_paths]

    # Build per-file reason map (reason_map overrides default reason)
    effective_reasons: dict[str, str] = {}
    if reason:
        for f in committed_files:
            effective_reasons[f] = reason
    if reason_map:
        for f in committed_files:
            if f in reason_map:
                effective_reasons[f] = reason_map[f]

    # Record HITL knowledge in graph
    config = load_project_config(project_root)
    knowledge_count = 0
    if committed_files and effective_reasons:
        knowledge_count = _record_hitl_knowledge(
            project_root, state, committed_files, effective_reasons, config=config,
        )

    # Git commit
    all_paths = []
    for item in state.get("auto_docs", []) + state.get("hitl_docs", []):
        path = item["path"]
        full = project_root / path
        if full.exists():
            all_paths.append(path)

    commit_reason = _format_commit_reason(effective_reasons, reason)
    if all_paths:
        _git_commit_propagation(project_root, all_paths, commit_reason)

    # Clean up state file
    _clear_verify_state(project_root)

    return CommitResult(committed_files=committed_files, knowledge_recorded=knowledge_count)


def _detect_design_md_changes(project_root: Path, base_ref: str) -> list[dict[str, str]]:
    """Detect DESIGN.md token value changes via git diff."""
    diff_text = _diff_files(base_ref, cwd=project_root, paths=DESIGN_MD_DIFF_PATHS)
    changes: list[dict[str, str]] = []
    pending: list[dict[str, str]] = []
    context_stack: list[tuple[int, str]] = []
    current_file = ""

    for line in diff_text.splitlines():
        header = _DIFF_HEADER_RE.match(line)
        if header:
            current_file = header.group(2)
            pending.clear()
            context_stack.clear()
            continue
        if not current_file or current_file not in DESIGN_MD_DIFF_PATHS:
            continue
        if line.startswith("@@"):
            pending.clear()
            continue
        if line.startswith(" ") and not line.startswith(" ---"):
            _update_yaml_context(context_stack, line[1:])
            continue
        if line.startswith("-") and not line.startswith("---"):
            candidate = _extract_design_md_candidate(line[1:], context_stack, current_file)
            if candidate is not None:
                pending.append(candidate)
            continue
        if line.startswith("+") and not line.startswith("+++"):
            candidate = _extract_design_md_candidate(line[1:], context_stack, current_file)
            if candidate is None:
                continue
            match_index = _find_pending_change(pending, candidate, keys=("token",))
            if match_index is None:
                continue
            old = pending.pop(match_index)
            if old["value"] == candidate["value"]:
                continue
            changes.append(
                {
                    "token": candidate["token"],
                    "old": old["value"],
                    "new": candidate["value"],
                    "source_file": current_file,
                }
            )

    return changes


def _detect_lexicon_changes(project_root: Path, base_ref: str) -> list[dict[str, str]]:
    """Detect project_lexicon.yaml naming_convention changes via git diff."""
    diff_text = _diff_files(base_ref, cwd=project_root, paths=LEXICON_DIFF_PATHS)
    changes: list[dict[str, str]] = []
    pending: list[dict[str, str]] = []
    current_file = ""
    current_convention = ""

    for line in diff_text.splitlines():
        header = _DIFF_HEADER_RE.match(line)
        if header:
            current_file = header.group(2)
            current_convention = ""
            pending.clear()
            continue
        if not current_file or current_file not in LEXICON_DIFF_PATHS:
            continue
        if line.startswith("@@"):
            pending.clear()
            continue
        if line.startswith(" "):
            parsed = _parse_yaml_key_value(line[1:])
            if parsed and parsed[0] == "id":
                current_convention = parsed[1]
            continue
        if line.startswith("-") and not line.startswith("---"):
            candidate = _extract_lexicon_candidate(line[1:], current_convention, current_file)
            if candidate is not None:
                pending.append(candidate)
            if candidate is not None and candidate["kind"] == "id":
                current_convention = candidate["value"]
            continue
        if line.startswith("+") and not line.startswith("+++"):
            candidate = _extract_lexicon_candidate(line[1:], current_convention, current_file)
            if candidate is None:
                continue
            match_index = _find_pending_change(
                pending,
                candidate,
                keys=("convention", "kind"),
                fallback_keys=("kind",),
            )
            if match_index is None:
                continue
            old = pending.pop(match_index)
            if old["value"] == candidate["value"]:
                continue
            convention = candidate["convention"] if candidate["kind"] != "id" else candidate["value"]
            changes.append(
                {
                    "convention": convention,
                    "old": old["value"],
                    "new": candidate["value"],
                    "kind": candidate["kind"],
                    "source_file": current_file,
                }
            )
            if candidate["kind"] == "id":
                current_convention = candidate["value"]

    return changes


def propagate_reverse(
    project_root: Path,
    source: str,
    base_ref: str | None,
    apply: bool = False,
) -> int:
    """Reverse-propagate DESIGN.md or lexicon changes toward implementation."""
    project_root = Path(project_root).resolve()
    resolved_base = _resolve_base_ref(base_ref, cwd=project_root)
    if source == "design_token":
        changes = _detect_design_md_changes(project_root, resolved_base)
    elif source == "lexicon":
        changes = _detect_lexicon_changes(project_root, resolved_base)
    else:
        raise ValueError(f"Unknown source: {source!r}")

    if not changes:
        print(f"No changes detected: no {source} changes detected since {resolved_base}.")
        return 0

    bus = EventBus()
    routing = None if apply else {"red": "manual", "amber": "manual", "green": "manual"}
    Orchestrator(
        bus,
        routing=routing,
        hitl_path=str(project_root / "docs" / "coherence" / "pending_hitl.md"),
    )

    events = _build_reverse_drift_events(source, changes)
    with use_coherence_bus(bus):
        for event in events:
            bus.publish(event)

    proposals = _build_reverse_fix_proposals(project_root, events)
    applied_files = _apply_reverse_changes(project_root, source, changes) if apply else []
    result = ReversePropagationResult(
        source=source,
        base_ref=resolved_base,
        changes=changes,
        events=events,
        proposals=proposals,
        applied_files=applied_files,
    )
    _print_reverse_result(result, apply=apply)
    return 0


def _build_reverse_drift_events(source: str, changes: list[dict[str, str]]) -> list[DriftEvent]:
    events: list[DriftEvent] = []
    for change in changes:
        if source == "design_token":
            token = change["token"]
            old_value = change["old"]
            new_value = change["new"]
            safe_literal = _is_safe_literal_replacement(old_value, new_value)
            events.append(
                DriftEvent(
                    source_artifact="design_md",
                    target_artifact="implementation",
                    change_type="modified",
                    payload={
                        "description": (
                            f"DESIGN.md token {token!r} changed from "
                            f"{old_value!r} to {new_value!r}."
                        ),
                        "suggested_action": "Update implementation references that still use the old token value.",
                        "token": token,
                        "token_name": token,
                        "old_value": old_value,
                        "new_value": new_value,
                        "actual_value": old_value,
                        "expected_value": new_value,
                        "file": change.get("source_file", "DESIGN.md"),
                    },
                    severity="red" if safe_literal else "amber",
                    fix_strategy="auto" if safe_literal else "hitl",
                    kind="design_token_drift",
                )
            )
            continue

        convention = change["convention"]
        events.append(
            DriftEvent(
                source_artifact="lexicon",
                target_artifact="implementation",
                change_type="modified",
                payload={
                    "description": (
                        f"Lexicon naming convention {convention!r} "
                        f"{change['kind']} changed from {change['old']!r} to {change['new']!r}."
                    ),
                    "suggested_action": "Review identifiers and design nodes that use this convention.",
                    "convention": convention,
                    "term": convention,
                    "violation_type": f"naming_convention_{change['kind']}_changed",
                    "old_value": change["old"],
                    "new_value": change["new"],
                    "file": change.get("source_file", "project_lexicon.yaml"),
                },
                severity="amber",
                fix_strategy="hitl",
                kind="lexicon_violation",
            )
        )
    return events


def _build_reverse_fix_proposals(project_root: Path, events: list[DriftEvent]) -> list[FixProposal]:
    proposals: list[FixProposal] = []
    for event in events:
        strategy = get_strategy(event.kind, project_root)
        if strategy is None:
            continue
        proposals.extend(strategy.propose(event))
    return proposals


def _apply_reverse_changes(
    project_root: Path,
    source: str,
    changes: list[dict[str, str]],
) -> list[str]:
    if source != "design_token":
        return []

    changed_files: set[str] = set()
    safe_changes = [
        change
        for change in changes
        if _is_safe_literal_replacement(change.get("old", ""), change.get("new", ""))
    ]
    if not safe_changes:
        return []

    for path in _iter_reverse_implementation_files(project_root):
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        updated = content
        for change in safe_changes:
            updated = updated.replace(change["old"], change["new"])
        if updated == content:
            continue
        path.write_text(updated, encoding="utf-8")
        changed_files.add(path.relative_to(project_root).as_posix())

    return sorted(changed_files)


def _iter_reverse_implementation_files(project_root: Path):
    for path in project_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _IMPLEMENTATION_EXTENSIONS:
            continue
        try:
            relative_parts = path.relative_to(project_root).parts
        except ValueError:
            relative_parts = path.parts
        if any(part in _SKIPPED_REVERSE_DIRS for part in relative_parts):
            continue
        yield path


def _print_reverse_result(result: ReversePropagationResult, apply: bool) -> None:
    print(
        f"Reverse propagation source={result.source} base={result.base_ref}: "
        f"{len(result.changes)} change(s), {len(result.events)} event(s)."
    )
    for change in result.changes:
        name = change.get("token", change.get("convention", "<unknown>"))
        print(f"  - {name}: {change['old']} -> {change['new']}")
    if result.proposals:
        print(f"Fix proposals: {len(result.proposals)}")
        for proposal in result.proposals:
            mode = "auto" if proposal.can_auto_apply else "hitl"
            print(f"  - [{mode}] {proposal.kind}: {proposal.description}")
    if apply:
        print(f"Applied implementation files: {len(result.applied_files)}")
        for file_path in result.applied_files:
            print(f"  - {file_path}")
    else:
        print("Dry run: no implementation files changed. Re-run with --apply to apply safe literal replacements.")


def _extract_design_md_candidate(
    line: str,
    context_stack: list[tuple[int, str]],
    source_file: str,
) -> dict[str, str] | None:
    parsed = _parse_yaml_key_value(line)
    if parsed:
        key, value = parsed
        if key in {"$type", "type", "description"}:
            return None
        token_path = ".".join(key for _, key in context_stack if key not in {"codd"})
        if key in {"$value", "value"} and token_path:
            token = token_path
        elif token_path and "." not in key:
            token = f"{token_path}.{key}"
        else:
            token = key
        if not token or value in {"", "{}", "[]"}:
            return None
        return {"token": token, "value": value, "source_file": source_file}

    table = _parse_markdown_token_row(line)
    if table is not None:
        token, value = table
        return {"token": token, "value": value, "source_file": source_file}
    return None


def _extract_lexicon_candidate(
    line: str,
    current_convention: str,
    source_file: str,
) -> dict[str, str] | None:
    parsed = _parse_yaml_key_value(line)
    if parsed is None:
        return None
    key, value = parsed
    if key == "id":
        return {
            "convention": value,
            "kind": "id",
            "value": value,
            "source_file": source_file,
        }
    if key in {"regex", "pattern"}:
        return {
            "convention": current_convention,
            "kind": key,
            "value": value,
            "source_file": source_file,
        }
    return None


def _parse_yaml_key_value(line: str) -> tuple[str, str] | None:
    match = _YAML_KEY_VALUE_RE.match(line.rstrip())
    if not match:
        return None
    key = match.group("key")
    value = _clean_yaml_value(match.group("value"))
    return key, value


def _clean_yaml_value(value: str) -> str:
    value = value.strip()
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    if value.endswith(","):
        value = value[:-1].rstrip()
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        value = value[1:-1]
    return value


def _parse_markdown_token_row(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped.startswith("|") or stripped.count("|") < 3:
        return None
    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
    if len(cells) < 2 or set(cells[0]) <= {"-", ":"}:
        return None
    token = cells[0].strip("`")
    value = cells[1].strip("`")
    if not token or not value or token.lower() in {"token", "name"}:
        return None
    return token, value


def _update_yaml_context(context_stack: list[tuple[int, str]], line: str) -> None:
    match = _YAML_KEY_VALUE_RE.match(line.rstrip())
    if not match:
        return
    key = match.group("key")
    if key.startswith("$"):
        return
    value = _clean_yaml_value(match.group("value"))
    if value not in {"", "|", ">", "{}"}:
        return
    indent = len(match.group("indent"))
    while context_stack and context_stack[-1][0] >= indent:
        context_stack.pop()
    context_stack.append((indent, key))


def _find_pending_change(
    pending: list[dict[str, str]],
    candidate: dict[str, str],
    keys: tuple[str, ...],
    fallback_keys: tuple[str, ...] = (),
) -> int | None:
    for index, item in enumerate(pending):
        if all(item.get(key) == candidate.get(key) for key in keys):
            return index
    if fallback_keys:
        for index, item in enumerate(pending):
            if all(item.get(key) == candidate.get(key) for key in fallback_keys):
                return index
    if len(pending) == 1:
        return 0
    return None


def _is_safe_literal_replacement(old_value: str, new_value: str) -> bool:
    old_value = old_value.strip()
    new_value = new_value.strip()
    hex_re = re.compile(r"^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})$")
    px_re = re.compile(r"^\d+(?:\.\d+)?px$")
    return bool(
        old_value
        and new_value
        and (
            (hex_re.match(old_value) and hex_re.match(new_value))
            or (px_re.match(old_value) and px_re.match(new_value))
        )
    )


def _format_commit_reason(
    effective_reasons: dict[str, str],
    default_reason: str | None,
) -> str | None:
    """Format commit message from per-file reasons."""
    if not effective_reasons:
        return default_reason

    # If all reasons are the same, use a single line
    unique_reasons = set(effective_reasons.values())
    if len(unique_reasons) == 1:
        return next(iter(unique_reasons))

    # Multiple reasons → per-file listing
    lines = []
    for path, r in sorted(effective_reasons.items()):
        lines.append(f"- {path}: {r}")
    return "\n".join(lines)


def _classify_docs_by_band(
    project_root: Path,
    config: dict[str, Any],
    affected_docs: list[AffectedDoc],
    bands_config: dict[str, Any],
) -> list[VerifiedDoc]:
    """Classify affected docs into green/amber/gray bands using graph evidence."""
    from codd.graph import CEG

    green_cfg = bands_config.get("green", {})
    green_threshold = green_cfg.get("min_confidence", 0.90)
    green_min_evidence = green_cfg.get("min_evidence_count", 2)
    amber_threshold = bands_config.get("amber", {}).get("min_confidence", 0.50)

    # Try to load graph for confidence data
    graph = _load_graph(project_root, config)

    verified: list[VerifiedDoc] = []
    for doc in affected_docs:
        confidence, evidence_count = _get_doc_confidence(graph, doc)

        # Use CEG.classify_band if graph available, else inline fallback
        if graph is not None:
            band = graph.classify_band(
                confidence, evidence_count,
                green_threshold, green_min_evidence, amber_threshold,
            )
        else:
            band = "amber"  # no graph → always HITL

        verified.append(VerifiedDoc(
            doc=doc, band=band,
            confidence=confidence, evidence_count=evidence_count,
        ))

    return verified


def _load_graph(project_root: Path, config: dict[str, Any]):
    """Load CEG graph if it exists, return None otherwise."""
    from codd.graph import CEG

    graph_path = config.get("graph", {}).get("path", "codd/scan")
    codd_dir = find_codd_dir(project_root)
    if codd_dir is None:
        return None

    scan_dir = project_root / graph_path
    if not scan_dir.exists() or not (scan_dir / "nodes.jsonl").exists():
        return None

    return CEG(scan_dir)


def _get_doc_confidence(graph, doc: AffectedDoc) -> tuple[float, int]:
    """Get aggregated confidence for a doc from graph edges.

    Looks at ALL edges involving the doc's node_id (both incoming and
    outgoing) and aggregates confidence. The presence and quality of
    evidence on any edge indicates how well-established the doc is.
    Returns (confidence, evidence_count). Falls back to (0.5, 0) if no graph.
    """
    if graph is None:
        return (0.5, 0)

    node = graph.get_node(doc.node_id)
    if node is None:
        return (0.5, 0)

    # Collect all edges involving this node
    all_edges = graph.get_incoming_edges(doc.node_id) + graph.get_outgoing_edges(doc.node_id)
    if not all_edges:
        return (0.5, 0)

    # Aggregate: max confidence across all edges, sum evidence
    max_confidence = 0.0
    total_evidence = 0
    for edge in all_edges:
        conf = edge.get("confidence", 0.0)
        ev_count = len(edge.get("evidence", []))
        if conf > max_confidence:
            max_confidence = conf
        total_evidence += ev_count

    return (max_confidence, total_evidence)


def _save_verify_state(
    project_root: Path,
    auto_applied: list[VerifiedDoc],
    hitl: list[VerifiedDoc],
    updated: list[str],
    diff_target: str,
) -> None:
    """Save verify state for subsequent --commit."""
    codd_dir = find_codd_dir(project_root)
    if codd_dir is None:
        return

    state = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "diff_target": diff_target,
        "auto_node_ids": updated,
        "hitl_node_ids": [v.doc.node_id for v in hitl],
        "auto_docs": [
            {"node_id": v.doc.node_id, "path": v.doc.path, "band": v.band,
             "confidence": v.confidence}
            for v in auto_applied
        ],
        "hitl_docs": [
            {"node_id": v.doc.node_id, "path": v.doc.path, "band": v.band,
             "confidence": v.confidence}
            for v in hitl
        ],
    }

    state_path = codd_dir / VERIFY_STATE_FILE
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_verify_state(project_root: Path) -> dict | None:
    """Load verify state from previous --verify run."""
    codd_dir = find_codd_dir(project_root)
    if codd_dir is None:
        return None

    state_path = codd_dir / VERIFY_STATE_FILE
    if not state_path.exists():
        return None

    return json.loads(state_path.read_text(encoding="utf-8"))


def _clear_verify_state(project_root: Path) -> None:
    """Remove verify state file after commit."""
    codd_dir = find_codd_dir(project_root)
    if codd_dir is None:
        return

    state_path = codd_dir / VERIFY_STATE_FILE
    if state_path.exists():
        state_path.unlink()


def _record_hitl_knowledge(
    project_root: Path,
    state: dict,
    committed_files: list[str],
    reasons: dict[str, str],
    config: dict[str, Any] | None = None,
) -> int:
    """Record HITL corrections as human evidence in the graph.

    Args:
        reasons: Per-file reason map {path: reason}.

    This evidence survives purge_auto_generated() and improves future
    confidence scores via Noisy-OR aggregation.
    """
    if config is None:
        config = load_project_config(project_root)
    graph = _load_graph(project_root, config)
    if graph is None:
        return 0

    count = 0
    committed_set = set(committed_files)
    hitl_docs = state.get("hitl_docs", [])

    for item in hitl_docs:
        path = item["path"]
        if path not in committed_set:
            continue

        reason = reasons.get(path, "HITL correction (no reason provided)")
        node_id = item["node_id"]
        # Find all edges involving this node and add human evidence
        all_edges = graph.get_incoming_edges(node_id) + graph.get_outgoing_edges(node_id)
        for edge in all_edges:
            graph.add_evidence(
                edge_id=edge["id"],
                source_type="human",
                method="hitl_correction",
                score=0.85,
                detail=f"HITL: {reason}",
            )
            count += 1

    graph.close()
    return count


def _git_commit_propagation(
    project_root: Path,
    files: list[str],
    reason: str | None,
) -> None:
    """Git add and commit propagation changes."""
    import logging

    logger = logging.getLogger("codd.propagator")
    try:
        add_result = subprocess.run(
            ["git", "add"] + files,
            cwd=str(project_root), capture_output=True, text=True, encoding="utf-8",
        )
        if add_result.returncode != 0:
            logger.warning("git add failed: %s", add_result.stderr.strip())
            return

        msg = "docs: propagate design changes"
        if reason:
            msg += f"\n\n{reason}"
        commit_result = subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=str(project_root), capture_output=True, text=True, encoding="utf-8",
        )
        if commit_result.returncode != 0:
            logger.warning("git commit failed: %s", commit_result.stderr.strip())
    except FileNotFoundError:
        pass  # git not available


def _get_changed_files(project_root: Path, diff_target: str) -> list[str]:
    """Get changed files from git diff."""
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotePath=false", "diff", "--name-only", diff_target],
            capture_output=True, text=True, encoding="utf-8", cwd=str(project_root),
        )
        if result.returncode != 0:
            return []
        return [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
    except FileNotFoundError:
        return []


def _map_files_to_modules(
    changed_files: list[str],
    source_dirs: list[str],
) -> dict[str, str]:
    """Map changed source files to module names.

    Module = first directory under a source_dir.
    e.g. src/auth/service.py with source_dirs=["src"] → module "auth"
    """
    file_module: dict[str, str] = {}
    normalized_dirs = [d.rstrip("/") for d in source_dirs]

    for f in changed_files:
        parts = PurePosixPath(f).parts
        for src_dir in normalized_dirs:
            src_parts = PurePosixPath(src_dir).parts
            if parts[: len(src_parts)] == src_parts and len(parts) > len(src_parts) + 1:
                # First dir after source_dir is the module
                module_name = parts[len(src_parts)]
                file_module[f] = module_name
                break

    return file_module


def _find_design_docs_by_modules(
    project_root: Path,
    config: dict[str, Any],
    changed_modules: set[str],
    file_module_map: dict[str, str],
) -> list[AffectedDoc]:
    """Find design documents whose `modules` field overlaps with changed modules."""
    affected: list[AffectedDoc] = []
    seen_node_ids: set[str] = set()

    doc_dirs = config.get("scan", {}).get("doc_dirs", [])

    for doc_dir in doc_dirs:
        full_path = project_root / doc_dir
        if not full_path.exists():
            continue

        for md_file in full_path.rglob("*.md"):
            codd_data = _extract_frontmatter(md_file)
            if not codd_data or "node_id" not in codd_data:
                continue

            doc_modules = codd_data.get("modules", [])
            if not doc_modules:
                continue

            matched = set(doc_modules) & changed_modules
            if not matched:
                continue

            node_id = codd_data["node_id"]
            if node_id in seen_node_ids:
                continue
            seen_node_ids.add(node_id)

            # Collect changed files for matched modules
            matched_files = [f for f, m in file_module_map.items() if m in matched]

            rel_path = md_file.relative_to(project_root).as_posix()
            affected.append(AffectedDoc(
                node_id=node_id,
                path=rel_path,
                title=codd_data.get("title", node_id),
                modules=doc_modules,
                matched_modules=sorted(matched),
                changed_files=matched_files,
            ))

    # Also check wave_config artifacts (they have modules but may not be scanned yet)
    wave_config = config.get("wave_config")
    if wave_config:
        for _wave, artifacts in wave_config.items():
            for art in artifacts:
                modules = art.get("modules", [])
                matched = set(modules) & changed_modules
                if not matched:
                    continue
                node_id = art["node_id"]
                if node_id in seen_node_ids:
                    continue
                output_path = project_root / art["output"]
                if not output_path.exists():
                    continue
                seen_node_ids.add(node_id)
                matched_files = [f for f, m in file_module_map.items() if m in matched]
                affected.append(AffectedDoc(
                    node_id=node_id,
                    path=art["output"],
                    title=art.get("title", node_id),
                    modules=modules,
                    matched_modules=sorted(matched),
                    changed_files=matched_files,
                ))

    return affected


def _find_changed_docs(
    project_root: Path,
    config: dict[str, Any],
    changed_files: list[str],
) -> list[dict]:
    """Identify changed files that are CoDD design documents (have frontmatter).

    Returns a list of dicts with node_id, path for each changed design doc.
    """
    changed_docs: list[dict] = []
    doc_dirs = config.get("scan", {}).get("doc_dirs", [])

    for f in changed_files:
        # Check if the file is under a doc_dir and has frontmatter
        md_path = project_root / f
        if not md_path.exists() or not f.endswith(".md"):
            continue

        # Verify it's under a recognized doc_dir
        in_doc_dir = any(f.startswith(d.rstrip("/") + "/") for d in doc_dirs)
        if not in_doc_dir:
            continue

        codd_data = _extract_frontmatter(md_path)
        if codd_data and "node_id" in codd_data:
            changed_docs.append({
                "node_id": codd_data["node_id"],
                "path": f,
                "title": codd_data.get("title", codd_data["node_id"]),
            })

    return changed_docs


def _find_docs_depending_on(
    project_root: Path,
    config: dict[str, Any],
    changed_docs: list[dict],
) -> list[AffectedDoc]:
    """Find design documents that depend on the changed design documents.

    Uses the CEG graph: for each changed doc, find all docs that have an edge
    pointing TO the changed doc (incoming edges = docs that depend on it).
    """
    graph = _load_graph(project_root, config)
    if graph is None:
        return []

    changed_node_ids = {d["node_id"] for d in changed_docs}
    changed_paths = {d["path"] for d in changed_docs}
    dependent_node_ids: dict[str, set[str]] = {}  # node_id → set of triggering node_ids

    for changed_doc in changed_docs:
        # Find docs that depend on this changed doc
        incoming = graph.get_incoming_edges(changed_doc["node_id"])
        for edge in incoming:
            source_id = edge["source_id"]
            # Skip self-references and skip docs that are themselves changed
            if source_id in changed_node_ids:
                continue
            if source_id not in dependent_node_ids:
                dependent_node_ids[source_id] = set()
            dependent_node_ids[source_id].add(changed_doc["node_id"])

    if not dependent_node_ids:
        return []

    # Resolve node_ids to file paths by scanning doc_dirs
    affected: list[AffectedDoc] = []
    doc_dirs = config.get("scan", {}).get("doc_dirs", [])

    for doc_dir in doc_dirs:
        full_path = project_root / doc_dir
        if not full_path.exists():
            continue

        for md_file in full_path.rglob("*.md"):
            codd_data = _extract_frontmatter(md_file)
            if not codd_data or "node_id" not in codd_data:
                continue

            node_id = codd_data["node_id"]
            if node_id not in dependent_node_ids:
                continue

            rel_path = md_file.relative_to(project_root).as_posix()
            if rel_path in changed_paths:
                continue  # Don't update the doc that was changed

            triggering = sorted(dependent_node_ids[node_id])
            affected.append(AffectedDoc(
                node_id=node_id,
                path=rel_path,
                title=codd_data.get("title", node_id),
                modules=codd_data.get("modules", []),
                matched_modules=[],  # not module-based
                changed_files=[d["path"] for d in changed_docs
                               if d["node_id"] in dependent_node_ids[node_id]],
            ))

    return affected


def _get_code_diff(
    project_root: Path,
    diff_target: str,
    files: list[str],
) -> str:
    """Get the actual code diff for specific files."""
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotePath=false", "diff", diff_target, "--"] + files,
            capture_output=True, text=True, encoding="utf-8", cwd=str(project_root),
        )
        if result.returncode != 0:
            return ""
        return result.stdout
    except FileNotFoundError:
        return ""


def _build_update_prompt(
    doc: AffectedDoc,
    current_content: str,
    code_diff: str,
    feedback: str | None = None,
    coherence_context: dict[str, Any] | None = None,
) -> str:
    """Build a prompt for AI to update a design document based on changes."""
    # Detect whether the diff is from source code or design docs
    is_doc_change = all(f.endswith(".md") for f in doc.changed_files) if doc.changed_files else False

    if is_doc_change:
        intro = [
            "You are UPDATING an existing design document to reflect changes in an upstream design document.",
            "",
            "The diff below shows what changed in the upstream design document that this document depends on.",
            "The current design document is provided in full.",
            "Your task is to update ONLY the parts that reference, quote, or depend on values from the changed upstream document.",
            "For example, if the upstream document changed a threshold from 100 to 200, update any reference to that threshold.",
        ]
    else:
        intro = [
            "You are UPDATING an existing design document to reflect source code changes.",
            "",
            "The code diff below shows what changed in the source code.",
            "The current design document is provided in full.",
            "Your task is to update ONLY the parts of the design document that are affected by the code changes.",
        ]

    lines = intro + [
        "",
        "CRITICAL RULES:",
        "- Preserve the existing document structure and frontmatter EXACTLY.",
        "- Only modify sections that are directly affected by the diff.",
        "- If the change doesn't affect this document's content, output the body UNCHANGED.",
        "- Do NOT add new sections or remove existing sections.",
        "- Do NOT change the title.",
        "- Do NOT emit YAML frontmatter — only the body content.",
        "- Do NOT prepend analysis comments or preamble before the document body.",
        "  Start directly with the first heading (# Title). No 'The code diff is...' lines.",
        "",
        f"Document: {doc.node_id}",
        f"Title: {doc.title}",
        f"Covers modules: {', '.join(doc.modules)}" if doc.modules else "",
        f"Changed modules: {', '.join(doc.matched_modules)}" if doc.matched_modules else "",
        f"Changed files: {', '.join(doc.changed_files)}",
        "",
        "--- CURRENT DESIGN DOCUMENT ---",
        current_content.rstrip(),
        "--- END CURRENT DOCUMENT ---",
        "",
        "--- CODE DIFF ---",
        code_diff[:8000] if code_diff else "(no diff available)",
        "--- END CODE DIFF ---",
        "",
    ]

    if feedback:
        lines.extend([
            "",
            "--- REVIEW FEEDBACK (from previous update attempt) ---",
            "A reviewer found issues with a previous version of this update.",
            "You MUST address ALL of the following feedback:",
            feedback.rstrip(),
            "--- END REVIEW FEEDBACK ---",
        ])

    if coherence_context:
        lexicon = coherence_context.get("lexicon")
        if lexicon:
            lines.extend([
                "",
                "## Project Lexicon (must respect naming conventions)",
                _format_coherence_text(lexicon),
            ])

        design_md = coherence_context.get("design_md")
        if design_md:
            lines.extend([
                "",
                "## Design Tokens (must respect these values)",
                _format_coherence_text(design_md)[:2000],
            ])

    lines.append(
        "Output the updated document body now. If no design-level changes are needed, "
        "output the existing body unchanged. Start with the first section heading.",
    )
    return "\n".join(lines).rstrip() + "\n"


def _format_coherence_text(value: Any) -> str:
    """Render optional coherence context into prompt-friendly text."""
    if hasattr(value, "as_context_string"):
        return str(value.as_context_string()).strip()
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _sanitize_update_body(body: str) -> str:
    """Light sanitization for update bodies — no structural validation."""
    import re

    normalized = body.lstrip()
    # Strip accidental frontmatter
    if normalized.startswith("---"):
        match = re.match(r"^---\s*\n.*?\n---\s*\n?", normalized, re.DOTALL)
        if match:
            normalized = normalized[match.end():]
    # Strip markdown fences
    fenced = MARKDOWN_FENCE_RE.match(normalized)
    if fenced:
        normalized = fenced.group("body")
    # Strip AI preamble before the first heading.
    # Some models emit analysis comments like "The code diff is..." before
    # the actual document body.  Drop everything before the first "# " heading.
    heading_match = re.search(r"^(# .+)$", normalized, re.MULTILINE)
    if heading_match and heading_match.start() > 0:
        normalized = normalized[heading_match.start():]
    if not normalized.strip():
        raise ValueError("AI command returned empty output for propagation update")
    return normalized.strip() + "\n"


def _write_updated_doc(doc_path: Path, original_content: str, new_body: str) -> None:
    """Replace the body of a design document while preserving frontmatter."""
    # Split original into frontmatter + body
    import re

    match = re.match(r'^(---\s*\n.*?\n---\s*\n)', original_content, re.DOTALL)
    if match:
        frontmatter = match.group(1)
    else:
        frontmatter = ""

    # Find the title in the new body and skip it (frontmatter already has context)
    body_lines = new_body.strip().split("\n")
    if body_lines and body_lines[0].startswith("# "):
        # Keep the title from original
        title_match = re.search(r'^# .+$', original_content, re.MULTILINE)
        if title_match:
            title_line = title_match.group(0)
            body_lines[0] = title_line

    doc_path.write_text(frontmatter + "\n".join(body_lines) + "\n", encoding="utf-8")
