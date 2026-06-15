"""Tests for the SCOPED rerun of the implement-time native-oracle gate.

The oracle previously re-ran EVERY implement task on failure (a broad rerun:
~17 units, ~40-50 min/attempt). This localizes the rerun to the artifacts the
diagnostics implicate — BOTH ENDS of the broken demand edge (importer + the
exporter the importer demands) — with broad DEMOTED to an escalation fallback,
a diagnostic-signature loop-breaker, and a write-fence so a "targeted" rerun
cannot silently regenerate the whole tree.

Coverage:

1. **Edge derivation** (pure): a cross-file symbol error → the scope's task set
   includes BOTH the importer and the exporter task (not all tasks, not only the
   importer); the TS2307 module-resolution + TS2304 import-derived-name edge
   classes; the path→owning-task index.

2. **Escalation ladder** (gate-level, fake oracle — no toolchain): a
   signature-unchanged-after-scoped rerun escalates narrow→expanded→broad; an
   unowned diagnostic goes straight to broad; the breadth + fan-out guards.

3. **Write-fence**: an out-of-scope CREATE/MODIFY/DELETE during a scoped rerun is
   reverted; an in-scope write is kept; a broad rerun has NO fence.

4. **Back-compat**: a stack with no scope index reruns broad (legacy); the
   Python NO-OP path is untouched (covered in test_implement_oracle.py).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codd.implement_oracle import ImplementOracleResult, run_implement_oracle_gate
from codd.implement_oracle_scope import (
    PROGRESS_CYCLE,
    PROGRESS_OSCILLATION,
    PROGRESS_SOFT,
    PROGRESS_STRICT,
    PROGRESS_STUCK,
    SCOPE_BROAD,
    SCOPE_EXPANDED,
    SCOPE_NARROW,
    StructuredDiagnostic,
    build_path_owner_index,
    classify_signature_progress,
    derive_oracle_rerun_scope,
    diagnostic_signature,
    exporter_surface_for_diagnostics,
    extract_public_surface,
    find_orphan_artifacts,
    next_rung,
)


# ─────────────────────────────────────────────────────────────
# Test doubles
# ─────────────────────────────────────────────────────────────


class _Task:
    """A minimal ImplementTaskRef-shaped stand-in (task_id + output_paths)."""

    def __init__(self, task_id: str, output_paths) -> None:
        self.task_id = task_id
        self.output_paths = tuple(output_paths)


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ═════════════════════════════════════════════════════════════
# (a) Cross-file edge → BOTH importer + exporter tasks
# ═════════════════════════════════════════════════════════════


def _cross_file_project(root: Path) -> None:
    """importer ``src/index.ts`` demands ``runCli`` from exporter ``src/cli.ts``."""
    _write(root, "src/index.ts", 'import { runCli } from "./cli.js";\nexport { runCli };\n')
    _write(root, "src/cli.ts", "export function run(): number { return 0; }\n")
    _write(root, "src/extra.ts", "export const z = 1;\n")


def _missing_export_output(primary: str = "src/index.ts") -> str:
    return (
        f'{primary}(1,10): error TS2305: Module "./cli.js" has no exported member "runCli".\n'
    )


def test_scope_includes_both_importer_and_exporter_tasks(tmp_path: Path) -> None:
    """A cross-file symbol error scopes to BOTH ends of the broken edge.

    The error is reported on the IMPORTER (index.ts), but the fix may belong to
    the EXPORTER (cli.ts). The scope must therefore include the importer's task
    AND the exporter's task — and exclude an unrelated task.
    """
    _cross_file_project(tmp_path)
    tasks = [
        _Task("index_task", ["src/index.ts"]),
        _Task("cli_task", ["src/cli.ts"]),
        _Task("extra_task", ["src/extra.ts"]),
    ]
    index = build_path_owner_index(tasks, project_root=tmp_path)

    decision = derive_oracle_rerun_scope(
        output=_missing_export_output(),
        project_root=tmp_path,
        index=index,
        rung=SCOPE_NARROW,
    )

    assert decision.force_broad is False, decision.reason
    assert decision.scope is not None
    assert decision.scope.rung == SCOPE_NARROW
    task_set = set(decision.scope.task_ids)
    assert "index_task" in task_set, "importer task must be in scope"
    assert "cli_task" in task_set, "exporter task must be in scope (the fix may belong here)"
    assert "extra_task" not in task_set, "an unrelated task must NOT be in scope"
    assert task_set != {"index_task"}, "naive-targeted (importer only) is INVALID"


def test_scope_is_not_all_tasks(tmp_path: Path) -> None:
    """The scope is the edge, not the whole project (anti-broad)."""
    _cross_file_project(tmp_path)
    # Add many unrelated tasks; the scope must stay the 2 edge tasks.
    tasks = [_Task(f"t{i}", [f"src/mod{i}.ts"]) for i in range(8)]
    tasks += [_Task("index_task", ["src/index.ts"]), _Task("cli_task", ["src/cli.ts"])]
    index = build_path_owner_index(tasks, project_root=tmp_path)
    decision = derive_oracle_rerun_scope(
        output=_missing_export_output(), project_root=tmp_path, index=index, rung=SCOPE_NARROW
    )
    assert decision.scope is not None and not decision.force_broad
    assert set(decision.scope.task_ids) == {"index_task", "cli_task"}


def test_owner_resolution_by_directory(tmp_path: Path) -> None:
    """A file with no exact-declared owner resolves to the task owning its dir."""
    _cross_file_project(tmp_path)
    # One task owns the whole ``src/`` directory; the diagnostic file lives under it.
    tasks = [_Task("src_task", ["src"]), _Task("docs_task", ["docs"])]
    index = build_path_owner_index(tasks, project_root=tmp_path)
    decision = derive_oracle_rerun_scope(
        output=_missing_export_output(), project_root=tmp_path, index=index, rung=SCOPE_NARROW
    )
    assert decision.scope is not None
    assert set(decision.scope.task_ids) == {"src_task"}
    assert "docs_task" not in decision.scope.task_ids


def test_module_resolution_edge_pulls_candidate_owner(tmp_path: Path) -> None:
    """TS2307 (cannot find module) scopes the importer + the missing module's owner."""
    _write(tmp_path, "src/index.ts", 'import { thing } from "./missing.js";\nexport { thing };\n')
    tasks = [
        _Task("index_task", ["src/index.ts"]),
        _Task("missing_task", ["src/missing.ts"]),  # owns the not-yet-created module
        _Task("other_task", ["src/other.ts"]),
    ]
    index = build_path_owner_index(tasks, project_root=tmp_path)
    out = (
        'src/index.ts(1,22): error TS2307: Cannot find module "./missing.js" or its '
        "corresponding type declarations.\n"
    )
    decision = derive_oracle_rerun_scope(
        output=out, project_root=tmp_path, index=index, rung=SCOPE_NARROW
    )
    assert decision.scope is not None, decision.reason
    task_set = set(decision.scope.task_ids)
    assert "index_task" in task_set
    assert "missing_task" in task_set, "the owner of the missing module path must be in scope"
    assert "other_task" not in task_set


def test_cannot_find_name_promotes_to_edge_when_import_derived(tmp_path: Path) -> None:
    """TS2304 for an import-derived name promotes to an importer→exporter edge."""
    _write(tmp_path, "src/index.ts", 'import { helper } from "./util.js";\nexport const r = helper();\n')
    _write(tmp_path, "src/util.ts", "export const other = 1;\n")
    tasks = [_Task("index_task", ["src/index.ts"]), _Task("util_task", ["src/util.ts"])]
    index = build_path_owner_index(tasks, project_root=tmp_path)
    out = "src/index.ts(2,16): error TS2304: Cannot find name 'helper'.\n"
    decision = derive_oracle_rerun_scope(
        output=out, project_root=tmp_path, index=index, rung=SCOPE_NARROW
    )
    assert decision.scope is not None
    assert "index_task" in decision.scope.task_ids
    assert "util_task" in decision.scope.task_ids, "import-derived name → exporter in scope"


def test_allowed_paths_fence_covers_both_ends_and_manifest(tmp_path: Path) -> None:
    """The scope's write-fence allows both edge files + their dirs + manifest."""
    _cross_file_project(tmp_path)
    tasks = [_Task("index_task", ["src/index.ts"]), _Task("cli_task", ["src/cli.ts"])]
    index = build_path_owner_index(tasks, project_root=tmp_path)
    decision = derive_oracle_rerun_scope(
        output=_missing_export_output(),
        project_root=tmp_path,
        index=index,
        rung=SCOPE_NARROW,
        manifest_paths=("package.json",),
    )
    assert decision.scope is not None
    allowed = set(decision.scope.allowed_paths)
    assert "src/index.ts" in allowed and "src/cli.ts" in allowed
    assert "package.json" in allowed, "manifest must be writable by a scoped rerun"


# ═════════════════════════════════════════════════════════════
# Escalation triggers (no owner / too-wide / wide-fan-out)
# ═════════════════════════════════════════════════════════════


def test_no_owner_forces_broad(tmp_path: Path) -> None:
    """Diagnostics whose paths map to no task → escalate straight to broad."""
    _cross_file_project(tmp_path)
    tasks = [_Task("unrelated", ["docs"])]  # owns nothing under src/
    index = build_path_owner_index(tasks, project_root=tmp_path)
    decision = derive_oracle_rerun_scope(
        output=_missing_export_output(), project_root=tmp_path, index=index, rung=SCOPE_NARROW
    )
    assert decision.scope is None
    assert decision.force_broad is True


def test_no_diagnostics_forces_broad(tmp_path: Path) -> None:
    """Unparseable / empty oracle output → broad (we cannot localize blindly)."""
    tasks = [_Task("t", ["src"])]
    index = build_path_owner_index(tasks, project_root=tmp_path)
    decision = derive_oracle_rerun_scope(
        output="some opaque toolchain failure\n", project_root=tmp_path, index=index, rung=SCOPE_NARROW
    )
    assert decision.scope is None and decision.force_broad is True


def test_too_wide_scope_forces_broad(tmp_path: Path) -> None:
    """A scope wider than max(5, 30%) of tasks → broad (cost gap to broad is small)."""
    # 10 importer files each demanding a missing export from its own exporter — 20
    # owners would be > max(5, 0.30*N). Build N small so 30% is the binding bound.
    diagnostics = []
    tasks = []
    for i in range(10):
        _write(tmp_path, f"src/imp{i}.ts", f'import {{ x{i} }} from "./exp{i}.js";\nexport {{ x{i} }};\n')
        _write(tmp_path, f"src/exp{i}.ts", "export const y = 1;\n")
        tasks.append(_Task(f"imp{i}", [f"src/imp{i}.ts"]))
        tasks.append(_Task(f"exp{i}", [f"src/exp{i}.ts"]))
        diagnostics.append(
            f'src/imp{i}.ts(1,10): error TS2305: Module "./exp{i}.js" has no exported member "x{i}".'
        )
    index = build_path_owner_index(tasks, project_root=tmp_path)
    decision = derive_oracle_rerun_scope(
        output="\n".join(diagnostics) + "\n", project_root=tmp_path, index=index, rung=SCOPE_NARROW
    )
    # 20 owners > max(5, 0.30*20=6) → broad.
    assert decision.scope is None and decision.force_broad is True
    assert "exceeds" in decision.reason


def test_wide_fanout_artifact_forces_broad(tmp_path: Path) -> None:
    """A barrel imported by MANY files (measured fan-out) → broad, not narrow."""
    # A barrel ``src/barrel.ts`` imported by 7 consumers; a diagnostic implicating
    # it must go broad (regenerating it touches every consumer).
    _write(tmp_path, "src/barrel.ts", 'import { z } from "./dep.js";\nexport const q = z;\n')
    _write(tmp_path, "src/dep.ts", "export const z = 1;\n")
    for i in range(7):
        _write(tmp_path, f"src/c{i}.ts", 'import { q } from "./barrel.js";\nexport const u = q;\n')
    tasks = [_Task("barrel_task", ["src/barrel.ts"]), _Task("dep_task", ["src/dep.ts"])]
    index = build_path_owner_index(tasks, project_root=tmp_path)
    out = 'src/barrel.ts(1,10): error TS2305: Module "./dep.js" has no exported member "z".\n'
    decision = derive_oracle_rerun_scope(
        output=out, project_root=tmp_path, index=index, rung=SCOPE_NARROW
    )
    assert decision.scope is None and decision.force_broad is True
    assert "fan-out" in decision.reason


def test_tiny_index_is_not_wide_fanout(tmp_path: Path) -> None:
    """A small ``index.ts`` with few importers stays SCOPABLE (no name-overfit)."""
    # index.ts is named like a barrel but only ONE file imports it → not wide.
    _write(tmp_path, "src/index.ts", 'import { runCli } from "./cli.js";\nexport { runCli };\n')
    _write(tmp_path, "src/cli.ts", "export function run(): number { return 0; }\n")
    _write(tmp_path, "src/app.ts", 'import { runCli } from "./index.js";\nexport const a = runCli;\n')
    tasks = [_Task("index_task", ["src/index.ts"]), _Task("cli_task", ["src/cli.ts"])]
    index = build_path_owner_index(tasks, project_root=tmp_path)
    decision = derive_oracle_rerun_scope(
        output=_missing_export_output(), project_root=tmp_path, index=index, rung=SCOPE_NARROW
    )
    assert decision.scope is not None, f"index.ts with 1 importer must stay scopable: {decision.reason}"
    assert set(decision.scope.task_ids) == {"index_task", "cli_task"}


# ═════════════════════════════════════════════════════════════
# Signature (loop-breaker) + ladder
# ═════════════════════════════════════════════════════════════


def test_diagnostic_signature_stable_and_distinguishing() -> None:
    a = [StructuredDiagnostic(code="TS2305", primary_path="src/a.ts", symbol="x", module_specifier="./b")]
    b = [StructuredDiagnostic(code="TS2305", primary_path="src/a.ts", symbol="x", module_specifier="./b")]
    c = [StructuredDiagnostic(code="TS2305", primary_path="src/a.ts", symbol="y", module_specifier="./b")]
    assert diagnostic_signature(a) == diagnostic_signature(b), "same diags ⇒ same signature"
    assert diagnostic_signature(a) != diagnostic_signature(c), "different symbol ⇒ different signature"


def test_next_rung_ladder() -> None:
    assert next_rung(SCOPE_NARROW) == SCOPE_EXPANDED
    assert next_rung(SCOPE_EXPANDED) == SCOPE_BROAD
    assert next_rung(SCOPE_BROAD) is None  # past broad → caller fails honestly


def test_broad_rung_returns_all_tasks_no_fence(tmp_path: Path) -> None:
    tasks = [_Task("a", ["src/a.ts"]), _Task("b", ["src/b.ts"])]
    index = build_path_owner_index(tasks, project_root=tmp_path)
    decision = derive_oracle_rerun_scope(output="", project_root=tmp_path, index=index, rung=SCOPE_BROAD)
    assert decision.scope is not None
    assert decision.scope.is_broad()
    assert set(decision.scope.task_ids) == {"a", "b"}
    assert decision.scope.allowed_paths == (), "broad ⇒ no write-fence"


# ═════════════════════════════════════════════════════════════
# Progress / oscillation classification (the anti-oscillation escalation)
# ═════════════════════════════════════════════════════════════


def _sig(*entries: int) -> tuple:
    """A signature with one 4-tuple entry per int (distinct, comparable)."""
    return tuple(sorted({("TS2305", f"src/f{n}.ts", f"sym{n}", "") for n in entries}))


def test_classify_strict_progress_is_subset() -> None:
    """20 → 4 where the 4 are a SUBSET of the 20 = strict progress (keep narrow)."""
    big = _sig(*range(20))
    smaller_subset = _sig(0, 1, 2, 3)
    assert classify_signature_progress(smaller_subset, big) == PROGRESS_STRICT


def test_classify_oscillation_fewer_but_different() -> None:
    """20 → 4 DIFFERENT errors (not a subset, many new) = oscillation, not progress.

    This is the codex11 failure mode: the SUT 'fixed' the 20 errors but invented 4
    brand-new ones. Exact-equality escalation mis-read this as progress; the
    set-relation classifier catches it as oscillation.
    """
    big = _sig(*range(20))
    four_different = _sig(100, 101, 102, 103)
    assert classify_signature_progress(four_different, big) == PROGRESS_OSCILLATION


def test_classify_oscillation_grew() -> None:
    """4 → 6 (grew, different set) = oscillation (the codex11 second swing)."""
    four = _sig(100, 101, 102, 103)
    six = _sig(200, 201, 202, 203, 204, 205)
    assert classify_signature_progress(six, four) == PROGRESS_OSCILLATION


def test_classify_stuck_when_identical() -> None:
    same = _sig(1, 2, 3)
    assert classify_signature_progress(same, same) == PROGRESS_STUCK


def test_classify_soft_progress_fewer_one_new() -> None:
    """5 → 4 with only ONE new signature = soft progress (allowed once per rung)."""
    five = _sig(0, 1, 2, 3, 4)
    four_one_new = _sig(0, 1, 2, 99)  # 3 carried over + 1 new
    assert classify_signature_progress(four_one_new, five) == PROGRESS_SOFT


def test_classify_cycle_when_signature_recurs() -> None:
    """A signature that reappears in history = cycle (auxiliary escalation)."""
    a = _sig(1, 2)
    b = _sig(3, 4, 5)
    assert classify_signature_progress(a, b, history=[a]) == PROGRESS_CYCLE


def test_classify_first_rerun_is_strict() -> None:
    """previous=None (nothing to compare) ⇒ strict (give the rung its turn)."""
    assert classify_signature_progress(_sig(1, 2, 3), None) == PROGRESS_STRICT


# ═════════════════════════════════════════════════════════════
# Public-surface extraction (the contract-feedback exporter interface)
# ═════════════════════════════════════════════════════════════


def test_extract_public_surface_ts_all_export_forms(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/helpers.ts",
        "export const projectRoot = '.';\n"
        "export function runTempconv(a: string, c: string): number { return 0; }\n"
        "export class CliRunResult {}\n"
        "export interface Opts {}\n"
        "export type Alias = string;\n"
        "export { foo as bar, baz } from './other.js';\n"
        "export default function main() {}\n",
    )
    surface = extract_public_surface("src/helpers.ts", tmp_path)
    assert surface is not None
    names = set(surface)
    assert {"projectRoot", "runTempconv", "CliRunResult", "Opts", "Alias"} <= names
    assert "bar" in names and "baz" in names, "aliased re-export uses the EXPORTED name"
    assert "default" in names, "default export surfaced as the synthetic 'default'"


def test_extract_public_surface_unknown_language_is_none(tmp_path: Path) -> None:
    """A non-extractable language degrades to None (feedback omits the surface)."""
    _write(tmp_path, "mod.py", "def f():\n    return 1\n")
    assert extract_public_surface("mod.py", tmp_path) is None


def test_exporter_surface_for_diagnostics_maps_exporter_to_exports(tmp_path: Path) -> None:
    """A missing-export diagnostic → {exporter_path: [its real exports]}."""
    _write(tmp_path, "src/index.ts", 'import { expectSuccess } from "./helpers.js";\n')
    _write(
        tmp_path,
        "src/helpers.ts",
        "export function runTempconv(): number { return 0; }\nexport const projectRoot = '.';\n",
    )
    diags = [
        StructuredDiagnostic(
            code="TS2305", primary_path="src/index.ts", symbol="expectSuccess", module_specifier="./helpers.js"
        )
    ]
    surfaces = exporter_surface_for_diagnostics(diags, tmp_path)
    assert "src/helpers.ts" in surfaces
    assert set(surfaces["src/helpers.ts"]) == {"runTempconv", "projectRoot"}
    assert "expectSuccess" not in surfaces["src/helpers.ts"], "the invented symbol is NOT in the real surface"


# ═════════════════════════════════════════════════════════════
# Orphan-artifact detection (the ACG invariant primitive)
# ═════════════════════════════════════════════════════════════


def test_find_orphan_artifacts_flags_unowned_file(tmp_path: Path) -> None:
    """A generated source file in a tree NO task owns is an orphan."""
    _write(tmp_path, "src/index.ts", "export const a = 1;\n")
    _write(tmp_path, "e2e/invented.test.ts", "export const x = 1;\n")  # no task owns e2e/
    index = build_path_owner_index([_Task("src_task", ["src/index.ts"])], project_root=tmp_path)
    orphans = [o.path for o in find_orphan_artifacts(index, tmp_path)]
    assert "e2e/invented.test.ts" in orphans
    assert "src/index.ts" not in orphans, "an owned file is not an orphan"


def test_find_orphan_artifacts_adopts_dir_owned_helper(tmp_path: Path) -> None:
    """adopt-or-reject: a helper under a task's OWN output dir is adopted (not orphan)."""
    _write(tmp_path, "src/index.ts", "export const a = 1;\n")
    _write(tmp_path, "src/helper.ts", "export const h = 1;\n")  # undeclared, but under src/
    # A task owns the whole src/ directory → src/helper.ts is dir-owned (adopted).
    index = build_path_owner_index([_Task("src_task", ["src"])], project_root=tmp_path)
    orphans = [o.path for o in find_orphan_artifacts(index, tmp_path)]
    assert orphans == [], f"a legitimate dir-owned helper must be adopted, not flagged: {orphans}"


def test_find_orphan_artifacts_extra_owned_escape_hatch(tmp_path: Path) -> None:
    """A path in ``extra_owned`` (harness/profile contract) is never an orphan."""
    _write(tmp_path, "scripts/build.ts", "export const b = 1;\n")
    index = build_path_owner_index([_Task("t", ["src"])], project_root=tmp_path)
    assert [o.path for o in find_orphan_artifacts(index, tmp_path)] == ["scripts/build.ts"]
    exempt = find_orphan_artifacts(index, tmp_path, extra_owned=("scripts/build.ts",))
    assert exempt == [], "extra_owned must exempt the path"


# ─────────────────────────────────────────────────────────────
# Gate-level escalation with a FAKE oracle (no toolchain needed)
# ─────────────────────────────────────────────────────────────


class _FakeOracle:
    """A scripted oracle: returns a queued ``ImplementOracleResult`` per run.

    Lets us drive the gate's escalation ladder deterministically without tsc:
    the sequence of results models "scoped rerun did not fix it (same signature)
    → escalate → … → finally passes / honestly fails".
    """

    def __init__(self, results: list[ImplementOracleResult]) -> None:
        self._results = list(results)
        self.runs = 0

    def __call__(self, project_root, profile, spec, config) -> ImplementOracleResult:
        self.runs += 1
        if self._results:
            return self._results.pop(0)
        return self._results_default

    _results_default = None


def _fail_result(diags) -> ImplementOracleResult:
    from codd.implement_oracle import EVIDENCE_MISSING_SYMBOL, ImplementOracleFinding

    return ImplementOracleResult(
        passed=False,
        executed=True,
        command="tsc --noEmit",
        findings=[ImplementOracleFinding(category=EVIDENCE_MISSING_SYMBOL, code="TS2305", message="x")],
        diagnostics=list(diags),
        raw_output='src/index.ts(1,10): error TS2305: Module "./cli.js" has no exported member "runCli".\n',
        detail="native oracle failed",
    )


def _pass_result() -> ImplementOracleResult:
    return ImplementOracleResult(passed=True, executed=True, command="tsc --noEmit", detail="clean")


@pytest.fixture
def _patched_gate(tmp_path: Path, monkeypatch):
    """Patch the gate's heavy bits (install/certify/run) so only the loop runs."""
    import codd.implement_oracle as mod

    monkeypatch.setattr(mod, "_run_node_install", lambda *a, **k: None)
    monkeypatch.setattr(mod, "certify_oracle_scope", lambda *a, **k: "certified (test)")
    return mod


def _ts_index(tmp_path: Path):
    _cross_file_project(tmp_path)
    tasks = [
        _Task("index_task", ["src/index.ts"]),
        _Task("cli_task", ["src/cli.ts"]),
        _Task("extra_task", ["src/extra.ts"]),
    ]
    return build_path_owner_index(tasks, project_root=tmp_path)


def test_gate_scoped_rerun_passes_records_scope(tmp_path: Path, _patched_gate, monkeypatch) -> None:
    """A scoped rerun that fixes the incoherence → PASS, with a NARROW scope."""
    mod = _patched_gate
    diags = [StructuredDiagnostic(code="TS2305", primary_path="src/index.ts", symbol="runCli", module_specifier="./cli.js")]
    oracle = _FakeOracle([_fail_result(diags), _pass_result()])
    monkeypatch.setattr(mod, "_run_oracle_command", oracle)
    index = _ts_index(tmp_path)

    seen_scopes: list = []

    def rerun(feedback: str, scope=None) -> None:
        seen_scopes.append(scope)

    result = run_implement_oracle_gate(
        tmp_path,
        language="typescript",
        project_name="demo-cli",
        config={"implement": {"oracle_max_attempts": 4}},
        rerun=rerun,
        echo=lambda _m: None,
        scope_index=index,
    )
    assert result.passed is True
    assert len(seen_scopes) == 1, "one corrective rerun"
    assert seen_scopes[0] is not None and not seen_scopes[0].is_broad()
    assert set(seen_scopes[0].task_ids) == {"index_task", "cli_task"}


def test_gate_signature_unchanged_escalates_to_broad(tmp_path: Path, _patched_gate, monkeypatch) -> None:
    """Same signature after a scoped rerun → escalate narrow→expanded→broad."""
    mod = _patched_gate
    diags = [StructuredDiagnostic(code="TS2305", primary_path="src/index.ts", symbol="runCli", module_specifier="./cli.js")]
    # 4 failing runs with the SAME signature, then it stops at the attempt cap.
    oracle = _FakeOracle([_fail_result(diags) for _ in range(5)])
    monkeypatch.setattr(mod, "_run_oracle_command", oracle)
    index = _ts_index(tmp_path)

    rungs: list[str] = []

    def rerun(feedback: str, scope=None) -> None:
        rungs.append("broad" if scope is None or scope.is_broad() else scope.rung)

    result = run_implement_oracle_gate(
        tmp_path,
        language="typescript",
        project_name="demo-cli",
        config={"implement": {"oracle_max_attempts": 4}},  # initial + 3 reruns
        rerun=rerun,
        echo=lambda _m: None,
        scope_index=index,
    )
    assert result.passed is False
    # attempt budget = 4 total → 3 reruns. First scoped (narrow), then escalate.
    assert rungs[0] == SCOPE_NARROW, rungs
    assert SCOPE_EXPANDED in rungs, f"must escalate to expanded: {rungs}"
    assert "broad" in rungs, f"must escalate to broad: {rungs}"


def test_gate_no_index_reruns_broad(tmp_path: Path, _patched_gate, monkeypatch) -> None:
    """Back-compat: with no scope_index every rerun is broad (scope=None)."""
    mod = _patched_gate
    diags = [StructuredDiagnostic(code="TS2305", primary_path="src/index.ts", symbol="runCli")]
    oracle = _FakeOracle([_fail_result(diags), _pass_result()])
    monkeypatch.setattr(mod, "_run_oracle_command", oracle)

    scopes: list = []

    def rerun(feedback: str, scope=None) -> None:
        scopes.append(scope)

    result = run_implement_oracle_gate(
        tmp_path,
        language="typescript",
        project_name="demo-cli",
        config={"implement": {"oracle_max_attempts": 3}},
        rerun=rerun,
        echo=lambda _m: None,
        # NO scope_index
    )
    assert result.passed is True
    assert scopes == [None], "no index ⇒ broad rerun (scope None)"


def test_gate_legacy_single_arg_callback_still_works(tmp_path: Path, _patched_gate, monkeypatch) -> None:
    """A legacy ``rerun(feedback)`` callback is invoked with one arg (no crash)."""
    mod = _patched_gate
    diags = [StructuredDiagnostic(code="TS2305", primary_path="src/index.ts", symbol="runCli")]
    oracle = _FakeOracle([_fail_result(diags), _pass_result()])
    monkeypatch.setattr(mod, "_run_oracle_command", oracle)

    calls = {"n": 0}

    def legacy_rerun(feedback: str) -> None:  # single positional only
        calls["n"] += 1

    result = run_implement_oracle_gate(
        tmp_path,
        language="typescript",
        project_name="demo-cli",
        config={"implement": {"oracle_max_attempts": 3}},
        rerun=legacy_rerun,
        echo=lambda _m: None,
        scope_index=_ts_index(tmp_path),
    )
    assert result.passed is True
    assert calls["n"] == 1


# ─────────────────────────────────────────────────────────────
# Anti-oscillation escalation + ladder budget (the new behaviour)
# ─────────────────────────────────────────────────────────────


def _fail_with_diags(n: int, *, base: int) -> ImplementOracleResult:
    """A failing oracle result carrying ``n`` DISTINCT diagnostics (ids from base).

    Distinct ``primary_path``/``symbol`` per id ⇒ a distinct signature, so a
    sequence with different ``base`` values models OSCILLATION (the SUT invents a
    different error set each rerun), while reusing ``base`` models a STUCK set.
    """
    from codd.implement_oracle import EVIDENCE_MISSING_SYMBOL, ImplementOracleFinding

    diags = [
        StructuredDiagnostic(
            code="TS2305",
            primary_path=f"src/f{base + i}.ts",
            symbol=f"sym{base + i}",
            module_specifier="./cli.js",
        )
        for i in range(n)
    ]
    return ImplementOracleResult(
        passed=False,
        executed=True,
        command="tsc --noEmit",
        findings=[ImplementOracleFinding(category=EVIDENCE_MISSING_SYMBOL, code="TS2305", message="x")],
        diagnostics=diags,
        raw_output='src/index.ts(1,10): error TS2305: Module "./cli.js" has no exported member "runCli".\n',
        detail=f"native oracle failed ({n} diagnostics)",
    )


def test_gate_oscillation_escalates_not_pinned_to_narrow(tmp_path: Path, _patched_gate, monkeypatch) -> None:
    """The codex11 failure mode: 20 → 4 → 6 (all DIFFERENT) must ESCALATE.

    Under the OLD exact-equality loop-breaker these three runs never matched (the
    sets differ each time) so the gate stayed pinned at NARROW until the budget was
    spent. The set-relation classifier sees oscillation and escalates the ladder,
    so ``expanded`` (and beyond) is reached — the whole point of the fix.
    """
    mod = _patched_gate
    # 20 → 4 → 6 → 6 → 6 : oscillation on every transition (disjoint id ranges).
    oracle = _FakeOracle(
        [
            _fail_with_diags(20, base=0),
            _fail_with_diags(4, base=100),
            _fail_with_diags(6, base=200),
            _fail_with_diags(6, base=300),
            _fail_with_diags(6, base=400),
        ]
    )
    monkeypatch.setattr(mod, "_run_oracle_command", oracle)
    index = _ts_index(tmp_path)

    rungs: list[str] = []

    def rerun(feedback: str, scope=None) -> None:
        rungs.append("broad" if scope is None or scope.is_broad() else scope.rung)

    result = run_implement_oracle_gate(
        tmp_path,
        language="typescript",
        project_name="demo-cli",
        config={"implement": {"oracle_max_attempts": 5}},  # the new ladder budget
        rerun=rerun,
        echo=lambda _m: None,
        scope_index=index,
    )
    assert result.passed is False
    assert rungs[0] == SCOPE_NARROW, rungs
    # The oscillation (4 vs 20, then 6 vs 4) escalates instead of staying narrow.
    assert SCOPE_EXPANDED in rungs, f"oscillation must escalate past narrow: {rungs}"
    assert "broad" in rungs, f"continued oscillation must reach broad: {rungs}"


def test_gate_strict_progress_keeps_narrow(tmp_path: Path, _patched_gate, monkeypatch) -> None:
    """A strict shrink (subset) keeps the rung at NARROW — no premature escalation."""
    mod = _patched_gate
    # 20 ⊃ 8 ⊃ 2 (strict subsets via overlapping low id ranges), then pass.
    oracle = _FakeOracle(
        [
            _fail_with_diags(20, base=0),
            _fail_with_diags(8, base=0),  # {f0..f7} ⊂ {f0..f19}
            _fail_with_diags(2, base=0),  # {f0,f1} ⊂ {f0..f7}
            _pass_result(),
        ]
    )
    monkeypatch.setattr(mod, "_run_oracle_command", oracle)
    index = _ts_index(tmp_path)

    rungs: list[str] = []

    def rerun(feedback: str, scope=None) -> None:
        rungs.append("broad" if scope is None or scope.is_broad() else scope.rung)

    result = run_implement_oracle_gate(
        tmp_path,
        language="typescript",
        project_name="demo-cli",
        config={"implement": {"oracle_max_attempts": 5}},
        rerun=rerun,
        echo=lambda _m: None,
        scope_index=index,
    )
    assert result.passed is True
    assert rungs == [SCOPE_NARROW, SCOPE_NARROW, SCOPE_NARROW], f"strict progress stays narrow: {rungs}"


def test_gate_default_budget_reaches_broad(tmp_path: Path, _patched_gate, monkeypatch) -> None:
    """With the DEFAULT cap (5) the ladder can reach broad on persistent failure.

    No explicit ``oracle_max_attempts`` — proves the new default (5 = initial +
    narrow≤2 + expanded + broad) is sized so every rung is reachable (the old
    default 3 died at narrow).
    """
    mod = _patched_gate
    # Stuck (identical) signature every run → escalate each rerun until broad.
    oracle = _FakeOracle([_fail_with_diags(3, base=0) for _ in range(6)])
    monkeypatch.setattr(mod, "_run_oracle_command", oracle)
    index = _ts_index(tmp_path)

    rungs: list[str] = []

    def rerun(feedback: str, scope=None) -> None:
        rungs.append("broad" if scope is None or scope.is_broad() else scope.rung)

    result = run_implement_oracle_gate(
        tmp_path,
        language="typescript",
        project_name="demo-cli",
        config={},  # DEFAULT budget
        rerun=rerun,
        echo=lambda _m: None,
        scope_index=index,
    )
    assert result.passed is False
    assert "broad" in rungs, f"default budget must reach broad: {rungs}"
    # initial + up to 4 reruns; broad is the last reachable rung.
    assert rungs[0] == SCOPE_NARROW and rungs[-1] == "broad", rungs


# ─────────────────────────────────────────────────────────────
# Contract-aware feedback (exporter surface + targeted-edit directive)
# ─────────────────────────────────────────────────────────────


def test_gate_scoped_feedback_carries_exporter_surface_and_targeted_edit(
    tmp_path: Path, _patched_gate, monkeypatch
) -> None:
    """A scoped rerun's feedback shows the exporter's REAL surface + the edit fence.

    Reproduces the codex11 case (importer demands ``expectSuccess`` the exporter
    never exports): the feedback must (a) list the exporter's actual exports so the
    SUT reconciles to a real symbol, and (b) carry the targeted-edit/minimal-diff
    directive with the allowed-paths fence — the convergence levers.
    """
    mod = _patched_gate
    _write(tmp_path, "src/index.ts", 'import { expectSuccess } from "./cli.js";\nexport { expectSuccess };\n')
    _write(tmp_path, "src/cli.ts", "export function run(): number { return 0; }\nexport const helper = 1;\n")
    tasks = [_Task("index_task", ["src/index.ts"]), _Task("cli_task", ["src/cli.ts"])]
    index = build_path_owner_index(tasks, project_root=tmp_path)

    diags = [
        StructuredDiagnostic(
            code="TS2305", primary_path="src/index.ts", symbol="expectSuccess", module_specifier="./cli.js"
        )
    ]
    oracle = _FakeOracle([_fail_result(diags), _pass_result()])
    monkeypatch.setattr(mod, "_run_oracle_command", oracle)

    seen: list[str] = []

    def rerun(feedback: str, scope=None) -> None:
        seen.append(feedback)

    result = run_implement_oracle_gate(
        tmp_path,
        language="typescript",
        project_name="demo-cli",
        config={"implement": {"oracle_max_attempts": 4}},
        rerun=rerun,
        echo=lambda _m: None,
        scope_index=index,
    )
    assert result.passed is True
    assert len(seen) == 1
    fb = seen[0]
    # (a) exporter surface — the REAL exports of ./cli, NOT the invented symbol.
    assert "CURRENT PUBLIC INTERFACE" in fb
    assert "src/cli.ts" in fb and "run" in fb and "helper" in fb
    # (b) targeted-edit directive + the write-fence allow-list.
    assert "TARGETED EDIT" in fb
    assert "You may ONLY create/modify these paths" in fb
    assert "src/index.ts" in fb and "src/cli.ts" in fb
    # the no-invent contract rule is always present.
    assert "Do NOT invent" in fb


def test_gate_broad_feedback_has_no_targeted_edit_block(tmp_path: Path, _patched_gate, monkeypatch) -> None:
    """A BROAD rerun (no scope) gets the surface but NOT the minimal-diff fence.

    Broad legitimately regenerates everything, so it must not be told to make a
    'smallest edit' to a fixed file list.
    """
    mod = _patched_gate
    diags = [StructuredDiagnostic(code="TS2305", primary_path="src/index.ts", symbol="runCli", module_specifier="./cli.js")]
    oracle = _FakeOracle([_fail_result(diags), _pass_result()])
    monkeypatch.setattr(mod, "_run_oracle_command", oracle)

    seen: list[str] = []

    def rerun(feedback: str, scope=None) -> None:
        seen.append(feedback)

    run_implement_oracle_gate(
        tmp_path,
        language="typescript",
        project_name="demo-cli",
        config={"implement": {"oracle_max_attempts": 3}},
        rerun=rerun,
        echo=lambda _m: None,
        # NO scope_index ⇒ broad
    )
    assert len(seen) == 1
    assert "TARGETED EDIT" not in seen[0], "broad rerun must not carry the minimal-diff fence"


# ─────────────────────────────────────────────────────────────
# Orphan-artifact gate (warn default / enforce / off) at gate level
# ─────────────────────────────────────────────────────────────


def test_gate_orphan_warn_records_but_passes(tmp_path: Path, _patched_gate, monkeypatch) -> None:
    """WARN (default): a clean typecheck with an orphan still PASSES but records it."""
    mod = _patched_gate
    _write(tmp_path, "src/index.ts", "export const a = 1;\n")
    _write(tmp_path, "e2e/invented.test.ts", "export const x = 1;\n")  # unowned orphan
    index = build_path_owner_index([_Task("src_task", ["src/index.ts"])], project_root=tmp_path)
    oracle = _FakeOracle([_pass_result()])  # typecheck clean from the start
    monkeypatch.setattr(mod, "_run_oracle_command", oracle)

    result = run_implement_oracle_gate(
        tmp_path,
        language="typescript",
        project_name="demo-cli",
        config={},  # default orphan_artifact_gate = warn
        rerun=lambda *a, **k: None,
        echo=lambda _m: None,
        scope_index=index,
    )
    assert result.passed is True, "warn must NOT block"
    assert "e2e/invented.test.ts" in result.orphan_artifacts


def test_gate_orphan_enforce_fails(tmp_path: Path, _patched_gate, monkeypatch) -> None:
    """ENFORCE: a clean typecheck with an orphan is flipped to a HARD failure."""
    mod = _patched_gate
    _write(tmp_path, "src/index.ts", "export const a = 1;\n")
    _write(tmp_path, "e2e/invented.test.ts", "export const x = 1;\n")
    index = build_path_owner_index([_Task("src_task", ["src/index.ts"])], project_root=tmp_path)
    oracle = _FakeOracle([_pass_result()])
    monkeypatch.setattr(mod, "_run_oracle_command", oracle)

    result = run_implement_oracle_gate(
        tmp_path,
        language="typescript",
        project_name="demo-cli",
        config={"implement": {"orphan_artifact_gate": "enforce"}},
        rerun=lambda *a, **k: None,
        echo=lambda _m: None,
        scope_index=index,
    )
    assert result.passed is False, "enforce must fail on an orphan"
    assert any(f.code == "orphan_artifact" for f in result.findings)
    assert "e2e/invented.test.ts" in result.orphan_artifacts


def test_gate_orphan_off_ignores(tmp_path: Path, _patched_gate, monkeypatch) -> None:
    """OFF: the orphan gate is a no-op (no record, no fail)."""
    mod = _patched_gate
    _write(tmp_path, "src/index.ts", "export const a = 1;\n")
    _write(tmp_path, "e2e/invented.test.ts", "export const x = 1;\n")
    index = build_path_owner_index([_Task("src_task", ["src/index.ts"])], project_root=tmp_path)
    oracle = _FakeOracle([_pass_result()])
    monkeypatch.setattr(mod, "_run_oracle_command", oracle)

    result = run_implement_oracle_gate(
        tmp_path,
        language="typescript",
        project_name="demo-cli",
        config={"implement": {"orphan_artifact_gate": "off"}},
        rerun=lambda *a, **k: None,
        echo=lambda _m: None,
        scope_index=index,
    )
    assert result.passed is True
    assert result.orphan_artifacts == []
