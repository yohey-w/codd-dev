"""Runtime execution ledger — invariant (a), the runtime stage actually RAN.

``runtime_smoke.enabled: true`` is a **declaration**. It says the stage is
configured; it says nothing about whether the stage was ever executed. Reading
that boolean as evidence made the cheapest way to clear the acceptance gate
"write one line of YAML" instead of "run the system" — measured on a real
project, flipping it moved ``runtime_evidence_not_executable`` 56 -> 0 and
``unbound_acceptance`` 57 -> 19 without a single runtime check being executed.

This ledger is the evidence the declaration is not. The runtime smoke runner
writes it when the stage is enabled AND at least one check actually ran; the
acceptance-evidence gate reads it and asks three questions a boolean cannot
answer: did a run happen, did it target what the project targets NOW
(``config_digest``), and is it recent enough to speak for the current state
(``recorded_at``).

Storage mirrors the ``acceptance_ledger.json`` precedent: a JSON document in the
project's codd directory, pretty-printed because it is reviewed in diffs. Unlike
the manual ledger it is written automatically, and that difference is the point
rather than an inconsistency — a manual record is a HUMAN VERDICT, which nothing
but a human may refresh, while this one records THE FACT THAT A MACHINE RAN,
which only the machine that ran can honestly write.

A missing or malformed ledger reads as no evidence, never as evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

LEDGER_FILENAME = "runtime_ledger.json"
LEDGER_VERSION = 1

# The configuration sections that decide WHAT the runtime stage exercises. A
# change to either means an earlier run no longer covers what is declared today.
DIGESTED_SECTIONS = ("runtime_smoke", "runtime")

_NORMALISE_RE = re.compile(r"[\s_\-./]+")


def _normalise(value: str) -> str:
    """Fold the spellings one name is written in across a config and a document.

    ``print_sheet`` / ``Print Sheet`` / ``print-sheet`` name the same check. This
    is orthography, not domain knowledge: no project, framework or language
    vocabulary is involved.
    """

    return _NORMALISE_RE.sub("", str(value)).strip().lower()


@dataclass(frozen=True)
class RuntimeCheckRecord:
    """One check as the run left it."""

    name: str
    category: str = ""
    passed: bool = False
    skipped: bool = False

    @property
    def executed(self) -> bool:
        return not self.skipped

    def answers_to(self, target: str) -> bool:
        wanted = _normalise(target)
        if not wanted:
            return False
        return wanted in {_normalise(self.name), _normalise(self.category)}


@dataclass(frozen=True)
class RuntimeExecutionRecord:
    """A recorded execution of the runtime stage."""

    recorded_at: datetime
    passed: bool
    config_digest: str
    checks: tuple[RuntimeCheckRecord, ...] = ()

    @property
    def executed_checks(self) -> tuple[RuntimeCheckRecord, ...]:
        return tuple(check for check in self.checks if check.executed)

    def covers(self, target: str) -> bool:
        """Whether a NON-SKIPPED check of this run answers to *target*.

        A failed check still counts as executed: Step 8 already reported that
        failure in red, and reporting it again here would count one defect twice.
        """

        return any(check.answers_to(target) for check in self.executed_checks)

    def age_hours(self, now: datetime | None = None) -> float:
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return max(0.0, (moment - self.recorded_at).total_seconds() / 3600.0)


def runtime_config_digest(config: Mapping[str, Any] | None) -> str:
    """Hash of the configuration that decides what the runtime stage exercises.

    Canonical JSON (sorted keys) of the project's ``runtime_smoke`` and
    ``runtime`` sections, so the same configuration hashes the same on the
    producing and the consuming side, and any change to the targets expires the
    earlier run.
    """

    payload = {
        section: (config.get(section) if isinstance(config, Mapping) else None)
        for section in DIGESTED_SECTIONS
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def ledger_path(project_root: Path | str, codd_dir: Path | None = None) -> Path:
    """Location of the ledger: ``<codd-dir>/runtime_ledger.json``."""

    root = Path(project_root).resolve()
    if codd_dir is not None:
        return Path(codd_dir) / LEDGER_FILENAME
    from codd.config import find_codd_dir

    try:
        resolved = find_codd_dir(root)
    except Exception:  # pragma: no cover - config-less project
        resolved = None
    return (Path(resolved) if resolved else root / "codd") / LEDGER_FILENAME


def _parse_recorded_at(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load_runtime_ledger(
    project_root: Path | str,
    codd_dir: Path | None = None,
) -> RuntimeExecutionRecord | None:
    """Read the ledger. Missing, malformed or undated reads as NO EVIDENCE."""

    path = ledger_path(project_root, codd_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    recorded_at = _parse_recorded_at(payload.get("recorded_at"))
    if recorded_at is None:
        return None

    raw_checks = payload.get("checks")
    checks: list[RuntimeCheckRecord] = []
    for raw in raw_checks if isinstance(raw_checks, list) else []:
        if not isinstance(raw, Mapping):
            continue
        checks.append(
            RuntimeCheckRecord(
                name=str(raw.get("name", "")),
                category=str(raw.get("category", "")),
                passed=bool(raw.get("passed", False)),
                skipped=bool(raw.get("skipped", False)),
            )
        )
    return RuntimeExecutionRecord(
        recorded_at=recorded_at,
        passed=bool(payload.get("passed", False)),
        config_digest=str(payload.get("config_digest", "")),
        checks=tuple(checks),
    )


def write_runtime_ledger(
    project_root: Path | str,
    record: RuntimeExecutionRecord,
    codd_dir: Path | None = None,
) -> Path | None:
    """Persist the ledger. A filesystem that refuses the write never fails the run."""

    path = ledger_path(project_root, codd_dir)
    payload = {
        "version": LEDGER_VERSION,
        "recorded_at": record.recorded_at.astimezone(timezone.utc).isoformat(),
        "passed": record.passed,
        "config_digest": record.config_digest,
        "checks": [
            {
                "name": check.name,
                "category": check.category,
                "passed": check.passed,
                "skipped": check.skipped,
            }
            for check in record.checks
        ],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        return None
    return path


def record_runtime_execution(
    project_root: Path | str,
    config: Mapping[str, Any] | None,
    checks: Iterable[Any],
    codd_dir: Path | None = None,
    now: datetime | None = None,
) -> Path | None:
    """Write the ledger for a run that EXECUTED something; otherwise write nothing.

    *checks* are the runner's own result objects (anything carrying ``name`` /
    ``category`` / ``passed`` / ``skipped``). A run in which every check was
    skipped records nothing: a record of nothing is not a record, and writing one
    would re-create the hole this ledger closes.
    """

    collected = tuple(
        RuntimeCheckRecord(
            name=str(getattr(check, "name", "")),
            category=str(getattr(check, "category", "") or ""),
            passed=bool(getattr(check, "passed", False)),
            skipped=bool(getattr(check, "skipped", False)),
        )
        for check in checks
    )
    if not any(check.executed for check in collected):
        return None
    record = RuntimeExecutionRecord(
        recorded_at=(now or datetime.now(timezone.utc)).astimezone(timezone.utc),
        passed=all(check.passed or check.skipped for check in collected),
        config_digest=runtime_config_digest(config),
        checks=collected,
    )
    return write_runtime_ledger(project_root, record, codd_dir)
