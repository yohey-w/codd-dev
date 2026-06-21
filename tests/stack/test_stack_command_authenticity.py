"""Stack command AUTHENTICITY — Contract Kernel v2.77d (STEP 0 characterization).

v2.77c made each composed stack command slot actually RUN (exit-code pass/fail).
v2.77d adds the AUTHENTICITY layer: exit 0 is necessary but NOT sufficient — each
command must prove it DID its job for its KIND. This is the anti-false-green HEART of
Cut B, so anti-false-green is the WHOLE point.

Exit gates (v3_goal_contract_kernel.md §"v2.77d — Command Authenticity"):
  1. ``"build": "true"`` (a shell/script no-op build) is RED;
  2. an empty / no-op command is RED;
  3. a command that exits 0 but observes NO tests is RED;
  4. a SEEDED MUTATION verifies the command observes the SUT (a mutated SUT → the
     test command's report shows a failure → RED).
Plus: a TEST command whose report is required but NOT produced → RED; a TEST command
whose report adapter is missing/unregistered → RED; an HONEST build/test command
(real work, real report, ≥1 test observed) → GREEN; a non-stack project →
byte-identical (no authenticity gate at all).

Exercised BOTH ways: through the real greenfield pipeline / verify CLI entry (the
same harness used by v2.77a/b/c), AND through the pure authenticity classifier +
executor seam where a real subprocess is not feasible (GPT-5.5 Pro consult
2026-06-21: "a fake may fake process execution; a fake must NOT fake classification"
— a passing fake writes a REAL parseable report; a mutation fake writes a REAL
failing report; a no-op fake is rejected by the static no-op gate, never accepted).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from codd.languages.registry import default_registry as LANG
from codd.stack.command_authenticity import (
    BUILD_EXECUTION_POLICY,
    STATIC_EXECUTION_POLICY,
    TEST_REPORT_POLICY,
    StackCommandAuthenticityCode,
    StackCommandAuthenticityError,
    StackCommandObservationKind,
    StackCommandObservationPolicy,
    assert_stack_commands_authentic,
    classify_plan_authenticity,
    classify_stack_command_authenticity,
    is_noop_shell_fragment,
    is_static_noop_command,
    observe_stack_command_report,
    resolve_stack_command_observation_policy,
)
from codd.stack.command_plan import (
    StackCommandPlan,
    StackCommandPlanResult,
    StackCommandSlot,
    StackCommandSlotResult,
    execute_stack_command_plan,
    materialize_stack_command_plan,
    stack_command_evidence_path,
    stack_command_plan,
)
from codd.stack.compose import compose
from codd.stack.profile import (
    AddonProfile,
    CommandSpec,
    FrameworkProfile,
    LayerIdentity,
)
from codd.stack.registry import default_addon_registry, default_framework_registry
from codd.stack.resolve import resolve_stack_from_declaration


# ── helpers: build a slot + a fake exit-0 result without a real subprocess ──

def _slot(slot_id: str, owner: str, argv: tuple[str, ...], **kw) -> StackCommandSlot:
    return StackCommandSlot(slot_id=slot_id, owner=owner, argv=argv, **kw)


def _ok_result(slot: StackCommandSlot) -> StackCommandSlotResult:
    return StackCommandSlotResult(
        slot_id=slot.slot_id,
        owner=slot.owner,
        command_str=slot.command_str,
        spawned=True,
        returncode=0,
        timed_out=False,
    )


def _plan(slot: StackCommandSlot) -> StackCommandPlan:
    return StackCommandPlan(stack_id="t", content_hash="sha256:x", slots=(slot,))


def _passing_pw_report() -> str:
    return json.dumps(
        {
            "suites": [
                {
                    "title": "e2e",
                    "specs": [
                        {
                            "title": "home",
                            "file": "tests/e2e/home.spec.ts",
                            "tests": [
                                {"title": "home", "status": "expected", "results": [{"status": "passed"}]}
                            ],
                        }
                    ],
                }
            ]
        }
    )


def _failing_pw_report() -> str:
    return json.dumps(
        {
            "suites": [
                {
                    "title": "e2e",
                    "specs": [
                        {
                            "title": "home",
                            "file": "tests/e2e/home.spec.ts",
                            "tests": [
                                {"title": "home", "status": "unexpected", "results": [{"status": "failed"}]}
                            ],
                        }
                    ],
                }
            ]
        }
    )


def _zero_test_pw_report() -> str:
    return json.dumps({"suites": []})  # parses fine, zero tests observed


def _write_evidence(slot: StackCommandSlot, project_root: Path, body: str) -> None:
    path = stack_command_evidence_path(slot, project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════
# Exit gate 1 — `"build": "true"` is RED  (incl. the package-script wrapper form)
# ═══════════════════════════════════════════════════════════════════════════

def test_build_true_argv_is_red() -> None:
    """A BUILD slot whose argv IS ``["true"]`` (a shell no-op) → RED (NOOP_ARGV)."""
    slot = _slot("framework_build", "framework:x", ("true",))
    assert is_static_noop_command(slot.argv, Path("."))
    verdict = classify_stack_command_authenticity(
        slot, _ok_result(slot), BUILD_EXECUTION_POLICY, None, static_noop=True
    )
    assert verdict.code is StackCommandAuthenticityCode.NOOP_ARGV
    assert not verdict.ok


def test_build_true_package_script_is_red(tmp_path: Path) -> None:
    """``npm run build`` whose ``scripts.build == "true"`` → RED (the canonical
    ``"build": "true"`` false-green, caught by package-script unwrapping)."""
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"build": "true"}}), encoding="utf-8"
    )
    slot = _slot("framework_build", "framework:x", ("npm", "run", "build"))
    assert is_static_noop_command(slot.argv, tmp_path)
    # A real-work build script is NOT a no-op (the gate is precise, not blanket).
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"build": "next build"}}), encoding="utf-8"
    )
    assert not is_static_noop_command(slot.argv, tmp_path)


def test_build_true_through_real_pipeline_is_red(tmp_path: Path, noop_build_framework) -> None:
    """A framework whose ``framework_build`` is ``["true"]`` drives the REAL pipeline to RED.

    An exit-0 executor is injected so the language ``typecheck``/``verify`` slots pass
    the exit-CODE gate (they cannot really spawn ``tsc``/``vitest`` in CI) — the
    no-op build is then caught STATICALLY by the v2.77d authenticity gate, proving a
    command that EXITS 0 but does no real work is RED through the real pipeline."""
    project = _make_project(tmp_path, stack=_NOOP_BUILD_STACK)
    _write_lock_for(project, _NOOP_BUILD_STACK)

    def exit0(slot: StackCommandSlot, root: Path, *, timeout: float):
        # Honest for non-test slots (real argv from profiles); the no-op build is
        # detected statically regardless of this exit code.
        return StackCommandSlotResult(
            slot_id=slot.slot_id, owner=slot.owner, command_str=slot.command_str,
            spawned=True, returncode=0, timed_out=False,
        )

    result, lines = _run(project, executor=exit0)

    assert result.status == "failed", "a no-op build slot must be RED even though `true` exits 0"
    assert result.failed_stage == "stack_commands"
    assert "authenticity" in (result.error or "").lower()
    assert "framework_build" in (result.error or "")
    assert "NOOP_ARGV" in (result.error or "")


# ═══════════════════════════════════════════════════════════════════════════
# Exit gate 2 — an empty / no-op command is RED
# ═══════════════════════════════════════════════════════════════════════════

def test_empty_and_noop_argv_are_red() -> None:
    for argv in [(), ("",), ("   ",), (":",), ("echo", "done"), ("printf", "x")]:
        slot = _slot("framework_build", "framework:x", argv)
        assert is_static_noop_command(argv, Path(".")), f"{argv} should be no-op"
        verdict = classify_stack_command_authenticity(
            slot, _ok_result(slot), BUILD_EXECUTION_POLICY, None, static_noop=True
        )
        assert verdict.code is StackCommandAuthenticityCode.NOOP_ARGV, argv


def test_noop_shell_fragment_classification() -> None:
    assert is_noop_shell_fragment("true")
    assert is_noop_shell_fragment(":")
    assert is_noop_shell_fragment("echo ok")
    assert is_noop_shell_fragment("true && echo ok")
    assert not is_noop_shell_fragment("echo starting && next build")
    assert not is_noop_shell_fragment("next build")


def test_package_script_without_package_json_is_not_false_red(tmp_path: Path) -> None:
    """``npm run build`` with NO package.json cannot be PROVEN a no-op → NOT flagged
    (false-RED avoidance: the gate only reds a no-op it can actually prove)."""
    assert not is_static_noop_command(("npm", "run", "build"), tmp_path)
    # A real binary build is never no-op even with a package.json present.
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {}}), encoding="utf-8")
    assert not is_static_noop_command(("npx", "next", "build"), tmp_path)


# ═══════════════════════════════════════════════════════════════════════════
# Exit gate 3 — a TEST command that exits 0 but observes NO tests is RED
# ═══════════════════════════════════════════════════════════════════════════

def test_test_command_exit0_zero_tests_is_red(tmp_path: Path) -> None:
    """e2e_test exits 0 but its report observed ZERO tests → ZERO_TESTS RED."""
    slot = _slot(
        "e2e_test", "addon:playwright", ("npx", "playwright", "test", "--reporter=json"),
        report_adapter="playwright_json", report_capture="stdout",
    )
    _write_evidence(slot, tmp_path, _zero_test_pw_report())
    obs = observe_stack_command_report(slot, tmp_path, TEST_REPORT_POLICY)
    assert obs.readable and obs.total_cases == 0
    verdict = classify_stack_command_authenticity(
        slot, _ok_result(slot), TEST_REPORT_POLICY, obs, static_noop=False
    )
    assert verdict.code is StackCommandAuthenticityCode.ZERO_TESTS
    assert not verdict.ok


def test_test_command_no_report_produced_is_red(tmp_path: Path) -> None:
    """e2e_test exits 0 but produced NO report at all → REPORT_MISSING RED."""
    slot = _slot(
        "e2e_test", "addon:playwright", ("npx", "playwright", "test", "--reporter=json"),
        report_adapter="playwright_json", report_capture="stdout",
    )
    # No evidence file written → not produced.
    obs = observe_stack_command_report(slot, tmp_path, TEST_REPORT_POLICY)
    assert not obs.produced
    verdict = classify_stack_command_authenticity(
        slot, _ok_result(slot), TEST_REPORT_POLICY, obs, static_noop=False
    )
    assert verdict.code is StackCommandAuthenticityCode.REPORT_MISSING


def test_test_command_unregistered_adapter_is_red(tmp_path: Path) -> None:
    """A TEST command whose required report adapter is NOT registered → REPORT_UNREADABLE
    (fail-closed; the honest fix is to register the adapter, never to bypass)."""
    slot = _slot(
        "unit_test", "addon:custom", ("run", "tests"),
        report_path="report.json", report_adapter="does-not-exist",
    )
    (tmp_path / "report.json").write_text("{}", encoding="utf-8")
    obs = observe_stack_command_report(slot, tmp_path, TEST_REPORT_POLICY)
    assert obs.produced and not obs.readable
    verdict = classify_stack_command_authenticity(
        slot, _ok_result(slot), TEST_REPORT_POLICY, obs, static_noop=False
    )
    assert verdict.code is StackCommandAuthenticityCode.REPORT_UNREADABLE


def test_report_path_escape_is_red(tmp_path: Path) -> None:
    """A report path that resolves OUTSIDE the project root is fail-closed (never
    trusted/deleted) → REPORT_MISSING (path-safety; an out-of-tree report is not ours)."""
    slot = _slot(
        "unit_test", "addon:custom", ("run", "tests"),
        report_path="../../../etc/passwd", report_adapter="vitest-json",
    )
    obs = observe_stack_command_report(slot, tmp_path, TEST_REPORT_POLICY)
    assert not obs.produced  # path rejected, never read
    verdict = classify_stack_command_authenticity(
        slot, _ok_result(slot), TEST_REPORT_POLICY, obs, static_noop=False
    )
    assert verdict.code is StackCommandAuthenticityCode.REPORT_MISSING


def test_test_command_unreadable_report_is_red(tmp_path: Path) -> None:
    """A produced-but-garbled report → REPORT_UNREADABLE (parse failure, never a pass)."""
    slot = _slot(
        "e2e_test", "addon:playwright", ("npx", "playwright", "test"),
        report_adapter="playwright_json", report_capture="stdout",
    )
    _write_evidence(slot, tmp_path, "not json at all")
    obs = observe_stack_command_report(slot, tmp_path, TEST_REPORT_POLICY)
    assert obs.produced and not obs.readable
    verdict = classify_stack_command_authenticity(
        slot, _ok_result(slot), TEST_REPORT_POLICY, obs, static_noop=False
    )
    assert verdict.code is StackCommandAuthenticityCode.REPORT_UNREADABLE


# ═══════════════════════════════════════════════════════════════════════════
# Exit gate 4 — a SEEDED MUTATION makes the TEST command RED (observes the SUT)
# ═══════════════════════════════════════════════════════════════════════════

def test_seeded_mutation_makes_test_command_red(tmp_path: Path) -> None:
    """A mutated SUT → the runner's report shows a FAILED test file → OBSERVED_TEST_FAILURE.

    This is exit gate 4 the way GPT framed it: the seeded mutation surfaces as a failed
    case in the produced report, so the command is RED *because it observed the SUT*
    (before the exit code is even consulted)."""
    slot = _slot(
        "e2e_test", "addon:playwright", ("npx", "playwright", "test", "--reporter=json"),
        report_adapter="playwright_json", report_capture="stdout",
    )
    _write_evidence(slot, tmp_path, _failing_pw_report())  # the mutated SUT's report
    obs = observe_stack_command_report(slot, tmp_path, TEST_REPORT_POLICY)
    assert obs.executed_failed_files
    verdict = classify_stack_command_authenticity(
        slot, _ok_result(slot), TEST_REPORT_POLICY, obs, static_noop=False
    )
    assert verdict.code is StackCommandAuthenticityCode.OBSERVED_TEST_FAILURE
    assert not verdict.ok


def test_seeded_mutation_through_executor_seam_is_red(tmp_path: Path) -> None:
    """End-to-end via the executor seam: a mutation fake WRITES a failing report (it does
    not merely return nonzero) → the authenticity gate reds at the report observation."""
    slot = _slot(
        "e2e_test", "addon:playwright", ("npx", "playwright", "test", "--reporter=json"),
        report_adapter="playwright_json", report_capture="stdout",
    )
    plan = _plan(slot)

    def mutation_executor(s: StackCommandSlot, root: Path, *, timeout: float):
        _write_evidence(s, root, _failing_pw_report())
        return StackCommandSlotResult(
            slot_id=s.slot_id, owner=s.owner, command_str=s.command_str,
            spawned=True, returncode=1, timed_out=False, detail="exit 1",
        )

    result = execute_stack_command_plan(plan, tmp_path, executor=mutation_executor)
    verdicts = classify_plan_authenticity(plan, result, tmp_path)
    assert verdicts[0].code is StackCommandAuthenticityCode.OBSERVED_TEST_FAILURE


# ═══════════════════════════════════════════════════════════════════════════
# HONEST command → GREEN  (real work, real report, ≥1 test observed)
# ═══════════════════════════════════════════════════════════════════════════

def test_honest_test_command_is_green(tmp_path: Path) -> None:
    """e2e_test that produced a REAL passing report observing ≥1 test → PASS (green)."""
    slot = _slot(
        "e2e_test", "addon:playwright", ("npx", "playwright", "test", "--reporter=json"),
        report_adapter="playwright_json", report_capture="stdout",
    )
    _write_evidence(slot, tmp_path, _passing_pw_report())
    obs = observe_stack_command_report(slot, tmp_path, TEST_REPORT_POLICY)
    assert obs.readable and obs.total_cases == 1 and not obs.executed_failed_files
    verdict = classify_stack_command_authenticity(
        slot, _ok_result(slot), TEST_REPORT_POLICY, obs, static_noop=False
    )
    assert verdict.code is StackCommandAuthenticityCode.PASS
    assert verdict.ok


def test_honest_build_command_is_green() -> None:
    """A non-no-op build that exits 0 → PASS (v2.77d build minimum: real work + exit 0)."""
    slot = _slot("framework_build", "framework:nextjs", ("npx", "next", "build"))
    assert not is_static_noop_command(slot.argv, Path("."))
    verdict = classify_stack_command_authenticity(
        slot, _ok_result(slot), BUILD_EXECUTION_POLICY, None, static_noop=False
    )
    assert verdict.code is StackCommandAuthenticityCode.PASS


def test_honest_static_command_is_green() -> None:
    """A non-no-op typecheck/generate that exits 0 → PASS."""
    slot = _slot("typecheck", "language:typescript", ("npx", "tsc", "--noEmit"))
    verdict = classify_stack_command_authenticity(
        slot, _ok_result(slot), STATIC_EXECUTION_POLICY, None, static_noop=False
    )
    assert verdict.code is StackCommandAuthenticityCode.PASS


# ═══════════════════════════════════════════════════════════════════════════
# Unknown slot with no policy → fail-closed RED (the harness never guesses)
# ═══════════════════════════════════════════════════════════════════════════

def test_unknown_slot_without_policy_is_red() -> None:
    slot = _slot("totally_custom_slot", "addon:custom", ("do", "thing"))
    assert resolve_stack_command_observation_policy("totally_custom_slot") is None
    verdict = classify_stack_command_authenticity(
        slot, _ok_result(slot), None, None, static_noop=False
    )
    assert verdict.code is StackCommandAuthenticityCode.AUTHENTICITY_POLICY_MISSING
    assert not verdict.ok


def test_contract_can_declare_policy_for_custom_slot() -> None:
    """A custom stack profile may DECLARE the observation policy for its own slot id
    (the declarative extension point) — it then classifies under that policy."""
    slot = _slot("custom_build", "addon:custom", ("npx", "build-it"))
    policy = resolve_stack_command_observation_policy(
        "custom_build", contract_policies={"custom_build": BUILD_EXECUTION_POLICY}
    )
    assert policy is BUILD_EXECUTION_POLICY
    verdict = classify_stack_command_authenticity(
        slot, _ok_result(slot), policy, None, static_noop=False
    )
    assert verdict.code is StackCommandAuthenticityCode.PASS


# ═══════════════════════════════════════════════════════════════════════════
# Anti-false-green ORDERING — a missing observation reds even on exit 0
# ═══════════════════════════════════════════════════════════════════════════

def test_report_observation_beats_exit_code_for_test_kind(tmp_path: Path) -> None:
    """exit 0 with zero tests is RED (ZERO_TESTS), NOT PASS — observability beats exit."""
    slot = _slot(
        "e2e_test", "addon:playwright", ("npx", "playwright", "test"),
        report_adapter="playwright_json", report_capture="stdout",
    )
    _write_evidence(slot, tmp_path, _zero_test_pw_report())
    obs = observe_stack_command_report(slot, tmp_path, TEST_REPORT_POLICY)
    verdict = classify_stack_command_authenticity(
        slot, _ok_result(slot), TEST_REPORT_POLICY, obs, static_noop=False
    )
    # exit code was 0, yet the verdict is RED on the observation.
    assert verdict.code is StackCommandAuthenticityCode.ZERO_TESTS


def test_noop_beats_everything(tmp_path: Path) -> None:
    """A no-op argv reds FIRST even if (hypothetically) a report existed."""
    slot = _slot(
        "e2e_test", "addon:playwright", ("true",),
        report_adapter="playwright_json", report_capture="stdout",
    )
    _write_evidence(slot, tmp_path, _passing_pw_report())
    verdict = classify_stack_command_authenticity(
        slot, _ok_result(slot), TEST_REPORT_POLICY, None, static_noop=True
    )
    assert verdict.code is StackCommandAuthenticityCode.NOOP_ARGV


# ═══════════════════════════════════════════════════════════════════════════
# Stale-report prevention — the real executor unlinks stale evidence before running
# ═══════════════════════════════════════════════════════════════════════════

def test_default_executor_unlinks_stale_evidence_then_tees_current(tmp_path: Path) -> None:
    """The real executor removes a stale stdout-evidence file before running and writes
    THIS run's stdout — so a leftover green report can never be read as this run."""
    slot = _slot(
        "e2e_test", "addon:playwright", ("printf", "%s", _passing_pw_report()),
        report_adapter="playwright_json", report_capture="stdout",
    )
    # Seed a STALE passing report from a "prior run".
    _write_evidence(slot, tmp_path, _passing_pw_report())
    plan = _plan(slot)
    # The real executor runs `printf <passing-json>` which RE-writes the same passing
    # report as THIS run's stdout (the point: it is current-run evidence, freshly teed).
    result = execute_stack_command_plan(plan, tmp_path)  # default real executor
    assert result.results[0].spawned
    body = stack_command_evidence_path(slot, tmp_path).read_text(encoding="utf-8")
    assert "tests/e2e/home.spec.ts" in body  # the freshly teed current-run report


def test_stale_report_with_noop_command_is_red(tmp_path: Path) -> None:
    """A no-op command that leaves yesterday's passing report on disk is STILL RED:
    the real executor unlinks the stale evidence before running, and a `true` no-op
    writes nothing → REPORT_MISSING (the stale-report false-green is closed)."""
    slot = _slot(
        "e2e_test", "addon:playwright", ("true",),
        report_adapter="playwright_json", report_capture="stdout",
    )
    _write_evidence(slot, tmp_path, _passing_pw_report())  # stale green report
    plan = _plan(slot)
    result = execute_stack_command_plan(plan, tmp_path)  # default real executor: true
    # The stale passing report was unlinked before running and `true` teed only its
    # (empty) stdout — so the stale green content is GONE, not read as this run.
    body = stack_command_evidence_path(slot, tmp_path).read_text(encoding="utf-8")
    assert "tests/e2e/home.spec.ts" not in body, "stale green report must NOT survive"
    verdicts = classify_plan_authenticity(plan, result, tmp_path)
    # A no-op argv reds first regardless; the stale report could NOT rescue it.
    assert verdicts[0].code is StackCommandAuthenticityCode.NOOP_ARGV


# ═══════════════════════════════════════════════════════════════════════════
# REAL curated stack through the executor seam — honest e2e GREEN, missing report RED
# ═══════════════════════════════════════════════════════════════════════════

def test_curated_stack_authentic_with_honest_executor(tmp_path: Path) -> None:
    """The curated typescript+nextjs+prisma+playwright stack passes authenticity when the
    executor produces honest evidence (e2e writes a real report; build/typecheck/generate
    have real non-no-op argv)."""
    contract = resolve_stack_from_declaration(_VALID_STACK)

    def honest_executor(slot: StackCommandSlot, root: Path, *, timeout: float):
        pol = resolve_stack_command_observation_policy(slot.slot_id)
        if pol is not None and pol.kind is StackCommandObservationKind.TEST_REPORT:
            _write_evidence(slot, root, _passing_pw_report())
        return StackCommandSlotResult(
            slot_id=slot.slot_id, owner=slot.owner, command_str=slot.command_str,
            spawned=True, returncode=0, timed_out=False,
        )

    # Must NOT raise — all slots authentic.
    plan, result = materialize_stack_command_plan(contract, tmp_path, executor=honest_executor)
    assert result.ok


def test_curated_stack_e2e_no_report_raises(tmp_path: Path) -> None:
    """The same curated stack RED when the e2e executor exits 0 but writes NO report
    (the exact false-green v2.77d closes: a green-looking e2e that observed nothing)."""
    contract = resolve_stack_from_declaration(_VALID_STACK)

    def silent_executor(slot: StackCommandSlot, root: Path, *, timeout: float):
        # Exit 0 for everything, but NEVER write the e2e report.
        return StackCommandSlotResult(
            slot_id=slot.slot_id, owner=slot.owner, command_str=slot.command_str,
            spawned=True, returncode=0, timed_out=False,
        )

    with pytest.raises(StackCommandAuthenticityError) as excinfo:
        materialize_stack_command_plan(contract, tmp_path, executor=silent_executor)
    assert "e2e_test" in str(excinfo.value)
    assert "authenticity" in str(excinfo.value).lower()


# ═══════════════════════════════════════════════════════════════════════════
# Non-stack project → byte-identical (no authenticity gate at all)
# ═══════════════════════════════════════════════════════════════════════════

def test_no_stack_block_has_no_authenticity_gate(tmp_path: Path) -> None:
    """A project with no ``stack:`` block never reaches the authenticity gate — no plan,
    no execution, no new trace key (byte-identical to the pre-stack behavior)."""
    project = _make_project(tmp_path, stack=None)

    result, lines = _run(project, executor=None)

    assert result.status == "success"
    assert not any("authenticity" in line.lower() for line in lines)
    from codd.greenfield.pipeline import load_session

    session = load_session(project)
    assert "stack_contract" not in session


def test_no_stack_block_verify_has_no_authenticity_gate(tmp_path: Path) -> None:
    """The verify path also never materializes/authenticates without a ``stack:`` block."""
    from codd.cli import _intake_stack_contract_for_verify

    project = _make_project(tmp_path, stack=None)
    _intake_stack_contract_for_verify(project)  # must NOT raise


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures + real-pipeline plumbing (mirror test_stack_command_materialization.py)
# ═══════════════════════════════════════════════════════════════════════════

_VALID_STACK = {
    "language": "typescript",
    "frameworks": ["nextjs"],
    "addons": ["prisma", "playwright"],
}

# A framework whose framework_build is a NO-OP (`true`) — drives the real pipeline to
# an authenticity RED even though `true` exits 0.
_NOOP_BUILD_STACK = {
    "language": "typescript",
    "frameworks": ["noopbuildfw"],
    "addons": [],
}


@pytest.fixture
def noop_build_framework(monkeypatch: pytest.MonkeyPatch):
    bad = FrameworkProfile(
        identity=LayerIdentity(id="noopbuildfw", kind="framework"),
        commands={"framework_build": CommandSpec(id="framework_build", argv=("true",))},
    )
    profiles = dict(default_framework_registry._ensure_loaded())
    profiles["noopbuildfw"] = bad
    monkeypatch.setattr(default_framework_registry, "_profiles", profiles)
    return bad


def _make_project(tmp_path: Path, *, stack: dict | None) -> Path:
    project = tmp_path / "proj"
    codd_dir = project / "codd"
    codd_dir.mkdir(parents=True)
    config: dict = {"project": {"name": "proj", "language": "typescript"}}
    if stack is not None:
        config["stack"] = stack
    (codd_dir / "codd.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return project


def _write_lock_for(project: Path, declaration: dict) -> Path:
    from codd.stack.lock import build_lock, dump_lock, stack_lock_path

    contract = resolve_stack_from_declaration(declaration)
    path = stack_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_lock(build_lock(contract)), encoding="utf-8")
    return path


class _StageStubPipeline:
    """Constructed lazily inside _run to avoid importing the pipeline at module import
    when only the unit-level classifier tests run."""


def _run(project: Path, *, executor) -> tuple[object, list[str]]:
    from codd.greenfield.pipeline import GreenfieldPipeline

    class _StageStub(GreenfieldPipeline):
        def _stage_init(self, p, r, o):  # noqa: ANN001
            r["status"] = "done"

        def _stage_elicit(self, p, r, o):  # noqa: ANN001
            r["status"] = "done"

        def _stage_plan(self, p, r, o):  # noqa: ANN001
            r["status"] = "done"

        def _stage_generate(self, p, r, o):  # noqa: ANN001
            r["status"] = "done"

        def _stage_implement(self, p, r, o):  # noqa: ANN001
            r["status"] = "done"

        def _stage_verify(self, p, r, o):  # noqa: ANN001
            r["status"] = "done"

        def _stage_ci_scaffold(self, p, r, o):  # noqa: ANN001
            r["status"] = "done"

        def _stage_propagate(self, p, r, o):  # noqa: ANN001
            r["status"] = "done"

        def _stage_check(self, p, r, o):  # noqa: ANN001
            r["status"] = "done"

    lines: list[str] = []
    result = _StageStub(echo=lines.append, stack_command_executor=executor).run(project)
    return result, lines
