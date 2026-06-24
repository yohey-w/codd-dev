"""DAG check: stale_evidence — fingerprint-only evidence freshness.

Some evidence payloads attached to DAG nodes (for example extraction caches or
runtime-evidence records) can carry a *content fingerprint* of the source file
they were derived from: a ``source_path`` plus a recorded ``source_sha256``. If
the source file is later edited, that recorded evidence is **stale** — it
describes a version of the file that no longer exists, a quiet false-green source
("the cache says capability X is verified" while the file it was read from has
since changed).

This check surfaces that drift, and *only* that drift:

* **diagnostic-only, amber only — never red, never blocks deploy.** Stale
  evidence is an advisory signal, not a logical contradiction derivable from the
  contracts; gating it red would be a false red.
* **fingerprint-driven, never wall-clock / mtime.** A file's modification time
  is environment-dependent (checkout order, clone, ``touch``); using it would
  produce false reds across machines. The check acts **only** when a recorded
  ``source_sha256`` is present and recomputes the hash to compare. ``generated_at``
  age alone is never a warning — see :func:`_evidence_records`.
* **missing source is not stale.** If ``source_path`` no longer exists we cannot
  recompute a hash, so we emit ``source_missing`` (amber), distinct from
  ``stale_evidence``, and still never red.
* **no fingerprint ⇒ silent.** Evidence without a recorded hash is *not*
  warned about (no ``freshness_not_provable`` spam); it is simply not checkable.
* **0 checkable evidence ⇒ skip** (``checked_count == 0``, ``skipped=True``).
  This is the current real-world state: ``runtime_evidence`` does not yet record
  ``source_sha256``, so on real projects this check is a dormant forward-guard
  that activates automatically once a writer starts recording fingerprints.
* **generality.** The core carries no project / framework / language literal; it
  inspects whatever node attributes look like a fingerprinted evidence payload.

The result dataclass mirrors ``extraction_diagnostics.ExtractionDiagnosticsResult``
(check_name / severity / status / passed / block_deploy / skipped /
checked_count / warnings) so downstream materiality / formatting treats it
uniformly. Registration is by import: importing this module runs
``@register_dag_check("stale_evidence")`` — the runner module list registers it,
this module never touches ``runner.py``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from codd.dag.checks import DagCheck, register_dag_check


# Node attribute keys that may hold a list (or single mapping) of evidence
# payloads. These are *candidate carriers*: a payload only becomes checkable when
# it actually contains both a source path and a recorded sha256. Adding a new
# carrier key here is the only change needed to extend coverage to a new payload.
_EVIDENCE_ATTRIBUTE_KEYS = (
    "runtime_evidence",
    "extraction_evidence",
    "extraction_diagnostics",
    "evidence",
)

# Accepted key spellings for the source path and the recorded fingerprint, so the
# check matches whatever a future writer happens to emit without a schema change.
_SOURCE_PATH_KEYS = ("source_path", "path", "file", "source")
_SOURCE_HASH_KEYS = ("source_sha256", "sha256", "source_hash", "content_sha256")


@dataclass
class StaleEvidenceResult:
    check_name: str = "stale_evidence"
    severity: str = "amber"
    status: str = "pass"
    message: str = ""
    block_deploy: bool = False
    passed: bool = True
    skipped: bool = False
    # evidence records that carried a recorded fingerprint (= were checkable);
    # 0 with status=skip means "no fingerprinted evidence present" (forward-guard)
    checked_count: int = 0
    warnings: list[dict[str, Any]] = field(default_factory=list)


@register_dag_check("stale_evidence")
class StaleEvidenceCheck(DagCheck):
    """Amber-only: a recorded source fingerprint no longer matches the file."""

    check_name = "stale_evidence"
    severity = "amber"
    block_deploy = False

    def run(
        self,
        dag: Any | None = None,
        project_root: str | Path | None = None,
        settings: dict[str, Any] | None = None,
        codd_config: dict[str, Any] | None = None,
    ) -> StaleEvidenceResult:
        del codd_config  # config-free: this check reads node evidence only
        target_dag = dag if dag is not None else self.dag
        if project_root is not None:
            self.project_root = Path(project_root)
        if settings is not None:
            self.settings = settings
        root = self.project_root or Path.cwd()

        if target_dag is None:
            return StaleEvidenceResult(
                status="skip",
                skipped=True,
                passed=True,
                block_deploy=False,
                message="stale_evidence SKIP (no DAG available)",
            )

        checked_count = 0
        warnings: list[dict[str, Any]] = []

        for node in _iter_nodes(target_dag):
            for record in _evidence_records(node):
                source_path = _first_str(record, _SOURCE_PATH_KEYS)
                recorded_hash = _first_str(record, _SOURCE_HASH_KEYS)
                # Only fingerprinted evidence is checkable. No hash -> silent
                # (never freshness_not_provable). No path -> nothing to hash.
                if not recorded_hash or not source_path:
                    continue

                checked_count += 1
                resolved = _resolve_source(root, source_path)
                if resolved is None or not resolved.is_file():
                    warnings.append(
                        _source_missing_warning(node, source_path, recorded_hash)
                    )
                    continue

                current_hash = _sha256_of(resolved)
                if current_hash is None:
                    # Unreadable (binary / permission) — treat like missing, amber,
                    # never red, never a false stale claim.
                    warnings.append(
                        _source_missing_warning(node, source_path, recorded_hash)
                    )
                    continue

                if current_hash != recorded_hash:
                    warnings.append(
                        _stale_warning(
                            node, source_path, recorded_hash, current_hash
                        )
                    )

        if checked_count == 0:
            return StaleEvidenceResult(
                status="skip",
                skipped=True,
                passed=True,
                block_deploy=False,
                checked_count=0,
                message=(
                    "stale_evidence SKIP "
                    "(no evidence payload records a source_sha256 fingerprint)"
                ),
            )

        if warnings:
            stale = sum(1 for w in warnings if w.get("type") == "stale_evidence")
            missing = sum(1 for w in warnings if w.get("type") == "source_missing")
            return StaleEvidenceResult(
                status="warn",
                severity="amber",
                passed=True,
                block_deploy=False,
                checked_count=checked_count,
                warnings=warnings,
                message=(
                    f"stale_evidence found {stale} stale and {missing} missing-source "
                    f"evidence record(s) ({checked_count} fingerprinted record(s) checked)"
                ),
            )

        return StaleEvidenceResult(
            status="pass",
            severity="amber",
            passed=True,
            block_deploy=False,
            checked_count=checked_count,
            message=(
                f"stale_evidence PASS "
                f"({checked_count} fingerprinted evidence record(s) match their source)"
            ),
        )


def _iter_nodes(dag: Any) -> Iterable[Any]:
    nodes = getattr(dag, "nodes", None)
    if isinstance(nodes, Mapping):
        return list(nodes.values())
    if isinstance(nodes, Iterable):
        return list(nodes)
    return []


def _evidence_records(node: Any) -> list[Mapping[str, Any]]:
    """Yield candidate evidence mappings carried by a node's attributes."""
    attributes = getattr(node, "attributes", None)
    if not isinstance(attributes, Mapping):
        return []
    records: list[Mapping[str, Any]] = []
    for key in _EVIDENCE_ATTRIBUTE_KEYS:
        payload = attributes.get(key)
        if isinstance(payload, Mapping):
            records.append(payload)
        elif isinstance(payload, (list, tuple)):
            records.extend(item for item in payload if isinstance(item, Mapping))
    return records


def _first_str(record: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _resolve_source(root: Path, source_path: str) -> Path | None:
    candidate = Path(source_path)
    try:
        resolved = (
            candidate.resolve()
            if candidate.is_absolute()
            else (root / candidate).resolve()
        )
    except (TypeError, ValueError, OSError):  # pragma: no cover - defensive
        return None
    # Root-jail: never hash a file outside the project root. An absolute source_path
    # from untrusted DAG content must not leak the existence/hash of arbitrary files.
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return None
    return resolved


def _sha256_of(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:  # pragma: no cover - defensive (permission / race)
        return None


def _stale_warning(
    node: Any, source_path: str, recorded_hash: str, current_hash: str
) -> dict[str, Any]:
    return {
        "type": "stale_evidence",
        "severity": "amber",
        "node": getattr(node, "id", None),
        "source_path": source_path,
        "recorded_sha256": recorded_hash,
        "current_sha256": current_hash,
        "remediation": (
            f"Evidence for '{source_path}' was recorded against a different "
            "version of the file (sha256 mismatch). Re-extract / refresh the "
            "evidence so its fingerprint matches the current source."
        ),
    }


def _source_missing_warning(
    node: Any, source_path: str, recorded_hash: str
) -> dict[str, Any]:
    return {
        "type": "source_missing",
        "severity": "amber",
        "node": getattr(node, "id", None),
        "source_path": source_path,
        "recorded_sha256": recorded_hash,
        "remediation": (
            f"Evidence references source '{source_path}', which no longer exists "
            "(cannot recompute fingerprint). Re-point or remove the evidence record."
        ),
    }


__all__ = ["StaleEvidenceCheck", "StaleEvidenceResult"]
