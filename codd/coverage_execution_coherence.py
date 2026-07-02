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
   ``vitest-json`` and ``go-test-json`` are implemented; ``pytest-junit-xml`` is a
   documented extension point (:func:`resolve_runner_report_adapter`). A report
   the gate cannot read is an EXPLICIT degrade/observability error — never a
   silent green. A runner that reports per-test-case identity (Go) reconciles each
   VB at TEST granularity via :class:`RunnerExecution.executed_passed_cases`; a
   file-level runner (vitest/pytest) reconciles at FILE granularity — both share
   the same gate, selected by the adapter's ``produces_test_case_identity``.

GENERALITY (see ``feedback_codd_generality_preservation``): the gate logic here
is language-agnostic. Every language-specific operation — the campaign command,
the report format, the report parse — is a per-profile spec/adapter. A stack
whose profile declares ``verify_campaign=None`` (Python today) makes the whole
gate a strict NO-OP for it; its existing verify-stage coherence gates remain its
backstop, UNCHANGED.

MULTI-REPORT CAMPAIGNS (design: multi-report verify campaigns, 2026-07-02). A
:class:`~codd.project_types.VerifyCampaignSpec` is an ordered tuple of
:class:`~codd.project_types.VerifyCampaignStep`, each with one-or-more
:class:`~codd.project_types.CampaignReportSpec` artifacts — Maven's Surefire/
Failsafe split is ONE invocation writing TWO report roots, not two invocations.
Every single-report profile (TS/C#/C++/Go's extension point) stays on the
original flat-field shape via :meth:`~codd.project_types.VerifyCampaignSpec.resolved_steps`,
byte-identical. THE DECLARATION DISCIPLINE THIS BUYS FRESHNESS: an adapter may
only ever read a path the campaign DECLARED (a ``CampaignReportSpec.relpath`` —
see :func:`run_verify_campaign`'s up-front clear-every-declared-path step). A
root the campaign does not declare is a root :func:`run_verify_campaign` does
not clear, so an adapter that quietly reads a "conventional sibling" directory
the campaign never named (e.g. a ``surefire-xml`` adapter that also peeked at
``failsafe-reports/`` without that path being a declared, cleared
``CampaignReportSpec``) could let a STALE prior run's evidence sail straight
into this run's verdict — the exact hazard multi-root stale-clearing exists to
close. Every report an adapter may read MUST be declared, in the profile, as
its own report artifact.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

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

# The runner-report parser cluster (RunnerExecution / Protocol / the vitest & Go
# adapters + their module-level helpers) was RELOCATED to the leaf module
# :mod:`codd.languages.adapters.runner_report` for the Contract Kernel so the
# coverage-execution coherence gate AND the language contract resolve ONE source
# of these adapters. They are RE-EXPORTED here (same objects — identity preserved)
# so ``from codd.coverage_execution_coherence import RunnerExecution`` keeps working
# unchanged. The format→adapter RESOLUTION below also defers to that ONE source —
# the process-wide ``default_adapter_registry`` (see ``resolve_runner_report_adapter``)
# — so there is no second local table to drift from it. ``_norm_test_path``
# moved with them (the moved Go index needs it) and is re-imported here because the
# inventory/gate code below still uses it. (runner_report imports only stdlib +
# operational_e2e_audit + vb_marker_authenticity — none import codd.languages —
# so this import introduces NO cycle.)
from codd.languages.adapters.runner_report import (  # noqa: E402 — leaf re-export
    GoTestJsonReportAdapter,
    RunnerExecution,
    RunnerReportAdapter,
    RunnerReportUnsupported,
    VitestJsonReportAdapter,
    _norm_test_path,
)


COHERENCE_CONTRACT_VERSION = "coverage-execution-coherence/v1"


# ───────────────────────────────────────────────────────────────────────────
# 1. Runner-report adapter (the ONLY language-specific report surface)
# ───────────────────────────────────────────────────────────────────────────


# The runner-report PARSERS (RunnerReportUnsupported / RunnerExecution /
# RunnerReportAdapter / VitestJsonReportAdapter / GoTestJsonReportAdapter and the
# compiler-language Surefire / CTest / TRX adapters) and their helpers were
# RELOCATED to codd/languages/adapters/runner_report.py for the Contract Kernel
# (re-imported above; same objects). The format→adapter RESOLUTION resolves against
# the SINGLE source of truth — the process-wide ``default_adapter_registry``
# populated by ``codd.languages.builtin_adapters.ensure_builtin_adapters_registered``
# (the ONE place built-in adapters are registered). There is deliberately NO local
# format→adapter table here: a second table is exactly what let ``surefire-xml`` /
# ``ctest-junit`` / ``dotnet-trx`` register on the registry yet stay unresolvable to
# this gate. The coverage gate and the language contract now read the SAME registry,
# so they cannot drift.


def _runner_report_registry() -> tuple[Any, str]:
    """The ``(registry, kind)`` pair for runner-report adapter resolution.

    Returns the process-wide ``default_adapter_registry`` with the built-in adapters
    lazily registered, plus the ``runner_report`` adapter kind. Registration is
    idempotent and cheap after the first call (guarded inside
    :func:`~codd.languages.builtin_adapters.ensure_builtin_adapters_registered`); it
    is done lazily INSIDE this function so importing this module pulls no adapter
    implementation at load time (mirrors :func:`codd.languages.contract.build_language_contract`'s
    lazy registration). This registry is the SINGLE source — no local table.
    """

    from codd.languages.builtin_adapters import ensure_builtin_adapters_registered
    from codd.languages.contract import KIND_RUNNER_REPORT
    from codd.languages.registry import default_adapter_registry

    ensure_builtin_adapters_registered(default_adapter_registry)
    return default_adapter_registry, KIND_RUNNER_REPORT


def resolve_runner_report_adapter(report_format: str | None) -> RunnerReportAdapter | None:
    """The adapter for a campaign ``report_format``, or ``None`` if unregistered.

    Resolves against the SINGLE source of truth — the process-wide
    ``default_adapter_registry`` (kind ``runner_report``) populated by
    :func:`codd.languages.builtin_adapters.ensure_builtin_adapters_registered` — so
    the coverage-execution coherence gate and the language contract resolve the SAME
    adapters: ``vitest-json`` / ``go-test-json`` and the compiler-language
    ``surefire-xml`` / ``dotnet-trx`` / ``ctest-junit`` (and the stack
    ``playwright_json``). The lookup is data-driven on the ``report_format`` string
    key — no language-name branch.

    ``None`` (an unknown / not-yet-implemented format such as ``pytest-junit-xml``)
    makes the gate degrade EXPLICITLY — it surfaces "this stack's campaign report has
    no adapter" rather than silently passing a build whose executions it cannot read.
    """

    if not report_format:
        return None
    key = str(report_format).strip().lower()
    if not key:
        return None
    registry, kind = _runner_report_registry()
    return registry.get(kind, key)


def supported_runner_report_formats() -> list[str]:
    """Report formats with a registered runner adapter (deterministic order).

    Enumerated from the SAME registry :func:`resolve_runner_report_adapter` resolves
    against, so the advertised-supported set cannot drift from what actually resolves.
    """
    registry, kind = _runner_report_registry()
    return registry.ids(kind)


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


def _norm_vb_key(vb_id: str) -> str:
    """The audit's canonical VB id (so the per-case map keys match a row's id)."""
    from codd.verifiable_behavior_audit import _normalize_vb_id

    return _normalize_vb_id(vb_id)


def _authentic_cover_case_keys(
    project_root: Path,
    *,
    config: dict[str, Any] | None,
    profile: Any,
    vb_audit: VBAuditReport,
    authenticity: AuthenticityReport,
) -> dict[str, set[str]] | None:
    """Per-VB ``{normalized_vb_id → {"<relfile>::TestFunc", ...}}`` covering case keys.

    Returns ``None`` (⇒ the gate uses FILE-level reconciliation, unchanged) UNLESS
    this stack supports per-test-case reconciliation: its runner adapter declares
    ``produces_test_case_identity()`` True (Go) AND its test-block profile parses
    blocks whose label is the runner-case identity (``TestFunc``). For every AUTHENTIC
    ``codd: covers vb=`` marker, the covering case key is ``"<relfile>::<top-level
    TestFunc>"`` — the file the marker sits in plus the top-level test function of the
    block it attaches to, using the SAME ``_attached_block`` attachment the
    authenticity gate uses (so static coverage and execution reconciliation agree).
    A marker that does not attach to an executable block, or whose block is
    inauthentic for this VB, contributes NO key (it is not a credible covering case).

    This is the static half of the ``(Package, Test)``→case-key join the Go runner
    adapter performs; matching keys on both sides is what lets a passed ``TestA``
    prove its VB while a sibling ``TestB`` skip in the same file does not drag it down.
    """

    adapter_getter = getattr(profile, "runner_report_adapter", None) if profile is not None else None
    adapter = adapter_getter() if callable(adapter_getter) else None
    if adapter is None or not _adapter_produces_case_identity(adapter):
        return None
    block_getter = getattr(profile, "test_block_profile", None)
    test_block_profile = block_getter() if callable(block_getter) else None
    if test_block_profile is None:
        return None

    from codd.vb_marker_authenticity import _attached_block, _scan_cover_markers_with_lines

    # Authentic-marker filter: a (path, vb) flagged inauthentic (skipped/unattached/
    # no-assertion/orphan) contributes no covering case — same files-drop discipline
    # as ``_authentic_cover_files`` but at marker granularity.
    inauthentic: set[tuple[str, str]] = {
        (_norm_test_path(v.path), _norm_vb_key(v.vb_id))
        for v in authenticity.violations
        if v.kind != "hook"
    }

    keys_by_vb: dict[str, set[str]] = {}
    for path in _iter_test_files(project_root, test_dirs=_resolve_vb_scan_dirs(project_root, config)):
        rel = _norm_test_path(_rel_path(path, project_root))
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        markers = _scan_cover_markers_with_lines(text, rel)
        if not markers:
            continue
        if not test_block_profile.handles_file(rel):
            continue
        try:
            blocks = test_block_profile.parse_test_blocks(text)
        except Exception:  # noqa: BLE001 — an unparseable file contributes no keys.
            blocks = []
        if not blocks:
            continue
        rel_dir = rel.rsplit("/", 1)[0] if "/" in rel else ""
        for marker in markers:
            vb_key = _norm_vb_key(marker.vb_id)
            if (rel, vb_key) in inauthentic:
                continue  # this VB's marker here is not credible coverage
            block = _attached_block(marker.line, text, blocks)
            if block is None or not block.is_executable:
                continue
            test_func = (block.label or "").split("/", 1)[0].strip()
            if not test_func:
                continue
            # The runner-case key the Go adapter emits is module-relative-dir + func;
            # here the relfile already carries the dir, so the key is rel-file based —
            # we key by ``<relfile>::TestFunc`` (the adapter writes the SAME, having
            # resolved (Package, Test) back to relfile via the static index).
            keys_by_vb.setdefault(vb_key, set()).add(f"{rel}::{test_func}")
    return keys_by_vb


def _adapter_produces_case_identity(adapter: Any) -> bool:
    """Whether ``adapter`` declares per-test-case identity (best-effort, default False).

    A runner adapter opts into per-case reconciliation by defining
    ``produces_test_case_identity() -> True`` (the Go adapter). vitest/pytest do not
    define it ⇒ False ⇒ they keep the file-level branch (byte-identical)."""
    getter = getattr(adapter, "produces_test_case_identity", None)
    if not callable(getter):
        return False
    try:
        return bool(getter())
    except Exception:  # noqa: BLE001
        return False


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

    # PER-CASE reconciliation capability (GPT-5.5 design, dogfood go-test-json):
    # a runner whose adapter emits per-test-case identities AND a static profile that
    # can map a VB marker to the SAME runner-case key reconciles a VB by its covering
    # (file, test-case) — NOT by file status. This is REQUIRED for Go, where many
    # independent ``func TestXxx`` share one ``_test.go`` (file-level status would
    # false-RED a passed VB whose sibling skipped). It is OFF for vitest/pytest (their
    # adapters expose no per-case identity), which keep the byte-identical file branch
    # below. The static covering case keys reuse the SAME marker→block attachment the
    # authenticity gate uses (no re-invented attachment ⇒ coverage + execution agree).
    case_keys_by_vb = _authentic_cover_case_keys(
        project_root, config=config, profile=profile, vb_audit=vb_audit, authenticity=authenticity
    )
    case_reconciliation = (
        case_keys_by_vb is not None
        and execution is not None
        and execution.test_level_available
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

        if case_reconciliation:
            # VB verified iff ANY authentic covering (file, top-level TestFunc) case
            # key passed in the runner report. A skipped/failed/missing covering case
            # never appears in ``executed_passed_cases`` ⇒ correctly unverified; a
            # sibling test's outcome in the same file is irrelevant (the false-RED the
            # file branch would cause for Go is avoided).
            covering_cases = case_keys_by_vb.get(_norm_vb_key(row.vb_id), set())
            assert execution is not None  # case_reconciliation implies execution
            passed = sorted(k for k in covering_cases if k in execution.executed_passed_cases)
            if passed:
                verified_count += 1
                continue
            # Distinguish "ran but failed/skipped" from "never ran" for the message: a
            # covering file that the runner included (failed/skipped) → failed; else
            # never ran. (Falls back to file-level inclusion for the reason only.)
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
                    covering_files=tuple(sorted(covering_cases) or sorted(authentic_files)),
                    reason=reason,
                )
            )
            continue

        # FILE-level reconciliation (vitest/pytest — UNCHANGED, byte-identical).
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
class CampaignReportRun:
    """Outcome of clearing + producing + parsing ONE declared report artifact.

    ``execution`` is ``None`` when the report is ``optional`` AND contributed no
    evidence (absent, or present-but-unparseable — see :func:`run_verify_campaign`);
    a non-``optional`` report always has an ``execution`` when a
    :class:`CampaignReportRun` for it exists at all (any other outcome raises
    :class:`CampaignError` before one is constructed).
    """

    spec: Any  # a project_types.CampaignReportSpec (duck-typed — see module docstring)
    report_path: Path
    produced: bool
    execution: RunnerExecution | None


@dataclass
class CampaignStepRun:
    """Outcome of running ONE campaign step (one command invocation)."""

    command: str
    exit_code: int
    output_tail: str
    reports: tuple[CampaignReportRun, ...]


@dataclass
class CampaignRun:
    """Outcome of executing a verify campaign (one or more steps).

    Generalized (2026-07-02) from one command/one report to N steps/N reports;
    every field below keeps its ORIGINAL meaning for a single-step/single-report
    campaign (every profile before this generalization), so existing call sites
    are unaffected: ``command`` is that one step's display string, ``exit_code``
    is that one step's exit code, ``report_path`` is that one report's path, and
    ``execution`` is that one report's parsed evidence — unchanged, byte-for-byte.
    For a multi-step/multi-report campaign, ``command``/``output_tail`` join every
    step's in order, ``exit_code`` is the WORST across steps (the first non-zero,
    else 0), ``report_path`` is the FIRST declared report's path, and
    ``execution`` is the MERGED evidence (see :func:`run_verify_campaign`) — the
    per-step/per-report detail is always available via ``steps``.
    """

    command: str
    exit_code: int
    report_path: Path
    execution: RunnerExecution
    output_tail: str = ""
    #: Per-step, per-report detail (always populated, even for a single-step
    #: campaign — a uniform introspection point regardless of campaign shape).
    steps: tuple[CampaignStepRun, ...] = ()


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


def _report_produced(report_path: Path) -> bool:
    """"Produced a report" is a filesystem-shape question, not a language one: a
    file must be non-empty; a directory (Maven Surefire's one-file-per-class
    convention) must contain at least one entry. Either empty case is
    indistinguishable from "wrote nothing" and must fail exactly like a missing
    path — never a silent pass.
    """
    return (report_path.is_file() and report_path.stat().st_size > 0) or (
        report_path.is_dir() and any(report_path.iterdir())
    )


def _clear_stale_report(report_path: Path) -> None:
    """Remove a stale report (file OR directory) before this run — fail-closed.

    A stale report from a prior run must never be mistaken for this run's
    evidence. The report shape is runner-defined, not language-defined: a single
    file (vitest JSON, C#'s ``.trx``) unlinks; a directory of per-class files
    (Maven Surefire's ``target/surefire-reports/``) is removed wholesale so no
    stale sibling file survives into this run's parse.

    An unremovable path (permission-locked, mid-write by another process, ...)
    is an observability hazard in its own right — "we cannot guarantee what we
    read next is THIS run's evidence" — so it raises :class:`CampaignError`
    (mirroring :mod:`codd.languages.verify_executor`'s identical guard) rather
    than escaping as a raw, uncaught ``OSError``.
    """
    try:
        if report_path.is_dir():
            shutil.rmtree(report_path)
        else:
            report_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise CampaignError(
            f"could not remove stale report at {report_path} before this run: {exc} "
            "(an unremovable stale report is an observability hazard — we cannot "
            "guarantee what we read next is this run's evidence)"
        ) from exc


def run_verify_campaign(
    project_root: Path | str,
    profile: Any,
    *,
    config: dict[str, Any] | None = None,
    echo: Callable[[str], None] = print,
) -> CampaignRun:
    """Execute the profile-owned verify campaign (one or more steps) and parse its
    report(s).

    Generalized (2026-07-02) from "one command → one report" to an ordered
    sequence of :class:`~codd.project_types.VerifyCampaignStep`, each with
    one-or-more report artifacts (Maven: one ``mvn verify`` invocation writing
    BOTH ``surefire-reports/`` and ``failsafe-reports/``), while staying
    BYTE-IDENTICAL in behavior for every single-step/single-report campaign
    (every profile before this generalization). Order of operations (design
    "Anti-false-green integrity for multiple roots"):

      1. CLEAR every declared report path across EVERY step, UP FRONT, before any
         step runs (dir-aware; an unremovable path is a :class:`CampaignError`,
         fail-closed — see :func:`_clear_stale_report`). Doing this up front,
         before running ANY step, means nothing THIS run writes can ever be
         deleted by this run's own cleanup, so overlapping/nested report roots
         across steps are harmless.
      2. RUN each step's command, in order, each under the SAME configured
         timeout (a per-step budget, not a shared/divided one).
      3. For each report a step declares: check it was PRODUCED (see
         :func:`_report_produced`). An absent, non-``optional`` report — or a
         report that IS produced but fails to parse
         (:class:`RunnerReportUnsupported`), regardless of ``optional`` — is a
         :class:`CampaignError`. An ``optional`` report that is absent, OR that
         is present but fails to parse, contributes ZERO evidence (no
         :class:`RunnerExecution`) rather than failing the campaign — the SAME
         tolerance for BOTH shapes is deliberate: Maven's Failsafe plugin
         ALWAYS writes a ``failsafe-summary.xml`` even with zero ``*IT``
         classes (a real, non-empty file whose root is ``<failsafe-summary>``,
         not ``<testsuite>`` — confirmed via a throwaway Maven project during
         this change), so "absent" alone would never actually trigger for a
         unit-only Java project; treating "present but not a real testsuite
         document" the same as "absent" is what makes ``optional: true``
         deliver its stated purpose. This is safety-preserving, not a
         loosened gate: whether an e2e surface that NEEDED this evidence
         actually exists is decided DOWNSTREAM by reconciliation (the
         ``e2e_scan_zero`` observability check + per-VB coherence), which
         fires identically regardless of WHY the evidence is missing.
      4. MERGE every report's execution into ONE :class:`RunnerExecution`:
         ``failed`` = the union of every failed file; ``passed`` = the union of
         every passed file MINUS ``failed`` (taint dominates — the SAME rule
         every adapter already applies WITHIN one report); ``passed_cases`` =
         the union of every passed-case key; ``total_cases`` / ``passed_cases``
         (counts) are summed; ``test_level_available`` is the CONSERVATIVE
         ``all(...)`` across every report that contributed evidence, so a merge
         is trusted for per-case reconciliation only when EVERY contributing
         report supports it.
      5. The SAME zero-evidence guard as before, applied to the MERGED result: a
         campaign whose merged report shows 0 total_cases and 0 executed files
         is a :class:`CampaignError` — an empty-but-present OPTIONAL report can
         never, by itself, trigger this (only a WHOLLY silent campaign can).

    The command's own exit code is the WORST across steps (the first non-zero,
    else 0) and is captured AND consulted by ``enforce_campaign_clean_execution``
    (contract verify.campaign.clean_execution.v1): a non-zero exit from ANY step,
    or ANY failed executed test file, hard-fails there — independent of the
    per-VB coherence gate, which alone would miss a failing test that covers no
    declared VB.

    Caller contract: only invoke when ``profile.verify_campaign`` is not None and
    every declared report's format resolves an adapter (see
    ``certify_verify_campaign_observable``). A None campaign is the caller's
    NO-OP branch (the gate does not apply to that stack).
    """

    project_root = Path(project_root).resolve()
    if config is None:
        config = _load_optional_config(project_root)
    campaign = getattr(profile, "verify_campaign", None)
    if campaign is None:
        raise CampaignError("profile declares no verify_campaign (caller must NO-OP)")
    steps = list(campaign.resolved_steps())
    if not steps:
        raise CampaignError(
            "verify_campaign declares no steps (a campaign with nothing to run "
            "cannot be observed)"
        )

    # Fail fast — BEFORE clearing/running anything — when any declared report's
    # format has no registered adapter (mirrors the single-report check this
    # replaces; generalized to EVERY report across EVERY step).
    unresolved_formats = sorted(
        {
            report.format
            for step in steps
            for report in step.reports
            if resolve_runner_report_adapter(report.format) is None
        }
    )
    if unresolved_formats:
        raise CampaignError(
            f"no runner-report adapter for campaign report format(s) {unresolved_formats!r} "
            "— cannot read this campaign's executions"
        )

    test_root = getattr(profile, "test_root", "tests")
    timeout = _campaign_timeout_seconds(config)

    # (1) Clear EVERY declared report path across EVERY step, up front (see the
    # docstring's ordering rationale) — before any step runs.
    all_report_paths: list[Path] = []
    for step in steps:
        for report in step.reports:
            report_path = project_root / report.relpath
            report_path.parent.mkdir(parents=True, exist_ok=True)
            _clear_stale_report(report_path)
            all_report_paths.append(report_path)

    # (2)+(3) Run each step, in order; check + parse its report(s).
    step_runs: list[CampaignStepRun] = []
    exit_codes: list[int] = []
    for step in steps:
        # Design A — a step's command is EITHER a shell string (vitest/go:
        # shell=True, ``{test_root}``/``{report}`` substituted) OR an argv list
        # (C#/Java/dotnet: shell=False so an argument with shell metacharacters —
        # ``trx;LogFileName=test.trx`` — is passed VERBATIM, never split by a
        # shell). ``{report}`` (when referenced) resolves against the step's
        # FIRST declared report (see ``VerifyCampaignStep``'s docstring).
        first_report_relpath = step.reports[0].relpath
        use_argv = step.command_argv is not None
        if use_argv:
            argv = step.resolve_argv(test_root=test_root, report_path=first_report_relpath)
            display = shlex.join(argv)
        else:
            shell_command = step.resolve_command(
                test_root=test_root, report_path=first_report_relpath
            )
            display = shell_command
        echo(f"[greenfield] verify: running coverage-execution campaign step — {display}")
        try:
            completed = subprocess.run(
                argv if use_argv else shell_command,
                shell=not use_argv,
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise CampaignError(
                f"verify campaign step timed out after {timeout:g}s: {display}"
            ) from exc

        output_tail = _output_tail(completed.stdout, completed.stderr)
        exit_codes.append(completed.returncode)

        report_runs: list[CampaignReportRun] = []
        for report in step.reports:
            report_path = project_root / report.relpath
            if str(report.capture or "").strip().lower() == "stdout":
                # The command streams its machine-readable report to stdout (the
                # documented extension point for a future stdout-reporting
                # language opting into a multi-report campaign, e.g. a Go-shaped
                # ``go test -json``) — persist it before checking "produced"
                # (mirrors codd.languages.verify_executor's identical mechanism;
                # this generalization closes the gap where the legacy
                # ``_synthesize_verify_campaign`` silently dropped ``capture``).
                try:
                    report_path.write_text(completed.stdout or "", encoding="utf-8")
                except OSError as exc:
                    raise CampaignError(
                        f"could not persist captured stdout to {report_path}: {exc}"
                    ) from exc
            produced = _report_produced(report_path)
            if not produced:
                if report.optional:
                    report_runs.append(
                        CampaignReportRun(
                            spec=report, report_path=report_path, produced=False, execution=None
                        )
                    )
                    continue
                raise CampaignError(
                    f"verify campaign produced no report at {report.relpath} "
                    f"(exit {completed.returncode}): {display}\n{output_tail}"
                )
            adapter = resolve_runner_report_adapter(report.format)
            try:
                execution = adapter.parse(report_path, project_root=project_root)
            except RunnerReportUnsupported as exc:
                if report.optional:
                    # Present but structurally empty/unreadable — tolerated ONLY
                    # for an optional report (see the docstring's Failsafe-
                    # summary example). Contributes zero evidence, never a hard
                    # failure.
                    report_runs.append(
                        CampaignReportRun(
                            spec=report, report_path=report_path, produced=True, execution=None
                        )
                    )
                    continue
                raise CampaignError(str(exc)) from exc
            report_runs.append(
                CampaignReportRun(
                    spec=report, report_path=report_path, produced=True, execution=execution
                )
            )

        step_runs.append(
            CampaignStepRun(
                command=display,
                exit_code=completed.returncode,
                output_tail=output_tail,
                reports=tuple(report_runs),
            )
        )

    # (4) Merge every report's execution into ONE RunnerExecution.
    executions = [r.execution for s in step_runs for r in s.reports if r.execution is not None]
    passed_files: set[str] = set()
    failed_files: set[str] = set()
    passed_case_keys: set[str] = set()
    total_cases = 0
    passed_count = 0
    for ex in executions:
        passed_files |= ex.executed_passed_files
        failed_files |= ex.executed_failed_files
        passed_case_keys |= ex.executed_passed_cases
        total_cases += ex.total_cases
        passed_count += ex.passed_cases
    passed_files -= failed_files  # taint dominates across reports, same as within one
    merged = RunnerExecution(
        executed_passed_files=frozenset(passed_files),
        executed_failed_files=frozenset(failed_files),
        executed_passed_cases=frozenset(passed_case_keys),
        test_level_available=bool(executions) and all(ex.test_level_available for ex in executions),
        total_cases=total_cases,
        passed_cases=passed_count,
    )

    # (5) The SAME zero-evidence guard as before, applied to the MERGED result.
    if merged.total_cases == 0 and not merged.executed_files:
        # The campaign ran but the reports show no executed tests at all — the
        # JS-runner "collected 0 tests" hard-fail, generalized to N reports.
        joined = "; ".join(s.command for s in step_runs)
        raise CampaignError(
            f"verify campaign collected/ran 0 tests across all step(s)/report(s): {joined}"
        )

    worst_exit_code = next((c for c in exit_codes if c != 0), 0)
    return CampaignRun(
        command="; ".join(s.command for s in step_runs),
        exit_code=worst_exit_code,
        report_path=all_report_paths[0],
        execution=merged,
        output_tail="\n".join(s.output_tail for s in step_runs if s.output_tail),
        steps=tuple(step_runs),
    )


def _unresolvable_campaign_report_formats(campaign: Any) -> list[str]:
    """Every declared report format across every resolved step that has NO
    registered runner-report adapter (deterministic order; empty when all
    resolve). Pure, side-effect-free — resolves against the registry only.
    """
    try:
        steps = campaign.resolved_steps()
    except Exception:  # noqa: BLE001 — a malformed campaign resolves to "none observable".
        return ["<unresolvable campaign shape>"]
    return sorted(
        {
            report.format
            for step in steps
            for report in step.reports
            if resolve_runner_report_adapter(report.format) is None
        }
    )


def coherence_gate_applies(profile: Any) -> bool:
    """Whether the coverage-execution coherence gate applies to this stack.

    True only when the profile declares a verify campaign AND EVERY declared
    report format (across every resolved step — see
    :meth:`~codd.project_types.VerifyCampaignSpec.resolved_steps`) resolves a
    runner-report adapter. For a single-report campaign this is exactly the
    original check (``report_format`` resolves); a multi-report campaign
    (Java: Surefire + Failsafe, both ``surefire-xml``) requires ALL of them to
    resolve, not just one. Any other case (no campaign — Python today; or a
    campaign with an unresolvable format) is a NO-OP for the gate — the caller
    skips it (the no-adapter case is surfaced separately as an explicit degrade
    where it matters, never a silent green for a stack that HAS a campaign but
    no reader for part of its evidence).
    """

    campaign = getattr(profile, "verify_campaign", None)
    if campaign is None:
        return False
    try:
        return not _unresolvable_campaign_report_formats(campaign)
    except Exception:  # noqa: BLE001
        return False


def certify_verify_campaign_observable(profile: Any) -> None:
    """HARD GATE (contract verify.campaign.observable.v1; GPT round-2 §3.1).

    A profile that DECLARES a verify campaign but has ANY report whose format
    has no registered adapter cannot have that report's executions read — the
    campaign would run and the coherence gate would silently NO-OP
    (``coherence_gate_applies`` returns False for exactly this state). That is
    an OBSERVABILITY failure, not a pass: a declared-but-partially-unreadable
    campaign must honest-fail, never no-op.

    Raises :class:`CampaignError` listing EVERY unresolvable format (plural —
    generalized from the original single-format check so a multi-report
    campaign's second, third, ... report is held to the SAME fail-closed
    contract as its first) when ``profile.verify_campaign is not None`` and any
    declared report format is unresolvable. A profile with NO campaign (Python
    today) is a legitimate no-op and passes silently; a profile whose campaign's
    reports ALL resolve passes. Deterministic, side-effect-free (it runs no
    command). The caller wires it BEFORE the campaign runs so the failure is
    surfaced even though ``coherence_gate_applies`` would otherwise skip the stack.
    """
    campaign = getattr(profile, "verify_campaign", None)
    if campaign is None:
        return
    unresolved = _unresolvable_campaign_report_formats(campaign)
    if unresolved:
        raise CampaignError(
            "verify campaign is declared but report format(s) "
            f"{unresolved!r} have no registered runner-report adapter — the "
            "campaign's executions cannot be observed, so the coverage-execution "
            "coherence gate would silently NO-OP. An unobservable verification is "
            "not a pass; register an adapter for each format listed (or remove the "
            "campaign from the profile)."
        )


def enforce_campaign_clean_execution(
    execution: RunnerExecution,
    exit_code: int,
    *,
    echo: Callable[[str], None] = print,
) -> None:
    """HARD GATE (contract verify.campaign.clean_execution.v1; GPT round-2 §3.2).

    The verify campaign's OWN result is a green authority IN ITS OWN RIGHT —
    independent of VB reconciliation. ``build_coherence_report`` only checks that
    each UNBLOCKED VB has an authentic covering file that executed and passed; a
    FAILING test that covers NO declared VB (a plain integration / e2e / unit
    test), or a runner that exited NON-ZERO for a non-VB reason, is invisible to it
    and would pass the coherence gate alone — a false-green. This gate closes that
    hole.

    Raises :class:`CoherenceError` when ``execution.executed_failed_files`` is
    non-empty (ANY executed test file had a failing/erroring case) OR
    ``exit_code != 0`` (the runner itself reported failure). Deterministic and
    side-effect-free — it runs no command (the caller already executed the
    campaign). honest-fail: a failing test, or a non-zero runner exit, means the
    build is RED, never silently green.
    """

    failed = sorted(execution.executed_failed_files)
    nonzero_exit = exit_code != 0
    if not failed and not nonzero_exit:
        return
    reasons: list[str] = []
    if failed:
        shown = ", ".join(failed[:10]) + (" …" if len(failed) > 10 else "")
        reasons.append(f"{len(failed)} executed test file(s) FAILED: {shown}")
    if nonzero_exit:
        reasons.append(f"the verify campaign exited non-zero ({exit_code})")
    detail = "; ".join(reasons)
    echo(f"[greenfield] verify: campaign clean-execution gate FAILED — {detail}")
    raise CoherenceError(
        "verify campaign did not execute cleanly: "
        + detail
        + ". A failing test — even one that covers no declared verifiable behavior "
        "— or a non-zero runner exit means the build is RED. The campaign result is "
        "itself a green authority, independent of per-VB reconciliation; fix the "
        "failing test(s) (or the runner error) before the run can be green."
    )


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
    # HARD anti-false-green gate (verify.campaign.clean_execution.v1): the
    # campaign's OWN result gates green BEFORE VB reconciliation — a failing test
    # that covers no declared VB, or a non-zero runner exit, is RED here even
    # though build_coherence_report (which only reconciles UNBLOCKED VBs) would
    # miss it.
    enforce_campaign_clean_execution(run.execution, run.exit_code, echo=echo)
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
    "GoTestJsonReportAdapter",
    "RunnerExecution",
    "RunnerReportAdapter",
    "RunnerReportUnsupported",
    "TestInventory",
    "TestInventoryEntry",
    "UnverifiedVB",
    "VitestJsonReportAdapter",
    "build_coherence_report",
    "build_test_inventory",
    "certify_verify_campaign_observable",
    "coherence_gate_applies",
    "enforce_campaign_clean_execution",
    "enforce_coverage_execution_coherence",
    "format_coherence_feedback",
    "render_coherence_markdown",
    "resolve_runner_report_adapter",
    "run_verify_campaign",
    "supported_runner_report_formats",
]
