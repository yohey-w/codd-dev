"""Mechanical auto-repair for DAG-verify violations.

Reads the violation output produced by ``codd dag verify`` and applies the
``suggested_*`` payloads to the project's filesystem without invoking AI.

Currently supported repair types:

* ``missing_journey_lexicon`` — append the ``suggested_lexicon_entry`` to
  ``project_lexicon.yaml`` under ``required_artifacts``.

Repair types that need a project-specific anchor (e.g. ``no_plan_task_for_journey``
which requires the user to choose which plan section gets the new outputs)
are listed in ``RepairOutcome.skipped`` with a human-readable reason so the
operator knows what still has to be done by hand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml


@dataclass(frozen=True)
class RepairAction:
    description: str


@dataclass
class RepairOutcome:
    applied: list[RepairAction] = field(default_factory=list)
    skipped: list[RepairAction] = field(default_factory=list)


def apply_auto_repair(
    project_root: Path,
    results: Iterable[Any],
    *,
    dry_run: bool = True,
) -> RepairOutcome:
    """Apply mechanical repairs derived from DAG verify violation output.

    Parameters
    ----------
    project_root
        Project root used to resolve relative file paths.
    results
        Iterable of DAG check result objects (or dicts) as returned by
        ``codd.dag.runner.run_all_checks``.
    dry_run
        When True, the function reports what *would* be applied without
        modifying the filesystem.
    """

    outcome = RepairOutcome()
    suggested_entries: list[dict[str, Any]] = []

    for result in results:
        violations = _result_value(result, "violations") or []
        if not isinstance(violations, list):
            continue
        for violation in violations:
            if not isinstance(violation, dict):
                continue
            v_type = violation.get("type")
            if v_type == "missing_journey_lexicon":
                entry = violation.get("suggested_lexicon_entry")
                if isinstance(entry, dict) and entry.get("id"):
                    suggested_entries.append(entry)
                else:
                    outcome.skipped.append(
                        RepairAction(
                            description=(
                                "missing_journey_lexicon without suggested_lexicon_entry; "
                                "violation cannot be auto-repaired"
                            )
                        )
                    )
                continue
            outcome.skipped.append(
                RepairAction(
                    description=f"{v_type or '<unknown>'} — no mechanical repair available",
                )
            )

    if suggested_entries:
        repaired_ids = _apply_lexicon_entries(
            project_root, suggested_entries, dry_run=dry_run
        )
        for entry_id in repaired_ids:
            verb = "would append" if dry_run else "appended"
            outcome.applied.append(
                RepairAction(
                    description=(
                        f"project_lexicon.yaml: {verb} required_artifacts entry "
                        f"'{entry_id}'"
                    )
                )
            )
        skipped_existing = {entry["id"] for entry in suggested_entries} - set(repaired_ids)
        for entry_id in sorted(skipped_existing):
            outcome.skipped.append(
                RepairAction(
                    description=(
                        f"project_lexicon.yaml: required_artifacts entry "
                        f"'{entry_id}' already declared"
                    )
                )
            )

    return outcome


def _apply_lexicon_entries(
    project_root: Path,
    entries: list[dict[str, Any]],
    *,
    dry_run: bool,
) -> list[str]:
    lexicon_path = project_root / "project_lexicon.yaml"
    if not lexicon_path.is_file():
        return []

    payload = yaml.safe_load(lexicon_path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        return []

    required_artifacts = payload.get("required_artifacts")
    if not isinstance(required_artifacts, list):
        required_artifacts = []

    existing_ids = {
        entry.get("id")
        for entry in required_artifacts
        if isinstance(entry, dict) and entry.get("id")
    }

    added_ids: list[str] = []
    for entry in entries:
        entry_id = entry.get("id")
        if not entry_id or entry_id in existing_ids:
            continue
        required_artifacts.append(dict(entry))
        existing_ids.add(entry_id)
        added_ids.append(entry_id)

    if not added_ids:
        return added_ids

    payload["required_artifacts"] = required_artifacts
    if not dry_run:
        lexicon_path.write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    return added_ids


def _result_value(result: Any, key: str) -> Any:
    if isinstance(result, dict):
        return result.get(key)
    return getattr(result, key, None)
