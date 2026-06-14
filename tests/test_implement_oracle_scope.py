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
    SCOPE_BROAD,
    SCOPE_EXPANDED,
    SCOPE_NARROW,
    StructuredDiagnostic,
    build_path_owner_index,
    derive_oracle_rerun_scope,
    diagnostic_signature,
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
