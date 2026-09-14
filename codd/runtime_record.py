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
import os
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

# ...minus the keys that decide only where OUTPUT is written. Moving a report
# file does not change what was exercised, and expiring a run over it would train
# owners to ignore the finding. `report.fail_fast` is NOT among them: it decides
# whether the checks after the first failure run at all, which is the plan.
UNDIGESTED_REPORT_KEYS = ("log_to_file", "file_path")

# Fold only the separators one NAME is spelled with. `.` and `/` are left alone:
# collapsing them would make `a.b`, `a/b` and `ab` one name.
_NORMALISE_RE = re.compile(r"[\s_\-]+")


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

    @property
    def discharged(self) -> bool:
        """Ran AND passed — the only state that proves anything about a target."""

        return self.executed and self.passed

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
    # The effective target the run went against (``--runtime-base-url`` may have
    # pointed it somewhere other than the configured dev server). Empty when the
    # run recorded none.
    target_url: str = ""

    @property
    def executed_checks(self) -> tuple[RuntimeCheckRecord, ...]:
        return tuple(check for check in self.checks if check.executed)

    def covers(self, target: str) -> bool:
        """Whether a check of this run answering to *target* actually PASSED.

        A failed check is executed but proves nothing, and the ledger is read by
        every LATER verification — where "it ran, and it was broken" would
        otherwise pass for proof. A failure elsewhere in the same run does not
        sink this target: what is asked is whether the named check passed.
        """

        return any(check.discharged and check.answers_to(target) for check in self.checks)

    def age_hours(self, now: datetime | None = None) -> float:
        """Hours since the run. NEGATIVE for a record dated in the future.

        Not clamped: a future timestamp (a skewed clock, a hand-edited file) that
        read as "zero hours old" would be permanently fresh.
        """

        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return (moment - self.recorded_at).total_seconds() / 3600.0


def _raw_project_config(project_root: Path | str) -> Mapping[str, Any]:
    """The project's OWN codd.yaml, unmerged.

    Deliberately not the defaults-merged view: merging makes the hash depend on
    the CoDD version, so upgrading the tool would expire every recorded run
    across every project at once.
    """

    import yaml

    from codd.config import find_codd_dir

    root = Path(project_root).resolve()
    try:
        codd_dir = find_codd_dir(root)
    except Exception:  # pragma: no cover - config-less project
        codd_dir = None
    path = (Path(codd_dir) if codd_dir else root / "codd") / "codd.yaml"
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}
    return loaded if isinstance(loaded, Mapping) else {}


def runtime_config_digest(project_root: Path | str) -> str:
    """Hash of the configuration that decides what the runtime stage exercises.

    Canonical JSON (sorted keys) of the project's own ``runtime_smoke`` (minus
    ``report``) and ``runtime`` sections, so the same configuration hashes the
    same on the producing and the consuming side, and any change to what the
    stage targets expires the earlier run.
    """

    config = _raw_project_config(project_root)
    payload: dict[str, Any] = {}
    for section in DIGESTED_SECTIONS:
        value = config.get(section)
        if section == "runtime_smoke" and isinstance(value, Mapping):
            report = value.get("report")
            if isinstance(report, Mapping):
                kept = {
                    key: item
                    for key, item in report.items()
                    if key not in UNDIGESTED_REPORT_KEYS
                }
                value = dict(value)
                # An empty remainder is dropped, not stored: `report: {file_path:
                # ...}` must hash the same as no `report:` at all, or declaring
                # where output goes would expire the run after all.
                if kept:
                    value["report"] = kept
                else:
                    value.pop("report", None)
        payload[section] = value
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


def _strict_bool(raw: Any) -> bool:
    """Only a real JSON ``true`` is true.

    ``bool("false")`` is ``True``, so coercing a hand-edited string would read the
    word "false" as a pass.
    """

    return raw is True


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
    """Read the ledger. Anything that is not a well-formed record of an actual
    run reads as NO EVIDENCE: missing, malformed, undated, written by a version
    of the format this CoDD does not know, or listing no executed check at all.

    That last one matters because the file is hand-editable: ``{"checks": []}``
    with today's date would otherwise certify every inferred obligation in the
    project. This is not tamper-proofing — a JSON file never is — it is refusing
    to read "nothing ran" as "something ran".
    """

    path = ledger_path(project_root, codd_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    if payload.get("version") != LEDGER_VERSION:
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
                passed=_strict_bool(raw.get("passed")),
                skipped=_strict_bool(raw.get("skipped")),
            )
        )
    if not any(check.executed for check in checks):
        return None
    return RuntimeExecutionRecord(
        recorded_at=recorded_at,
        passed=_strict_bool(payload.get("passed")),
        config_digest=str(payload.get("config_digest", "")),
        checks=tuple(checks),
        target_url=str(payload.get("target_url", "")),
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
        "target_url": record.target_url,
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
    body = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    # Written through a sibling temp file and renamed: two runs racing produce
    # one whole record or the other, never a half-read file that would then be
    # discarded as malformed.
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(body, encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        return None
    return path


NOTHING_EXECUTED = "nothing_executed"
WRITE_FAILED = "write_failed"
STALE_RECORD_LEFT = "stale_record_left"


def record_runtime_execution(
    project_root: Path | str,
    checks: Iterable[Any],
    target_url: str = "",
    codd_dir: Path | None = None,
    now: datetime | None = None,
) -> tuple[Path | None, str]:
    """Write the ledger for a run that EXECUTED something; otherwise write nothing.

    *checks* are the runner's own result objects (anything carrying ``name`` /
    ``category`` / ``passed`` / ``skipped``). A run in which every check was
    skipped records nothing: a record of nothing is not a record, and writing one
    would re-create the hole this ledger closes.

    Returns ``(path, status)`` — ``status`` is empty on success, and otherwise
    says which of the two silences happened, so the caller can stay quiet about
    a run that had nothing to record and say so loudly about evidence it lost.
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
        return None, NOTHING_EXECUTED
    record = RuntimeExecutionRecord(
        recorded_at=(now or datetime.now(timezone.utc)).astimezone(timezone.utc),
        passed=all(check.passed or check.skipped for check in collected),
        config_digest=runtime_config_digest(project_root),
        checks=collected,
        # Stripped on both sides: the gate compares against the configured URL,
        # and YAML keeps whatever whitespace the author typed.
        target_url=(target_url or "").strip(),
    )
    written = write_runtime_ledger(project_root, record, codd_dir)
    if written is not None:
        return written, ""
    # The write failed. An OLDER record must not survive this run: yesterday's
    # green ledger plus today's unrecordable run reads as evidence for a system
    # that has since been proved otherwise. Remove it, and if even that is
    # refused, say the stale record is still standing.
    stale = ledger_path(project_root, codd_dir)
    try:
        stale.unlink()
    except FileNotFoundError:
        return None, WRITE_FAILED
    except OSError:
        return None, STALE_RECORD_LEFT
    return None, WRITE_FAILED
