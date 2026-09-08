"""Explicit, project-local source fingerprints for stale evidence checks.

The snapshot is a committed observation from an explicit refresh, not a cache.
Normal DAG builds and verification only read it; refreshing immediately before
every comparison would make source drift approve itself and hide stale evidence.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from codd.path_safety import PathEscapeError, require_project_path, resolve_project_path


SNAPSHOT_RELATIVE_PATH = Path(".codd/evidence_fingerprints.yaml")
SNAPSHOT_VERSION = 1
EVIDENCE_ATTRIBUTE_KEYS = (
    "runtime_evidence",
    "extraction_evidence",
    "extraction_diagnostics",
    "evidence",
)
_EXPLICIT_SOURCE_PATH_KEYS = ("source_path", "path", "file")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class EvidenceSnapshotError(ValueError):
    """The fingerprint snapshot is unsafe, malformed, or changed concurrently."""


@dataclass(frozen=True)
class EvidenceSnapshot:
    records: tuple[dict[str, str], ...]
    raw_bytes: bytes | None


def evidence_snapshot_path(project_root: str | Path) -> Path:
    """Return the confined snapshot path, rejecting escaping symlinks."""

    try:
        return require_project_path(
            project_root,
            SNAPSHOT_RELATIVE_PATH,
            context="evidence fingerprint snapshot",
        )
    except PathEscapeError as exc:
        raise EvidenceSnapshotError(str(exc)) from exc


def load_evidence_snapshot(project_root: str | Path) -> EvidenceSnapshot:
    """Load and strictly validate the snapshot without reading outside the root."""

    path = evidence_snapshot_path(project_root)
    if not path.exists():
        return EvidenceSnapshot(records=(), raw_bytes=None)
    if not path.is_file():
        raise EvidenceSnapshotError("evidence fingerprint snapshot is not a regular file")
    try:
        raw_bytes = path.read_bytes()
        payload = yaml.safe_load(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise EvidenceSnapshotError(
            f"cannot parse evidence fingerprint snapshot: {type(exc).__name__}: {exc}"
        ) from exc
    records = _validated_records(payload)
    return EvidenceSnapshot(records=tuple(records), raw_bytes=raw_bytes)


def collect_evidence_fingerprints(
    dag: Any,
    project_root: str | Path,
) -> list[dict[str, str]]:
    """Fingerprint current in-root files that supplied DAG evidence records."""

    root = Path(project_root).resolve()
    collected: dict[tuple[str, str], dict[str, str]] = {}
    for node in _iter_nodes(dag):
        node_id = str(getattr(node, "id", "") or "").strip()
        if not node_id:
            continue
        for record in _node_evidence_records(node):
            source_path, explicit = _record_source_path(node, record)
            if not source_path:
                continue
            resolved = resolve_project_path(root, source_path)
            if resolved is None:
                if explicit:
                    raise EvidenceSnapshotError(
                        f"evidence source resolves outside the project root: {source_path!r}"
                    )
                continue
            if not resolved.is_file():
                if explicit:
                    raise EvidenceSnapshotError(
                        f"evidence source is not a regular file: {source_path!r}"
                    )
                continue
            try:
                # Preserve the logical project path instead of serializing the
                # resolved symlink target. If an in-root source symlink is later
                # retargeted, the next check must follow that same logical path
                # and compare the new target rather than keep hashing old T0.
                relative_path = _logical_source_path(root, source_path)
                source_sha256 = _sha256_file(resolved)
            except (OSError, ValueError) as exc:
                raise EvidenceSnapshotError(
                    f"cannot fingerprint evidence source {source_path!r}: {exc}"
                ) from exc
            key = (node_id, relative_path)
            collected[key] = {
                "node_id": node_id,
                "source_path": relative_path,
                "source_sha256": source_sha256,
            }
    return [collected[key] for key in sorted(collected)]


def write_evidence_snapshot(
    project_root: str | Path,
    records: list[dict[str, str]],
    *,
    expected_bytes: bytes | None,
) -> Path:
    """Atomically replace the snapshot after confinement and stale-input checks."""

    if not records:
        raise EvidenceSnapshotError("refusing to replace the snapshot with 0 records")
    # Validate what we are about to persist through the same strict reader schema.
    normalized = _validated_records({"version": SNAPSHOT_VERSION, "records": records})
    root = Path(project_root).resolve()
    raw_path = root / SNAPSHOT_RELATIVE_PATH
    parent = evidence_snapshot_path(root).parent
    try:
        require_project_path(root, parent, context="evidence fingerprint snapshot directory")
    except PathEscapeError as exc:
        raise EvidenceSnapshotError(str(exc)) from exc
    parent.mkdir(parents=True, exist_ok=True)
    path = evidence_snapshot_path(root)
    if raw_path.is_symlink():
        raise EvidenceSnapshotError("evidence fingerprint snapshot must not be a symlink")
    _require_expected_bytes(path, expected_bytes)

    text = yaml.safe_dump(
        {"version": SNAPSHOT_VERSION, "records": normalized},
        sort_keys=False,
        allow_unicode=True,
    )
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        # Re-resolve both paths immediately before replacement. This rejects a
        # parent or destination symlink switched outside the root mid-command.
        try:
            current_parent = require_project_path(
                root, raw_path.parent, context="evidence fingerprint snapshot directory"
            )
            current_path = require_project_path(
                root, raw_path, context="evidence fingerprint snapshot"
            )
        except PathEscapeError as exc:
            raise EvidenceSnapshotError(str(exc)) from exc
        if current_parent != parent or current_path != path or raw_path.is_symlink():
            raise EvidenceSnapshotError("evidence fingerprint snapshot path changed during refresh")
        _require_expected_bytes(path, expected_bytes)
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise
    return path


def _validated_records(payload: Any) -> list[dict[str, str]]:
    if not isinstance(payload, Mapping) or payload.get("version") != SNAPSHOT_VERSION:
        raise EvidenceSnapshotError(
            f"evidence fingerprint snapshot must have version: {SNAPSHOT_VERSION}"
        )
    raw_records = payload.get("records")
    if not isinstance(raw_records, list):
        raise EvidenceSnapshotError("evidence fingerprint snapshot records must be a list")
    records: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(raw_records):
        if not isinstance(item, Mapping):
            raise EvidenceSnapshotError(f"evidence fingerprint record {index} must be a mapping")
        node_id = item.get("node_id")
        source_path = item.get("source_path")
        source_sha256 = item.get("source_sha256")
        if not isinstance(node_id, str) or not node_id.strip():
            raise EvidenceSnapshotError(f"evidence fingerprint record {index} has invalid node_id")
        if not isinstance(source_path, str) or not source_path.strip():
            raise EvidenceSnapshotError(f"evidence fingerprint record {index} has invalid source_path")
        if not isinstance(source_sha256, str) or not _SHA256_RE.fullmatch(source_sha256):
            raise EvidenceSnapshotError(f"evidence fingerprint record {index} has invalid source_sha256")
        key = (node_id, source_path)
        if key in seen:
            raise EvidenceSnapshotError(f"duplicate evidence fingerprint record: {node_id} {source_path}")
        seen.add(key)
        records.append(
            {
                "node_id": node_id,
                "source_path": source_path,
                "source_sha256": source_sha256,
            }
        )
    return sorted(records, key=lambda item: (item["node_id"], item["source_path"]))


def _iter_nodes(dag: Any) -> Iterable[Any]:
    nodes = getattr(dag, "nodes", None)
    if isinstance(nodes, Mapping):
        return list(nodes.values())
    if isinstance(nodes, Iterable):
        return list(nodes)
    return []


def _node_evidence_records(node: Any) -> list[Mapping[str, Any]]:
    attributes = getattr(node, "attributes", None)
    if not isinstance(attributes, Mapping):
        return []
    records: list[Mapping[str, Any]] = []
    for key in EVIDENCE_ATTRIBUTE_KEYS:
        payload = attributes.get(key)
        if isinstance(payload, Mapping):
            records.append(payload)
        elif isinstance(payload, (list, tuple)):
            records.extend(item for item in payload if isinstance(item, Mapping))
    return records


def _record_source_path(node: Any, record: Mapping[str, Any]) -> tuple[str | None, bool]:
    for key in _EXPLICIT_SOURCE_PATH_KEYS:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value, True
    node_path = getattr(node, "path", None)
    if isinstance(node_path, str) and node_path.strip():
        return node_path, False
    return None, False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _logical_source_path(root: Path, source_path: str) -> str:
    candidate = Path(source_path)
    lexical = Path(
        os.path.abspath(candidate if candidate.is_absolute() else root / candidate)
    )
    try:
        return lexical.relative_to(root).as_posix()
    except ValueError as exc:
        raise EvidenceSnapshotError(
            f"evidence source has no project-local logical path: {source_path!r}"
        ) from exc


def _require_expected_bytes(path: Path, expected_bytes: bytes | None) -> None:
    try:
        current = path.read_bytes() if path.exists() else None
    except OSError as exc:
        raise EvidenceSnapshotError(f"cannot read evidence fingerprint snapshot: {exc}") from exc
    if current != expected_bytes:
        raise EvidenceSnapshotError(
            "evidence fingerprint snapshot changed during refresh; refusing stale overwrite"
        )


__all__ = [
    "EVIDENCE_ATTRIBUTE_KEYS",
    "EvidenceSnapshot",
    "EvidenceSnapshotError",
    "SNAPSHOT_RELATIVE_PATH",
    "collect_evidence_fingerprints",
    "evidence_snapshot_path",
    "load_evidence_snapshot",
    "write_evidence_snapshot",
]
