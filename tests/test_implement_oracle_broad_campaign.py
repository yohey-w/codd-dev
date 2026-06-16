"""Anti-false-green acceptance tests for the BROAD REPAIR CAMPAIGN.

When the implement-time native oracle forces the BROAD rung on a wide-fan-out
artifact, broad's EXECUTION is no longer "regenerate every task" (~17 tasks,
~40-50 min/attempt, 4h wall-clock blow-up). It is a BUDGETED RESIDUAL COHERENCE
CAMPAIGN (``codd.implement_oracle._execute_broad_campaign``):

    result = whole_project_oracle()
    while result.failed and budget_left and rechecks < broad_max_rechecks:
        phase = next_phase(result)        # supplier_first → residual_importers
                                          #   (re-derived from residual) → chunked_broad
        rerun_targeted_phase(phase)       # ONLY phase tasks, fenced, contract feedback
        result = whole_project_oracle()   # ALWAYS the global authority
        if result.passed: return passed
        if no_progress_or_cycle: advance_phase_or_break
    return honest_fail

THE LOAD-BEARING INVARIANT under test: the WHOLE-PROJECT oracle is the ONLY green
authority. A phase's local typecheck success proves NOTHING; budget exhaustion /
non-convergence / unresolved residual is an HONEST FAILURE (the failing result is
returned → the caller raises StageError), NEVER a silent green.

These tests use FAKES (no real tsc/SUT): a scripted fake oracle that returns
``ImplementOracleResult``s with realistic tsc-format ``raw_output`` (so the live
residual derivation parses real diagnostics), and a fake rerun that mutates a
shared diagnostic state — letting us drive supplier-fix-clears-importers, residual,
oscillation, and budget deterministically.

The 8 acceptance gates (named in the module task) are:
  1. chunk-never-green        — test_chunk_local_success_does_not_green_gate
  2. budget-exhaustion        — test_budget_exhaustion_honest_fail_with_audit
  3. non-convergence/oscill.  — test_oscillation_honest_fail_no_infinite_loop
  4. exporter-first-resolves  — test_supplier_fix_clears_importers_without_importer_rerun
  5. residual-only            — test_residual_only_reruns_owner_importers_not_all
  6. fence-restored           — test_broad_phase_with_allowed_paths_reverts_out_of_scope
  7. termination              — test_campaign_is_bounded_supplier_once_and_recheck_cap
  8. backward-compat          — test_narrow_expanded_unchanged_and_legacy_broad_path
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codd import implement_oracle as io_mod
from codd.implement_oracle import (
    EVIDENCE_MISSING_SYMBOL,
    ImplementOracleFinding,
    ImplementOracleResult,
    _execute_broad_campaign,
    build_contract_feedback,
)
from codd.implement_oracle_scope import (
    PHASE_CHUNKED_BROAD,
    PHASE_RESIDUAL_IMPORTERS,
    PHASE_SUPPLIER_FIRST,
    BroadRepairPlan,
    OracleRepairPhase,
    OracleRerunScope,
    build_path_owner_index,
    derive_oracle_rerun_scope,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fakes: a scripted oracle + a state-driven rerun (no real tsc / SUT)
# ─────────────────────────────────────────────────────────────────────────────


class _Task:
    """An ImplementTaskRef-shaped stand-in for the owner index."""

    def __init__(self, task_id: str, output_paths) -> None:
        self.task_id = task_id
        self.output_paths = tuple(output_paths)
        self.design_node = task_id
        self.source = "design.md"


def _ts_line(path: str, code: str, symbol: str, module: str) -> str:
    """One realistic tsc diagnostic line (so the live residual derivation parses it)."""
    return f'{path}(1,10): error {code}: Module "{module}" has no exported member "{symbol}".'


def _result_from_diag_paths(diag_paths, *, module="./barrel.js", symbol="q") -> ImplementOracleResult:
    """A FAILING ``ImplementOracleResult`` whose raw_output names ``diag_paths``.

    Each path becomes a TS2305 diagnostic line on that file demanding ``symbol``
    from ``module`` — i.e. the importer-side error a wide-fan-out break produces.
    ``diagnostics`` is filled from the same parse so the signature + scope
    derivation see the structured diagnostics.
    """
    from codd.implement_oracle_scope import _parse_ts_diagnostics

    lines = [_ts_line(p, "TS2305", symbol, module) for p in diag_paths]
    raw = "\n".join(lines) + "\n"
    findings = [
        ImplementOracleFinding(category=EVIDENCE_MISSING_SYMBOL, code="TS2305", message=f"missing {symbol}", path=p)
        for p in diag_paths
    ]
    diags = _parse_ts_diagnostics(raw, Path("/nonexistent"))  # parse is path-agnostic for relative inputs
    return ImplementOracleResult(
        passed=False,
        executed=True,
        command="fake-tsc",
        findings=findings,
        failed_paths=list(diag_paths),
        detail=f"fake oracle: {len(diag_paths)} diagnostic(s)",
        raw_output=raw,
        diagnostics=diags,
    )


def _passing_result() -> ImplementOracleResult:
    return ImplementOracleResult(
        passed=True, executed=True, command="fake-tsc", detail="fake oracle: clean", raw_output=""
    )


class _ScriptedOracle:
    """A fake whole-project oracle that returns results from a state-reading fn.

    ``state["diag_paths"]`` is the CURRENT set of broken importer paths. Each call
    snapshots that state into an ``ImplementOracleResult`` (empty ⇒ passing). The
    rerun callback mutates ``state`` to model "the supplier fix cleared importers"
    etc. ``calls`` records every snapshot for assertions.
    """

    def __init__(self, state: dict) -> None:
        self.state = state
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, project_root, profile, spec, config) -> ImplementOracleResult:
        paths = tuple(self.state.get("diag_paths", ()))
        self.calls.append(paths)
        if not paths:
            return _passing_result()
        return _result_from_diag_paths(paths)


class _RecordingRerun:
    """A fake ``rerun(feedback, scope)`` that records scopes + applies a state fn.

    ``on_rerun(scope, state)`` is called with the scope's ``task_ids`` so a test can
    model what a phase repairs (e.g. supplier_first clears all importer diagnostics).
    ``scopes`` / ``feedbacks`` capture every call for assertions.
    """

    def __init__(self, state: dict, on_rerun=None) -> None:
        self.state = state
        self.on_rerun = on_rerun
        self.scopes: list = []
        self.feedbacks: list[str] = []

    def __call__(self, feedback: str, scope=None) -> None:
        self.scopes.append(scope)
        self.feedbacks.append(feedback)
        if self.on_rerun is not None:
            self.on_rerun(scope, self.state)


def _plan_for(supplier_ids, importer_ids, *, focus="src/barrel.ts", manifest=()) -> OracleRerunScope:
    """A broad-campaign scope (rung=broad, repair_plan set) over the given owners."""
    supplier_scope = OracleRerunScope(
        rung="broad",
        task_ids=tuple(supplier_ids),
        allowed_paths=(focus, *manifest),
        detail="supplier_first",
    )
    importer_scope = OracleRerunScope(
        rung="broad",
        task_ids=tuple(importer_ids),
        allowed_paths=tuple(manifest),
        detail="residual_importers (static)",
    )
    chunked_scope = OracleRerunScope(
        rung="broad",
        task_ids=tuple([*supplier_ids, *importer_ids]),
        allowed_paths=tuple(manifest),
        detail="chunked_broad",
    )
    plan = BroadRepairPlan(
        focus_paths=(focus,),
        supplier_task_ids=tuple(supplier_ids),
        importer_task_ids=tuple(importer_ids),
        phases=(
            OracleRepairPhase(phase=PHASE_SUPPLIER_FIRST, scope=supplier_scope, focus_paths=(focus,)),
            OracleRepairPhase(phase=PHASE_RESIDUAL_IMPORTERS, scope=importer_scope),
            OracleRepairPhase(phase=PHASE_CHUNKED_BROAD, scope=chunked_scope),
        ),
    )
    return OracleRerunScope(
        rung="broad",
        task_ids=tuple([*supplier_ids, *importer_ids]),
        allowed_paths=supplier_scope.allowed_paths,
        detail="broad-campaign",
        repair_plan=plan,
    )


def _index_for(tmp_path: Path, importer_count: int = 3):
    """An owner index: barrel_task (supplier) + dep_task + cN_task (importers)."""
    tasks = [_Task("barrel_task", ["src/barrel.ts"]), _Task("dep_task", ["src/dep.ts"])]
    for i in range(importer_count):
        tasks.append(_Task(f"c{i}_task", [f"src/c{i}.ts"]))
    return build_path_owner_index(tasks, project_root=tmp_path)


def _run_campaign(tmp_path, *, oracle, rerun, scope, config, scope_index=None, monkeypatch):
    """Drive ``_execute_broad_campaign`` with the fake oracle patched in."""
    monkeypatch.setattr(io_mod, "_run_oracle_command", oracle)
    # The campaign re-runs the WHOLE-PROJECT oracle via _run_oracle_command; the
    # INITIAL failing result is the oracle's first snapshot.
    initial = oracle(tmp_path, None, None, config)
    return _execute_broad_campaign(
        result=initial,
        plan=scope.repair_plan,
        project_root=tmp_path,
        profile=None,
        spec=None,
        config=config,
        rerun=rerun,
        scope_index=scope_index,
        structured_source=None,
        manifest_paths=(),
        echo=lambda _m: None,
    )


# ═════════════════════════════════════════════════════════════════════════════
# 1. chunk-never-green: a phase whose LOCAL files typecheck but the WHOLE-PROJECT
#    oracle still fails → the gate does NOT pass. Only a passing whole-project
#    oracle discharges the gate.
# ═════════════════════════════════════════════════════════════════════════════


def test_chunk_local_success_does_not_green_gate(tmp_path: Path, monkeypatch) -> None:
    state = {"diag_paths": ["src/c0.ts", "src/c1.ts"]}

    def on_rerun(scope, st):
        # The supplier phase "succeeds locally" (it edits barrel.ts cleanly) but
        # does NOT clear the importer diagnostics — the whole-project oracle still
        # sees src/c0.ts + src/c1.ts broken. Residual/chunked phases ALSO fail to
        # clear them. So no phase's local success may flip the gate green.
        pass  # state never changes ⇒ whole-project oracle never goes clean

    oracle = _ScriptedOracle(state)
    rerun = _RecordingRerun(state, on_rerun)
    scope = _plan_for(["barrel_task"], ["c0_task", "c1_task"])
    config = {"implement": {"oracle_broad_max_rechecks": 5}}
    index = _index_for(tmp_path)

    result = _run_campaign(
        tmp_path, oracle=oracle, rerun=rerun, scope=scope, config=config, scope_index=index, monkeypatch=monkeypatch
    )

    assert result.passed is False, "a phase's local success must NEVER green the gate"
    # Every recheck re-ran the WHOLE-PROJECT oracle (the only authority); it stayed red.
    assert all(snapshot for snapshot in oracle.calls if snapshot != ()), "oracle stayed red"
    assert oracle.calls[-1] != (), "the final authority check was a FAILING whole-project oracle"


# ═════════════════════════════════════════════════════════════════════════════
# 2. budget-exhaustion → honest-fail + partial-progress audit record (never green)
# ═════════════════════════════════════════════════════════════════════════════


def test_budget_exhaustion_honest_fail_with_audit(tmp_path: Path, monkeypatch) -> None:
    state = {"diag_paths": ["src/c0.ts", "src/c1.ts"]}
    oracle = _ScriptedOracle(state)
    rerun = _RecordingRerun(state, on_rerun=lambda scope, st: None)  # never fixes anything
    scope = _plan_for(["barrel_task"], ["c0_task", "c1_task"])
    # A tiny positive wall-clock budget (0.001s) ⇒ remaining < (min_call + reserve
    # = 90s) on the FIRST gate → stop immediately, run no AI phase, honest-fail with
    # a budget audit record. (0 is rejected by the reader as nonsensical config and
    # falls back to the default; a tiny positive value exercises the budget gate.)
    config = {"implement": {"oracle_broad_wall_clock_seconds": 0.001, "oracle_broad_max_rechecks": 8}}
    index = _index_for(tmp_path)

    result = _run_campaign(
        tmp_path, oracle=oracle, rerun=rerun, scope=scope, config=config, scope_index=index, monkeypatch=monkeypatch
    )

    assert result.passed is False, "budget exhaustion must honest-fail (never green)"
    assert rerun.scopes == [], "no AI phase should run once the budget is exhausted"
    # A partial-progress audit record was written.
    audit = tmp_path / ".codd" / "oracle_repair" / "campaign.jsonl"
    assert audit.is_file(), "a campaign audit file must be written"
    records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(r["status"] == "budget_exhausted" for r in records), records


# ═════════════════════════════════════════════════════════════════════════════
# 3. non-convergence / oscillation → honest-fail, no infinite loop
# ═════════════════════════════════════════════════════════════════════════════


def test_oscillation_honest_fail_no_infinite_loop(tmp_path: Path, monkeypatch) -> None:
    # The SUT oscillates: each rerun "fixes" the current break but INVENTS a new one
    # of the SAME shape, so the diagnostics flip between two sets forever. The
    # campaign must terminate (recheck cap / phase exhaustion / cycle key) and
    # honest-fail — it must NOT loop unboundedly.
    flip = {"n": 0}
    state = {"diag_paths": ["src/c0.ts"]}

    def on_rerun(scope, st):
        flip["n"] += 1
        st["diag_paths"] = ["src/c1.ts"] if flip["n"] % 2 == 1 else ["src/c0.ts"]

    oracle = _ScriptedOracle(state)
    rerun = _RecordingRerun(state, on_rerun)
    scope = _plan_for(["barrel_task"], ["c0_task", "c1_task"])
    config = {"implement": {"oracle_broad_max_rechecks": 6}}
    index = _index_for(tmp_path)

    result = _run_campaign(
        tmp_path, oracle=oracle, rerun=rerun, scope=scope, config=config, scope_index=index, monkeypatch=monkeypatch
    )

    assert result.passed is False, "an oscillating SUT must honest-fail"
    # Bounded: the number of AI reruns never exceeds the recheck cap (no infinite loop).
    assert len(rerun.scopes) <= 6, f"campaign must be bounded by the recheck cap, ran {len(rerun.scopes)}"


# ═════════════════════════════════════════════════════════════════════════════
# 4. exporter-first-resolves-importers: fixing the supplier clears the importer
#    diagnostics → green WITHOUT re-implementing any importer task.
# ═════════════════════════════════════════════════════════════════════════════


def test_supplier_fix_clears_importers_without_importer_rerun(tmp_path: Path, monkeypatch) -> None:
    state = {"diag_paths": ["src/c0.ts", "src/c1.ts", "src/c2.ts"]}

    def on_rerun(scope, st):
        # The supplier_first phase (barrel_task) fixes the shared exporter, which
        # clears EVERY importer diagnostic — the whole-project oracle goes clean.
        if "barrel_task" in (scope.task_ids or ()):
            st["diag_paths"] = []

    oracle = _ScriptedOracle(state)
    rerun = _RecordingRerun(state, on_rerun)
    scope = _plan_for(["barrel_task"], ["c0_task", "c1_task", "c2_task"])
    config = {"implement": {"oracle_broad_max_rechecks": 8}}
    index = _index_for(tmp_path)

    result = _run_campaign(
        tmp_path, oracle=oracle, rerun=rerun, scope=scope, config=config, scope_index=index, monkeypatch=monkeypatch
    )

    assert result.passed is True, "supplier fix clearing importers must reach GREEN"
    # Exactly ONE rerun (the supplier phase); NO importer task was re-implemented.
    assert len(rerun.scopes) == 1, f"only the supplier phase should run, got {len(rerun.scopes)}"
    ran_tasks = set(rerun.scopes[0].task_ids or ())
    assert ran_tasks == {"barrel_task"}, ran_tasks
    assert not (ran_tasks & {"c0_task", "c1_task", "c2_task"}), "no importer task may be re-implemented"


# ═════════════════════════════════════════════════════════════════════════════
# 5. residual-only: supplier fix leaves residual importer diagnostics → ONLY the
#    residual importer owner-tasks are re-run (not all tasks).
# ═════════════════════════════════════════════════════════════════════════════


def test_residual_only_reruns_owner_importers_not_all(tmp_path: Path, monkeypatch) -> None:
    # supplier fix clears c0 + c1 but leaves c2 broken; the residual phase must
    # re-run ONLY c2_task (the owner of the residual diagnostic), then go green.
    state = {"diag_paths": ["src/c0.ts", "src/c1.ts", "src/c2.ts"]}

    def on_rerun(scope, st):
        ran = set(scope.task_ids or ())
        if "barrel_task" in ran:
            st["diag_paths"] = ["src/c2.ts"]  # residual: only c2 still broken
        elif ran == {"c2_task"}:
            st["diag_paths"] = []  # the residual importer fix clears it → green

    oracle = _ScriptedOracle(state)
    rerun = _RecordingRerun(state, on_rerun)
    scope = _plan_for(["barrel_task"], ["c0_task", "c1_task", "c2_task"])
    config = {"implement": {"oracle_broad_max_rechecks": 8}}
    index = _index_for(tmp_path)

    result = _run_campaign(
        tmp_path, oracle=oracle, rerun=rerun, scope=scope, config=config, scope_index=index, monkeypatch=monkeypatch
    )

    assert result.passed is True, "supplier + residual-importer fix must reach GREEN"
    assert len(rerun.scopes) == 2, f"supplier + ONE residual phase, got {len(rerun.scopes)}"
    supplier_tasks = set(rerun.scopes[0].task_ids or ())
    residual_tasks = set(rerun.scopes[1].task_ids or ())
    assert supplier_tasks == {"barrel_task"}
    # The residual phase re-ran ONLY the owner of the residual diagnostic (c2_task),
    # re-derived from the LIVE residual — NOT all importers, NOT the supplier.
    assert residual_tasks == {"c2_task"}, residual_tasks
    assert "barrel_task" not in residual_tasks, "residual phase must exclude the already-fixed supplier"


def test_residual_repaired_in_multiple_chunks_across_rechecks(tmp_path: Path) -> None:
    # The residual_importers phase is ITERATIVE: it re-runs across rechecks,
    # repairing the residual chunk-by-chunk (each recheck re-derives the LIVE
    # residual owner-tasks), until the whole-project oracle is clean — without ever
    # falling back to a whole-project regen.
    import codd.implement_oracle as _io

    index = _index_for(tmp_path, importer_count=3)
    state = {"diag_paths": ["src/c0.ts", "src/c1.ts", "src/c2.ts"]}

    def on_rerun(scope, st):
        ran = set(scope.task_ids or ())
        if "barrel_task" in ran:
            st["diag_paths"] = ["src/c0.ts", "src/c1.ts", "src/c2.ts"]  # supplier didn't help
        elif ran == {"c0_task"}:
            st["diag_paths"] = ["src/c1.ts", "src/c2.ts"]
        elif ran == {"c1_task"}:
            st["diag_paths"] = ["src/c2.ts"]
        elif ran == {"c2_task"}:
            st["diag_paths"] = []

    oracle = _ScriptedOracle(state)
    rerun = _RecordingRerun(state, on_rerun)
    scope = _plan_for(["barrel_task"], ["c0_task", "c1_task", "c2_task"])
    # chunk size 1 ⇒ the residual phase repairs ONE residual owner-task per recheck
    # (dependency-ordered), iterating c0 → c1 → c2.
    config = {"implement": {"oracle_broad_max_rechecks": 8, "oracle_residual_chunk_size": 1}}

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(_io, "_run_oracle_command", oracle)
        initial = oracle(tmp_path, None, None, config)
        result = _io._execute_broad_campaign(
            result=initial,
            plan=scope.repair_plan,
            project_root=tmp_path,
            profile=None,
            spec=None,
            config=config,
            rerun=rerun,
            scope_index=index,
            structured_source=None,
            manifest_paths=(),
            echo=lambda _m: None,
        )
    finally:
        monkeypatch.undo()

    assert result.passed is True, "iterative residual repair must converge to GREEN"
    # supplier(barrel) + 3 residual chunks (c0, c1, c2), each its own owner-task —
    # never a whole-project regen.
    ran_sets = [set(s.task_ids or ()) for s in rerun.scopes]
    assert ran_sets[0] == {"barrel_task"}
    assert {"c0_task"} in ran_sets and {"c1_task"} in ran_sets and {"c2_task"} in ran_sets
    # No phase ever re-implemented ALL importers at once (residual is per-owner here).
    assert not any(s == {"c0_task", "c1_task", "c2_task"} for s in ran_sets), "no whole-importer regen"


# ═════════════════════════════════════════════════════════════════════════════
# 6. fence-restored: a broad PHASE with allowed_paths runs UNDER the write-fence —
#    out-of-scope writes are reverted (the pipeline's _OracleWriteFence is active).
# ═════════════════════════════════════════════════════════════════════════════


def test_broad_phase_with_allowed_paths_reverts_out_of_scope(tmp_path: Path) -> None:
    # Drive the PIPELINE's scoped-rerun dispatch with a broad-campaign PHASE scope
    # (rung=broad, allowed_paths non-empty). The fence must treat it as a FENCED
    # scoped execution: a write OUTSIDE allowed_paths during the rerun is reverted.
    from codd.greenfield.pipeline import GreenfieldPipeline

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "barrel.ts").write_text("export const q = 0;\n", encoding="utf-8")
    (tmp_path / "src" / "other.ts").write_text("export const k = 0;\n", encoding="utf-8")

    barrel_task = _Task("barrel_task", ["src/barrel.ts"])
    other_task = _Task("other_task", ["src/other.ts"])

    # A broad PHASE scope fenced to src/barrel.ts ONLY (rung=broad — the campaign's
    # supplier phase shape). The reimplement stub writes BOTH the in-scope barrel.ts
    # AND an out-of-scope src/other.ts; only the in-scope write may survive.
    phase_scope = OracleRerunScope(
        rung="broad",
        task_ids=("barrel_task",),
        allowed_paths=("src/barrel.ts",),
        detail="broad-campaign supplier phase",
    )

    pipe = GreenfieldPipeline.__new__(GreenfieldPipeline)
    pipe.echo = lambda _m: None
    pipe.ai_command = "fake"

    def _fake_reimplement(project_root, tasks, feedback, config):
        # The SUT writes in-scope (allowed) AND out-of-scope (must be reverted).
        (project_root / "src" / "barrel.ts").write_text("export const q = 1;  // fixed\n", encoding="utf-8")
        (project_root / "src" / "other.ts").write_text("export const k = 999;  // ROGUE\n", encoding="utf-8")
        return {t.task_id: 0.0 for t in tasks}

    pipe._reimplement_tasks = _fake_reimplement  # type: ignore[method-assign]
    pipe._rerun_tasks_with_feedback(
        tmp_path, [barrel_task, other_task], "feedback", {}, scope=phase_scope
    )

    # In-scope write survived; out-of-scope write was REVERTED to pre-rerun bytes.
    assert "// fixed" in (tmp_path / "src" / "barrel.ts").read_text(encoding="utf-8"), "in-scope write kept"
    assert (tmp_path / "src" / "other.ts").read_text(encoding="utf-8") == "export const k = 0;\n", (
        "out-of-scope write under a broad-campaign phase MUST be reverted (fence active)"
    )


# ═════════════════════════════════════════════════════════════════════════════
# 7. termination: the campaign is bounded — supplier max-1/artifact + recheck cap.
# ═════════════════════════════════════════════════════════════════════════════


def test_campaign_is_bounded_supplier_once_and_recheck_cap(tmp_path: Path, monkeypatch) -> None:
    # A persistently-red oracle that NEVER clears (no rerun ever helps). The
    # campaign must (a) run the supplier phase at most ONCE, (b) stop at the recheck
    # cap, (c) terminate (no infinite loop), and (d) honest-fail.
    state = {"diag_paths": ["src/c0.ts"]}
    oracle = _ScriptedOracle(state)
    rerun = _RecordingRerun(state, on_rerun=lambda scope, st: None)
    scope = _plan_for(["barrel_task"], ["c0_task"])
    cap = 4
    config = {"implement": {"oracle_broad_max_rechecks": cap}}
    index = _index_for(tmp_path)

    result = _run_campaign(
        tmp_path, oracle=oracle, rerun=rerun, scope=scope, config=config, scope_index=index, monkeypatch=monkeypatch
    )

    assert result.passed is False
    # supplier_first appears at most once across all reruns (max-1/artifact).
    supplier_runs = sum(1 for s in rerun.scopes if set(s.task_ids or ()) == {"barrel_task"})
    assert supplier_runs <= 1, f"supplier phase must run at most once, ran {supplier_runs}"
    # Total reruns are bounded by the recheck cap (termination guarantee).
    assert len(rerun.scopes) <= cap, f"campaign bounded by recheck cap, ran {len(rerun.scopes)}"
    # The recheck-cap audit record is present.
    audit = tmp_path / ".codd" / "oracle_repair" / "campaign.jsonl"
    records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(r["status"] in {"recheck_cap", "stuck"} for r in records), records


# ═════════════════════════════════════════════════════════════════════════════
# 8. backward-compat: narrow/expanded scope behaviour unchanged; the legacy_broad
#    path still works when oracle_legacy_broad_enabled is true.
# ═════════════════════════════════════════════════════════════════════════════


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_narrow_expanded_unchanged_and_legacy_broad_path(tmp_path: Path) -> None:
    from codd.implement_oracle_scope import SCOPE_NARROW

    # (a) A NON-wide-fan-out break still derives an ordinary SCOPED scope (narrow),
    # exactly as before — no repair_plan, not a campaign.
    _write(tmp_path, "src/index.ts", 'import { runCli } from "./cli.js";\nexport { runCli };\n')
    _write(tmp_path, "src/cli.ts", "export function run(): number { return 0; }\n")
    _write(tmp_path, "src/app.ts", 'import { runCli } from "./index.js";\nexport const a = runCli;\n')
    tasks = [_Task("index_task", ["src/index.ts"]), _Task("cli_task", ["src/cli.ts"])]
    index = build_path_owner_index(tasks, project_root=tmp_path)
    out = 'src/index.ts(1,10): error TS2305: Module "./cli.js" has no exported member "runCli".\n'
    decision = derive_oracle_rerun_scope(output=out, project_root=tmp_path, index=index, rung=SCOPE_NARROW)
    assert decision.scope is not None and not decision.scope.is_broad_campaign(), "narrow stays a plain scope"
    assert decision.scope.repair_plan is None
    assert decision.scope.rung == SCOPE_NARROW
    assert set(decision.scope.task_ids) == {"index_task", "cli_task"}

    # (b) The LEGACY whole-project broad path still works when opted in: a wide-fan-
    # out artifact with legacy_broad=True returns the old (scope=None, force_broad).
    wide = tmp_path / "wide"
    _write(wide, "src/barrel.ts", 'import { z } from "./dep.js";\nexport const q = z;\n')
    _write(wide, "src/dep.ts", "export const z = 1;\n")
    for i in range(7):
        _write(wide, f"src/c{i}.ts", 'import { q } from "./barrel.js";\nexport const u = q;\n')
    wtasks = [_Task("barrel_task", ["src/barrel.ts"]), _Task("dep_task", ["src/dep.ts"])]
    windex = build_path_owner_index(wtasks, project_root=wide)
    wout = 'src/barrel.ts(1,10): error TS2305: Module "./dep.js" has no exported member "z".\n'
    legacy = derive_oracle_rerun_scope(
        output=wout, project_root=wide, index=windex, rung=SCOPE_NARROW, legacy_broad=True
    )
    assert legacy.scope is None and legacy.force_broad is True, "legacy broad opt-in restores whole-project broad"


# ─────────────────────────────────────────────────────────────────────────────
# Supporting unit checks: the campaign feeds phase-scope into contract feedback
# (so broad subphases ALSO get the minimal-diff / allowed-paths directives).
# ─────────────────────────────────────────────────────────────────────────────


def test_broad_phase_feedback_carries_targeted_edit_directive(tmp_path: Path) -> None:
    # A broad-campaign PHASE scope (rung=broad WITH allowed_paths) must produce the
    # TARGETED-EDIT block in build_contract_feedback — the broad subphase gets the
    # same minimal-diff / allowed-paths convergence lever scoped reruns get.
    result = _result_from_diag_paths(["src/c0.ts"])
    phase_scope = OracleRerunScope(
        rung="broad",
        task_ids=("barrel_task",),
        allowed_paths=("src/barrel.ts",),
        detail="supplier phase",
    )
    feedback = build_contract_feedback(result, project_root=tmp_path, scope=phase_scope)
    assert "TARGETED EDIT" in feedback, "a fenced broad phase must emit the minimal-diff directive"
    assert "src/barrel.ts" in feedback, "the allowed-paths fence list must appear"

    # The LEGACY whole-project broad (no allowed_paths, no repair_plan) must NOT get
    # the targeted-edit fence (it legitimately regenerates everything).
    legacy_scope = OracleRerunScope(rung="broad", task_ids=("barrel_task",), allowed_paths=())
    legacy_feedback = build_contract_feedback(result, project_root=tmp_path, scope=legacy_scope)
    assert "TARGETED EDIT" not in legacy_feedback, "legacy whole-project broad gets no minimal-diff fence"
