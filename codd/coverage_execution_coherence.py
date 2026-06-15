"""Coverage-execution coherence — the anti-false-green axis that binds STATIC VB
coverage to ACTUAL test EXECUTION (design: /tmp/gpt_vscope_result.txt, GPT-5.5
Pro consult 2026-06-15).

THE FALSE-GREEN THIS CLOSES (greenfield codex14)
================================================
verify resolved the test command by the SUT's ``package.json`` script priority
(``detect_test_command``: ``test:unit`` > ``test`` > ``test:e2e``), ran
``test:unit`` (39 unit tests), exited 0, and declared "verification passed" —
while 28 declared verifiable behaviors (VBs) were covered ONLY by
``tests/e2e/*.e2e.test.ts`` files that ``test:unit`` NEVER executes. The static
VB coverage gate (:mod:`codd.verifiable_behavior_audit`) saw those ``codd: covers
vb=`` markers and called the behaviors "covered"; verify never ran them. COVERAGE
and EXECUTION were two SEPARATE proof systems, so "covered but UNEXECUTED" passed
green.

THE INVARIANT (design section B/E)
==================================
For every UNBLOCKED verifiable behavior ``v``::

    ∃ test t:  marker covers vb=v  ∧  authentic(t)
               ∧  t ∈ verify-campaign selection
               ∧  t actually executed  ∧  t passed

``unblocked_VB − verified_VB ≠ ∅  →  HARD FAIL``. Static coverage ALONE never
makes a VB pass — its covering test must have RUN and PASSED in the harness-owned
verify campaign.

THE THREE PIECES (all per-profile; gate logic is language-agnostic)
==================================================================
1. **Verify campaign** — :class:`~codd.project_types.VerifyCampaignSpec` on the
   :class:`~codd.project_types.LayoutProfile`. The harness OWNS the verification
   command (runs the WHOLE VB surface — unit AND e2e — and emits a machine-
   readable report); ``detect_test_command``'s one-SUT-script pick is NOT the
   pass authority here. (Brownfield/fixer watch/partial-run keep
   ``detect_test_command`` — UNCHANGED.)
2. **TestInventory** — the SINGLE source of test files every gate consumes
   (:class:`TestInventory`). It reuses the SAME discovery + suffix + level
   classification the VB scan / authenticity / e2e-contract audits already use
   (:func:`codd.operational_e2e_audit._iter_test_files` /
   ``_classify_test_level`` / ``_TEST_SUFFIXES``), so all gates see ONE glob —
   never a per-gate glob that lets e2e files be visible to one gate and invisible
   to another (the codex14 ``0 e2e scanned`` half of the bug).
3. **Runner-report adapter** — per-profile normalization of the campaign report
   into the set of executed + passed test FILES (and, when available, cases).
   ``vitest-json`` is implemented; ``pytest-junit-xml`` / ``go-test-json`` are
   documented extension points (:func:`resolve_runner_report_adapter`). A report
   the gate cannot read is an EXPLICIT degrade/observability error — never a
   silent green.

GENERALITY (see ``feedback_codd_generality_preservation``): the gate logic here
is language-agnostic. Every language-specific operation — the campaign command,
the report format, the report parse — is a per-profile spec/adapter. A stack
whose profile declares ``verify_campaign=None`` (Python today) makes the whole
gate a strict NO-OP for it; its existing verify-stage coherence gates remain its
backstop, UNCHANGED.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from codd.operational_e2e_audit import (
    _classify_test_level,
    _iter_test_files,
    _load_optional_config,
    _rel_path,
    _resolve_vb_scan_dirs,
)
from codd.verifiable_behavior_audit import (
    VBAuditReport,
    build_vb_coverage_audit,
)
from codd.vb_marker_authenticity import (
    AuthenticityReport,
    build_authenticity_report,
)


COHERENCE_CONTRACT_VERSION = "coverage-execution-coherence/v1"


# ───────────────────────────────────────────────────────────────────────────
# 1. Runner-report adapter (the ONLY language-specific report surface)
# ───────────────────────────────────────────────────────────────────────────


class RunnerReportUnsupported(RuntimeError):
    """Raised when a campaign declares a report format with no registered adapter."""


@dataclass(frozen=True)
class RunnerExecution:
    """Normalized execution evidence parsed from a verify-campaign report.

    ``executed_passed_files`` / ``executed_failed_files`` are project-relative,
    POSIX, normalized test-file paths. A file is in ``executed_passed_files`` only
    when it ran AND every executed test case in it passed (no failure/error) AND
    it collected at least one case — the file-level pass that the coherence gate
    treats as a VB's execution proof. ``executed_passed_cases`` is the set of
    ``"<relfile>::<fully-qualified-test-name>"`` keys for passed cases (when the
    runner reports test-level granularity), available for finer reconciliation;
    it is best-effort and may be empty for a file-level-only runner.

    ``test_level_available`` is True when the report carried per-test-case results
    (vitest JSON does); when False, only file-level reconciliation is possible and
    the gate applies the design's degraded-pass rule (file in runner + no static
    skip/todo). ``total_cases`` / ``passed_cases`` are summary counts for
    diagnostics.
    """

    executed_passed_files: frozenset[str] = frozenset()
    executed_failed_files: frozenset[str] = frozenset()
    executed_passed_cases: frozenset[str] = frozenset()
    test_level_available: bool = False
    total_cases: int = 0
    passed_cases: int = 0

    @property
    def executed_files(self) -> frozenset[str]:
        return self.executed_passed_files | self.executed_failed_files


class RunnerReportAdapter(Protocol):
    """Per-profile parser of a verify-campaign report → :class:`RunnerExecution`.

    Implementations are PURE and best-effort on shape, but MUST raise on a report
    that is structurally unreadable (missing/garbled) so the gate degrades
    EXPLICITLY rather than silently treating "no parseable executions" as "nothing
    needed to run". ``report_path`` is the resolved campaign report file;
    ``project_root`` lets the adapter normalize absolute runner paths to project-
    relative POSIX (the form :class:`TestInventory` and the VB audit use).
    """

    def parse(self, report_path: Path, *, project_root: Path) -> RunnerExecution:
        """Parse the report at ``report_path`` into normalized execution evidence."""


def _relativize(raw: str, project_root: Path) -> str | None:
    """Normalize a runner-emitted path to project-relative POSIX, or ``None``.

    Runners report absolute file paths (vitest: ``testResults[].name``). We make
    them project-relative + POSIX so they reconcile with the VB audit's
    ``matched_tests`` / :class:`TestInventory` keys. A path outside the project
    tree (a stray absolute path) returns ``None`` (it is not one of our tests).
    """

    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    path = Path(text)
    try:
        if path.is_absolute():
            return path.resolve().relative_to(project_root).as_posix()
    except ValueError:
        return None
    # Already relative (or a bare name) — normalize separators only.
    return text.replace("\\", "/").lstrip("./") or None


@dataclass(frozen=True)
class VitestJsonReportAdapter:
    """vitest (and Jest-compatible) JSON reporter adapter.

    The vitest ``--reporter=json`` output is a top-level object with
    ``testResults: [{name, status, assertionResults: [{status, fullName, ...}]}]``
    (``name`` is the absolute test FILE; ``assertionResults`` are its test CASES,
    each ``status`` one of ``passed`` / ``failed`` / ``skipped`` / ``todo`` /
    ``pending``). A file is PASSED only when it ran ≥1 case and NONE failed/errored
    — a file with a failing OR a skipped/todo case is NOT a clean execution proof
    for a VB (a skipped case proves nothing; the static authenticity gate also
    rejects skip, so they agree). Jest's near-identical schema parses through the
    same path.
    """

    def parse(self, report_path: Path, *, project_root: Path) -> RunnerExecution:
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RunnerReportUnsupported(
                f"vitest JSON report unreadable at {report_path}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise RunnerReportUnsupported(
                f"vitest JSON report at {report_path} is not an object"
            )
        file_results = payload.get("testResults")
        if not isinstance(file_results, list):
            raise RunnerReportUnsupported(
                f"vitest JSON report at {report_path} has no testResults array"
            )

        passed_files: set[str] = set()
        failed_files: set[str] = set()
        passed_cases: set[str] = set()
        total_cases = 0
        passed_count = 0
        for file_entry in file_results:
            if not isinstance(file_entry, dict):
                continue
            rel = _relativize(str(file_entry.get("name") or ""), project_root)
            if rel is None:
                continue
            cases = file_entry.get("assertionResults")
            cases = cases if isinstance(cases, list) else []
            any_case = False
            file_clean = True
            for case in cases:
                if not isinstance(case, dict):
                    continue
                status = str(case.get("status") or "").strip().lower()
                if not status:
                    continue
                any_case = True
                total_cases += 1
                if status == "passed":
                    passed_count += 1
                    name = str(case.get("fullName") or case.get("title") or "").strip()
                    if name:
                        passed_cases.add(f"{rel}::{name}")
                else:
                    # failed / skipped / todo / pending — none is a clean pass; a
                    # file carrying any of them is not a VB execution proof.
                    file_clean = False
            # Fall back to the file-level status when a runner omits case detail.
            file_status = str(file_entry.get("status") or "").strip().lower()
            if not any_case:
                if file_status == "passed":
                    passed_files.add(rel)
                elif file_status in ("failed", "error"):
                    failed_files.add(rel)
                # an empty file with no status is neither (collected nothing)
                continue
            if file_clean and file_status not in ("failed", "error"):
                passed_files.add(rel)
            else:
                failed_files.add(rel)
        return RunnerExecution(
            executed_passed_files=frozenset(passed_files),
            executed_failed_files=frozenset(failed_files),
            executed_passed_cases=frozenset(passed_cases),
            test_level_available=total_cases > 0,
            total_cases=total_cases,
            passed_cases=passed_count,
        )


#: report_format → adapter. A new runner registers ONE entry here + sets the
#: matching ``report_format`` on its profile's :class:`VerifyCampaignSpec`.
#: ``pytest-junit-xml`` / ``go-test-json`` are documented extension points
#: (the design's per-language adapter list): pytest JUnit XML parses
#: ``<testcase classname name>`` + ``<skipped>``/``<failure>``/``<error>``
#: children; ``go test -json`` parses the streamed ``{Action: "pass"|"fail",
#: Test, Package}`` events. Until added they resolve to ``None`` and the gate
#: degrades EXPLICITLY for that stack (never a silent green).
_RUNNER_REPORT_ADAPTERS: dict[str, RunnerReportAdapter] = {
    "vitest-json": VitestJsonReportAdapter(),
}


def resolve_runner_report_adapter(report_format: str | None) -> RunnerReportAdapter | None:
    """The adapter for a campaign ``report_format``, or ``None`` if unregistered.

    ``None`` (an unknown / not-yet-implemented format) makes the gate degrade
    EXPLICITLY — it surfaces "this stack's campaign report has no adapter" rather
    than silently passing a build whose executions it cannot read.
    """

    if not report_format:
        return None
    return _RUNNER_REPORT_ADAPTERS.get(str(report_format).strip().lower())


def supported_runner_report_formats() -> list[str]:
    """Report formats with a registered runner adapter (deterministic order)."""
    return sorted(_RUNNER_REPORT_ADAPTERS)


# ───────────────────────────────────────────────────────────────────────────
# 2. TestInventory — the single source of test files every gate consumes
# ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TestInventoryEntry:
    """One test file in the unified inventory.

    ``rel_path`` is project-relative POSIX (the key every gate + the VB audit's
    ``matched_tests`` + the runner report use). ``kind`` is ``unit`` /
    ``integration`` / ``e2e`` (from the SAME ``_classify_test_level`` the
    operational audit uses; ``lower_test`` is mapped to ``unit``).
    ``runner_inclusion`` is True when the file appears in the campaign report (it
    was collected/executed). ``execution_status`` is one of
    ``executed_passed`` / ``executed_failed`` / ``not_executed``.
    """

    rel_path: str
    kind: str
    runner_inclusion: bool = False
    execution_status: str = "not_executed"

    @property
    def is_e2e(self) -> bool:
        return self.kind == "e2e"


@dataclass
class TestInventory:
    """The unified set of test files + their execution status.

    Built ONCE from the shared discovery glob (the same one the VB scan /
    authenticity / e2e-contract / operational audits consume), then annotated with
    the campaign's runner report. Every coherence decision reads from THIS — no
    gate re-globs the tree with its own pattern.
    """

    entries: dict[str, TestInventoryEntry] = field(default_factory=dict)
    #: True when a runner report was successfully parsed and applied.
    execution_applied: bool = False

    def get(self, rel_path: str) -> TestInventoryEntry | None:
        return self.entries.get(_norm_test_path(rel_path))

    @property
    def files(self) -> list[str]:
        return sorted(self.entries)

    @property
    def e2e_files(self) -> list[str]:
        return sorted(rel for rel, e in self.entries.items() if e.is_e2e)

    @property
    def executed_e2e_files(self) -> list[str]:
        return sorted(
            rel
            for rel, e in self.entries.items()
            if e.is_e2e and e.runner_inclusion
        )

    def passed(self, rel_path: str) -> bool:
        entry = self.get(rel_path)
        return entry is not None and entry.execution_status == "executed_passed"


def _norm_test_path(rel_path: str) -> str:
    return str(rel_path).replace("\\", "/").strip().lstrip("./")


def _inventory_kind(rel_path: str) -> str:
    """Map the operational audit's test level to the inventory kind vocabulary."""
    level = _classify_test_level(rel_path)
    # ``_classify_test_level`` returns "e2e" or "lower_test"; refine lower_test
    # into unit/integration by path so the inventory carries the finer kind the
    # design's TestInventory schema names, while staying on the SAME classifier.
    if level == "e2e":
        return "e2e"
    normalized = "/" + rel_path.replace("\\", "/").lower()
    if "/integration/" in normalized:
        return "integration"
    return "unit"


def build_test_inventory(
    project_root: Path | str,
    *,
    config: dict[str, Any] | None = None,
    test_dirs: Iterable[Path | str] | None = None,
    execution: RunnerExecution | None = None,
) -> TestInventory:
    """Build the unified test inventory from the shared discovery glob.

    Discovery uses :func:`codd.operational_e2e_audit._iter_test_files` with the
    SAME scope resolution (:func:`_resolve_vb_scan_dirs`) the VB and operational
    audits use, so the inventory is byte-for-byte the file set those gates see.
    When ``execution`` is provided, each file is annotated with its runner
    inclusion + pass/fail status.
    """

    project_root = Path(project_root).resolve()
    if config is None:
        config = _load_optional_config(project_root)
    if test_dirs is None:
        test_dirs = _resolve_vb_scan_dirs(project_root, config)

    entries: dict[str, TestInventoryEntry] = {}
    passed_files = execution.executed_passed_files if execution else frozenset()
    failed_files = execution.executed_failed_files if execution else frozenset()
    for path in _iter_test_files(project_root, test_dirs=test_dirs):
        rel = _norm_test_path(_rel_path(path, project_root))
        if rel in entries:
            continue
        included = rel in passed_files or rel in failed_files
        if rel in passed_files:
            status = "executed_passed"
        elif rel in failed_files:
            status = "executed_failed"
        else:
            status = "not_executed"
        entries[rel] = TestInventoryEntry(
            rel_path=rel,
            kind=_inventory_kind(rel),
            runner_inclusion=included,
            execution_status=status,
        )
    return TestInventory(entries=entries, execution_applied=execution is not None)


# ───────────────────────────────────────────────────────────────────────────
# 3. The coverage-execution coherence gate (the new invariant)
# ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class UnverifiedVB:
    """A VB that is statically covered (authentically) but NOT proven by execution."""

    vb_id: str
    description: str
    source_doc: str
    #: covering test files (static, authentic) and their per-file execution status
    covering_files: tuple[str, ...]
    #: why it is unverified: ``no_covering_test_executed`` (all covering files
    #: not_executed) / ``covering_test_failed`` (a covering file ran but failed) /
    #: ``no_authentic_cover`` (its only markers were inauthentic — already a
    #: separate gate's failure, surfaced here for completeness).
    reason: str

    @property
    def message(self) -> str:
        files = ", ".join(self.covering_files) if self.covering_files else "(none)"
        desc = f" — {self.description}" if self.description else ""
        if self.reason == "covering_test_failed":
            why = (
                "its covering test ran in the verify campaign but did NOT pass — a "
                "failing test does not prove the behavior."
            )
        elif self.reason == "no_authentic_cover":
            why = (
                "it has no AUTHENTIC covering test (its covers marker(s) are not "
                "credible — see the marker-authenticity gate)."
            )
        else:
            why = (
                "its covering test(s) were NOT executed by the verify campaign — "
                "static coverage alone does not prove the behavior. The covering "
                "test must actually RUN and PASS (e.g. an e2e-only VB whose e2e "
                "suite the campaign never ran)."
            )
        return (
            f"{self.vb_id}{desc} (declared in {self.source_doc}; covering test(s): "
            f"{files}): {why}"
        )


@dataclass(frozen=True)
class CoherenceObservabilityError:
    """A harness observability failure (NOT a SUT defect): the campaign report is
    unreadable, or an e2e surface exists but the campaign scanned ZERO e2e files."""

    kind: str  # "report_unreadable" | "no_adapter" | "e2e_scan_zero"
    message: str


@dataclass
class CoherenceReport:
    """Verdict of the coverage-execution coherence gate."""

    version: str
    applicable: bool
    unverified_vbs: list[UnverifiedVB] = field(default_factory=list)
    observability_errors: list[CoherenceObservabilityError] = field(default_factory=list)
    #: counts for diagnostics / the success line
    unblocked_count: int = 0
    verified_count: int = 0
    executed_files: int = 0
    e2e_files: int = 0
    executed_e2e_files: int = 0
    detail: str = ""

    @property
    def passed(self) -> bool:
        return not self.unverified_vbs and not self.observability_errors


def _authentic_cover_files(
    vb_id: str,
    audit: VBAuditReport,
    authenticity: AuthenticityReport,
) -> tuple[set[str], bool]:
    """Covering test FILES for ``vb_id`` that carry an AUTHENTIC marker.

    Returns ``(files, had_any_marker)``. ``files`` are the audit's matched test
    files MINUS those whose marker for this VB is an authenticity violation
    (orphan / skipped / unattached / no-assertion). ``had_any_marker`` is True
    when the VB had at least one covering file in the static audit (so the gate
    can distinguish "no marker at all" — a coverage-gate problem — from "marker
    present but inauthentic"). Authenticity is reconciled at FILE granularity
    (the static audit's ``matched_tests`` is file-level), conservatively: a file
    is dropped only when EVERY one of its markers for this VB is inauthentic, so a
    file with one good and one bad marker for the same VB still counts (the bad
    marker is the authenticity gate's separate failure).
    """

    matched: set[str] = set()
    for row in audit.rows:
        if _vb_eq(row.vb_id, vb_id):
            matched = {_norm_test_path(p) for p in row.matched_tests}
            break
    if not matched:
        return set(), False

    # Files where this VB's marker(s) are ALL inauthentic → drop from evidence.
    inauthentic_for_vb: dict[str, int] = {}
    for v in authenticity.violations:
        if v.kind == "hook":
            continue
        if _vb_eq(v.vb_id, vb_id):
            inauthentic_for_vb[_norm_test_path(v.path)] = (
                inauthentic_for_vb.get(_norm_test_path(v.path), 0) + 1
            )
    # A file is dropped only if it appears in matched AND has an inauthentic marker
    # for this VB AND no offsetting evidence. The static audit cannot tell us how
    # many GOOD markers a file has, so we conservatively keep the file unless the
    # authenticity gate flagged it for this VB (the authenticity gate is itself a
    # HARD gate run alongside, so a truly inauthentic-only VB is already failing
    # there; dropping here just prevents it from masquerading as execution-proven).
    authentic = {f for f in matched if f not in inauthentic_for_vb}
    return authentic, True


def build_coherence_report(
    project_root: Path | str,
    *,
    config: dict[str, Any] | None = None,
    profile: Any = None,
    execution: RunnerExecution | None = None,
    inventory: TestInventory | None = None,
    vb_audit: VBAuditReport | None = None,
    authenticity: AuthenticityReport | None = None,
) -> CoherenceReport:
    """Reconcile static VB coverage against actual test execution.

    The new invariant: every UNBLOCKED VB must have ≥1 AUTHENTIC covering test
    file that the verify campaign EXECUTED and PASSED. ``unblocked − verified ≠ ∅``
    is a HARD FAIL (each surfaced as an :class:`UnverifiedVB`).

    ``execution`` is the parsed campaign report (None ⇒ no campaign ran: every
    covering file is "not executed", so any covered-unblocked VB is unverified —
    this is the e2e-not-run case made into a hard failure). ``profile`` (a
    :class:`~codd.project_types.LayoutProfile`) supplies the e2e modality for the
    observability check.
    """

    project_root = Path(project_root).resolve()
    if config is None:
        config = _load_optional_config(project_root)
    if vb_audit is None:
        vb_audit = build_vb_coverage_audit(project_root, config=config)
    if authenticity is None:
        authenticity = build_authenticity_report(project_root, config=config, profile=profile)
    if inventory is None:
        inventory = build_test_inventory(project_root, config=config, execution=execution)

    # Applicability: the gate is meaningful only when there are declared VBs to
    # reconcile. (The CALLER decides whether the STACK has a campaign; a stack
    # with no campaign passes None execution and the gate still runs — but then a
    # covered VB with no executed covering test is correctly unverified. The
    # greenfield wiring only INVOKES this gate for a stack with a campaign; see
    # the pipeline.)
    if not vb_audit.rows:
        return CoherenceReport(
            version=COHERENCE_CONTRACT_VERSION,
            applicable=False,
            detail="no verifiable behaviors declared — coverage-execution coherence is N/A",
        )

    unblocked = [row for row in vb_audit.rows if row.coverage_status != "blocked"]
    unverified: list[UnverifiedVB] = []
    verified_count = 0
    for row in unblocked:
        authentic_files, had_marker = _authentic_cover_files(row.vb_id, vb_audit, authenticity)
        if not had_marker:
            # No covering marker at all → a COVERAGE-gate failure, not this gate's
            # (the coverage gate already fails it). Do not double-report here.
            unverified.append(
                UnverifiedVB(
                    vb_id=row.vb_id,
                    description=row.description,
                    source_doc=row.source_doc,
                    covering_files=(),
                    reason="no_covering_test_executed",
                )
            )
            continue
        if not authentic_files:
            unverified.append(
                UnverifiedVB(
                    vb_id=row.vb_id,
                    description=row.description,
                    source_doc=row.source_doc,
                    covering_files=tuple(sorted(_norm_test_path(p) for p in row.matched_tests)),
                    reason="no_authentic_cover",
                )
            )
            continue
        # Verified iff ANY authentic covering file executed AND passed.
        passed_files = sorted(f for f in authentic_files if inventory.passed(f))
        if passed_files:
            verified_count += 1
            continue
        # Not verified: distinguish "ran but failed" from "never ran" for the msg.
        ran_files = sorted(
            f for f in authentic_files
            if (entry := inventory.get(f)) is not None and entry.runner_inclusion
        )
        reason = "covering_test_failed" if ran_files else "no_covering_test_executed"
        unverified.append(
            UnverifiedVB(
                vb_id=row.vb_id,
                description=row.description,
                source_doc=row.source_doc,
                covering_files=tuple(sorted(authentic_files)),
                reason=reason,
            )
        )

    observability = _observability_errors(
        project_root,
        config=config,
        profile=profile,
        inventory=inventory,
        authenticity=authenticity,
        vb_audit=vb_audit,
        execution=execution,
    )

    report = CoherenceReport(
        version=COHERENCE_CONTRACT_VERSION,
        applicable=True,
        unverified_vbs=unverified,
        observability_errors=observability,
        unblocked_count=len(unblocked),
        verified_count=verified_count,
        executed_files=len(inventory.executed_e2e_files)
        + sum(1 for e in inventory.entries.values() if e.runner_inclusion and not e.is_e2e),
        e2e_files=len(inventory.e2e_files),
        executed_e2e_files=len(inventory.executed_e2e_files),
    )
    report.detail = (
        f"{verified_count}/{len(unblocked)} unblocked VB(s) execution-verified; "
        f"e2e files {len(inventory.executed_e2e_files)}/{len(inventory.e2e_files)} executed"
    )
    return report


def _observability_errors(
    project_root: Path,
    *,
    config: dict[str, Any] | None,
    profile: Any,
    inventory: TestInventory,
    authenticity: AuthenticityReport,
    vb_audit: VBAuditReport,
    execution: RunnerExecution | None,
) -> list[CoherenceObservabilityError]:
    """E2E observability hard-fail (design section D).

    ``0 e2e scanned`` is itself a harness failure when an e2e surface EXISTS. The
    surface exists when ANY of: an e2e test file is in the inventory; a VB covers-
    marker sits in an e2e file (a covering file the VB audit matched whose kind is
    e2e); or the profile declares an e2e modality. If a surface exists but the
    campaign executed ZERO e2e files, that is an observability error (the campaign
    did not run the e2e layer it should have), NOT a pass.
    """

    errors: list[CoherenceObservabilityError] = []

    e2e_in_inventory = inventory.e2e_files
    # A VB covering file classified e2e (markers in an e2e file).
    vb_e2e_files = sorted(
        {
            _norm_test_path(p)
            for row in vb_audit.rows
            for p in row.matched_tests
            if _inventory_kind(_norm_test_path(p)) == "e2e"
        }
    )

    # A CONCRETE e2e surface is required to assert "0 e2e scanned is a failure":
    # actual e2e test files in the tree, OR VB covers-markers living in e2e files.
    # The profile's declared e2e MODALITY is NOT a sole trigger — the ``generic``
    # baseline declares ``e2e_modality="cli"`` for EVERY project, so firing on
    # modality alone would false-RED a pure-unit project that legitimately has no
    # e2e files. Modality only CORROBORATES (it sharpens the message) when a
    # concrete surface already exists; it never manufactures one.
    concrete_surface = bool(e2e_in_inventory or vb_e2e_files)
    if not concrete_surface:
        return errors
    modality_e2e = _profile_has_e2e_modality(project_root, config=config, profile=profile)

    # When execution evidence is present, an existing e2e surface MUST show ≥1
    # executed e2e file; zero is the "0 e2e scanned" observability failure.
    if execution is not None and not inventory.executed_e2e_files:
        evidence = []
        if e2e_in_inventory:
            evidence.append(f"{len(e2e_in_inventory)} e2e test file(s) present")
        if vb_e2e_files:
            evidence.append(f"{len(vb_e2e_files)} VB marker(s) in e2e file(s)")
        if modality_e2e:
            evidence.append("profile declares an e2e modality")
        errors.append(
            CoherenceObservabilityError(
                kind="e2e_scan_zero",
                message=(
                    "e2e observability failure: an e2e surface exists ("
                    + "; ".join(evidence)
                    + ") but the verify campaign executed 0 e2e file(s). The campaign "
                    "must run the e2e layer (check the campaign command's test root / "
                    "the runner's collection include for the .e2e.* convention) — a "
                    "0-e2e-scanned run is a harness error, not a pass."
                ),
            )
        )
    return errors


def _profile_has_e2e_modality(
    project_root: Path,
    *,
    config: dict[str, Any] | None,
    profile: Any,
) -> bool:
    """Whether the project's configured type declares a non-trivial e2e modality.

    Reuses the SAME modality resolution the e2e-contract gate uses (configured
    project type → capability profile ``e2e_modality``). ``browser`` / ``cli`` /
    ``device`` are e2e surfaces; ``none`` is not. Best-effort: any resolution
    failure returns False (no spurious observability error from an undecidable
    modality).
    """

    try:
        from codd.project_types import load_capabilities, resolve_project_type

        cfg = config if config is not None else _load_optional_config(project_root)
        project_section = cfg.get("project") if isinstance(cfg.get("project"), dict) else {}
        configured = (
            project_section.get("type") or project_section.get("project_type")
            if isinstance(project_section, dict)
            else None
        )
        resolved_type, _ = resolve_project_type(configured, None, project_root)
        modality = load_capabilities(resolved_type, project_root).e2e_modality
        return str(modality).strip().lower() in ("browser", "cli", "device")
    except Exception:  # noqa: BLE001 — undecidable modality ⇒ no observability claim.
        return False


def format_coherence_feedback(report: CoherenceReport) -> str:
    """Render coherence failures as SUT-facing rerun feedback.

    Like the coverage / authenticity feedback, this is about making the COVERING
    TEST actually run + pass under the harness campaign — never about adding a
    marker or weakening the gate.
    """

    lines: list[str] = []
    if report.observability_errors:
        lines.append(
            "The verify campaign did not observe part of the test surface it must:"
        )
        for err in report.observability_errors:
            lines.append(f"- {err.message}")
        lines.append("")
    if report.unverified_vbs:
        lines.append(
            "Some declared verifiable behaviors are statically covered but were NOT "
            "proven by EXECUTION. A behavior is only verified when its covering test "
            "actually RAN and PASSED in the harness-owned verify campaign (unit AND "
            "e2e). Fix each by ensuring the covering test executes and passes under "
            "the campaign (do NOT remove the marker, weaken the campaign, or mark the "
            "test skipped):"
        )
        for vb in report.unverified_vbs:
            lines.append(f"- {vb.message}")
    return "\n".join(lines)


def render_coherence_markdown(report: CoherenceReport) -> str:
    """Render the coherence report as Markdown (for ``codd test audit`` etc.)."""

    lines = [
        "# Coverage-Execution Coherence",
        "",
        f"- Contract: {report.version}",
        f"- Applicable: {report.applicable}",
        f"- Unblocked VBs execution-verified: {report.verified_count}/{report.unblocked_count}",
        f"- E2E files executed: {report.executed_e2e_files}/{report.e2e_files}",
        f"- Execution-unverified VBs: {len(report.unverified_vbs)}",
        f"- Observability errors: {len(report.observability_errors)}",
    ]
    if report.observability_errors:
        lines.extend(["", "## Observability Errors", ""])
        for err in report.observability_errors:
            lines.append(f"- [{err.kind}] {err.message}")
    if report.unverified_vbs:
        lines.extend(
            [
                "",
                "## Execution-Unverified Verifiable Behaviors",
                "| VB | Source Doc | Covering Test(s) | Reason |",
                "| --- | --- | --- | --- |",
            ]
        )
        for vb in report.unverified_vbs:
            files = ", ".join(vb.covering_files) if vb.covering_files else "-"
            lines.append(
                f"| {vb.vb_id} | {vb.source_doc} | {files} | {vb.reason} |"
            )
    lines.append("")
    return "\n".join(lines)


# ───────────────────────────────────────────────────────────────────────────
# Small shared helpers
# ───────────────────────────────────────────────────────────────────────────


def _vb_eq(a: str, b: str) -> bool:
    """Whether two VB ids match under the audit's canonical normalization."""
    from codd.verifiable_behavior_audit import _normalize_vb_id

    return _normalize_vb_id(a) == _normalize_vb_id(b)


# ───────────────────────────────────────────────────────────────────────────
# Campaign execution + the greenfield-verify gate entry point
# ───────────────────────────────────────────────────────────────────────────


#: Bounded wall-clock for the verify campaign. The campaign runs the WHOLE test
#: surface (unit + e2e), so it gets a generous-but-bounded budget; an override
#: lives under ``verify.campaign_timeout_seconds``.
DEFAULT_CAMPAIGN_TIMEOUT_SECONDS = 1200.0


class CampaignError(RuntimeError):
    """The verify campaign could not run to a parseable report (harness error)."""


@dataclass
class CampaignRun:
    """Outcome of executing a verify campaign command."""

    command: str
    exit_code: int
    report_path: Path
    execution: RunnerExecution
    output_tail: str = ""


def _campaign_timeout_seconds(config: dict[str, Any] | None) -> float:
    verify = (config or {}).get("verify")
    if isinstance(verify, dict):
        raw = verify.get("campaign_timeout_seconds")
        try:
            seconds = float(raw)
            if seconds > 0:
                return seconds
        except (TypeError, ValueError):
            pass
    return DEFAULT_CAMPAIGN_TIMEOUT_SECONDS


def run_verify_campaign(
    project_root: Path | str,
    profile: Any,
    *,
    config: dict[str, Any] | None = None,
    echo: Callable[[str], None] = print,
) -> CampaignRun:
    """Execute the profile-owned verify campaign and parse its report.

    Runs ``profile.verify_campaign``'s resolved command from the project root,
    then parses the machine-readable report through the per-profile runner
    adapter. ANTI-FALSE-GREEN: a campaign that produced NO report, an UNREADABLE
    report, or an empty-execution report (collected 0 tests) is a :class:`CampaignError`
    (a harness/observability failure), NEVER a silent pass. The command's own exit
    code is captured but is NOT the pass authority — the coherence gate is (a
    campaign may exit nonzero because a covering test failed, which the gate
    reports per-VB).

    Caller contract: only invoke when ``profile.verify_campaign`` is not None and
    ``profile.runner_report_adapter()`` resolves. A None campaign/adapter is the
    caller's NO-OP branch (the gate does not apply to that stack).
    """

    project_root = Path(project_root).resolve()
    if config is None:
        config = _load_optional_config(project_root)
    campaign = getattr(profile, "verify_campaign", None)
    if campaign is None:
        raise CampaignError("profile declares no verify_campaign (caller must NO-OP)")
    adapter = profile.runner_report_adapter()
    if adapter is None:
        raise CampaignError(
            f"no runner-report adapter for campaign report_format "
            f"{getattr(campaign, 'report_format', None)!r} — cannot read campaign executions"
        )

    test_root = getattr(profile, "test_root", "tests")
    report_path = project_root / campaign.report_relpath
    report_path.parent.mkdir(parents=True, exist_ok=True)
    # A stale report from a prior run must never be mistaken for this run's
    # evidence — remove it first so a campaign that silently writes nothing fails
    # the "no report" check instead of reusing old executions.
    try:
        report_path.unlink()
    except FileNotFoundError:
        pass
    command = campaign.resolve_command(
        test_root=test_root, report_path=campaign.report_relpath
    )
    timeout = _campaign_timeout_seconds(config)
    echo(f"[greenfield] verify: running coverage-execution campaign — {command}")
    try:
        completed = subprocess.run(
            command,
            shell=True,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise CampaignError(
            f"verify campaign timed out after {timeout:g}s: {command}"
        ) from exc

    output_tail = _output_tail(completed.stdout, completed.stderr)
    if not report_path.is_file():
        raise CampaignError(
            f"verify campaign produced no report at {campaign.report_relpath} "
            f"(exit {completed.returncode}): {command}\n{output_tail}"
        )
    try:
        execution = adapter.parse(report_path, project_root=project_root)
    except RunnerReportUnsupported as exc:
        raise CampaignError(str(exc)) from exc
    if execution.total_cases == 0 and not execution.executed_files:
        # The campaign ran but the report shows no executed tests at all — the
        # JS-runner "collected 0 tests" hard-fail, generalized to the report.
        raise CampaignError(
            f"verify campaign collected/ran 0 tests (report at {campaign.report_relpath} "
            f"is empty): {command}\n{output_tail}"
        )
    return CampaignRun(
        command=command,
        exit_code=completed.returncode,
        report_path=report_path,
        execution=execution,
        output_tail=output_tail,
    )


def coherence_gate_applies(profile: Any) -> bool:
    """Whether the coverage-execution coherence gate applies to this stack.

    True only when the profile declares a verify campaign AND a runner-report
    adapter resolves for its format. Any other case (no campaign — Python today;
    or a campaign whose format has no adapter yet) is a NO-OP for the gate — the
    caller skips it (the no-adapter case is surfaced separately as an explicit
    degrade where it matters, never a silent green for a stack that HAS a campaign
    but no reader).
    """

    campaign = getattr(profile, "verify_campaign", None)
    if campaign is None:
        return False
    getter = getattr(profile, "runner_report_adapter", None)
    if not callable(getter):
        return False
    try:
        return getter() is not None
    except Exception:  # noqa: BLE001
        return False


def enforce_coverage_execution_coherence(
    project_root: Path | str,
    profile: Any,
    *,
    config: dict[str, Any] | None = None,
    echo: Callable[[str], None] = print,
) -> CoherenceReport:
    """Run the campaign + the coherence gate; raise on a hard failure.

    The single greenfield-verify entry point. Steps:
      1. NO-OP when the stack has no applicable campaign (returns a non-applicable
         report) — Python today, or a campaign with no adapter.
      2. Execute the profile-owned verify campaign (the WHOLE VB surface) →
         :class:`RunnerExecution`.
      3. Reconcile static VB coverage (+ authenticity) against that execution.
      4. Raise :class:`CoherenceError` when ``unblocked − verified ≠ ∅`` or an
         e2e-observability error is present.

    The CALLER (greenfield ``_stage_verify``) wires this AFTER the normal verify
    runner certified the structural/typecheck/test-command path; this gate ADDS
    the execution-coherence proof on top (it never weakens the existing gates).
    """

    project_root = Path(project_root).resolve()
    if config is None:
        config = _load_optional_config(project_root)

    if not coherence_gate_applies(profile):
        return CoherenceReport(
            version=COHERENCE_CONTRACT_VERSION,
            applicable=False,
            detail="stack declares no applicable verify campaign — coherence gate is a no-op",
        )

    run = run_verify_campaign(project_root, profile, config=config, echo=echo)
    report = build_coherence_report(
        project_root, config=config, profile=profile, execution=run.execution
    )
    if not report.applicable:
        echo(f"[greenfield] verify: coverage-execution coherence — {report.detail}")
        return report

    if not report.passed:
        for err in report.observability_errors:
            echo(err.message)
        for vb in report.unverified_vbs:
            echo(vb.message)
        raise CoherenceError(
            "coverage-execution coherence gate failed: "
            f"{len(report.unverified_vbs)} declared verifiable behavior(s) are statically "
            "covered but were NOT executed+passed by the verify campaign, and/or "
            f"{len(report.observability_errors)} e2e-observability error(s). Static coverage "
            "does not prove a behavior — its covering test must actually run and pass. "
            f"({report.detail})"
        )
    echo(
        f"[greenfield] verify: coverage-execution coherence OK — {report.detail}"
    )
    return report


class CoherenceError(RuntimeError):
    """The coverage-execution coherence gate failed (an anti-false-green hard fail)."""


def _output_tail(stdout: str | None, stderr: str | None, limit: int = 4000) -> str:
    combined = "\n".join(part.strip() for part in (stdout, stderr) if part and part.strip())
    if len(combined) <= limit:
        return combined
    return f"... (truncated) ...\n{combined[-limit:]}"


__all__ = [
    "COHERENCE_CONTRACT_VERSION",
    "CampaignError",
    "CampaignRun",
    "CoherenceError",
    "CoherenceObservabilityError",
    "CoherenceReport",
    "RunnerExecution",
    "RunnerReportAdapter",
    "RunnerReportUnsupported",
    "TestInventory",
    "TestInventoryEntry",
    "UnverifiedVB",
    "VitestJsonReportAdapter",
    "build_coherence_report",
    "build_test_inventory",
    "coherence_gate_applies",
    "enforce_coverage_execution_coherence",
    "format_coherence_feedback",
    "render_coherence_markdown",
    "resolve_runner_report_adapter",
    "run_verify_campaign",
    "supported_runner_report_formats",
]
