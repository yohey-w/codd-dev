"""Reconciliation ledger for doc-to-doc ``depends_on`` freshness.

Propagation is an *event*; coherence is a *state*. The ledger turns the
"this downstream document was reconciled against that upstream document"
judgement -- made when ``codd propagate --commit`` completes or when a HITL
review concludes no update is needed -- into durable state, keyed per
``depends_on`` edge. The ``dependency_freshness`` DAG check later compares
the acknowledged upstream commit with the upstream document's current last
commit, so an un-propagated upstream change can no longer silently vanish
once the git-diff window has moved on.

The ledger is generic by design: it stores only relative file paths and git
commit hashes. No project-, framework-, or domain-specific vocabulary.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codd.config import find_codd_dir


LEDGER_FILE = "reconciliation_ledger.json"
LEDGER_VERSION = 1


def ledger_path(project_root: Path) -> Path:
    """Return the ledger location for ``project_root``.

    Prefers the discovered CoDD config dir (``codd/`` or ``.codd/``); falls
    back to ``.codd/`` so reads never raise for non-CoDD layouts.
    """

    codd_dir = find_codd_dir(Path(project_root))
    if codd_dir is None:
        return Path(project_root) / ".codd" / LEDGER_FILE
    return codd_dir / LEDGER_FILE


def edge_key(downstream_path: str, upstream_path: str) -> str:
    """Stable ledger key for a downstream->upstream ``depends_on`` edge."""

    return f"{downstream_path} -> {upstream_path}"


def load_ledger(project_root: Path) -> dict[str, Any] | None:
    """Load the reconciliation ledger, or ``None`` when absent/corrupt."""

    path = ledger_path(project_root)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    edges = payload.get("edges")
    if not isinstance(edges, dict):
        payload["edges"] = {}
    return payload


def record_reconciliation(
    project_root: Path,
    downstream_path: str,
    upstream_path: str,
    *,
    method: str = "propagate_commit",
    reason: str | None = None,
) -> bool:
    """Acknowledge that ``downstream_path`` was reconciled with ``upstream_path``.

    Records the upstream document's current last commit hash. Returns ``True``
    when an entry was written, ``False`` when git history was unavailable.

    ``method`` records *how* the edge was acknowledged (e.g.
    ``"propagate_commit"``, ``"baseline_ack"``). ``reason``, when provided,
    stores an operator-supplied note alongside the entry. Both are optional and
    backward compatible: existing callers and the on-disk shape are unchanged
    when neither is passed (``reason`` is only added to the entry when given).
    """

    upstream_commit = last_commit_for_path(project_root, upstream_path)
    if not upstream_commit:
        return False

    ledger = load_ledger(project_root) or {"version": LEDGER_VERSION, "edges": {}}
    ledger.setdefault("version", LEDGER_VERSION)
    edges = ledger.setdefault("edges", {})
    entry: dict[str, Any] = {
        "upstream_commit": upstream_commit,
        "acked_at": datetime.now(timezone.utc).isoformat(),
        "method": method,
    }
    if reason is not None:
        entry["reason"] = reason
    edges[edge_key(downstream_path, upstream_path)] = entry

    path = ledger_path(project_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(ledger, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
    except OSError:
        return False
    return True


def last_commit_for_path(project_root: Path, rel_path: str) -> str | None:
    """Return the last commit hash touching ``rel_path``, or ``None``."""

    output = _git_log_value(project_root, rel_path, "%H")
    return output or None


def last_commit_timestamp_for_path(project_root: Path, rel_path: str) -> int | None:
    """Return the unix timestamp of the last commit touching ``rel_path``."""

    output = _git_log_value(project_root, rel_path, "%ct")
    if not output:
        return None
    try:
        return int(output)
    except ValueError:
        return None


def commit_history_for_path(project_root: Path, rel_path: str) -> list[tuple[str, int]]:
    """Return ``(commit_hash, unix_timestamp)`` pairs touching ``rel_path``.

    Newest first. Empty list when git history is unavailable. Used by the
    ``dependency_freshness`` fallback heuristic to disambiguate edges whose
    endpoints were last touched by the same commit (a joint commit carries no
    ordering signal between the two documents).
    """

    try:
        result = subprocess.run(
            ["git", "log", "--format=%H %ct", "--", rel_path],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (FileNotFoundError, OSError):
        return []
    if result.returncode != 0:
        return []
    history: list[tuple[str, int]] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) != 2:
            continue
        commit, raw_ts = parts
        try:
            history.append((commit, int(raw_ts)))
        except ValueError:
            continue
    return history


def _git_log_value(project_root: Path, rel_path: str, fmt: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "log", "-1", f"--format={fmt}", "--", rel_path],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None
