"""Verify run plan + semantic-class classifier (v2.69a — shadow only).

Builds a verify run plan from the resolved language contract's
``commands["verify"]`` (the declarative verify command: argv/cwd/env/report)
and classifies a verify outcome into one of six semantic classes. This runs in
SHADOW alongside the legacy ``detect_test_command`` path — it does NOT switch
verify behaviour (that is v2.69b). The point of the shadow stage is to prove,
on fixtures and on live runs, that the contract-driven plan classifies outcomes
the SAME way the legacy verifier does (or is intentionally stricter) BEFORE the
switch.

Anti-false-green: the classifier is a PURE mapping of already-computed signals
to a class — it never re-derives the zero-tests / report heuristics itself (the
caller supplies them, reusing the single legacy source), so there is no risk of
a second, drifting copy of the anti-false-green logic. The class ordering puts
every not-green verdict (TOOL_MISSING / timeout-FAIL / CONFIG_ERROR /
ZERO_TESTS / REPORT_MISSING) BEFORE the exit-0 PASS, so a run that exits 0 while
observing nothing is never classified green.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from .contract import ResolvedLanguageContract
from .profile import VerifyObservationPolicy


class VerifyClass(str, Enum):
    """Semantic class of a verify outcome (design §v2.69a / §5).

    ``REPORT_MISSING`` vs ``REPORT_UNREADABLE`` are deliberately distinct (GPT §5,
    "MECE では missing と unreadable は別"): the report being ABSENT is a different
    observability failure from the report being PRESENT but unparseable (a garbled
    JSON/JSONL, or no adapter able to read it). Both are not-green — only ``PASS``
    is green — but keeping them separate lets the executor report the honest reason.

    ``SCOPE_MISSING`` (Contract Kernel Step 5) is the verify that PARSED a clean,
    exit-0 report but did NOT cover a test set it was REQUIRED to cover (a
    ``scope.must_include_test_sets`` entry with zero executed files under its root —
    e.g. only the unit set ran while an e2e set was required). It closes the
    "unit-only PASS" hole: a report that looks green only because it never ran the
    set that matters is not-green, not PASS. Like the others it is NOT green
    (``is_green`` stays PASS-only).
    """

    PASS = "PASS"
    FAIL = "FAIL"
    ZERO_TESTS = "ZERO_TESTS"
    REPORT_MISSING = "REPORT_MISSING"
    REPORT_UNREADABLE = "REPORT_UNREADABLE"
    SCOPE_MISSING = "SCOPE_MISSING"
    TOOL_MISSING = "TOOL_MISSING"
    CONFIG_ERROR = "CONFIG_ERROR"

    @property
    def is_green(self) -> bool:
        return self is VerifyClass.PASS


@dataclass(frozen=True)
class VerifyRunPlan:
    """The contract-derived verify command (declarative, not yet executed)."""

    language_id: str
    argv: tuple[str, ...]
    cwd: str | None
    env: Mapping[str, str]
    report_path: str | None
    report_adapter: str | None
    report_required: bool
    must_include_test_sets: tuple[str, ...]
    #: Resolved ``(id, root)`` for each ``must_include_test_sets`` id that names a
    #: real TestSet in ``contract.profile.layout.test_sets`` — the scope check (Step
    #: 5) needs the set ROOTS, not just ids, to ask "did any executed file fall under
    #: this set's root?". Populated by :func:`build_verify_plan`; ids that match no
    #: TestSet are deliberately dropped here (a profile inconsistency, see there).
    required_test_sets: tuple[tuple[str, str], ...]
    observation: VerifyObservationPolicy
    #: ``report.capture`` from the verify CommandSpec (e.g. Go's ``stdout``). When
    #: ``"stdout"`` the verify command streams its machine-readable report to stdout
    #: and the EXECUTOR must persist that captured stdout to ``report_path`` before
    #: the adapter reads it (``go test -json`` writes no file of its own). ``None``
    #: ⇒ the command produces the report file directly (no executor capture).
    report_capture: str | None = None

    @property
    def command_str(self) -> str:
        return " ".join(self.argv)


def build_verify_plan(contract: ResolvedLanguageContract) -> VerifyRunPlan | None:
    """Derive the verify run plan from ``contract.profile.commands['verify']``.

    Returns ``None`` when the profile declares no verify command (the caller
    stays on the legacy path). A declared ``report.path`` means the command is
    expected to PRODUCE that report — its absence after a run is REPORT_MISSING
    (a not-green outcome), enforced once adapters land (v2.72) / the switch
    happens (v2.69b).
    """
    cmd = contract.profile.commands.get("verify")
    if cmd is None:
        return None
    report = cmd.report
    scope = cmd.scope
    must_include = scope.must_include_test_sets if scope else ()
    # Resolve each must_include id to its TestSet ROOT (the scope check in the
    # executor matches executed_files against roots, so it needs roots, not ids).
    # A must_include id that names NO TestSet in the layout is a PROFILE
    # INCONSISTENCY (a command requires coverage of a set the topology never
    # declares): we keep it simple and just don't add a root for it — but we do
    # NOT drop the id from ``must_include_test_sets`` and we do NOT silently make
    # scope un-checkable for a REAL set; only the unresolvable id is skipped here.
    sets_by_id = {ts.id: ts for ts in contract.profile.layout.test_sets}
    required_test_sets = tuple(
        (set_id, sets_by_id[set_id].root)
        for set_id in must_include
        if set_id in sets_by_id
    )
    return VerifyRunPlan(
        language_id=contract.language_id,
        argv=tuple(cmd.argv),
        cwd=cmd.cwd,
        env=dict(cmd.env),
        report_path=(report.path if report else None),
        report_adapter=(report.adapter if report else None),
        report_required=bool(report and report.path),
        report_capture=(report.capture if report else None),
        must_include_test_sets=must_include,
        required_test_sets=required_test_sets,
        # None on the command ⇒ the strict defaults (the invariant), never weaker.
        observation=(cmd.observation or VerifyObservationPolicy()),
    )


@dataclass(frozen=True)
class VerifyOutcome:
    """Observed signals from running a verify command.

    The signals are computed by the CALLER (reusing the single legacy source for
    the zero-tests heuristic, the report adapter for report presence, etc.) — this
    keeps the classifier a pure mapping with no duplicated anti-false-green logic.
    """

    spawned: bool  # did the tool execute at all? (False ⇒ tool missing / spawn error)
    returncode: int | None = None
    timed_out: bool = False
    zero_tests_observed: bool = False  # runner started but collected/ran 0 tests
    config_error: bool = False  # runner usage/config error (e.g. pytest exit 4)
    report_present: bool | None = None  # None ⇒ no report expected / not yet checked


def classify_verify_outcome(
    outcome: VerifyOutcome, plan: VerifyRunPlan | None = None
) -> VerifyClass:
    """Map an observed verify outcome to a semantic class (pure, anti-false-green).

    Not-green verdicts are checked BEFORE the exit-0 PASS so a command that exits
    0 while observing nothing (zero tests, missing required report) can never be
    classified green.
    """
    if not outcome.spawned:
        return VerifyClass.TOOL_MISSING
    if outcome.timed_out:
        return VerifyClass.FAIL  # a timeout is a failure, never green
    if outcome.config_error:
        return VerifyClass.CONFIG_ERROR
    if outcome.zero_tests_observed:
        return VerifyClass.ZERO_TESTS
    if outcome.returncode == 0:
        if plan is not None and plan.report_required and outcome.report_present is False:
            return VerifyClass.REPORT_MISSING
        return VerifyClass.PASS
    return VerifyClass.FAIL


@dataclass(frozen=True)
class ShadowComparison:
    """Result of comparing the contract verify plan against the legacy command."""

    language_id: str
    legacy_command: str
    profile_command: str | None
    commands_identical: bool
    legacy_class: VerifyClass
    profile_class: VerifyClass
    classes_match: bool
    note: str = ""

    def to_trace(self) -> dict[str, object]:
        return {
            "shadow_language_id": self.language_id,
            "legacy_command": self.legacy_command,
            "profile_command": self.profile_command,
            "commands_identical": self.commands_identical,
            "legacy_verify_class": self.legacy_class.value,
            "profile_verify_class": self.profile_class.value,
            "verify_classes_match": self.classes_match,
            "shadow_note": self.note,
        }


def shadow_compare(
    contract: ResolvedLanguageContract,
    *,
    legacy_command: str,
    legacy_outcome: VerifyOutcome,
) -> ShadowComparison | None:
    """Compare the contract verify plan to the legacy command for one run.

    SHADOW ONLY — never changes behaviour. The legacy outcome's signals are
    reused to classify what the profile plan WOULD report; divergence (or an
    intentionally stricter profile, e.g. Go's ``-json`` report variant) is
    recorded for review before the v2.69b switch.
    """
    plan = build_verify_plan(contract)
    if plan is None:
        return None
    legacy_class = classify_verify_outcome(legacy_outcome, plan=None)
    profile_class = classify_verify_outcome(legacy_outcome, plan=plan)
    identical = plan.command_str == legacy_command.strip()
    note = ""
    if not identical:
        note = (
            f"profile verify command differs from legacy "
            f"(profile={plan.command_str!r} vs legacy={legacy_command!r})"
        )
        if plan.report_required:
            note += "; profile is stricter (declares a required machine-readable report)"
    return ShadowComparison(
        language_id=contract.language_id,
        legacy_command=legacy_command,
        profile_command=plan.command_str,
        commands_identical=identical,
        legacy_class=legacy_class,
        profile_class=profile_class,
        classes_match=(legacy_class is profile_class),
        note=note,
    )
