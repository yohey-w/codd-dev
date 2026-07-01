"""Pipeline wiring of the SCOPED oracle rerun: the write-fence + scope dispatch.

These exercise the greenfield ``GreenfieldPipeline`` side of the scoped rerun:

1. **Write-fence** (``_OracleWriteFence``): a scoped rerun is fenced to the
   scope's allowed paths. An out-of-scope CREATE / MODIFY / DELETE the SUT makes
   during the rerun is reverted; an in-scope write is kept; a broad rerun (empty
   allow-set) imposes NO fence. This is the anti-false-green guard that keeps a
   "targeted" rerun from silently regenerating the tree.

2. **Scope dispatch** (``_rerun_tasks_with_feedback``): a scoped scope
   re-implements ONLY its tasks; a broad/None scope re-implements ALL tasks.
"""

from __future__ import annotations

from pathlib import Path

import codd.greenfield.pipeline as pipeline_mod
from codd.greenfield.pipeline import GreenfieldPipeline, ImplementTaskRef, _OracleWriteFence
from codd.implement_oracle_scope import OracleRerunScope


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ═════════════════════════════════════════════════════════════
# (c) Write-fence
# ═════════════════════════════════════════════════════════════


def test_write_fence_reverts_out_of_scope_create(tmp_path: Path) -> None:
    """A file CREATED out of scope during the fenced block is removed."""
    _write(tmp_path, "src/index.ts", "export const a = 1;\n")
    msgs: list[str] = []
    with _OracleWriteFence(tmp_path, allowed_paths=("src/index.ts",), echo=msgs.append) as fence:
        # SUT writes an in-scope file (kept) AND an out-of-scope file (reverted).
        _write(tmp_path, "src/index.ts", "export const a = 2;\n")  # in scope
        _write(tmp_path, "src/sneaky.ts", "export const b = 3;\n")  # OUT of scope
        fence.enforce()
    assert (tmp_path / "src/index.ts").read_text() == "export const a = 2;\n", "in-scope kept"
    assert not (tmp_path / "src/sneaky.ts").exists(), "out-of-scope create reverted"
    assert any("reverted" in m for m in msgs)


def test_write_fence_restores_out_of_scope_modify(tmp_path: Path) -> None:
    """A file MODIFIED out of scope is restored to its pre-rerun bytes."""
    _write(tmp_path, "src/other.ts", "ORIGINAL\n")
    _write(tmp_path, "src/owned.ts", "v1\n")
    with _OracleWriteFence(tmp_path, allowed_paths=("src/owned.ts",), echo=lambda _m: None) as fence:
        _write(tmp_path, "src/owned.ts", "v2\n")  # in scope
        _write(tmp_path, "src/other.ts", "TAMPERED\n")  # OUT of scope
        fence.enforce()
    assert (tmp_path / "src/owned.ts").read_text() == "v2\n", "in-scope modify kept"
    assert (tmp_path / "src/other.ts").read_text() == "ORIGINAL\n", "out-of-scope modify restored"


def test_write_fence_restores_out_of_scope_delete(tmp_path: Path) -> None:
    """A file DELETED out of scope during the rerun is re-created."""
    _write(tmp_path, "src/keep.ts", "do not delete\n")
    _write(tmp_path, "src/owned.ts", "v1\n")
    with _OracleWriteFence(tmp_path, allowed_paths=("src/owned.ts",), echo=lambda _m: None) as fence:
        (tmp_path / "src/keep.ts").unlink()  # OUT of scope deletion
        fence.enforce()
    assert (tmp_path / "src/keep.ts").read_text() == "do not delete\n", "out-of-scope delete restored"


def test_write_fence_allows_dir_prefix(tmp_path: Path) -> None:
    """An allowed DIRECTORY permits any file written under it."""
    _write(tmp_path, "src/a.ts", "1\n")
    with _OracleWriteFence(tmp_path, allowed_paths=("src",), echo=lambda _m: None) as fence:
        _write(tmp_path, "src/new_under_dir.ts", "ok\n")  # under allowed dir
        _write(tmp_path, "lib/outside.ts", "nope\n")  # outside
        fence.enforce()
    assert (tmp_path / "src/new_under_dir.ts").exists(), "write under allowed dir kept"
    assert not (tmp_path / "lib/outside.ts").exists(), "write outside allowed dir reverted"


def test_write_fence_ignores_node_modules(tmp_path: Path) -> None:
    """Vendored deps are never snapshotted/fenced (an install touching them is fine)."""
    _write(tmp_path, "src/a.ts", "1\n")
    with _OracleWriteFence(tmp_path, allowed_paths=("src/a.ts",), echo=lambda _m: None) as fence:
        _write(tmp_path, "node_modules/dep/index.js", "vendored\n")
        fence.enforce()
    assert (tmp_path / "node_modules/dep/index.js").exists(), "node_modules untouched by fence"


def test_write_fence_rejects_invented_orphan_test_file(tmp_path: Path) -> None:
    """ACG invariant (3): a scoped rerun may NOT create an unowned artifact.

    The codex11 oscillation invented a CONTRACT-OUTSIDE e2e test file each scoped
    rerun (a different unowned artifact that re-broke the typecheck). The fence is
    the live enforcement of 'scoped rerun may not create unowned artifacts': the
    invented orphan test outside the scope's allowed paths is reverted, so it can
    never persist to re-poison the next attempt.
    """
    _write(tmp_path, "src/cli.ts", "export function run(): number { return 0; }\n")
    msgs: list[str] = []
    with _OracleWriteFence(tmp_path, allowed_paths=("src/cli.ts",), echo=msgs.append) as fence:
        # The non-deterministic SUT 'reconciles' by inventing an unowned e2e test
        # (the contract-outside artifact) instead of fixing the real import.
        _write(tmp_path, "tests/e2e/invented-boundary.e2e.test.ts", "export const x = 1;\n")
        fence.enforce()
    assert not (tmp_path / "tests/e2e/invented-boundary.e2e.test.ts").exists(), (
        "an invented unowned artifact during a scoped rerun must be reverted"
    )
    assert any("reverted" in m for m in msgs)


# ═════════════════════════════════════════════════════════════
# (2) Scope dispatch in _rerun_tasks_with_feedback
# ═════════════════════════════════════════════════════════════


def _capture_reimplemented(monkeypatch) -> list[str]:
    """Patch implement_tasks to RECORD the design nodes it was asked to re-run."""
    reimplemented: list[str] = []

    def fake_implement_tasks(project_root, *, design=None, **kwargs):
        reimplemented.append(design)

        class _R:
            error = None
            generated_files: list = []

        return [_R()]

    import codd.implementer as implementer_mod

    monkeypatch.setattr(implementer_mod, "implement_tasks", fake_implement_tasks)
    # The pipeline imports implement_tasks lazily from codd.implementer, so the
    # patch on the module attribute is what the import picks up.
    return reimplemented


def _tasks() -> list[ImplementTaskRef]:
    return [
        ImplementTaskRef(task_id="a", design_node="design/a.md", output_paths=("src/a.ts",)),
        ImplementTaskRef(task_id="b", design_node="design/b.md", output_paths=("src/b.ts",)),
        ImplementTaskRef(task_id="c", design_node="design/c.md", output_paths=("src/c.ts",)),
    ]


def test_scoped_rerun_only_reimplements_scope_tasks(tmp_path: Path, monkeypatch) -> None:
    """A scoped scope re-implements ONLY its tasks (not all)."""
    _write(tmp_path, "src/a.ts", "1\n")
    _write(tmp_path, "src/b.ts", "1\n")
    _write(tmp_path, "src/c.ts", "1\n")
    import codd.config as _config_mod
    monkeypatch.setattr(_config_mod, "load_project_config", lambda root: {})
    monkeypatch.setattr(pipeline_mod, "_output_paths_for_task", lambda config, task: list(task.output_paths))
    reimplemented = _capture_reimplemented(monkeypatch)

    scope = OracleRerunScope(
        rung="narrow",
        task_ids=("a", "b"),
        allowed_paths=("src/a.ts", "src/b.ts"),
    )
    GreenfieldPipeline()._rerun_tasks_with_feedback(
        tmp_path, _tasks(), "feedback", {}, scope=scope
    )
    assert set(reimplemented) == {"design/a.md", "design/b.md"}, reimplemented
    assert "design/c.md" not in reimplemented, "out-of-scope task must NOT be re-implemented"


def test_broad_scope_reimplements_all_tasks(tmp_path: Path, monkeypatch) -> None:
    """A broad scope (or None) re-implements EVERY task (legacy behaviour)."""
    import codd.config as _config_mod
    monkeypatch.setattr(_config_mod, "load_project_config", lambda root: {})
    monkeypatch.setattr(pipeline_mod, "_output_paths_for_task", lambda config, task: list(task.output_paths))
    reimplemented = _capture_reimplemented(monkeypatch)

    GreenfieldPipeline()._rerun_tasks_with_feedback(tmp_path, _tasks(), "feedback", {}, scope=None)
    assert set(reimplemented) == {"design/a.md", "design/b.md", "design/c.md"}


def test_scoped_rerun_applies_fence(tmp_path: Path, monkeypatch) -> None:
    """End-to-end: a scoped rerun whose fake SUT writes out of scope is fenced.

    The fake implementer writes its in-scope file AND an out-of-scope file; after
    ``_rerun_tasks_with_feedback`` the out-of-scope write must be gone.
    """
    _write(tmp_path, "src/a.ts", "orig-a\n")
    _write(tmp_path, "src/c.ts", "orig-c\n")
    import codd.config as _config_mod
    monkeypatch.setattr(_config_mod, "load_project_config", lambda root: {})
    monkeypatch.setattr(pipeline_mod, "_output_paths_for_task", lambda config, task: list(task.output_paths))

    def fake_implement_tasks(project_root, *, design=None, **kwargs):
        # Re-implementing task "a" writes its own file (in scope) AND tampers with
        # task "c"'s file (out of scope).
        _write(Path(project_root), "src/a.ts", "regenerated-a\n")
        _write(Path(project_root), "src/c.ts", "TAMPERED-c\n")

        class _R:
            error = None
            generated_files: list = []

        return [_R()]

    import codd.implementer as implementer_mod

    monkeypatch.setattr(implementer_mod, "implement_tasks", fake_implement_tasks)

    scope = OracleRerunScope(rung="narrow", task_ids=("a",), allowed_paths=("src/a.ts",))
    GreenfieldPipeline()._rerun_tasks_with_feedback(tmp_path, _tasks(), "fb", {}, scope=scope)

    assert (tmp_path / "src/a.ts").read_text() == "regenerated-a\n", "in-scope regeneration kept"
    assert (tmp_path / "src/c.ts").read_text() == "orig-c\n", "out-of-scope tamper reverted by fence"


def test_empty_scope_falls_back_to_broad(tmp_path: Path, monkeypatch) -> None:
    """A scoped scope whose task_ids match no task → broad (never a silent no-op)."""
    import codd.config as _config_mod
    monkeypatch.setattr(_config_mod, "load_project_config", lambda root: {})
    monkeypatch.setattr(pipeline_mod, "_output_paths_for_task", lambda config, task: list(task.output_paths))
    reimplemented = _capture_reimplemented(monkeypatch)

    scope = OracleRerunScope(rung="narrow", task_ids=("nonexistent",), allowed_paths=("x.ts",))
    GreenfieldPipeline()._rerun_tasks_with_feedback(tmp_path, _tasks(), "fb", {}, scope=scope)
    assert set(reimplemented) == {"design/a.md", "design/b.md", "design/c.md"}, "empty scope ⇒ broad"


# ═════════════════════════════════════════════════════════════
# (3) Per-task resilience: one stuck task must not abort the rest, and a
#     multi-task scope must never collapse into one combined call.
# ═════════════════════════════════════════════════════════════


def test_each_scoped_task_gets_its_own_separate_call_not_a_batch(tmp_path: Path, monkeypatch) -> None:
    """A scope spanning N tasks issues N separate ``implement_tasks`` calls —
    never one call for the union of their design nodes/output paths.

    Direct evidence against "a multi-task rerun scope collapses into one
    oversized combined AI call": each invocation is asked to reimplement
    exactly ONE task's own ``design``/``output_paths``, never more than one.
    This holds for both multi-task-scope producers that share this dispatch —
    the VB coverage gate's batched test-task scope
    (``codd.vb_rerun_scope.derive_vb_rerun_scope``) and the native oracle's
    both-ends-of-a-broken-edge scope (``codd.implement_oracle_scope``).
    """
    import codd.config as _config_mod

    monkeypatch.setattr(_config_mod, "load_project_config", lambda root: {})
    monkeypatch.setattr(pipeline_mod, "_output_paths_for_task", lambda config, task: list(task.output_paths))

    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_implement_tasks(project_root, *, design=None, output_paths=None, **kwargs):
        calls.append((design, tuple(output_paths or ())))

        class _R:
            error = None
            generated_files: list = []

        return [_R()]

    import codd.implementer as implementer_mod

    monkeypatch.setattr(implementer_mod, "implement_tasks", fake_implement_tasks)

    scope = OracleRerunScope(
        rung="vb_targeted",
        task_ids=("a", "b", "c"),
        allowed_paths=("src/a.ts", "src/b.ts", "src/c.ts"),
    )
    GreenfieldPipeline()._rerun_tasks_with_feedback(tmp_path, _tasks(), "fb", {}, scope=scope)

    assert len(calls) == 3, "3 scoped tasks must yield 3 separate calls, never 1 combined call"
    by_design = {design: paths for design, paths in calls}
    assert by_design["design/a.md"] == ("src/a.ts",)
    assert by_design["design/b.md"] == ("src/b.ts",)
    assert by_design["design/c.md"] == ("src/c.ts",)


def test_one_task_exhaustion_does_not_abort_the_others(tmp_path: Path, monkeypatch) -> None:
    """A multi-task scope keeps re-implementing the REMAINING tasks after one
    task's bounded generation budget exhausts (``CoddCLIError``).

    Regression for a shared bug in ``_reimplement_tasks``: a single task's
    persistent malformed/empty AI output during a multi-task scope (the VB
    coverage gate's batched test-task scope, or the native oracle's
    both-ends-of-a-broken-edge scope) used to raise UNCAUGHT, aborting the
    whole rerun before the OTHER tasks in the SAME scope were even attempted —
    which also silently reduced the coverage gate's configured
    ``max_retries`` to one effective attempt, and bypassed the oracle's own
    escalation ladder entirely. Confirms the fix: task "b" exhausts, but "a"
    and "c" are still (separately) reimplemented, and the call does not raise.
    """
    import codd.config as _config_mod

    monkeypatch.setattr(_config_mod, "load_project_config", lambda root: {})
    monkeypatch.setattr(pipeline_mod, "_output_paths_for_task", lambda config, task: list(task.output_paths))

    from codd.cli import CoddCLIError

    reimplemented: list[str] = []

    def fake_implement_tasks(project_root, *, design=None, **kwargs):
        reimplemented.append(design)
        if design == "design/b.md":
            raise CoddCLIError("Design 'design/b.md' produced 0 generated files.")

        class _R:
            error = None
            generated_files: list = []

        return [_R()]

    import codd.implementer as implementer_mod

    monkeypatch.setattr(implementer_mod, "implement_tasks", fake_implement_tasks)

    scope = OracleRerunScope(
        rung="narrow",
        task_ids=("a", "b", "c"),
        allowed_paths=("src/a.ts", "src/b.ts", "src/c.ts"),
    )
    # Must NOT raise — the caller (coverage gate / oracle gate) re-derives
    # ground truth afterwards and reacts to whatever is still broken.
    GreenfieldPipeline()._rerun_tasks_with_feedback(tmp_path, _tasks(), "fb", {}, scope=scope)

    assert reimplemented == ["design/a.md", "design/b.md", "design/c.md"], (
        "every scoped task must get its OWN separate implement_tasks() call, "
        "in order, even though 'b' failed"
    )


def test_non_content_exception_still_propagates(tmp_path: Path, monkeypatch) -> None:
    """A SYSTEMIC failure (not a per-task content exhaustion) still aborts.

    Only ``CoddCLIError`` — a task's own bounded no-usable/syntax-gate retry
    budget exhausting — is absorbed so the remaining scoped tasks get a
    chance. Anything else (e.g. an environment/config error) is not a "this
    one task's AI output was bad" signal and must keep failing fast, unchanged.
    """
    import pytest

    import codd.config as _config_mod

    monkeypatch.setattr(_config_mod, "load_project_config", lambda root: {})
    monkeypatch.setattr(pipeline_mod, "_output_paths_for_task", lambda config, task: list(task.output_paths))

    def fake_implement_tasks(project_root, *, design=None, **kwargs):
        raise RuntimeError("disk full")

    import codd.implementer as implementer_mod

    monkeypatch.setattr(implementer_mod, "implement_tasks", fake_implement_tasks)

    scope = OracleRerunScope(rung="narrow", task_ids=("a", "b"), allowed_paths=("src/a.ts", "src/b.ts"))
    with pytest.raises(RuntimeError, match="disk full"):
        GreenfieldPipeline()._rerun_tasks_with_feedback(tmp_path, _tasks(), "fb", {}, scope=scope)
