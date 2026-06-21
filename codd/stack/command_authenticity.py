"""Stack command AUTHENTICITY — Contract Kernel v2.77d (anti-false-green HEART).

v2.77c made each composed stack command slot actually RUN (exit-code pass/fail). But
exit 0 is NECESSARY, NOT SUFFICIENT: a slot that exits 0 without doing real work —
``true`` / ``:`` / ``"build": "true"`` / an empty script / ``echo``-only / a TEST
command that observed zero tests / a TEST command that produced no parseable report —
is a faithful violation, RED, never green. This module is the layer that proves a
slot DID its job for its KIND, mirroring the language verify executor's
anti-false-green ordering (:mod:`codd.languages.verify_executor`) applied to stack
commands.

DESIGN (GPT-5.5 Pro consult 2026-06-21 — "Option C"):

* The executor (:mod:`codd.stack.command_plan`) stays EXIT-CODE-ONLY. This module
  owns observation + classification. The observation is DERIVED HERE from current-run
  evidence on disk — it is NEVER trusted from the executor result (a fake executor
  that wants a GREEN test slot must WRITE a real, parseable report; it cannot just
  assert ``observed_test_count=1``). That keeps the report contract exercised by the
  SAME code path in tests and production.
* The per-kind required-observation taxonomy is DECLARATIVE, keyed by the command's
  SLOT ID (the semantic API of the stack contract), NOT by a framework name. The core
  never branches on ``"nextjs"``/``"playwright"``; it branches on data keyed by
  ``"e2e_test"``/``"framework_build"``/``"typecheck"``. Report presence is used only
  as a CONSISTENCY check, never to weaken a requirement (deriving "kind" from "has a
  report block" is a false-green hole: a TEST command that forgot its report would be
  mis-classified as a build and escape the test-count requirement).
* Unknown slot id with no policy → fail-closed RED (``AUTHENTICITY_POLICY_MISSING``).
  A custom stack profile must DECLARE its observation policy (via the contract's
  ``command_observation_policies``); the harness never guesses.

ORDERING (not-green BEFORE any green; the seeded-mutation gate falls out naturally):
  no-op argv → policy missing → [TEST: report missing → report unreadable (incl.
  missing/unregistered adapter, parse failure, no test-level evidence) → zero tests →
  observed failure] → spawn failed → timed out → exit nonzero → PASS.

STALE-REPORT PREVENTION is the executor's responsibility (it unlinks a stale report
and tees captured stdout to a fresh per-run evidence file BEFORE/AFTER running, mirror
of :func:`codd.languages.verify_executor.execute_verify_plan` steps b+d); this module
only ever READS that current-run evidence. A report path that escapes the project root
is fail-closed (``REPORT_UNREADABLE``), never trusted/deleted.

SCOPE (stay in lane): v2.77d ONLY. NOT v2.77e (the obligation-checker gate
``verify_project_stack``), NOT v2.77f (repair governance). BUILD-kind authenticity is
"reject no-op + exit 0" — artifact-freshness observation is a stricter LATER step
(the ``require_build_outputs`` field exists but is intentionally OFF in v2.77d).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from codd.stack.command_plan import (
    StackCommandPlan,
    StackCommandPlanResult,
    StackCommandSlot,
    StackCommandSlotResult,
)


class StackCommandAuthenticityError(RuntimeError):
    """A materialized stack command slot exited 0 but did NOT prove it did its job.

    Raised by :func:`assert_stack_commands_authentic` (Contract Kernel v2.77d) when a
    slot is a no-op / has no observation policy / a TEST command observed no tests or
    produced no parseable report / an observed test failure. The call-sites (greenfield
    pipeline + verify CLI) translate it to their context's RED, same as
    :class:`codd.stack.command_plan.StackCommandMaterializationError` (exit-code RED) —
    v2.77d is the authenticity layer ON TOP of v2.77c's exit-code layer.
    """


# ── per-kind required-observation taxonomy (declarative; NO framework literal) ──


class StackCommandObservationKind(str, Enum):
    """The authenticity KIND a stack command must satisfy (declarative taxonomy).

    Keyed by slot id below — a command's id (``e2e_test`` / ``framework_build`` /
    ``typecheck``) is the stack contract's semantic API, not a framework name.
    """

    TEST_REPORT = "test_report"  # must OBSERVE >=1 executed test via a parseable report
    BUILD_EXECUTION = "build_execution"  # must do real (non-no-op) build work + exit 0
    STATIC_EXECUTION = "static_execution"  # must really run (non-no-op) + exit 0


@dataclass(frozen=True)
class StackCommandObservationPolicy:
    """What a command of a given kind must OBSERVE to be authentic (unweakenable here).

    The DEFAULTS for each kind are the invariant; a stack profile may STRENGTHEN via
    the contract's ``command_observation_policies`` but never WEAKEN it — strengthen-only
    is enforced by :func:`resolve_stack_command_observation_policy` (a weakening override is
    fail-closed RED), so the classifier never lets a not-green observation become green.
    """

    kind: StackCommandObservationKind
    #: Every kind rejects a static no-op argv (a command that cannot fail is not a
    #: check). Always True in v2.77d.
    reject_static_noop: bool = True

    # TEST_REPORT only ----------------------------------------------------------
    #: A test command MUST produce a parseable machine-readable report.
    report_required: bool = False
    #: Minimum collected/executed tests for a green test command (>=1: a run that
    #: observed zero tests is never green — mirror verify ``min_collected_tests``).
    min_collected_tests: int = 0
    #: The report MUST carry per-test-case granularity (a report that cannot tell us
    #: how many tests ran is REPORT_UNREADABLE, fail-closed, not ZERO_TESTS/PASS).
    require_test_level_available: bool = False
    #: Any observed failed/skipped/flaky test file → RED.
    fail_on_observed_failures: bool = False

    # BUILD_EXECUTION future hook (intentionally OFF in v2.77d) ------------------
    #: Observe the declared build output/artifact was freshly produced. NOT wired as
    #: required in v2.77d (artifact freshness is a stricter later step) — the field
    #: exists so the data model is ready without quietly broadening this release.
    require_build_outputs: bool = False

    def __post_init__(self) -> None:
        # Intrinsic validity (Cut C, GPT-5.5 Pro consult 2026-06-21): a policy may not
        # exist in a state that is unsound FOR ITS KIND — you cannot even CONSTRUCT a
        # no-op-accepting policy or a "test" policy that accepts zero tests / no report.
        # This is defense-in-depth UNDER the resolver's strengthen-only check: a policy
        # object handed around (or fed through the contract override for a custom slot)
        # is invalid-by-construction if it falls below its kind's anti-false-green floor.
        if not self.reject_static_noop:
            raise ValueError(
                "StackCommandObservationPolicy.reject_static_noop must be True for every "
                "kind — a command that cannot fail (true / : / echo / no-op script) is not "
                "a check (anti-false-green: a no-op-accepting policy is forbidden)"
            )
        if self.min_collected_tests < 0:
            raise ValueError("StackCommandObservationPolicy.min_collected_tests must be >= 0")
        if self.kind is StackCommandObservationKind.TEST_REPORT:
            if not self.report_required:
                raise ValueError(
                    "a TEST_REPORT policy must require a report (report_required=True) — a "
                    "test slot without a required machine-readable report is not proof-bearing"
                )
            if self.min_collected_tests < 1:
                raise ValueError(
                    "a TEST_REPORT policy must require >= 1 collected/executed test "
                    "(min_collected_tests >= 1) — a test run that observed zero tests is the "
                    "canonical exit-0-no-tests false-green"
                )
            if not self.require_test_level_available:
                raise ValueError(
                    "a TEST_REPORT policy must require test-level evidence "
                    "(require_test_level_available=True) — a summary-only report cannot prove "
                    "which tests ran"
                )
            if not self.fail_on_observed_failures:
                raise ValueError(
                    "a TEST_REPORT policy must fail on observed failures "
                    "(fail_on_observed_failures=True) — an observed failure/skip is "
                    "incompatible with a green test command"
                )


#: TEST-kind: must observe >=1 executed test via a parseable, test-level report, with
#: no observed failures. This is the anti-false-green invariant for a test command.
TEST_REPORT_POLICY = StackCommandObservationPolicy(
    kind=StackCommandObservationKind.TEST_REPORT,
    reject_static_noop=True,
    report_required=True,
    min_collected_tests=1,
    require_test_level_available=True,
    fail_on_observed_failures=True,
)

#: BUILD-kind: reject no-op + must exit 0 (v2.77d minimum; artifact obs is later).
BUILD_EXECUTION_POLICY = StackCommandObservationPolicy(
    kind=StackCommandObservationKind.BUILD_EXECUTION,
    reject_static_noop=True,
    report_required=False,
    require_build_outputs=False,
)

#: TYPECHECK/GENERATE/LINT-kind: reject no-op + must exit 0 (a static checker that
#: cannot fail is not a check).
STATIC_EXECUTION_POLICY = StackCommandObservationPolicy(
    kind=StackCommandObservationKind.STATIC_EXECUTION,
    reject_static_noop=True,
    report_required=False,
)


# ── strengthen-only ownership (Cut C: a profile may parameterize, never weaken) ──
#
# The contract override path (``command_observation_policies``) is a DECLARATIVE
# extension point: a stack profile may declare a policy for a NEW slot id, or
# STRENGTHEN a default. But "strengthen-only" must be OWNED BY THE CORE, not delegated
# to a docstring — an unconditional override could otherwise silently downgrade a known
# test slot to accept zero tests (the exact false-green the language ``observation``
# block forbids via :meth:`VerifyObservationPolicy.from_mapping`). These two predicates +
# the resolver's fail-closed check are the stack twin of that load-time rejection.


class StackObservationPolicyWeakeningError(ValueError):
    """A contract override would WEAKEN a command's anti-false-green observation policy.

    Raised by :func:`resolve_stack_command_observation_policy` when a stack contract
    declares a ``command_observation_policies`` entry that is LESS strict than the slot's
    built-in default (or, for a custom TEST slot, below the intrinsic test floor). A
    profile may give the invariant's PARAMETERS (raise ``min_collected_tests``) or add a
    policy for a new slot, but it may NEVER disable the invariant — that ownership lives in
    the core, fail-closed (mirrors the language ``observation`` block's load-time
    rejection).
    """


def _intrinsic_floor_for_kind(
    kind: StackCommandObservationKind,
) -> "StackCommandObservationPolicy":
    """The minimum a policy of ``kind`` must satisfy to be anti-false-green, regardless of
    any slot default.

    A TEST_REPORT policy of ANY slot (default or custom) must require a report, observe
    ``>= 1`` test, have per-test granularity, and fail on observed failures — a "test"
    command that accepts zero tests / no report is a false-green by construction. BUILD /
    STATIC floors only reject a static no-op (v2.77d minimum)."""
    if kind is StackCommandObservationKind.TEST_REPORT:
        return TEST_REPORT_POLICY
    if kind is StackCommandObservationKind.BUILD_EXECUTION:
        return BUILD_EXECUTION_POLICY
    return STATIC_EXECUTION_POLICY


def is_at_least_as_strict(
    candidate: "StackCommandObservationPolicy",
    baseline: "StackCommandObservationPolicy",
) -> bool:
    """True iff ``candidate`` is AT LEAST as anti-false-green-strict as ``baseline``.

    The strictness partial order (a profile may move each axis only in the STRICTER
    direction):

    * ``kind`` must be IDENTICAL — a kind change is never a "strengthen" (downgrading
      ``e2e_test`` to STATIC drops the whole test-count requirement; even a notional
      "upgrade" would silently change the contract's meaning, so we require equality and
      let a genuinely-different slot use a different id).
    * every boolean gate (``reject_static_noop``, ``report_required``,
      ``require_test_level_available``, ``fail_on_observed_failures``,
      ``require_build_outputs``) may only go ``False -> True`` or stay — never ``True ->
      False`` (turning a gate OFF is weakening).
    * ``min_collected_tests`` may only RISE or stay (a lower threshold accepts fewer
      tests — weaker).
    """
    if candidate.kind is not baseline.kind:
        return False
    bool_axes = (
        "reject_static_noop",
        "report_required",
        "require_test_level_available",
        "fail_on_observed_failures",
        "require_build_outputs",
    )
    for axis in bool_axes:
        if getattr(baseline, axis) and not getattr(candidate, axis):
            return False  # baseline had the gate ON; candidate turned it OFF — weaker.
    if candidate.min_collected_tests < baseline.min_collected_tests:
        return False
    return True

#: Slot id → required observation policy. DECLARATIVE; slot ids are the contract's
#: semantic API (composer's ``VERIFICATION_SLOTS`` vocabulary + the curated build/
#: codegen slots). NOT framework names. A slot id absent here AND not declared by the
#: contract → fail-closed RED (the harness never guesses an unknown slot's kind).
#: ``MappingProxyType`` (Cut C): the shared default map is IMMUTABLE so imported profile
#: code cannot mutate a default policy in place (e.g. flip ``e2e_test``'s policy to a
#: permissive one) after the core defined it — the strengthen-only ownership would be
#: meaningless if the baseline itself could be rewritten.
DEFAULT_STACK_COMMAND_OBSERVATION_POLICIES: Mapping[str, StackCommandObservationPolicy] = MappingProxyType({
    # TEST-kind — must observe real tests via a report.
    "unit_test": TEST_REPORT_POLICY,
    "integration_test": TEST_REPORT_POLICY,
    "e2e_test": TEST_REPORT_POLICY,
    "eval": TEST_REPORT_POLICY,
    # BUILD-kind — must do real build work + exit 0.
    "build": BUILD_EXECUTION_POLICY,
    "framework_build": BUILD_EXECUTION_POLICY,
    # STATIC-kind — must really run a static checker / codegen + exit 0.
    "typecheck": STATIC_EXECUTION_POLICY,
    "verify": STATIC_EXECUTION_POLICY,
    "lint": STATIC_EXECUTION_POLICY,
    "coverage": STATIC_EXECUTION_POLICY,
    "generate": STATIC_EXECUTION_POLICY,
    "migration_check": STATIC_EXECUTION_POLICY,
    "migration_status": STATIC_EXECUTION_POLICY,
    "migrate_deploy": STATIC_EXECUTION_POLICY,
    "migrate_status": STATIC_EXECUTION_POLICY,
    # Non-verification convenience slots a framework declares but that are NOT a
    # release check (a dev server / start). They still must not be a no-op, but they
    # carry no observation requirement — STATIC_EXECUTION (reject no-op + exit 0).
    "dev": STATIC_EXECUTION_POLICY,
    "start": STATIC_EXECUTION_POLICY,
})


#: The kind partial-order for a strictly-stronger UPGRADE (Contract Kernel v3.x). A kind
#: change is normally NOT a "strengthen" (``is_at_least_as_strict`` requires kind equality —
#: a DOWNGRADE like ``e2e_test`` TEST→STATIC drops the test-count requirement and is RED).
#: But two kind changes ADD requirements without removing any, so they ARE strictly
#: stronger (their floor strictly CONTAINS the baseline's floor):
#:
#:   * STATIC_EXECUTION → TEST_REPORT — TEST keeps STATIC's floor (reject no-op + exit 0)
#:     and ADDS report-required + >=1 test + test-level + fail-on-failure. This is the
#:     GPT-5.5 Pro consult 2026-06-21 "verify (vitest) is semantically a TEST slot" case:
#:     a project may STRENGTHEN its STATIC-default ``verify`` to TEST_REPORT so an
#:     overridden vitest command is judged as a test, not "some static command exited 0".
#:   * STATIC_EXECUTION → BUILD_EXECUTION — BUILD keeps STATIC's floor and is its
#:     anti-false-green peer (reject no-op + exit 0), with the build-artifact hook on top.
#:
#: The reverse of each (TEST→STATIC, BUILD→STATIC, TEST→BUILD, BUILD→TEST) is NOT here —
#: those drop or swap requirements and remain forbidden. This is a NARROW, explicitly
#: enumerated allowance, NOT a general kind reordering: it never permits dropping the
#: test-count/report floor of an already-TEST slot (the e2e-downgrade attack stays RED).
_STRICT_KIND_UPGRADES: frozenset = frozenset(
    {
        (StackCommandObservationKind.STATIC_EXECUTION, StackCommandObservationKind.TEST_REPORT),
        (StackCommandObservationKind.STATIC_EXECUTION, StackCommandObservationKind.BUILD_EXECUTION),
    }
)


def _is_strict_kind_upgrade(
    candidate: "StackCommandObservationPolicy",
    baseline: "StackCommandObservationPolicy",
) -> bool:
    """True iff ``candidate``'s kind is a strictly-stronger UPGRADE of ``baseline``'s kind.

    Provably anti-false-green-safe: an upgrade in :data:`_STRICT_KIND_UPGRADES` only ADDS
    failure conditions (the candidate's intrinsic floor strictly CONTAINS the baseline
    kind's floor), so accepting it can never turn a not-green outcome green — only a green
    outcome red (stricter). The candidate must ALSO meet its OWN kind's intrinsic floor
    (guaranteed for a TEST candidate by ``__post_init__``, which refuses to construct a
    below-floor test policy). Used ONLY by :func:`resolve_stack_command_observation_policy`
    as an alternative to the same-kind :func:`is_at_least_as_strict` — it never relaxes the
    same-kind predicate the conformance partial-order test verifies."""
    if (baseline.kind, candidate.kind) not in _STRICT_KIND_UPGRADES:
        return False
    # The candidate must be at least as strict as its OWN kind's intrinsic floor (a TEST
    # candidate must be a real TEST policy, not a degenerate one — defense in depth under
    # __post_init__).
    return is_at_least_as_strict(candidate, _intrinsic_floor_for_kind(candidate.kind))


def resolve_stack_command_observation_policy(
    slot_id: str,
    *,
    contract_policies: Mapping[str, StackCommandObservationPolicy] | None = None,
) -> StackCommandObservationPolicy | None:
    """Resolve a slot's observation policy: contract override > default > None (RED).

    A contract may DECLARE a policy for a custom slot id (or STRENGTHEN a default); that
    wins. Otherwise the built-in default map. An unknown slot with neither → ``None`` (the
    classifier turns that into ``AUTHENTICITY_POLICY_MISSING`` RED — the harness never
    defaults an unknown slot to a permissive kind).

    Cut C (strengthen-only is CORE-OWNED): a contract override is honored ONLY if it is at
    least as strict as its baseline — the slot's built-in default when one exists, or the
    intrinsic floor for the override's kind for a custom slot. A WEAKENING override raises
    :class:`StackObservationPolicyWeakeningError` (fail-closed) rather than silently
    downgrading the gate. This mirrors the language ``observation`` block, where
    :meth:`VerifyObservationPolicy.from_mapping` rejects every weakening at load: a profile
    may parameterize the invariant but may never disable it.

    Contract Kernel v3.x — a strictly-stronger kind UPGRADE is also honored
    (:func:`_is_strict_kind_upgrade`): a STATIC-default slot (e.g. the TypeScript
    ``verify``/vitest slot) may be STRENGTHENED to TEST_REPORT so a project's overridden
    vitest command is judged as a test (GPT-5.5 Pro consult 2026-06-21). This is a NARROW,
    provably-safe allowance (the upgrade only ADDS requirements); a kind DOWNGRADE (the
    e2e-downgrade attack) stays RED.
    """
    default = DEFAULT_STACK_COMMAND_OBSERVATION_POLICIES.get(slot_id)
    if contract_policies and slot_id in contract_policies:
        override = contract_policies[slot_id]
        # Baseline to compare against: the slot default if the core already protects this
        # slot, else the intrinsic floor for the override's OWN kind (a custom slot must
        # still meet its kind's anti-false-green floor — a "test" slot cannot accept zero
        # tests / no report just because it has a novel id).
        baseline = default if default is not None else _intrinsic_floor_for_kind(override.kind)
        # Honored iff (a) same-kind-at-least-as-strict, OR (b) a strictly-stronger kind
        # upgrade (STATIC -> TEST_REPORT/BUILD). Both can only ADD strictness — neither can
        # turn a not-green outcome green.
        if not (
            is_at_least_as_strict(override, baseline)
            or _is_strict_kind_upgrade(override, baseline)
        ):
            raise StackObservationPolicyWeakeningError(
                f"stack contract command_observation_policies[{slot_id!r}] is WEAKER than "
                f"its baseline ({'slot default' if default is not None else 'intrinsic ' + override.kind.value + ' floor'}): "
                f"override={override!r} baseline={baseline!r}. A profile may STRENGTHEN a "
                "policy (raise min_collected_tests, turn a gate on) or add a policy for a "
                "new slot, but it may NEVER weaken the anti-false-green invariant "
                "(strengthen-only is owned by the core, fail-closed — like the language "
                "observation block's load-time rejection)."
            )
        return override
    return default


# ── static no-op detection (kind-independent; always RED) ──

#: argv[0] basenames that DO NOTHING — a command that cannot fail is not a check.
_NOOP_BINS = frozenset({"true", ":"})
#: argv[0] basenames whose ENTIRE job is to print — ``echo ...`` is not a build/test.
_OUTPUT_ONLY_BINS = frozenset({"echo", "printf"})
#: Package-script runners we unwrap: ``npm run build`` whose script is ``"true"`` is a
#: no-op even though argv[0] is ``npm`` (the canonical ``"build": "true"`` false-green).
_PACKAGE_RUNNERS = frozenset({"npm", "pnpm", "yarn", "bun"})


def is_noop_shell_fragment(fragment: str) -> bool:
    """True iff a shell fragment does no real work (only no-op / output-only segments).

    Splits on ``&&`` / ``;`` / ``||`` / ``|`` and checks every segment: a fragment is a
    no-op only when EVERY segment is empty, ``true``, ``:`` or an ``echo``/``printf``
    command. ``"true && echo ok"`` → no-op; ``"echo starting && next build"`` → NOT a
    no-op (``next build`` is real work). Used to unwrap a package.json script body.
    """
    text = (fragment or "").strip()
    if not text:
        return True
    # Split on the common shell separators; a real command in ANY segment ⇒ not no-op.
    segments: list[str] = [text]
    for sep in ("&&", "||", ";", "|"):
        nxt: list[str] = []
        for seg in segments:
            nxt.extend(seg.split(sep))
        segments = nxt
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        head = seg.split()[0]
        base = Path(head).name
        if base in _NOOP_BINS or base in _OUTPUT_ONLY_BINS:
            continue
        return False  # a real command segment — the fragment does real work.
    return True


def _detect_package_script(argv: tuple[str, ...]) -> str | None:
    """Return the script name for ``<runner> run <script>`` (npm/pnpm/yarn/bun), else None.

    Handles ``npm run build`` / ``pnpm run build`` / ``bun run build`` and yarn's
    ``yarn build`` short form (``yarn <script>`` with no ``run``). Returns None when the
    argv is not a package-script invocation.
    """
    parts = [a for a in argv if a is not None]
    if not parts:
        return None
    runner = Path(parts[0]).name
    if runner not in _PACKAGE_RUNNERS:
        return None
    rest = parts[1:]
    if rest and rest[0] == "run" and len(rest) >= 2:
        return rest[1]
    # yarn allows ``yarn <script>`` (no ``run``); only yarn, and only a bare token.
    if runner == "yarn" and rest and rest[0] not in ("run",) and not rest[0].startswith("-"):
        return rest[0]
    return None


def _read_package_json_script(cwd: Path, script_name: str) -> str | None:
    """Read ``scripts.<script_name>`` from ``cwd/package.json`` (best-effort, or None)."""
    pkg = cwd / "package.json"
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return None
    value = scripts.get(script_name)
    return value if isinstance(value, str) else None


def is_static_noop_command(
    argv: tuple[str, ...], cwd: Path
) -> bool:
    """True iff a command is a static no-op (does no real work) — always RED.

    Catches: empty/whitespace argv; argv[0] is ``true``/``:`` or ``echo``/``printf``;
    AND the package-script-wrapper case (``npm run build`` whose ``scripts.build`` is a
    no-op shell fragment like ``"true"`` — the canonical ``"build": "true"`` false-
    green). ``cwd`` is the directory the package.json is read from. This is a GENERIC
    package-script-unwrapping rule, NOT a framework literal.
    """
    normalized = tuple(a.strip() for a in argv if a is not None and a.strip())
    if not normalized:
        return True
    base = Path(normalized[0]).name
    if base in _NOOP_BINS or base in _OUTPUT_ONLY_BINS:
        return True
    script_name = _detect_package_script(normalized)
    if script_name is not None:
        script = _read_package_json_script(cwd, script_name)
        if script is not None and is_noop_shell_fragment(script):
            return True
    return False


# ── observation (I/O: read CURRENT-RUN evidence; never trust the executor result) ──


@dataclass(frozen=True)
class StackCommandReportObservation:
    """What we OBSERVED from a stack command's current-run report (derived on disk).

    ``required`` mirrors the policy; ``produced``/``readable`` say whether the current-
    run evidence existed and parsed. The counts come from the runner_report adapter
    (:class:`codd.languages.adapters.runner_report.RunnerExecution`). ``None`` counts
    mean "not observed" (no report / unreadable).
    """

    required: bool
    produced: bool
    readable: bool
    adapter_id: str | None = None
    source_path: Path | None = None
    total_cases: int | None = None
    passed_cases: int | None = None
    executed_files: frozenset[str] = frozenset()
    executed_failed_files: frozenset[str] = frozenset()
    test_level_available: bool = False
    unreadable_reason: str | None = None


def _resolve_report_evidence_path(
    slot: StackCommandSlot, project_root: Path
) -> tuple[Path | None, str | None]:
    """Resolve the slot's current-run report evidence path under ``project_root``.

    For a ``capture: stdout`` command the executor tees stdout to a deterministic
    per-slot evidence file (:func:`codd.stack.command_plan.stack_command_evidence_path`);
    otherwise the declared ``report_path`` (relative to the project / command cwd).
    Returns ``(path, None)`` or ``(None, reason)`` when the path is missing or escapes
    the project root (fail-closed — never trust/delete an out-of-tree file).
    """
    from codd.stack.command_plan import stack_command_evidence_path

    capture = (slot.report_capture or "").strip().lower()
    if capture == "stdout":
        return stack_command_evidence_path(slot, project_root), None

    if slot.report_path:
        base = (project_root / slot.cwd) if slot.cwd else project_root
        candidate = (base / slot.report_path)
        try:
            resolved = candidate.resolve()
            root_resolved = project_root.resolve()
            resolved.relative_to(root_resolved)
        except (ValueError, OSError):
            return None, (
                f"report path {slot.report_path!r} resolves outside the project root "
                f"(fail-closed; an out-of-tree report is not trusted)"
            )
        return resolved, None

    return None, "command declares no report path or stdout capture"


def observe_stack_command_report(
    slot: StackCommandSlot,
    project_root: Path,
    policy: StackCommandObservationPolicy,
    *,
    adapter_registry: Any | None = None,
) -> StackCommandReportObservation:
    """Derive a report observation from CURRENT-RUN evidence (no executor trust).

    Resolves the runner_report adapter named by the slot's ``report_adapter`` (the same
    builtin registry the language verify executor uses) and parses the current-run
    report file. A required report with no produced file → ``produced=False``; an
    unregistered/missing adapter or a parse failure → ``readable=False`` with a reason
    (the classifier turns these into REPORT_MISSING / REPORT_UNREADABLE). Pure of the
    executor result — it reads ONLY the evidence the run wrote.
    """
    from codd.languages.builtin_adapters import ensure_builtin_adapters_registered
    from codd.languages.registry import default_adapter_registry

    registry = adapter_registry if adapter_registry is not None else default_adapter_registry
    ensure_builtin_adapters_registered(registry)

    report_path, path_reason = _resolve_report_evidence_path(slot, project_root)
    if report_path is None:
        return StackCommandReportObservation(
            required=policy.report_required,
            produced=False,
            readable=False,
            adapter_id=slot.report_adapter,
            unreadable_reason=path_reason,
        )

    if not report_path.exists():
        return StackCommandReportObservation(
            required=policy.report_required,
            produced=False,
            readable=False,
            adapter_id=slot.report_adapter,
            source_path=report_path,
            unreadable_reason=f"required report not found at {report_path} after the run",
        )

    adapter = None
    if slot.report_adapter is not None:
        adapter = registry.get("runner_report", slot.report_adapter)
    if adapter is None:
        # Required report with no adapter to read it → the harness cannot OBSERVE →
        # fail-closed (mirror verify_executor's required-adapter-missing → not green).
        return StackCommandReportObservation(
            required=policy.report_required,
            produced=True,
            readable=False,
            adapter_id=slot.report_adapter,
            source_path=report_path,
            unreadable_reason=(
                f"required runner_report adapter {slot.report_adapter!r} is not "
                "registered — the report cannot be observed (fail-closed; never green)"
            ),
        )

    try:
        execution = adapter.parse(report_path, project_root=project_root)
    except Exception as exc:  # noqa: BLE001 — ANY parse failure is unreadable, not a pass
        return StackCommandReportObservation(
            required=policy.report_required,
            produced=True,
            readable=False,
            adapter_id=slot.report_adapter,
            source_path=report_path,
            unreadable_reason=(
                f"report at {report_path} present but unparseable by adapter "
                f"{slot.report_adapter!r}: {exc}"
            ),
        )

    return StackCommandReportObservation(
        required=policy.report_required,
        produced=True,
        readable=True,
        adapter_id=slot.report_adapter,
        source_path=report_path,
        total_cases=execution.total_cases,
        passed_cases=execution.passed_cases,
        executed_files=execution.executed_files,
        executed_failed_files=execution.executed_failed_files,
        test_level_available=execution.test_level_available,
    )


# ── classification (PURE: no I/O, no adapter lookup; the ONLY place that says GREEN) ──


class StackCommandAuthenticityCode(str, Enum):
    """The authenticity verdict for one stack command slot. Only ``PASS`` is green."""

    PASS = "PASS"
    NOOP_ARGV = "NOOP_ARGV"
    AUTHENTICITY_POLICY_MISSING = "AUTHENTICITY_POLICY_MISSING"
    REPORT_MISSING = "REPORT_MISSING"
    REPORT_UNREADABLE = "REPORT_UNREADABLE"
    ZERO_TESTS = "ZERO_TESTS"
    OBSERVED_TEST_FAILURE = "OBSERVED_TEST_FAILURE"
    SPAWN_FAILED = "SPAWN_FAILED"
    TIMED_OUT = "TIMED_OUT"
    EXIT_NONZERO = "EXIT_NONZERO"

    @property
    def is_green(self) -> bool:
        return self is StackCommandAuthenticityCode.PASS


@dataclass(frozen=True)
class StackCommandAuthenticityResult:
    """The authenticity verdict for one slot (only ``code is PASS`` is green)."""

    slot_id: str
    owner: str
    code: StackCommandAuthenticityCode
    detail: str

    @property
    def ok(self) -> bool:
        return self.code.is_green


def classify_stack_command_authenticity(
    slot: StackCommandSlot,
    result: StackCommandSlotResult,
    policy: StackCommandObservationPolicy | None,
    observation: StackCommandReportObservation | None,
    *,
    static_noop: bool,
) -> StackCommandAuthenticityResult:
    """Map (slot, exit-result, policy, report observation) → an authenticity verdict.

    PURE: no file I/O, no adapter lookup (the I/O happened in ``is_static_noop_command``
    and ``observe_stack_command_report``). Not-green classifications are ordered BEFORE
    any green so a missing observation reds even on exit 0:

      1. NOOP_ARGV — a command that cannot fail is not a check (kind-independent).
      2. AUTHENTICITY_POLICY_MISSING — unknown slot with no declared policy (fail-closed).
      3. TEST kind: REPORT_MISSING → REPORT_UNREADABLE (incl. missing/unregistered
         adapter, parse failure, no test-level evidence) → ZERO_TESTS → OBSERVED_TEST_FAILURE.
      4. SPAWN_FAILED → TIMED_OUT → EXIT_NONZERO (exit-code observations).
      5. PASS — the ONLY green.

    The seeded-mutation gate "falls out" of step 3: a mutated SUT makes the runner
    write a report with a failed file → OBSERVED_TEST_FAILURE (before the exit code is
    even consulted); if the runner crashes before writing a report → REPORT_MISSING.
    """
    sid, owner = slot.slot_id, slot.owner

    def red(code: StackCommandAuthenticityCode, detail: str) -> StackCommandAuthenticityResult:
        return StackCommandAuthenticityResult(slot_id=sid, owner=owner, code=code, detail=detail)

    # 1. No-op argv — kind-independent, always RED (a command that cannot fail).
    if static_noop:
        return red(
            StackCommandAuthenticityCode.NOOP_ARGV,
            f"command {sid!r} ({owner}) is a static no-op ({slot.command_str!r}) — a "
            "command that does no real work (true / : / echo / a no-op package script) "
            "cannot prove it did its job (faithful violation: false-green forbidden)",
        )

    # 2. Unknown slot with no policy — fail-closed (never default to a permissive kind).
    if policy is None:
        return red(
            StackCommandAuthenticityCode.AUTHENTICITY_POLICY_MISSING,
            f"command slot {sid!r} ({owner}) has no required-observation policy and the "
            "stack contract did not declare one — fail-closed RED (the harness does not "
            "guess an unknown slot's authenticity kind; declare command_observation_policies)",
        )

    # 3. TEST-kind report observations — BEFORE the exit code (report observability
    #    beats exit-code interpretation: exit 0 with no/zero/failed tests is RED).
    if policy.kind is StackCommandObservationKind.TEST_REPORT:
        obs = observation
        if policy.report_required:
            if obs is None or not obs.produced:
                reason = (obs.unreadable_reason if obs else None) or (
                    "no current-run report was produced"
                )
                return red(
                    StackCommandAuthenticityCode.REPORT_MISSING,
                    f"test command {sid!r} ({owner}) requires a machine-readable report "
                    f"but none was produced this run: {reason} (an absent report is a "
                    "not-green observation, never an empty pass)",
                )
            if not obs.readable:
                return red(
                    StackCommandAuthenticityCode.REPORT_UNREADABLE,
                    f"test command {sid!r} ({owner}) produced a report that could not be "
                    f"observed: {obs.unreadable_reason or 'unreadable'} (fail-closed; "
                    "a report the harness cannot read is never a pass)",
                )
            # ZERO_TESTS (precise) BEFORE the test-level-available gate: a report that
            # PARSED but observed zero tests is the canonical exit-0-no-tests false-green,
            # classified ZERO_TESTS (not UNREADABLE). The test-level-available gate then
            # only catches a report that DID collect cases but cannot provide per-case
            # granularity (a summary-only report) — fail-closed, never a silent pass.
            collected = obs.total_cases if obs.total_cases is not None else 0
            if collected < max(policy.min_collected_tests, 1):
                return red(
                    StackCommandAuthenticityCode.ZERO_TESTS,
                    f"test command {sid!r} ({owner}) exited but its report observed "
                    f"{collected} test(s), below the required minimum "
                    f"{max(policy.min_collected_tests, 1)} (a run that observed no tests "
                    "is not an authentic pass — the canonical exit-0-no-tests false-green)",
                )
            if policy.require_test_level_available and not obs.test_level_available:
                return red(
                    StackCommandAuthenticityCode.REPORT_UNREADABLE,
                    f"test command {sid!r} ({owner}) produced a report that collected "
                    f"{collected} case(s) but with no per-test granularity (the adapter "
                    "cannot prove which tests ran) — fail-closed (a summary-only report "
                    "is not authentic test execution evidence)",
                )
            if policy.fail_on_observed_failures and obs.executed_failed_files:
                return red(
                    StackCommandAuthenticityCode.OBSERVED_TEST_FAILURE,
                    f"test command {sid!r} ({owner}) report shows failed/skipped test "
                    f"file(s): {sorted(obs.executed_failed_files)} (a fail or a skip is "
                    "not an authentic pass; this is where a seeded SUT mutation reds)",
                )

    # 4. Exit-code observations (after report observability for test kinds).
    if not result.spawned:
        return red(
            StackCommandAuthenticityCode.SPAWN_FAILED,
            f"command {sid!r} ({owner}) could not spawn: {result.detail or 'spawn failure'}",
        )
    if result.timed_out:
        return red(
            StackCommandAuthenticityCode.TIMED_OUT,
            f"command {sid!r} ({owner}) timed out: {result.detail or 'timeout'} "
            "(a timeout is never green)",
        )
    if result.returncode != 0:
        return red(
            StackCommandAuthenticityCode.EXIT_NONZERO,
            f"command {sid!r} ({owner}) exited {result.returncode} "
            f"({result.detail or 'nonzero exit'}) — a nonzero exit is not a pass",
        )

    # 5. PASS — the only green: not a no-op, policy resolved, (test) report observed
    #    >=1 test with no failures, and exit 0.
    return StackCommandAuthenticityResult(
        slot_id=sid,
        owner=owner,
        code=StackCommandAuthenticityCode.PASS,
        detail=(
            f"command {sid!r} ({owner}) is authentic "
            f"({policy.kind.value}: real work, exit 0"
            + (
                f", {observation.total_cases} test(s) observed"
                if observation is not None and observation.total_cases is not None
                else ""
            )
            + ")"
        ),
    )


def classify_plan_authenticity(
    plan: StackCommandPlan,
    result: StackCommandPlanResult,
    project_root: Path,
    *,
    contract_policies: Mapping[str, StackCommandObservationPolicy] | None = None,
    adapter_registry: Any | None = None,
) -> tuple[StackCommandAuthenticityResult, ...]:
    """Classify every slot's AUTHENTICITY (per-kind required observation), in order.

    For each slot (paired with its exit-code result): resolve the slot's observation
    policy (contract override > default > None=RED), compute static no-op (reads
    package.json for the package-script-wrapper case), observe the current-run report
    (TEST kinds), then PURELY classify. Returns one
    :class:`StackCommandAuthenticityResult` per slot — only ``PASS`` is green.
    """
    by_slot = {r.slot_id: r for r in result.results}
    verdicts: list[StackCommandAuthenticityResult] = []
    for slot in plan.slots:
        slot_result = by_slot.get(slot.slot_id)
        if slot_result is None:
            # A slot in the plan with no execution result is itself a not-green state
            # (the executor must run every slot) — fail-closed, never silently skipped.
            verdicts.append(
                StackCommandAuthenticityResult(
                    slot_id=slot.slot_id,
                    owner=slot.owner,
                    code=StackCommandAuthenticityCode.SPAWN_FAILED,
                    detail=f"slot {slot.slot_id!r} was in the plan but has no execution result",
                )
            )
            continue
        policy = resolve_stack_command_observation_policy(
            slot.slot_id, contract_policies=contract_policies
        )
        cwd = (project_root / slot.cwd) if slot.cwd else project_root
        static_noop = is_static_noop_command(slot.argv, cwd)
        observation: StackCommandReportObservation | None = None
        if (
            policy is not None
            and policy.kind is StackCommandObservationKind.TEST_REPORT
            and not static_noop
        ):
            observation = observe_stack_command_report(
                slot, project_root, policy, adapter_registry=adapter_registry
            )
        verdicts.append(
            classify_stack_command_authenticity(
                slot, slot_result, policy, observation, static_noop=static_noop
            )
        )
    return tuple(verdicts)


def assert_stack_commands_authentic(
    plan: StackCommandPlan,
    result: StackCommandPlanResult,
    project_root: Path,
    *,
    contract_policies: Mapping[str, StackCommandObservationPolicy] | None = None,
    adapter_registry: Any | None = None,
) -> tuple[StackCommandAuthenticityResult, ...]:
    """The v2.77d gate: RED (raise) unless EVERY slot is authentic; return verdicts on GREEN.

    Runs :func:`classify_plan_authenticity` and raises
    :class:`StackCommandAuthenticityError` if any slot is not green (no-op / no policy /
    test observed no tests / unreadable-or-missing report / observed failure / spawn /
    timeout / nonzero) — the anti-false-green HEART of Cut B: a command that exits 0
    without doing real work is RED. On GREEN, returns the per-slot verdicts so the caller
    can record them in the run trace.
    """
    verdicts = classify_plan_authenticity(
        plan,
        result,
        project_root,
        contract_policies=contract_policies,
        adapter_registry=adapter_registry,
    )
    failed = [v for v in verdicts if not v.ok]
    if failed:
        detail = "; ".join(f"{v.slot_id} ({v.owner}) [{v.code.value}]: {v.detail}" for v in failed)
        raise StackCommandAuthenticityError(
            f"stack command authenticity failed ({plan.stack_id}): {detail}. "
            "A materialized stack command exited 0 but did NOT prove it did its job for "
            "its kind (a no-op / observed-no-tests / missing-or-unreadable-report command "
            "is a faithful violation — false-green is forbidden; v2.77d command authenticity)."
        )
    return verdicts


__all__ = [
    "StackCommandAuthenticityError",
    "StackObservationPolicyWeakeningError",
    "classify_plan_authenticity",
    "assert_stack_commands_authentic",
    "StackCommandObservationKind",
    "StackCommandObservationPolicy",
    "TEST_REPORT_POLICY",
    "BUILD_EXECUTION_POLICY",
    "STATIC_EXECUTION_POLICY",
    "DEFAULT_STACK_COMMAND_OBSERVATION_POLICIES",
    "is_at_least_as_strict",
    "resolve_stack_command_observation_policy",
    "_is_strict_kind_upgrade",
    "is_static_noop_command",
    "is_noop_shell_fragment",
    "StackCommandReportObservation",
    "observe_stack_command_report",
    "StackCommandAuthenticityCode",
    "StackCommandAuthenticityResult",
    "classify_stack_command_authenticity",
]
