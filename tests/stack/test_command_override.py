"""Project-level stack command OVERRIDE — Contract Kernel v3.x (anti-false-green-critical).

A project may declare ``stack.command_overrides.<slot>`` in codd.yaml to change WHAT
command runs for an already-composed VERIFICATION slot (run its bespoke CI script,
``npm run test:vitest:ci``) — but NEVER the slot's semantic identity, authenticity kind,
observation policy, scope, report adapter, or obligations. This is a TRANSPORT-ONLY
override (GPT-5.5 Pro consult 2026-06-21).

The cardinal rule is anti-false-green: a GREEN result for an overridden slot STILL
requires the overridden command actually ran, produced current-run authentic evidence per
the core/profile-owned policy, satisfied obligations, and did not alter scope/policy. This
suite proves every weakening vector is RED and the legitimate transport change is honored,
AND that the no-override path is byte-identical (existing locks unchanged).

Exercised at the seams the production pipeline uses:
  * RESOLVE — :func:`codd.stack.resolve.resolve_stack_from_declaration` parses + applies
    the override (the codd.yaml seam).
  * APPLY — :func:`codd.stack.command_override.apply_project_command_overrides` (the pure
    validate+merge).
  * EXECUTE — :func:`codd.stack.command_plan.default_stack_command_executor` (cwd
    containment + stale file-report unlink).
  * AUTHENTICITY — :func:`codd.stack.command_authenticity.assert_stack_commands_authentic`
    (the green authority — an overridden TEST slot still needs a current-run report).
  * OBLIGATIONS — :func:`codd.stack.project.build_obligation_checker_inputs` (keyed
    evidence by slot id).
  * LOCK — :func:`codd.stack.compose._content_hash` (an override drifts the lock).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codd.stack.command_authenticity import (
    StackCommandAuthenticityCode,
    StackCommandAuthenticityError,
    StackCommandObservationKind,
    StackObservationPolicyWeakeningError,
    assert_stack_commands_authentic,
    classify_plan_authenticity,
    resolve_stack_command_observation_policy,
)
from codd.stack.command_override import (
    ProjectCommandOverride,
    StackCommandOverrideError,
    apply_project_command_overrides,
)
from codd.stack.command_plan import (
    StackCommandSlotResult,
    default_stack_command_executor,
    execute_stack_command_plan,
    materialize_stack_command_plan,
    stack_command_plan,
)
from codd.stack.lock import build_lock, verify_lock
from codd.stack.resolve import resolve_stack, resolve_stack_from_declaration

# ── the curated stack the brief targets ──────────────────────────────────────

_BASE = {
    "language": "typescript",
    "frameworks": ["nextjs"],
    "addons": ["prisma", "playwright"],
}


def _with(overrides=None, policies=None) -> dict:
    decl = dict(_BASE)
    if overrides is not None:
        decl["command_overrides"] = overrides
    if policies is not None:
        decl["command_observation_policies"] = policies
    return decl


def _slot(plan, slot_id):
    return next(s for s in plan.slots if s.slot_id == slot_id)


def _ok(slot) -> StackCommandSlotResult:
    return StackCommandSlotResult(
        slot_id=slot.slot_id, owner=slot.owner, command_str=slot.command_str,
        spawned=True, returncode=0, timed_out=False,
    )


def _vitest_report(root: Path, *, passed: bool = True, zero: bool = False) -> str:
    if zero:
        return json.dumps({"testResults": []})
    status = "passed" if passed else "failed"
    return json.dumps(
        {
            "testResults": [
                {
                    "name": str(root / "tests" / "a.test.ts"),
                    "status": status,
                    "assertionResults": [
                        {"status": status, "fullName": "a works"}
                    ],
                }
            ]
        }
    )


def _pw_report(*, passed: bool = True, zero: bool = False) -> str:
    if zero:
        return json.dumps({"suites": []})
    st = "expected" if passed else "unexpected"
    rs = "passed" if passed else "failed"
    return json.dumps(
        {
            "suites": [
                {
                    "title": "e2e",
                    "specs": [
                        {
                            "title": "home",
                            "file": "tests/e2e/home.spec.ts",
                            "tests": [{"title": "home", "status": st, "results": [{"status": rs}]}],
                        }
                    ],
                }
            ]
        }
    )


# ═══════════════════════════════════════════════════════════════════════════
# 1. The override APPLIES — argv / cwd / env / report transport changed
# ═══════════════════════════════════════════════════════════════════════════


def test_override_changes_argv_cwd_env_report_transport() -> None:
    """The legitimate transport change is honored: argv/cwd/env/report.path/capture all
    take the override's value on the resolved slot."""
    c = resolve_stack_from_declaration(
        _with(
            {
                "verify": {
                    "argv": ["npm", "run", "test:vitest:ci"],
                    "cwd": "frontend",
                    "env": {"CI": "1"},
                    "report": {"path": ".codd/verify/vitest.json", "capture": "file"},
                    "reason": "project vitest CI script with zero-skip gate",
                }
            }
        )
    )
    plan = stack_command_plan(c)
    v = _slot(plan, "verify")
    assert v.argv == ("npm", "run", "test:vitest:ci")
    assert v.cwd == "frontend"
    assert dict(v.env) == {"CI": "1"}
    assert v.report_path == ".codd/verify/vitest.json"
    assert v.report_capture == "file"
    # The override RECORD is kept on the contract (observability / trace).
    assert "verify" in c.command_override_records
    assert c.command_override_records["verify"].reason.startswith("project vitest")


def test_override_report_adapter_stays_base_owned() -> None:
    """An override may change the report PATH/CAPTURE but NEVER the adapter (how the report
    is PARSED is a green criterion). The base vitest-json / playwright_json adapters survive
    a transport override."""
    c = resolve_stack_from_declaration(
        _with(
            {
                "verify": {"argv": ["npm", "run", "v"], "report": {"path": "out/v.json", "capture": "file"}},
                "e2e_test": {"argv": ["npm", "run", "e"], "report": {"path": "out/e.json", "capture": "file"}},
            }
        )
    )
    plan = stack_command_plan(c)
    assert _slot(plan, "verify").report_adapter == "vitest-json"  # base-owned
    assert _slot(plan, "e2e_test").report_adapter == "playwright_json"  # base-owned


def test_override_env_is_additive_never_drops_a_base_env_key() -> None:
    """``env`` is ADDITIVE: an override ADDS env on top of the base env and never REMOVES a
    base key (a base key the override repeats is overwritten, but no base key is dropped)."""
    # Build a base contract whose slot carries a base env, then override with a NEW key.
    base = resolve_stack("typescript", ["nextjs"], ["prisma", "playwright"])
    import dataclasses
    from types import MappingProxyType

    cmds = dict(base.commands)
    cmds["verify"] = dataclasses.replace(
        cmds["verify"], env=MappingProxyType({"BASE_ONLY": "keep", "SHARED": "base"})
    )
    base = dataclasses.replace(base, commands=MappingProxyType(cmds))

    out = apply_project_command_overrides(
        base, {"verify": {"argv": ["npm", "run", "v"], "env": {"ADDED": "1", "SHARED": "override"}}}
    )
    env = dict(out.commands["verify"].env)
    assert env["BASE_ONLY"] == "keep"  # base key preserved
    assert env["ADDED"] == "1"  # override key added
    assert env["SHARED"] == "override"  # repeated key overwritten by override


def test_no_override_returns_the_same_contract_object() -> None:
    """A declaration with no ``command_overrides`` returns the contract UNCHANGED (same
    object) — the no-override path does no work (byte-identical guarantee)."""
    c = resolve_stack("typescript", ["nextjs"], ["prisma", "playwright"])
    assert apply_project_command_overrides(c, None) is c
    assert apply_project_command_overrides(c, {}) is c


# ═══════════════════════════════════════════════════════════════════════════
# 2. Forbidden keys — the override may NOT touch any green criterion
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param({"kind": "static_execution"}, id="kind"),
        pytest.param({"observation": {"min_collected_tests": 0}}, id="observation"),
        pytest.param({"policy": "static"}, id="policy"),
        pytest.param({"scope": []}, id="scope"),
        pytest.param({"adapter": "fake_green"}, id="adapter"),
        pytest.param({"report_adapter": "fake"}, id="report_adapter"),
        pytest.param({"obligations": []}, id="obligations"),
        pytest.param({"owner": "addon:evil"}, id="owner"),
        pytest.param({"id": "something_else"}, id="id"),
        pytest.param({"requires_materialized_deps": False}, id="requires_materialized_deps"),
        pytest.param({"min_collected_tests": 0}, id="min_collected_tests"),
    ],
)
def test_forbidden_override_key_is_red(bad) -> None:
    """A project override that declares ANY green-criterion key (kind / observation / scope /
    adapter / obligations / owner / id / …) is fail-closed RED at resolve."""
    decl = _with({"verify": {"argv": ["npm", "run", "v"], **bad}})
    with pytest.raises(StackCommandOverrideError):
        resolve_stack_from_declaration(decl)


def test_forbidden_report_adapter_key_is_red() -> None:
    """The report ``adapter`` (parser) is base-owned — an override that tries to set it
    inside the ``report:`` block is RED (changing the parser is changing a green criterion;
    the GPT ``report: {adapter: fake_green}`` attack)."""
    with pytest.raises(StackCommandOverrideError):
        resolve_stack_from_declaration(
            _with({"e2e_test": {"argv": ["npm", "run", "e"], "report": {"adapter": "fake_green"}}})
        )


def test_unknown_override_key_is_red() -> None:
    """The override schema is CLOSED — an unknown key (a typo, or a future weakening flag)
    is never silently ignored."""
    with pytest.raises(StackCommandOverrideError):
        resolve_stack_from_declaration(_with({"verify": {"argv": ["x"], "skip_report": True}}))


# ═══════════════════════════════════════════════════════════════════════════
# 3. Slot eligibility — only already-composed VERIFICATION slots are overrideable
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "slot_id",
    ["dev", "start", "generate", "migrate_deploy", "migrate_status"],
)
def test_non_verification_slot_is_red(slot_id) -> None:
    """A NON-verification convenience slot (dev/start/generate/migrate) cannot be turned
    into / replaced as a check by an override — RED (the 'turn dev into a release check'
    attack)."""
    with pytest.raises(StackCommandOverrideError):
        resolve_stack_from_declaration(_with({slot_id: {"argv": ["do", "thing"]}}))


def test_unknown_slot_is_red() -> None:
    """An override targeting a slot the stack did not compose is RED (an override cannot
    introduce a brand-new slot)."""
    with pytest.raises(StackCommandOverrideError):
        resolve_stack_from_declaration(_with({"frobnicate": {"argv": ["x"]}}))


def test_override_of_verification_slot_not_in_this_stack_is_red() -> None:
    """``unit_test`` is a VERIFICATION slot id but the curated TS+nextjs+playwright stack
    did not COMPOSE it — overriding it is RED (you can only override a composed slot)."""
    with pytest.raises(StackCommandOverrideError):
        resolve_stack_from_declaration(_with({"unit_test": {"argv": ["npm", "run", "u"]}}))


# ═══════════════════════════════════════════════════════════════════════════
# 4. No-op / shell-wrapper argv — rejected at validation (defense in depth)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["true"], id="true"),
        pytest.param(["echo", "ok"], id="echo_ok"),
        pytest.param([":"], id="colon"),
        pytest.param(["printf", "done"], id="printf"),
        pytest.param(["false"], id="false"),
        pytest.param(["/usr/bin/true"], id="abs_true"),
    ],
)
def test_noop_argv_is_red_at_validation(argv) -> None:
    """A literal no-op / always-fail argv is RED at RESOLVE (a command that cannot fail is
    not a check) — before it ever runs (defense in depth under the runtime authenticity
    no-op gate)."""
    with pytest.raises(StackCommandOverrideError):
        resolve_stack_from_declaration(_with({"verify": {"argv": argv}}))


def test_empty_argv_is_red() -> None:
    with pytest.raises(StackCommandOverrideError):
        resolve_stack_from_declaration(_with({"verify": {"argv": []}}))


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["sh", "-c", "vitest || true"], id="sh_-c"),
        pytest.param(["bash", "-c", "echo ok"], id="bash_-c"),
        pytest.param(["/bin/sh", "-c", "next build"], id="abs_sh_-c"),
    ],
)
def test_direct_shell_wrapper_is_red(argv) -> None:
    """A DIRECT shell wrapper with an inline ``-c`` script is RED (an inline shell script is
    opaque to the no-op/package-script analysis and is the natural carrier for a hidden
    no-op or fake-report writer)."""
    with pytest.raises(StackCommandOverrideError):
        resolve_stack_from_declaration(_with({"verify": {"argv": argv}}))


def test_shell_script_file_is_allowed() -> None:
    """Running a real script FILE (``bash ./scripts/ci.sh``) is fine — only the inline
    ``-c`` form is blocked (we do not ban shells outright)."""
    c = resolve_stack_from_declaration(_with({"verify": {"argv": ["bash", "./scripts/ci.sh"]}}))
    assert _slot(stack_command_plan(c), "verify").argv == ("bash", "./scripts/ci.sh")


def test_argv_as_string_is_red() -> None:
    """``argv`` must be a LIST — a shell string (which would be word-split by a shell, the
    very thing shell=False avoids) is RED."""
    with pytest.raises(StackCommandOverrideError):
        resolve_stack_from_declaration(_with({"verify": {"argv": "npm run test:ci"}}))


# ═══════════════════════════════════════════════════════════════════════════
# 5. Containment — cwd / report.path may not escape the project root
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "cwd",
    [
        pytest.param("../../etc", id="parent_escape"),
        pytest.param("/etc", id="absolute"),
        pytest.param("sub/../../up", id="normalized_escape"),
    ],
)
def test_cwd_outside_root_is_red_at_validation(cwd) -> None:
    """An override ``cwd`` that escapes the project/module root is RED at resolve (an
    override may not cwd outside the root to dodge the real tests)."""
    with pytest.raises(StackCommandOverrideError):
        resolve_stack_from_declaration(_with({"verify": {"argv": ["npm", "run", "v"], "cwd": cwd}}))


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("../../../etc/passwd", id="parent_escape"),
        pytest.param("/etc/passwd", id="absolute"),
    ],
)
def test_report_path_outside_root_is_red_at_validation(path) -> None:
    """An override ``report.path`` that escapes the project root is RED at resolve (an
    override may not write its report outside the tree)."""
    with pytest.raises(StackCommandOverrideError):
        resolve_stack_from_declaration(
            _with({"verify": {"argv": ["npm", "run", "v"], "report": {"path": path}}})
        )


def test_executor_reds_on_cwd_outside_root() -> None:
    """Defense in depth: even if a cwd somehow reached the executor, the EXECUTOR re-checks
    the RESOLVED cwd against the real root and reds (spawned=False)."""
    from codd.stack.command_plan import StackCommandSlot

    slot = StackCommandSlot(slot_id="verify", owner="language:typescript", argv=("echo",), cwd="../../outside")
    res = default_stack_command_executor(slot, Path("/tmp/some/project_root_xyz"), timeout=5)
    assert not res.spawned
    assert "outside" in res.detail.lower()


def test_executor_reds_on_report_path_outside_root(tmp_path: Path) -> None:
    """A file-capture report path that resolves outside the root reds in the executor
    (refusing to clear/own an out-of-tree file)."""
    from codd.stack.command_plan import StackCommandSlot

    slot = StackCommandSlot(
        slot_id="verify", owner="language:typescript", argv=("echo", "hi"),
        report_path="../../../etc/x.json", report_adapter="vitest-json", report_capture="file",
    )
    res = default_stack_command_executor(slot, tmp_path, timeout=5)
    assert not res.spawned
    assert "outside the project root" in res.detail.lower()


# ═══════════════════════════════════════════════════════════════════════════
# 6. An overridden TEST slot is STILL judged as TEST_REPORT (anti-false-green)
# ═══════════════════════════════════════════════════════════════════════════


def _ts_test_stack(*, drop_report=False) -> dict:
    """The brief scenario: vitest ``verify`` strengthened to TEST_REPORT + playwright
    ``e2e_test``, both overridden to the project's CI scripts (file-capture reports)."""
    verify_override: dict = {"argv": ["npm", "run", "test:vitest:ci"]}
    if drop_report:
        verify_override["report"] = None  # explicit "no report" → REPORT_MISSING for a TEST slot
    else:
        verify_override["report"] = {"path": ".codd/verify/vitest.json", "capture": "file"}
    return _with(
        overrides={
            "verify": verify_override,
            "e2e_test": {
                "argv": ["npm", "run", "test:e2e:ci"],
                "required_env": ["DATABASE_URL", "NEXTAUTH_SECRET"],
                "report": {"path": ".codd/verify/playwright.json", "capture": "file"},
            },
        },
        policies={"verify": {"kind": "test_report"}},  # verify (vitest) → TEST_REPORT
    )


def _honest_test_executor(contract, root: Path):
    def honest(slot, r, *, timeout):
        pol = resolve_stack_command_observation_policy(
            slot.slot_id, contract_policies=contract.command_observation_policies
        )
        if pol is not None and pol.kind is StackCommandObservationKind.TEST_REPORT:
            if (slot.report_capture or "").lower() == "file" and slot.report_path:
                p = r / slot.report_path
                p.parent.mkdir(parents=True, exist_ok=True)
                body = _vitest_report(r) if slot.report_adapter == "vitest-json" else _pw_report()
                p.write_text(body, encoding="utf-8")
        return _ok(slot)

    return honest


def test_overridden_verify_is_judged_as_test_report() -> None:
    """The policy strengthening makes the overridden vitest ``verify`` a TEST_REPORT slot
    (report required, >=1 test) — the GPT correction that a project vitest override is a
    test, not 'some static command exited 0'."""
    c = resolve_stack_from_declaration(_ts_test_stack())
    pol = resolve_stack_command_observation_policy(
        "verify", contract_policies=c.command_observation_policies
    )
    assert pol is not None
    assert pol.kind is StackCommandObservationKind.TEST_REPORT
    assert pol.report_required and pol.min_collected_tests >= 1


def test_overridden_test_stack_green_with_honest_evidence(tmp_path: Path) -> None:
    """The fully-overridden vitest+playwright stack GREENS when each overridden TEST slot
    produces a real current-run report — the legitimate end-to-end the brief enables."""
    c = resolve_stack_from_declaration(_ts_test_stack())
    plan, result = materialize_stack_command_plan(
        c, tmp_path, executor=_honest_test_executor(c, tmp_path)
    )
    assert result.ok


def test_overridden_verify_with_no_report_is_red(tmp_path: Path) -> None:
    """An overridden TEST ``verify`` whose command exits 0 but produces NO report → RED
    (report required). The override could drop the report transport, but the slot is still
    a TEST slot and a missing report is RED — the canonical 'green-looking test observed
    nothing' false-green stays closed."""
    c = resolve_stack_from_declaration(_ts_test_stack(drop_report=True))
    with pytest.raises(StackCommandAuthenticityError) as exc:
        # e2e writes its report; verify (report dropped) writes nothing → verify REDs.
        def half_honest(slot, r, *, timeout):
            if slot.slot_id == "e2e_test" and slot.report_path:
                p = r / slot.report_path
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(_pw_report(), encoding="utf-8")
            return _ok(slot)

        materialize_stack_command_plan(c, tmp_path, executor=half_honest)
    assert "verify" in str(exc.value)


def test_overridden_test_slot_zero_tests_is_red(tmp_path: Path) -> None:
    """An overridden TEST slot whose report observed ZERO tests → RED (ZERO_TESTS). Exit 0
    + a parseable report is NOT enough — the run must have OBSERVED a test."""
    c = resolve_stack_from_declaration(_ts_test_stack())

    def zero_test_executor(slot, r, *, timeout):
        pol = resolve_stack_command_observation_policy(
            slot.slot_id, contract_policies=c.command_observation_policies
        )
        if pol is not None and pol.kind is StackCommandObservationKind.TEST_REPORT and slot.report_path:
            p = r / slot.report_path
            p.parent.mkdir(parents=True, exist_ok=True)
            # zero-test report for whichever slot — verify reds first (sorted order).
            body = _vitest_report(r, zero=True) if slot.report_adapter == "vitest-json" else _pw_report(zero=True)
            p.write_text(body, encoding="utf-8")
        return _ok(slot)

    with pytest.raises(StackCommandAuthenticityError) as exc:
        materialize_stack_command_plan(c, tmp_path, executor=zero_test_executor)
    assert "ZERO_TESTS" in str(exc.value)


def test_overridden_test_slot_observed_failure_is_red(tmp_path: Path) -> None:
    """An overridden TEST slot whose report shows a FAILED test → RED (OBSERVED_TEST_FAILURE)
    — the seeded-mutation gate survives the override (the command still observes the SUT)."""
    c = resolve_stack_from_declaration(_ts_test_stack())

    def failing_executor(slot, r, *, timeout):
        pol = resolve_stack_command_observation_policy(
            slot.slot_id, contract_policies=c.command_observation_policies
        )
        if pol is not None and pol.kind is StackCommandObservationKind.TEST_REPORT and slot.report_path:
            p = r / slot.report_path
            p.parent.mkdir(parents=True, exist_ok=True)
            body = _vitest_report(r, passed=False) if slot.report_adapter == "vitest-json" else _pw_report(passed=False)
            p.write_text(body, encoding="utf-8")
        return _ok(slot)

    with pytest.raises(StackCommandAuthenticityError) as exc:
        materialize_stack_command_plan(c, tmp_path, executor=failing_executor)
    assert "OBSERVED_TEST_FAILURE" in str(exc.value)


def test_overridden_test_slot_noop_argv_through_executor_is_red(tmp_path: Path) -> None:
    """Even though the no-op is rejected at resolve, the RUNTIME authenticity no-op gate is
    the authority: a slot whose argv reaches authenticity as a no-op is RED (NOOP_ARGV).
    Built directly (bypassing the resolve-time reject) to prove the runtime gate."""
    from codd.stack.command_plan import StackCommandPlan, StackCommandSlot

    slot = StackCommandSlot(
        slot_id="verify", owner="language:typescript", argv=("true",),
        report_path=".codd/verify/vitest.json", report_adapter="vitest-json", report_capture="file",
    )
    from codd.stack.command_authenticity import TEST_REPORT_POLICY

    plan = StackCommandPlan(stack_id="t", content_hash="sha256:x", slots=(slot,))
    # A stale passing report on disk must NOT rescue a no-op.
    p = tmp_path / ".codd/verify/vitest.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_vitest_report(tmp_path), encoding="utf-8")
    result = execute_stack_command_plan(plan, tmp_path)  # real executor runs `true`
    # verify is treated as a TEST slot here (the brief's overridden-vitest case).
    verdicts = classify_plan_authenticity(
        plan, result, tmp_path, contract_policies={"verify": TEST_REPORT_POLICY}
    )
    # The real executor unlinked the stale file-report before running `true`; `true` writes
    # nothing → and a no-op argv reds first regardless.
    assert verdicts[0].code is StackCommandAuthenticityCode.NOOP_ARGV
    assert not p.exists(), "stale file-report must be unlinked before the no-op runs"


# ═══════════════════════════════════════════════════════════════════════════
# 7. Stale FILE report is unlinked before the run (current-run evidence only)
# ═══════════════════════════════════════════════════════════════════════════


def test_stale_file_report_unlinked_before_run(tmp_path: Path) -> None:
    """The executor unlinks a stale FILE-capture report BEFORE spawning — so a leftover
    green vitest.json from a prior run can never be read as this run (mirror of the
    existing stdout-capture stale-unlink). Contract Kernel v3.x hardening for file reports
    an override can point at."""
    from codd.stack.command_plan import StackCommandPlan, StackCommandSlot

    report = tmp_path / ".codd/verify/vitest.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(_vitest_report(tmp_path), encoding="utf-8")  # STALE green report

    # A `true` command that writes nothing this run.
    slot = StackCommandSlot(
        slot_id="verify", owner="language:typescript", argv=("true",),
        report_path=".codd/verify/vitest.json", report_adapter="vitest-json", report_capture="file",
    )
    plan = StackCommandPlan(stack_id="t", content_hash="sha256:x", slots=(slot,))
    execute_stack_command_plan(plan, tmp_path)
    assert not report.exists(), "stale file report must be unlinked before the run"


def test_stale_file_report_survives_only_if_command_rewrites_it(tmp_path: Path) -> None:
    """If the overridden command DOES rewrite the report this run, the fresh current-run
    evidence is what authenticity reads (the unlink-then-write is current-run-evidence
    transport, not a blanket delete)."""
    from codd.stack.command_plan import StackCommandPlan, StackCommandSlot

    report = tmp_path / ".codd/verify/vitest.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("STALE GARBAGE", encoding="utf-8")

    fresh = _vitest_report(tmp_path)

    def rewriting_executor(slot, r, *, timeout):
        # The default executor already unlinked the stale file; write THIS run's report.
        p = r / slot.report_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(fresh, encoding="utf-8")
        return _ok(slot)

    slot = StackCommandSlot(
        slot_id="verify", owner="language:typescript", argv=("npm", "run", "v"),
        report_path=".codd/verify/vitest.json", report_adapter="vitest-json", report_capture="file",
    )
    plan = StackCommandPlan(stack_id="t", content_hash="sha256:x", slots=(slot,))
    execute_stack_command_plan(plan, tmp_path, executor=rewriting_executor)
    assert report.read_text(encoding="utf-8") == fresh  # current-run evidence, not stale


# ═══════════════════════════════════════════════════════════════════════════
# 8. The override is in the content_hash — it DRIFTS the lock (re-reviewed)
# ═══════════════════════════════════════════════════════════════════════════


def test_override_drifts_the_content_hash() -> None:
    """An override changes the resolved contract ``content_hash`` (so a committed lock
    drifts and is re-reviewed — the locked-override-is-a-reviewed-trust-boundary rule)."""
    base = resolve_stack_from_declaration(_BASE)
    overridden = resolve_stack_from_declaration(
        _with({"verify": {"argv": ["npm", "run", "test:vitest:ci"]}})
    )
    assert base.content_hash != overridden.content_hash


@pytest.mark.parametrize(
    "override",
    [
        pytest.param({"argv": ["npm", "run", "a"]}, id="argv"),
        pytest.param({"argv": ["npm", "run", "v"], "cwd": "frontend"}, id="cwd"),
        pytest.param({"argv": ["npm", "run", "v"], "env": {"CI": "1"}}, id="env"),
        pytest.param({"argv": ["npm", "run", "v"], "report": {"path": "x/y.json", "capture": "file"}}, id="report_path"),
        pytest.param({"argv": ["npm", "run", "v"], "report": {"capture": "stdout"}}, id="report_capture"),
    ],
)
def test_every_transport_axis_drifts_the_lock(override) -> None:
    """EACH transport axis (argv / cwd / env / report.path / report.capture) drifts the
    content hash — none can change silently under a stable lock (GPT: the override must be
    part of the locked contract hash)."""
    base = resolve_stack_from_declaration(_BASE)
    changed = resolve_stack_from_declaration(_with({"verify": override}))
    assert base.content_hash != changed.content_hash, f"{override} did not drift the lock"


def test_override_drift_is_red_against_a_base_lock() -> None:
    """A lock built from the NO-override contract is RED (drift) against the OVERRIDDEN
    contract — the CI lock gate catches an added override."""
    base = resolve_stack_from_declaration(_BASE)
    base_lock = build_lock(base)
    overridden = resolve_stack_from_declaration(
        _with({"verify": {"argv": ["npm", "run", "test:vitest:ci"]}})
    )
    ok, diffs = verify_lock(overridden, base_lock)
    assert not ok
    assert any("resolved_contract_digest" in d for d in diffs)


def test_two_overrides_differing_only_in_argv_have_different_hashes() -> None:
    """The expanded canonicalization actually distinguishes transport: two overrides that
    differ ONLY in argv produce different hashes (a real per-command record, not just a
    set membership)."""
    a = resolve_stack_from_declaration(_with({"verify": {"argv": ["npm", "run", "a"]}}))
    b = resolve_stack_from_declaration(_with({"verify": {"argv": ["npm", "run", "b"]}}))
    assert a.content_hash != b.content_hash


# ═══════════════════════════════════════════════════════════════════════════
# 9. No-override path is BYTE-IDENTICAL (existing locks unchanged)
# ═══════════════════════════════════════════════════════════════════════════


def test_no_override_hash_is_byte_identical_to_direct_resolve() -> None:
    """A no-override declaration resolves to the SAME content_hash as the direct
    ``resolve_stack`` API used everywhere else — the override feature changed nothing for
    the no-override case (existing committed locks stay valid)."""
    direct = resolve_stack("typescript", ["nextjs"], ["prisma", "playwright"])
    declared = resolve_stack_from_declaration(_BASE)
    assert direct.content_hash == declared.content_hash


def test_no_override_contract_has_empty_override_records() -> None:
    c = resolve_stack_from_declaration(_BASE)
    assert dict(c.command_override_records) == {}
    assert dict(c.command_observation_policies) == {}


def test_no_override_lock_verifies_against_itself() -> None:
    """A lock from the no-override contract verifies clean against a freshly-resolved
    no-override contract (no spurious drift introduced by the feature)."""
    c = resolve_stack_from_declaration(_BASE)
    ok, diffs = verify_lock(resolve_stack_from_declaration(_BASE), build_lock(c))
    assert ok, diffs


# ═══════════════════════════════════════════════════════════════════════════
# 10. Obligation evidence is KEYED BY SLOT ID (multi-report stacks)
# ═══════════════════════════════════════════════════════════════════════════


def test_obligation_evidence_keyed_by_slot_for_multi_report_stack(tmp_path: Path) -> None:
    """A stack with BOTH a vitest ``verify`` (TEST) and a playwright ``e2e_test`` (TEST)
    binds report evidence BY SLOT ID — no longer raises 'ambiguous evidence', and the
    Playwright obligation can read ``report_data_by_slot['e2e_test']`` rather than a sibling
    vitest report."""
    from codd.stack.project import build_obligation_checker_inputs

    c = resolve_stack_from_declaration(_ts_test_stack())
    # Write current-run reports for BOTH TEST slots.
    for slot in stack_command_plan(c).slots:
        pol = resolve_stack_command_observation_policy(
            slot.slot_id, contract_policies=c.command_observation_policies
        )
        if pol is not None and pol.kind is StackCommandObservationKind.TEST_REPORT and slot.report_path:
            p = tmp_path / slot.report_path
            p.parent.mkdir(parents=True, exist_ok=True)
            body = _vitest_report(tmp_path) if slot.report_adapter == "vitest-json" else _pw_report()
            p.write_text(body, encoding="utf-8")

    inputs = build_obligation_checker_inputs(c, tmp_path)
    assert set(inputs["report_data_by_slot"].keys()) == {"verify", "e2e_test"}
    # With two report slots, the single backward-compat key is NOT set (ambiguous).
    assert "report_data" not in inputs


def test_single_report_stack_keeps_backward_compat_report_data(tmp_path: Path) -> None:
    """The curated single-e2e stack (no override) still sets the single ``report_data`` key
    for backward compat — every existing checker/test that reads ``report_data`` is
    byte-identical."""
    from codd.stack.command_plan import stack_command_evidence_path
    from codd.stack.project import build_obligation_checker_inputs

    c = resolve_stack_from_declaration(_BASE)  # only e2e_test is a TEST slot
    # e2e uses stdout capture → write the per-slot evidence file.
    e2e = _slot(stack_command_plan(c), "e2e_test")
    ev = stack_command_evidence_path(e2e, tmp_path)
    ev.parent.mkdir(parents=True, exist_ok=True)
    ev.write_text(_pw_report(), encoding="utf-8")

    inputs = build_obligation_checker_inputs(c, tmp_path)
    assert set(inputs["report_data_by_slot"].keys()) == {"e2e_test"}
    assert inputs["report_data"] == inputs["report_data_by_slot"]["e2e_test"]  # backward compat


def test_playwright_checker_reads_its_keyed_slot_not_a_sibling() -> None:
    """The Playwright obligation checker reads ``report_data_by_slot['e2e_test']`` — so a
    multi-report stack binds THIS slot's e2e report, never a sibling vitest ``verify``
    report (anti-false-green: the e2e obligation is judged on the e2e run)."""
    from codd.stack.adapters.playwright import check_executed

    # e2e executed 0 tests but a SIBLING verify report has passes — the checker must read
    # the e2e key (0 → finding), NOT be fooled by the sibling.
    by_slot = {
        "verify": {"stats": {"expected": 5, "unexpected": 0, "flaky": 0, "skipped": 0}},
        "e2e_test": {"stats": {"expected": 0, "unexpected": 0, "flaky": 0, "skipped": 3}},
    }
    findings = check_executed(report_data_by_slot=by_slot)
    assert findings, "e2e with 0 executed must be a finding even when a sibling slot passed"

    # e2e executed >=1 → no finding.
    by_slot["e2e_test"] = {"stats": {"expected": 2, "unexpected": 0, "flaky": 0, "skipped": 0}}
    assert check_executed(report_data_by_slot=by_slot) == []


# ═══════════════════════════════════════════════════════════════════════════
# 11. Policy strengthening vs override are SEPARATE keys (no smuggling)
# ═══════════════════════════════════════════════════════════════════════════


def test_observation_policy_downgrade_is_red_even_with_an_override() -> None:
    """A project cannot smuggle a WEAKER authenticity policy: ``command_observation_policies``
    is strengthen-only and core-owned. A downgrade of a KNOWN test slot (e2e_test → STATIC)
    is RED at resolve, independent of any transport override."""
    decl = _with(
        overrides={"e2e_test": {"argv": ["npm", "run", "e"]}},
        policies={"e2e_test": {"kind": "static_execution"}},  # DOWNGRADE attempt
    )
    with pytest.raises(StackObservationPolicyWeakeningError):
        resolve_stack_from_declaration(decl)


def test_override_cannot_set_kind_even_as_a_transport_key() -> None:
    """The transport override has NO path to the authenticity kind — ``kind`` in the
    override block is a forbidden key (the only kind tuning is the separate strengthen-only
    ``command_observation_policies``)."""
    with pytest.raises(StackCommandOverrideError):
        ProjectCommandOverride.from_mapping("verify", {"argv": ["x"], "kind": "static_execution"})


def test_policy_strengthening_only_does_not_drift_lock_but_changes_judgment() -> None:
    """``command_observation_policies`` shapes the GREEN CRITERIA, not the resolved command
    set — it is NOT in the content_hash (so it does not drift the lock on its own), but it
    DOES change how a slot is judged. (Transport drifts the lock; policy does not — they
    are orthogonal, as designed.)"""
    base = resolve_stack_from_declaration(_BASE)
    policy_only = resolve_stack_from_declaration(_with(policies={"verify": {"kind": "test_report"}}))
    assert base.content_hash == policy_only.content_hash  # policy is not hashed
    # …yet verify is now a TEST slot.
    pol = resolve_stack_command_observation_policy(
        "verify", contract_policies=policy_only.command_observation_policies
    )
    assert pol.kind is StackCommandObservationKind.TEST_REPORT


# ═══════════════════════════════════════════════════════════════════════════
# 12. ProjectCommandOverride.from_mapping unit shape checks
# ═══════════════════════════════════════════════════════════════════════════


def test_from_mapping_requires_argv() -> None:
    with pytest.raises(StackCommandOverrideError):
        ProjectCommandOverride.from_mapping("verify", {"cwd": "x"})


def test_from_mapping_report_capture_must_be_file_or_stdout() -> None:
    with pytest.raises(StackCommandOverrideError):
        ProjectCommandOverride.from_mapping("verify", {"argv": ["x"], "report": {"capture": "syslog"}})


def test_from_mapping_report_null_is_explicit_no_report() -> None:
    """``report: null`` is an explicit 'drop the report' transport (distinguishable from
    'no report key' — which keeps the base report)."""
    ov = ProjectCommandOverride.from_mapping("verify", {"argv": ["x"], "report": None})
    assert ov.report_declared is True
    assert ov.report_path is None and ov.report_capture is None

    ov2 = ProjectCommandOverride.from_mapping("verify", {"argv": ["x"]})
    assert ov2.report_declared is False  # no report key → base report kept on merge


def test_override_without_report_key_keeps_base_report() -> None:
    """An override that omits ``report:`` keeps the base report transport (only argv/env
    changed) — the adapter AND path survive."""
    c = resolve_stack_from_declaration(
        _with(overrides={"verify": {"argv": ["npm", "run", "v"]}})
    )
    v = _slot(stack_command_plan(c), "verify")
    assert v.report_path == ".codd/verify/vitest.json"  # base path kept
    assert v.report_adapter == "vitest-json"  # base adapter kept
