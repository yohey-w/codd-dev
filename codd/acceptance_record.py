"""Acceptance-evidence ledger — invariant (d), evidence bound to implementation.

Automated evidence re-runs itself, so it is *self-freshening*: change the code
and the test either still passes or turns red. A **manual** acceptance record
has no such property. Someone looks at the output once, says "correct", and that
verdict then sits in a document with nothing tying it to the artifact it was a
verdict about. Rewrite the implementation and the old "accepted" reads as
current — the evidence outlived the thing it verified.

This ledger is the binding. A manual pass is recorded together with the
**content hashes of the implementation files** that satisfied the criterion at
the moment of acceptance. When any of those files changes, the record is stale:
the check reports it and the criterion is unaccepted again, which is the honest
state — nobody has looked at the new implementation.

Storage mirrors the existing ``reconciliation_ledger.json`` precedent: a JSON
document inside the project's codd directory, written only by an explicit human
act (``codd acceptance record``). It is NEVER refreshed automatically — a
self-updating ledger detects nothing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

LEDGER_FILENAME = "acceptance_ledger.json"
LEDGER_VERSION = 1

# Bound the hashed read so a stray large artifact cannot stall verification.
_MAX_HASH_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class AcceptanceRecord:
    """A recorded human verdict on one acceptance criterion."""

    req_id: str
    status: str  # "pass" | "fail"
    by: str
    recorded_at: str
    note: str = ""
    # relative path -> sha256 of the file content at record time
    implementation: dict[str, str] = field(default_factory=dict)

    def is_stale(self, current: Mapping[str, str]) -> bool:
        """True when the implementation has moved since the verdict was given.

        Stale covers all three drifts: a recorded file whose content changed, a
        recorded file that disappeared, and a NEW implementer that appeared
        after the verdict (the criterion is now satisfied by code nobody
        accepted).
        """

        return dict(self.implementation) != dict(current)


def ledger_path(project_root: Path | str, codd_dir: Path | None = None) -> Path:
    """Location of the ledger: ``<codd-dir>/acceptance_ledger.json``."""

    root = Path(project_root).resolve()
    if codd_dir is not None:
        return Path(codd_dir) / LEDGER_FILENAME
    from codd.config import find_codd_dir

    try:
        resolved = find_codd_dir(root)
    except Exception:  # pragma: no cover - config-less project
        resolved = None
    return (Path(resolved) if resolved else root / "codd") / LEDGER_FILENAME


def load_ledger(project_root: Path | str, codd_dir: Path | None = None) -> dict[str, AcceptanceRecord]:
    """Read the ledger. A missing or malformed ledger reads as EMPTY, never as evidence."""

    path = ledger_path(project_root, codd_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    records = payload.get("records") if isinstance(payload, Mapping) else None
    if not isinstance(records, Mapping):
        return {}

    loaded: dict[str, AcceptanceRecord] = {}
    for req_id, raw in records.items():
        if not isinstance(raw, Mapping):
            continue
        implementation = raw.get("implementation")
        loaded[str(req_id)] = AcceptanceRecord(
            req_id=str(req_id),
            status=str(raw.get("status", "")),
            by=str(raw.get("by", "")),
            recorded_at=str(raw.get("recorded_at", "")),
            note=str(raw.get("note", "")),
            implementation={
                str(key): str(value)
                for key, value in (implementation.items() if isinstance(implementation, Mapping) else ())
            },
        )
    return loaded


def write_ledger(
    project_root: Path | str,
    records: Mapping[str, AcceptanceRecord],
    codd_dir: Path | None = None,
) -> Path:
    """Persist the ledger (sorted, pretty-printed — it is reviewed in diffs)."""

    path = ledger_path(project_root, codd_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": LEDGER_VERSION,
        "records": {
            req_id: {key: value for key, value in asdict(records[req_id]).items() if key != "req_id"}
            for req_id in sorted(records)
        },
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return path


def implementation_digest(project_root: Path | str, paths: Iterable[str]) -> dict[str, str]:
    """Content hashes of the implementation files that satisfy a criterion.

    A path that cannot be read is recorded as ``"missing"`` rather than skipped:
    dropping it would make a deleted implementer look like "nothing changed".
    """

    root = Path(project_root).resolve()
    digest: dict[str, str] = {}
    for relative in sorted(set(paths)):
        path = root / relative
        try:
            data = path.read_bytes()[:_MAX_HASH_BYTES]
        except OSError:
            digest[relative] = "missing"
            continue
        digest[relative] = hashlib.sha256(data).hexdigest()
    return digest


def expand_to_closure(project_root: Path | str, paths: Iterable[str]) -> list[str]:
    """Grow a set of implementation files to include what they import.

    A verdict about ``make-sheet.ts`` is really a verdict about the behaviour
    that file produces, which usually lives partly in the modules it imports.
    Recording only the named file would let the accepted behaviour move one file
    sideways and keep the record looking current — so the ledger stores the same
    closure the check re-hashes. Best effort: if the DAG cannot be built (a
    project with no CoDD config, a test harness), the paths are recorded as
    given rather than failing the record.
    """

    given = sorted({path for path in paths if path})
    if not given:
        return []
    try:
        from codd.acceptance_evidence import implementation_closure
        from codd.dag.builder import build_dag

        dag = build_dag(Path(project_root).resolve())
        impl_paths = {
            node.id
            for node in getattr(dag, "nodes", {}).values()
            if getattr(node, "kind", "") in {"impl_file", "common"}
        }
        expanded = implementation_closure(dag, given, impl_paths)
    except Exception:  # never block a human verdict on graph construction
        return given
    return sorted(set(expanded) | set(given))


def record_acceptance(
    project_root: Path | str,
    req_id: str,
    *,
    status: str,
    by: str,
    implementation_paths: Iterable[str],
    note: str = "",
    now: datetime | None = None,
    codd_dir: Path | None = None,
    expand_closure: bool = True,
) -> AcceptanceRecord:
    """Record a human verdict, bound to the current implementation content."""

    records = load_ledger(project_root, codd_dir)
    paths = (
        expand_to_closure(project_root, implementation_paths)
        if expand_closure
        else sorted(set(implementation_paths))
    )
    record = AcceptanceRecord(
        req_id=req_id,
        status=status,
        by=by,
        recorded_at=(now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        note=note,
        implementation=implementation_digest(project_root, paths),
    )
    records[req_id] = record
    write_ledger(project_root, records, codd_dir)
    return record


def ledger_summary(records: Mapping[str, AcceptanceRecord]) -> dict[str, Any]:
    """Counts for display."""

    return {
        "records": len(records),
        "passed": sum(1 for record in records.values() if record.status == "pass"),
    }
