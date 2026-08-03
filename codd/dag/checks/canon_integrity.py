"""DAG check: canon_integrity — canon documents still match what was accepted.

Scope of the claim (do not oversell this check)
-----------------------------------------------
It detects **unaware change**, not unauthorized change. There is no permission
model here: anyone who edits a canon document and runs ``codd canon accept`` gets
a green check, deliberately and by design. What the check buys is that a change
nobody decided to make — a formatter, a format-on-save, a bulk ``sed``, an agent
writing to the wrong path, a bad merge — can no longer pass silently, because
nobody runs ``accept`` for a change they do not know happened.

Against a *deliberate* bad edit the only defences are the recorded work reference
(written into the committed ledger next to each digest) and code review. Neither
is enforced by this check.

The failure this catches has no other trigger. A requirement unit is a Markdown
table ROW, so a formatter that merely re-aligns table pipes changes no meaning:
review passes, every other CoDD check stays green, and byte-identity with the
human-approved original is nonetheless destroyed. Observed: ``npx prettier
--write .`` rewrote 440 lines of a requirements table.

``codd init`` writing a ``.prettierignore`` (v3.38.0) keeps ONE named tool away
from the canon, only in a NEW project. This check is the tool-independent layer:
it compares the current bytes of every canon document against the digests a human
accepted into ``<codd-dir>/canon.lock``, whatever did the writing.

Severity, deliberately split
----------------------------
* **ALTERED or MISSING → red** (``canon.severity``, default ``red``). An approved
  document changed or vanished outside the accept path. This is the failure mode.
* **UNTRACKED (in scope, not in the ledger) → amber.** Adding a document is
  already visible in ``git status``; hard-failing it would make every greenfield
  run red the moment the first requirements file is written, and a gate that
  fires on normal work is a gate people learn to bypass. It is still surfaced —
  it is also how a reference copy dropped into ``docs/requirements/`` (the
  double-counting trap) shows up.
* **No ledger at all → amber with guidance.** An existing project must not turn
  red merely by upgrading CoDD. SKIP is reserved for "dormant / unconfigured";
  an un-adopted safety net is neither, so it renders WARN and says how to adopt.
* **No canon documents in scope AND no ledger → skip** (``severity="info"``).
  Nothing to protect yet; that is genuinely dormant.

The check never repairs and never refreshes the ledger: an auto-updating ledger
detects nothing. Updating is ``codd canon accept``, by a human, after a diff.

Generality: no project, framework, language or domain literal. Scope comes from
the project's own requirement-document declaration plus ``scan.exclude``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codd.canon import CANON_LOCK_NAME, CanonStatus, canon_settings, evaluate_canon
from codd.dag.checks import DagCheck, register_dag_check


ACCEPT_HINT = (
    "Review the diff. If changing this document WAS the work you were asked to "
    "do, run `codd canon accept --for <work reference>` to record the new "
    "digests; if it was not, restore the file (e.g. `git checkout -- <path>`). "
    "Accepting merely to turn this check green is the one use that defeats the "
    "mechanism. Formatters are the usual cause: prettier, dprint, "
    "`markdownlint --fix`, an editor's format-on-save."
)


@dataclass
class CanonIntegrityResult:
    check_name: str = "canon_integrity"
    severity: str = "red"
    status: str = "pass"
    message: str = ""
    block_deploy: bool = True
    passed: bool = True
    skipped: bool = False
    #: Documents compared against a recorded digest. 0 with a pass status is a
    #: vacuous pass and the materiality overlay surfaces it as such.
    checked_count: int = 0
    #: Red findings: altered / removed canon documents.
    violations: list[dict[str, Any]] = field(default_factory=list)
    #: Amber findings: untracked documents, or an absent ledger.
    warnings: list[dict[str, Any]] = field(default_factory=list)


@register_dag_check("canon_integrity")
class CanonIntegrityCheck(DagCheck):
    """Fail when a canon document's bytes no longer match the accepted ledger."""

    check_name = "canon_integrity"

    def run(
        self,
        dag: Any | None = None,
        project_root: str | Path | None = None,
        settings: dict[str, Any] | None = None,
        codd_config: dict[str, Any] | None = None,
    ) -> CanonIntegrityResult:
        del dag  # filesystem/ledger check: the DAG carries no canon bytes
        if project_root is not None:
            self.project_root = Path(project_root)
        if settings is not None:
            self.settings = settings
        root = self.project_root or Path.cwd()

        # The dag `settings` are the merged dag section; canon lives at the top
        # level of codd.yaml, so prefer the full config the runner passes in.
        config = codd_config if codd_config is not None else self.settings
        canon_config = canon_settings(config)

        status = evaluate_canon(root, config)

        if not status.enabled:
            return CanonIntegrityResult(
                severity="info",
                status="skip",
                skipped=True,
                passed=True,
                block_deploy=False,
                checked_count=0,
                message="canon_integrity SKIP (canon.enabled: false in codd.yaml)",
            )

        if status.ledger_unreadable:
            # A ledger we cannot interpret verified nothing — and it is a state
            # a human must resolve, not something to shrug off as a pass.
            return CanonIntegrityResult(
                severity="red",
                status="fail",
                passed=False,
                block_deploy=True,
                checked_count=0,
                violations=[
                    {
                        "type": "ledger_unreadable",
                        "path": status.ledger_path or CANON_LOCK_NAME,
                        "detail": status.ledger_unreadable,
                    }
                ],
                message=(
                    f"canon ledger {status.ledger_path or CANON_LOCK_NAME} could not be "
                    f"read: {status.ledger_unreadable}. Restore it from version control "
                    "or re-create it with `codd canon accept --for <work reference>`."
                ),
            )

        if not status.ledger_present:
            return _no_ledger_result(status)

        if status.has_drift:
            return _drift_result(status, severity=canon_config.severity)

        if status.untracked:
            return _untracked_result(status)

        if status.checked_count == 0:
            # Ledger present but empty AND nothing in scope: nothing was verified.
            # checked_count=0 makes the materiality overlay flag the vacuous pass;
            # say so in the message rather than showing a confident green.
            return CanonIntegrityResult(
                severity="info",
                status="skip",
                skipped=True,
                passed=True,
                block_deploy=False,
                checked_count=0,
                message=(
                    "canon_integrity SKIP (ledger present but no canon documents in "
                    "scope — nothing was verified)"
                ),
            )

        return CanonIntegrityResult(
            severity="red",
            status="pass",
            passed=True,
            block_deploy=True,
            checked_count=status.checked_count,
            message=(
                f"canon intact: {status.checked_count} document(s) match "
                f"{status.ledger_path or CANON_LOCK_NAME}"
            ),
        )


def _no_ledger_result(status: CanonStatus) -> CanonIntegrityResult:
    in_scope = len(status.untracked)
    if in_scope == 0:
        return CanonIntegrityResult(
            severity="info",
            status="skip",
            skipped=True,
            passed=True,
            block_deploy=False,
            checked_count=0,
            message=(
                "canon_integrity SKIP (no canon ledger and no canon documents in scope)"
            ),
        )
    # Amber, not red: an existing project must not break merely by upgrading
    # CoDD. Amber-with-findings renders WARN and prints "deploy allowed".
    return CanonIntegrityResult(
        severity="amber",
        status="warn",
        passed=True,
        block_deploy=False,
        checked_count=0,
        warnings=[
            {
                "type": "ledger_absent",
                "path": status.ledger_path or CANON_LOCK_NAME,
                "documents_in_scope": in_scope,
            }
        ],
        message=(
            f"no canon ledger yet — {in_scope} canon document(s) are unprotected. "
            f"Run `codd canon accept --for <work reference>` to record their digests into "
            f"{status.ledger_path or CANON_LOCK_NAME} and commit it; a later "
            "formatter or stray edit then fails this check instead of passing "
            "silently."
        ),
    )


def _drift_result(status: CanonStatus, severity: str) -> CanonIntegrityResult:
    violations: list[dict[str, Any]] = []
    for rel in status.modified:
        violations.append({"type": "canon_modified", "path": rel})
    for rel in status.missing:
        violations.append({"type": "canon_missing", "path": rel})
    warnings = [{"type": "canon_untracked", "path": rel} for rel in status.untracked]

    blocking = severity == "red"
    parts: list[str] = []
    if status.modified:
        parts.append(f"{len(status.modified)} altered")
    if status.missing:
        parts.append(f"{len(status.missing)} missing")
    summary = " and ".join(parts)
    return CanonIntegrityResult(
        severity=severity,
        status="fail" if blocking else "warn",
        passed=not blocking,
        block_deploy=blocking,
        checked_count=status.checked_count,
        violations=violations,
        warnings=warnings,
        message=(
            f"canon changed outside the accept path: {summary} document(s) "
            f"vs {status.ledger_path or CANON_LOCK_NAME}. {ACCEPT_HINT}"
        ),
    )


def _untracked_result(status: CanonStatus) -> CanonIntegrityResult:
    return CanonIntegrityResult(
        severity="amber",
        status="warn",
        passed=True,
        block_deploy=False,
        checked_count=status.checked_count,
        warnings=[{"type": "canon_untracked", "path": rel} for rel in status.untracked],
        message=(
            f"{len(status.untracked)} canon document(s) are not in "
            f"{status.ledger_path or CANON_LOCK_NAME} "
            f"({status.checked_count} recorded document(s) intact). Run "
            "`codd canon accept --for <work reference>` to record them — until "
            "then a change to those "
            "files is not detectable. If a file is an unintended copy (a "
            "reference copy under a requirements directory double-counts every "
            "unit), move it out or add it to `scan.exclude`."
        ),
    )
