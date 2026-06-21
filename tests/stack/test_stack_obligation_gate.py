"""Stack obligation CHECKER gate — Contract Kernel v2.77e (STEP 0 characterization).

v2.77a brought the framework-stack contract LIVE; v2.77b made the lock a gate; v2.77c
materialized + EXECUTED the composed commands (exit code); v2.77d added command
AUTHENTICITY (exit 0 is not enough). v2.77e turns the framework/addon OBLIGATIONS into a
red/green GATE: the declared obligations are CHECKED (their registered checkers run), not
merely materialized — so a stack obligation actually affects the run's verdict.

This is an ENFORCEMENT step, so anti-false-green is the WHOLE point. Exit gates
(v3_goal_contract_kernel.md §"v2.77e — Stack Obligation Checker Wiring"):
  1. checker seeded negative controls are RED (each error obligation's checker fires on
     its seeded violation — a "semantically-empty" checker that cannot fire is caught
     here, not by runtime introspection);
  2. a valid stack project is GREEN;
  3. checker removal (an error obligation whose checker is unregistered) is RED;
  4. obligation weakening (an addon redefining a base obligation more weakly) is RED.
Plus: missing/disabled/empty checker → RED; a checker that raises / returns None /
returns a malformed value → RED (the ``checker(...) or []`` false-green is closed); a
non-stack project → byte-identical (no obligation gate).

Exercised BOTH ways (mirroring v2.77c/d): through the REAL greenfield pipeline / verify
CLI entry, AND through the pure ``enforce_obligations`` / ``enforce_stack_obligation_gate``
seam. A recording executor (writing a REAL passing Playwright report) is injected so the
curated commands are not really run (CI has no node/npx) while the obligation gate still
sees genuine current-run evidence (GPT-5.5 Pro consult 2026-06-21: consume the SAME
authenticity-blessed evidence; do not re-parse with a divergent parser).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from codd.greenfield.pipeline import GreenfieldPipeline, load_session
from codd.languages.registry import default_registry as LANG
from codd.stack.adapters import OBLIGATION_CHECKERS
from codd.stack.command_plan import (
    StackCommandSlot,
    StackCommandSlotResult,
    stack_command_evidence_path,
)
from codd.stack.compose import compose
from codd.stack.lock import build_lock, dump_lock, stack_lock_path
from codd.stack.obligations import enforce_obligations
from codd.stack.profile import AddonProfile, FrameworkProfile, LayerIdentity, Obligation
from codd.stack.project import (
    StackObligationGateError,
    build_obligation_checker_inputs,
    enforce_stack_obligation_gate,
)
from codd.stack.registry import default_addon_registry, default_framework_registry
from codd.stack.resolve import resolve_stack_from_declaration

# The curated Next.js/Prisma/Playwright stack — composes CLEAN (the valid fixture).
_VALID_STACK = {
    "language": "typescript",
    "frameworks": ["nextjs"],
    "addons": ["prisma", "playwright"],
}


def _curated_contract():
    ts = LANG.resolve("typescript")
    return compose(
        ts,
        [default_framework_registry.resolve("nextjs")],
        [default_addon_registry.resolve("prisma"), default_addon_registry.resolve("playwright")],
    )


# ── recording executor: writes a REAL passing Playwright report (HONEST fake) ──

def _write_passing_playwright_report(slot: StackCommandSlot, project_root: Path) -> None:
    """Write a REAL parseable PASSING Playwright JSON report to the slot's evidence path.

    Mirrors the v2.77c/d test helper: a fake may avoid spawning Playwright but must NOT
    fake classification — it writes the ``suites`` report the canonical adapter parses, so
    the obligation gate (which consumes the canonical-normalized counts) sees >=1 real pass.
    """
    from codd.stack.command_authenticity import (
        StackCommandObservationKind,
        resolve_stack_command_observation_policy,
    )

    policy = resolve_stack_command_observation_policy(slot.slot_id)
    if policy is None or policy.kind is not StackCommandObservationKind.TEST_REPORT:
        return
    if (slot.report_capture or "").strip().lower() != "stdout":
        return
    path = stack_command_evidence_path(slot, project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "suites": [
                    {
                        "title": "e2e",
                        "specs": [
                            {
                                "title": "home page renders",
                                "file": "tests/e2e/home.spec.ts",
                                "tests": [
                                    {
                                        "title": "home page renders",
                                        "status": "expected",
                                        "results": [{"status": "passed"}],
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _write_skipped_playwright_report(slot: StackCommandSlot, project_root: Path) -> None:
    """Write a REAL parseable FULLY-SKIPPED Playwright report (0 executed → obligation RED)."""
    from codd.stack.command_authenticity import (
        StackCommandObservationKind,
        resolve_stack_command_observation_policy,
    )

    policy = resolve_stack_command_observation_policy(slot.slot_id)
    if policy is None or policy.kind is not StackCommandObservationKind.TEST_REPORT:
        return
    if (slot.report_capture or "").strip().lower() != "stdout":
        return
    path = stack_command_evidence_path(slot, project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "suites": [
                    {
                        "title": "e2e",
                        "specs": [
                            {
                                "title": "home page renders",
                                "file": "tests/e2e/home.spec.ts",
                                "tests": [
                                    {
                                        "title": "home page renders",
                                        "status": "skipped",
                                        "results": [{"status": "skipped"}],
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


class _RecordingExecutor:
    """Records invoked slots, passes (exit 0), writes a REAL passing Playwright report."""

    def __init__(self, report_writer=_write_passing_playwright_report) -> None:
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []
        self._report_writer = report_writer

    def __call__(self, slot: StackCommandSlot, project_root: Path, *, timeout: float):
        self.calls.append((slot.slot_id, slot.owner, slot.argv))
        self._report_writer(slot, project_root)
        return StackCommandSlotResult(
            slot_id=slot.slot_id,
            owner=slot.owner,
            command_str=slot.command_str,
            spawned=True,
            returncode=0,
            timed_out=False,
        )


# ── REAL-pipeline harness (stage bodies stubbed; intake→...→obligation gate run) ──

class _StageStubPipeline(GreenfieldPipeline):
    """The REAL pipeline with every STAGE BODY a no-op (the v2.77a-d technique) — so the
    real intake + lock + materialization + v2.77e obligation gate (all in ``run()``, before
    the stage loop) are exercised."""

    def _stage_init(self, project_root, record, options):  # noqa: ANN001
        record["status"] = "done"

    def _stage_elicit(self, project_root, record, options):  # noqa: ANN001
        record["status"] = "done"

    def _stage_plan(self, project_root, record, options):  # noqa: ANN001
        record["status"] = "done"

    def _stage_generate(self, project_root, record, options):  # noqa: ANN001
        record["status"] = "done"

    def _stage_implement(self, project_root, record, options):  # noqa: ANN001
        record["status"] = "done"

    def _stage_verify(self, project_root, record, options):  # noqa: ANN001
        record["status"] = "done"

    def _stage_ci_scaffold(self, project_root, record, options):  # noqa: ANN001
        record["status"] = "done"

    def _stage_propagate(self, project_root, record, options):  # noqa: ANN001
        record["status"] = "done"

    def _stage_check(self, project_root, record, options):  # noqa: ANN001
        record["status"] = "done"


def _make_project(tmp_path: Path, *, stack: dict | None, next_config: str | None = None) -> Path:
    """A pre-initialized CoDD project; optionally with a ``stack:`` block + a next.config.js."""
    project = tmp_path / "proj"
    codd_dir = project / "codd"
    codd_dir.mkdir(parents=True)
    config: dict = {"project": {"name": "proj", "language": "typescript"}}
    if stack is not None:
        config["stack"] = stack
    (codd_dir / "codd.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    if next_config is not None:
        (project / "next.config.js").write_text(next_config, encoding="utf-8")
    return project


def _write_lock_for(project: Path, declaration: dict) -> Path:
    contract = resolve_stack_from_declaration(declaration)
    path = stack_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_lock(build_lock(contract)), encoding="utf-8")
    return path


def _run(project: Path, *, executor=None):
    lines: list[str] = []
    result = _StageStubPipeline(echo=lines.append, stack_command_executor=executor).run(project)
    return result, lines


# ═══════════════════════════════════════════════════════════════════════════
# Exit gate 2 — a valid stack project is GREEN (through the REAL pipeline)
# ═══════════════════════════════════════════════════════════════════════════

def test_greenfield_valid_stack_obligations_green(tmp_path: Path) -> None:
    """A clean curated stack (no ignoreBuildErrors, e2e executed >=1 test) → GREEN, and
    the obligation gate's verdict is recorded in the run trace."""
    project = _make_project(tmp_path, stack=_VALID_STACK)  # no next.config → guard clean
    _write_lock_for(project, _VALID_STACK)
    rec = _RecordingExecutor()

    result, lines = _run(project, executor=rec)

    assert result.status == "success", f"a clean stack must be GREEN; {getattr(result, 'error', None)}"
    assert any("stack obligation gate" in line and "checked" in line for line in lines)
    session = load_session(project)
    rec_trace = session["stack_contract"]
    assert rec_trace["stack_obligations_checked"] >= 3  # nextjs(2) + prisma(1) + playwright(1)
    # The two warn obligations with no checker are surfaced as unenforced but did NOT block.
    assert "route_handler_must_be_exercised" in rec_trace["stack_obligations_unenforced"]
    assert "client_in_sync_with_schema" in rec_trace["stack_obligations_unenforced"]


def test_verify_valid_stack_obligations_green(tmp_path: Path) -> None:
    """The verify CLI path passes (no SystemExit) on a clean curated stack."""
    from codd.cli import _intake_stack_contract_for_verify

    project = _make_project(tmp_path, stack=_VALID_STACK)
    _write_lock_for(project, _VALID_STACK)
    rec = _RecordingExecutor()
    # Must NOT raise — a clean stack passes every gate incl. v2.77e.
    _intake_stack_contract_for_verify(project, stack_command_executor=rec)


# ═══════════════════════════════════════════════════════════════════════════
# Exit gate 1 / seeded negative control — a seeded obligation violation is RED
# ═══════════════════════════════════════════════════════════════════════════

_IGNORE_BUILD_ERRORS_CONFIG = "module.exports = { typescript: { ignoreBuildErrors: true } };\n"


def test_greenfield_seeded_ignore_build_errors_is_red(tmp_path: Path) -> None:
    """Seeded violation: next.config.js sets typescript.ignoreBuildErrors: true (a build that
    would pass WITH type errors) → the Next.js obligation checker FIRES → the gate reds the
    real pipeline. This is the checker's seeded negative control + exit gate 1."""
    project = _make_project(
        tmp_path, stack=_VALID_STACK, next_config=_IGNORE_BUILD_ERRORS_CONFIG
    )
    _write_lock_for(project, _VALID_STACK)
    rec = _RecordingExecutor()

    result, lines = _run(project, executor=rec)

    assert result.status == "failed", "a seeded ignoreBuildErrors violation must be RED"
    assert result.failed_stage == "stack_obligations"
    assert "no_ignore_build_errors_as_typecheck" in (result.error or "")
    assert any("stack obligation gate" in line for line in lines)


def test_verify_seeded_ignore_build_errors_is_red(tmp_path: Path) -> None:
    """The verify CLI path reds (non-zero exit) on the seeded ignoreBuildErrors violation."""
    from codd.cli import _intake_stack_contract_for_verify

    project = _make_project(
        tmp_path, stack=_VALID_STACK, next_config=_IGNORE_BUILD_ERRORS_CONFIG
    )
    _write_lock_for(project, _VALID_STACK)
    rec = _RecordingExecutor()

    with pytest.raises(SystemExit) as excinfo:
        _intake_stack_contract_for_verify(project, stack_command_executor=rec)
    assert excinfo.value.code != 0


def test_greenfield_skipped_e2e_obligation_is_red(tmp_path: Path) -> None:
    """Seeded violation: the e2e run executed 0 tests (all skipped) → the Playwright
    e2e_actually_executed obligation FIRES → the gate reds. (v2.77d also reds this via the
    same evidence — defense in depth on ONE evidence source, GPT-consult.) The pipeline
    fails at the FIRST gate that catches it (materialization/authenticity or obligations);
    either way a 0-test e2e is never GREEN."""
    project = _make_project(tmp_path, stack=_VALID_STACK)
    _write_lock_for(project, _VALID_STACK)
    rec = _RecordingExecutor(report_writer=_write_skipped_playwright_report)

    result, _lines = _run(project, executor=rec)

    assert result.status == "failed", "a 0-test (all-skipped) e2e must be RED"
    assert result.failed_stage in {"stack_commands", "stack_obligations"}


# ═══════════════════════════════════════════════════════════════════════════
# Exit gate 3 — checker removal (an error obligation with no checker) is RED
# ═══════════════════════════════════════════════════════════════════════════

def test_greenfield_checker_removal_is_red(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unregister the Next.js error obligation's checker → it becomes UNENFORCED → RED
    (an ERROR release-blocker with no registered checker is unverifiable, never silent
    green). Drives the REAL pipeline."""
    patched = dict(OBLIGATION_CHECKERS)
    patched.pop("nextjs_adapter:check_ignore_build_errors")
    monkeypatch.setattr("codd.stack.adapters.OBLIGATION_CHECKERS", patched)

    project = _make_project(tmp_path, stack=_VALID_STACK)  # otherwise-clean stack
    _write_lock_for(project, _VALID_STACK)
    rec = _RecordingExecutor()

    result, _lines = _run(project, executor=rec)

    assert result.status == "failed", "an unenforced ERROR obligation must be RED"
    assert result.failed_stage == "stack_obligations"
    assert "no_ignore_build_errors_as_typecheck" in (result.error or "")
    assert "unenforced" in (result.error or "").lower()


def test_unenforced_error_obligation_reds_seam(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pure-seam mirror: with the Next.js error checker unregistered, the gate raises (even
    with valid e2e evidence — the unenforced ERROR obligation reds regardless)."""
    patched = dict(OBLIGATION_CHECKERS)
    patched.pop("nextjs_adapter:check_ignore_build_errors")
    monkeypatch.setattr("codd.stack.adapters.OBLIGATION_CHECKERS", patched)

    contract = _curated_contract()
    from codd.stack.command_plan import stack_command_plan

    plan = stack_command_plan(contract)
    e2e = next(s for s in plan.slots if s.slot_id == "e2e_test")
    _write_passing_playwright_report(e2e, tmp_path)  # e2e satisfied; the unenforced one still reds

    with pytest.raises(StackObligationGateError):
        enforce_stack_obligation_gate(contract, tmp_path)


# ═══════════════════════════════════════════════════════════════════════════
# Exit gate 4 — obligation weakening (addon redefines a base obligation) is RED
# ═══════════════════════════════════════════════════════════════════════════

def test_obligation_weakening_severity_downgrade_is_red() -> None:
    """An addon redefining a base (framework) obligation to a WEAKER severity is a
    compose semantic conflict → the conflict gate reds BEFORE the obligation gate
    (weakening detected at composition, the single source of truth)."""
    ts = LANG.resolve("typescript")
    strong = FrameworkProfile(
        identity=LayerIdentity(id="sfw", kind="framework"),
        obligations=(Obligation(id="must_hold", severity="error", checker="x:y"),),
    )
    weak = AddonProfile(
        identity=LayerIdentity(id="wad", kind="addon"),
        obligations=(Obligation(id="must_hold", severity="warn", checker="x:y"),),
    )
    contract = compose(ts, [strong], [weak])
    assert any(c.kind == "semantic" for c in contract.conflicts)
    assert not contract.strict_ok
    # The conflict gate (inside the obligation-input builder's plan + the materialization
    # gate) reds a conflicted contract.
    from codd.stack.command_plan import StackContractConflictError

    with pytest.raises(StackContractConflictError):
        build_obligation_checker_inputs(contract, project_root=Path("."))


def test_greenfield_obligation_weakening_is_red(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An addon that WEAKENS a curated obligation (downgrades the Next.js error obligation
    to warn) → the composed contract carries a semantic conflict → the REAL pipeline reds
    (at the materialization conflict gate, which precedes the obligation gate)."""
    weak = AddonProfile(
        identity=LayerIdentity(id="weakener", kind="addon"),
        capability="lint",
        obligations=(
            Obligation(
                id="no_ignore_build_errors_as_typecheck",
                severity="warn",  # downgrade from the framework's error → semantic conflict
                checker="nextjs_adapter:check_ignore_build_errors",
            ),
        ),
    )
    profiles = dict(default_addon_registry._ensure_loaded())
    profiles["weakener"] = weak
    monkeypatch.setattr(default_addon_registry, "_profiles", profiles)

    stack = {"language": "typescript", "frameworks": ["nextjs"], "addons": ["weakener"]}
    project = _make_project(tmp_path, stack=stack)
    _write_lock_for(project, stack)
    rec = _RecordingExecutor()

    result, _lines = _run(project, executor=rec)

    assert result.status == "failed", "an obligation weakening must be RED"
    # The compose semantic conflict reds at the materialization conflict gate (which runs
    # before the obligation gate) — either stage attribution proves weakening is RED.
    assert result.failed_stage in {"stack_commands", "stack_obligations"}
    assert "weaken" in (result.error or "").lower() or "semantic" in (result.error or "").lower() or "conflict" in (result.error or "").lower()


def test_obligation_same_id_checker_ref_change_is_red() -> None:
    """A later layer redefining a base obligation at the SAME severity but with a DIFFERENT
    (here: nulled) checker ref is a compose semantic conflict (same-id checker-ref change) —
    "same severity, gutted checker" weakening that first-wins would otherwise hide."""
    ts = LANG.resolve("typescript")
    strong = FrameworkProfile(
        identity=LayerIdentity(id="sfw2", kind="framework"),
        obligations=(Obligation(id="must_hold", severity="error", checker="real:checker"),),
    )
    gutted = AddonProfile(
        identity=LayerIdentity(id="gutter", kind="addon"),
        obligations=(Obligation(id="must_hold", severity="error", checker=None),),  # gut the checker
    )
    contract = compose(ts, [strong], [gutted])
    assert any(c.kind == "semantic" for c in contract.conflicts)
    assert not contract.strict_ok


def test_obligation_exact_duplicate_is_idempotent() -> None:
    """Two layers declaring the SAME obligation id with the SAME severity AND the SAME
    checker ref is an idempotent duplicate — allowed, NOT a conflict (anti-false-RED)."""
    ts = LANG.resolve("typescript")
    a = FrameworkProfile(
        identity=LayerIdentity(id="dupfw", kind="framework"),
        obligations=(Obligation(id="must_hold", severity="error", checker="same:checker"),),
    )
    b = AddonProfile(
        identity=LayerIdentity(id="dupad", kind="addon"),
        obligations=(Obligation(id="must_hold", severity="error", checker="same:checker"),),
    )
    contract = compose(ts, [a], [b])
    assert not any(c.kind == "semantic" for c in contract.conflicts)
    assert contract.strict_ok


# ═══════════════════════════════════════════════════════════════════════════
# Hardened seam — a checker that raises / returns None / returns malformed is RED
# ═══════════════════════════════════════════════════════════════════════════

def _contract_with_error_obligation(checker_ref: str):
    ts = LANG.resolve("typescript")
    fw = FrameworkProfile(
        identity=LayerIdentity(id="faultfw", kind="framework"),
        obligations=(Obligation(id="blocker", severity="error", checker=checker_ref),),
    )
    return compose(ts, [fw])


def test_checker_returning_none_is_red(monkeypatch: pytest.MonkeyPatch) -> None:
    """The canonical ``checker(...) or []`` false-green: a checker that returns None must
    be a FAULT (RED for an error obligation), never coerced to 'no findings'."""
    patched = dict(OBLIGATION_CHECKERS)
    patched["fault:none"] = lambda **_: None  # unimplemented/fall-through checker
    monkeypatch.setattr("codd.stack.adapters.OBLIGATION_CHECKERS", patched)

    result = enforce_obligations(_contract_with_error_obligation("fault:none"), project_root=None)
    assert not result.passed
    assert any(f.obligation.id == "blocker" for f in result.blocking_faults)


def test_checker_raising_is_red(monkeypatch: pytest.MonkeyPatch) -> None:
    """A checker that raises is NOT 'satisfied' — it is a fault → RED for an error obligation."""
    def _boom(**_):
        raise RuntimeError("checker exploded")

    patched = dict(OBLIGATION_CHECKERS)
    patched["fault:raise"] = _boom
    monkeypatch.setattr("codd.stack.adapters.OBLIGATION_CHECKERS", patched)

    result = enforce_obligations(_contract_with_error_obligation("fault:raise"), project_root=None)
    assert not result.passed
    assert any("exploded" in f.reason for f in result.blocking_faults)


def test_checker_returning_malformed_is_red(monkeypatch: pytest.MonkeyPatch) -> None:
    """A checker that returns a non-list (e.g. a bare string/dict) is malformed → RED."""
    patched = dict(OBLIGATION_CHECKERS)
    patched["fault:malformed"] = lambda **_: "not a list"
    monkeypatch.setattr("codd.stack.adapters.OBLIGATION_CHECKERS", patched)

    result = enforce_obligations(_contract_with_error_obligation("fault:malformed"), project_root=None)
    assert not result.passed
    assert any(f.obligation.id == "blocker" for f in result.blocking_faults)


def test_warn_fault_does_not_block() -> None:
    """A WARN obligation whose checker faults is advisory — surfaced, but does NOT block
    (anti-false-RED: only ERROR obligations gate)."""
    ts = LANG.resolve("typescript")
    fw = FrameworkProfile(
        identity=LayerIdentity(id="warnfw", kind="framework"),
        obligations=(Obligation(id="advisory", severity="warn", checker="fault:warn"),),
    )
    contract = compose(ts, [fw])
    # checker ref resolves to nothing → unenforced warn → does NOT block.
    result = enforce_obligations(contract, project_root=None)
    assert result.passed
    assert any(o.id == "advisory" for o in result.unenforced)


def test_noncallable_registry_entry_is_unenforced_red(monkeypatch: pytest.MonkeyPatch) -> None:
    """A registry entry that is not callable cannot enforce → treated as unenforced → RED
    for an error obligation (no usable checker exists)."""
    patched = dict(OBLIGATION_CHECKERS)
    patched["fault:noncallable"] = "i am a string, not a function"  # type: ignore[assignment]
    monkeypatch.setattr("codd.stack.adapters.OBLIGATION_CHECKERS", patched)

    result = enforce_obligations(
        _contract_with_error_obligation("fault:noncallable"), project_root=None
    )
    assert not result.passed
    assert any(o.id == "blocker" for o in result.unenforced)


# ═══════════════════════════════════════════════════════════════════════════
# Playwright obligation binding — current-run evidence (not stale), via the gate seam
# ═══════════════════════════════════════════════════════════════════════════

def test_obligation_gate_playwright_zero_test_evidence_is_red(tmp_path: Path) -> None:
    """Fed the SAME current-run evidence shape v2.77d uses: a 0-test (all-skipped) e2e
    report → the Playwright obligation reds via the gate (build_obligation_checker_inputs →
    enforce_obligations)."""
    contract = _curated_contract()
    # Materialize the e2e slot's current-run evidence as a fully-skipped report.
    from codd.stack.command_plan import stack_command_plan

    plan = stack_command_plan(contract)
    e2e = next(s for s in plan.slots if s.slot_id == "e2e_test")
    _write_skipped_playwright_report(e2e, tmp_path)

    with pytest.raises(StackObligationGateError):
        enforce_stack_obligation_gate(contract, tmp_path)


def test_obligation_gate_playwright_valid_evidence_is_green(tmp_path: Path) -> None:
    """Fed a valid current-run e2e report (>=1 executed pass) + no ignoreBuildErrors →
    the gate passes (returns a result)."""
    contract = _curated_contract()
    from codd.stack.command_plan import stack_command_plan

    plan = stack_command_plan(contract)
    e2e = next(s for s in plan.slots if s.slot_id == "e2e_test")
    _write_passing_playwright_report(e2e, tmp_path)

    result = enforce_stack_obligation_gate(contract, tmp_path)
    assert result is not None and result.passed


def test_obligation_gate_playwright_missing_evidence_is_red(tmp_path: Path) -> None:
    """No current-run e2e evidence at all (the executor did not produce it this run) → the
    Playwright obligation cannot be satisfied → RED (never silently passed). The binding
    passes NO report (stale/absent not trusted); the checker reds on missing report."""
    contract = _curated_contract()
    # tmp_path has NO evidence file → build_obligation_checker_inputs returns {} →
    # check_executed sees no report_data → violation → RED.
    with pytest.raises(StackObligationGateError):
        enforce_stack_obligation_gate(contract, tmp_path)


# ═══════════════════════════════════════════════════════════════════════════
# Non-stack project → byte-identical (no obligation gate at all)
# ═══════════════════════════════════════════════════════════════════════════

def test_non_stack_project_has_no_obligation_gate() -> None:
    """A project that declared no stack (contract is None) → the gate is a no-op (None),
    byte-identical (no obligation enforcement)."""
    assert enforce_stack_obligation_gate(None, Path(".")) is None


def test_greenfield_non_stack_project_byte_identical(tmp_path: Path) -> None:
    """A greenfield run with NO stack block never reaches the obligation gate — no
    stack_obligations stage, no stack trace keys (behaviour-preserving)."""
    project = _make_project(tmp_path, stack=None)
    rec = _RecordingExecutor()

    result, lines = _run(project, executor=rec)

    assert result.status == "success"
    assert not any("stack obligation gate" in line for line in lines)
    session = load_session(project)
    assert "stack_contract" not in session  # no stack → no stack trace at all
    assert rec.calls == []  # no stack → no command execution
