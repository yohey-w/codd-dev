"""Certification fixtures for the ACG owner-uniqueness + declared-output gates.

These pin two of the GPT-5.5-Pro round-2 §3 contracts this PR implements:

* ``artifact.owner.unique.v1`` (GPT §3.3) — every generated artifact must have
  EXACTLY one owning task. The pure check ``validate_task_output_ownership_uniqueness``
  honest-fails (deterministically, before the implement-oracle) on the three
  conflict classes GPT names; the greenfield pipeline wires it BEFORE the owner
  index is built so an ambiguous topology fails fast.
* ``task.declared_output_completeness`` (GPT §3.4) — registered ``enforcement=warn``
  behind ``implement.declared_output_completeness``: a task that declared EXACT
  output paths should have produced them. WARN by default (no hard-fail of
  existing runs); ``enforce`` flips it to a ``StageError``.

The owner-uniqueness gate ADDS an honest-fail; the negative fixtures here are the
contract's "this MUST red" proof. None of them weakens an existing gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.greenfield.pipeline import (
    GreenfieldPipeline,
    ImplementTaskRef,
    StageError,
    _check_declared_output_completeness,
    _verify_task_contract,
)
from codd.implement_oracle_scope import (
    OwnerUniquenessError,
    OwnerUniquenessViolation,
    build_path_owner_index,
    validate_task_output_ownership_uniqueness,
)


class _Task:
    """A minimal ImplementTaskRef-shaped stand-in (task_id + output_paths)."""

    def __init__(self, task_id: str, output_paths) -> None:
        self.task_id = task_id
        self.output_paths = tuple(output_paths)


class _Result:
    """A minimal implement-result stand-in (carries generated_files)."""

    def __init__(self, generated_files=()) -> None:
        self.generated_files = tuple(generated_files)
        self.error = None


# ─────────────────────────────────────────────────────────────
# artifact.owner.unique.v1 — the three conflict classes (GPT §3.3)
# ─────────────────────────────────────────────────────────────


def test_duplicate_exact_output_owner_raises():
    """GPT §3.3 negative fixture: Task A and B both declare src/shared.ts."""
    tasks = [_Task("a", ["src/shared.ts"]), _Task("b", ["src/shared.ts"])]
    with pytest.raises(OwnerUniquenessError) as exc:
        validate_task_output_ownership_uniqueness(tasks)
    violations = exc.value.violations
    assert any(v.kind == "duplicate_exact" for v in violations)
    v = next(v for v in violations if v.kind == "duplicate_exact")
    assert v.path == "src/shared.ts"
    assert set(v.owners) == {"a", "b"}


def test_directory_and_exact_file_owner_conflict_raises():
    """A directory owner (src/) and a DIFFERENT task's exact file (src/x.ts)."""
    tasks = [_Task("dir_owner", ["src/"]), _Task("file_owner", ["src/x.ts"])]
    with pytest.raises(OwnerUniquenessError) as exc:
        validate_task_output_ownership_uniqueness(tasks)
    kinds = {v.kind for v in exc.value.violations}
    assert "dir_file_conflict" in kinds


def test_overlapping_directory_owners_raises():
    """Two different tasks own nested directories: src/ vs src/lib/."""
    tasks = [_Task("outer", ["src/"]), _Task("inner", ["src/lib/"])]
    with pytest.raises(OwnerUniquenessError) as exc:
        validate_task_output_ownership_uniqueness(tasks)
    kinds = {v.kind for v in exc.value.violations}
    assert "overlapping_dirs" in kinds


def test_non_conflicting_distinct_files_ok():
    """Two tasks owning DISTINCT files under a shared parent dir do NOT conflict.

    (The parent dir is only WEAKLY owned by ``setdefault`` in the owner index —
    mirroring that, the uniqueness gate must not invent a conflict here.) Also
    asserts the index itself still builds, so the gate agrees with the index it
    guards.
    """
    tasks = [_Task("a", ["src/a.ts"]), _Task("b", ["src/b.ts"])]
    # No raise.
    validate_task_output_ownership_uniqueness(tasks)
    index = build_path_owner_index(tasks, project_root=Path("/nonexistent"))
    assert index.owner_for("src/a.ts") == "a"
    assert index.owner_for("src/b.ts") == "b"


def test_same_task_dir_and_file_inside_is_not_a_conflict():
    """One task owning BOTH a dir and a file inside it is a single owner — OK."""
    tasks = [_Task("a", ["src/", "src/x.ts"])]
    validate_task_output_ownership_uniqueness(tasks)


def test_same_exact_path_twice_by_one_task_is_not_a_conflict():
    """A task listing the same path twice (or declaring + generating it) is OK."""
    tasks = [_Task("a", ["src/x.ts"])]
    validate_task_output_ownership_uniqueness(
        tasks, generated_files={"a": ["src/x.ts"]}
    )


def test_config_output_paths_participate_in_uniqueness():
    """A duplicate introduced via config_output_paths is also caught."""
    tasks = [_Task("a", []), _Task("b", [])]
    with pytest.raises(OwnerUniquenessError):
        validate_task_output_ownership_uniqueness(
            tasks,
            config_output_paths={"a": ["src/shared.ts"], "b": ["src/shared.ts"]},
        )


def test_owner_uniqueness_violation_messages_are_human_readable():
    v = OwnerUniquenessViolation(
        kind="duplicate_exact", path="src/x.ts", owners=("a", "b")
    )
    assert "src/x.ts" in v.message and "a, b" in v.message


# ─────────────────────────────────────────────────────────────
# pipeline wiring: the gate runs BEFORE the implement-oracle index build
# ─────────────────────────────────────────────────────────────


def test_pipeline_wires_owner_uniqueness_before_oracle():
    """``_certify_output_owner_uniqueness`` raises a StageError for a dup owner.

    This is the wiring proof: the pipeline method converts the pure
    OwnerUniquenessError into a StageError (the autopilot's honest-fail) and it is
    called from ``_enforce_implement_oracle_gate`` before ``_build_oracle_scope_index``.
    """
    pipeline = GreenfieldPipeline()
    tasks = [
        ImplementTaskRef(task_id="a", design_node="n", output_paths=("src/shared.ts",)),
        ImplementTaskRef(task_id="b", design_node="n", output_paths=("src/shared.ts",)),
    ]
    config_output_paths = {"a": ["src/shared.ts"], "b": ["src/shared.ts"]}
    with pytest.raises(StageError) as exc:
        pipeline._certify_output_owner_uniqueness(tasks, config_output_paths)
    assert "owner uniqueness" in str(exc.value).lower()


def test_pipeline_owner_uniqueness_passes_for_clean_topology():
    """A non-conflicting topology is a clean NO-OP (no StageError)."""
    pipeline = GreenfieldPipeline()
    tasks = [
        ImplementTaskRef(task_id="a", design_node="n", output_paths=("src/a.ts",)),
        ImplementTaskRef(task_id="b", design_node="n", output_paths=("src/b.ts",)),
    ]
    config_output_paths = {"a": ["src/a.ts"], "b": ["src/b.ts"]}
    pipeline._certify_output_owner_uniqueness(tasks, config_output_paths)  # no raise


def test_owner_uniqueness_no_false_red_on_python_src_layout_fallback():
    """REGRESSION (PC-owner-uniqueness-python-fallback-paths): two tasks with NO
    declared output_paths and NO config mapping must NOT trip owner-uniqueness.

    Their ``_output_paths_for_task`` FALLBACK is the PERMISSIVE source_root +
    package_root accept-list (``src`` + ``src/<pkg>``), which NESTS for a normal
    Python src-layout. Feeding it to the gate (the v2.41 bug, exposed by a real
    tipcalc Python greenfield run) false-RED'd the implement stage. The
    owner-uniqueness resolution must yield DECLARED claims only — here, nothing —
    while the ORACLE-scope resolution still carries the fallback."""
    pipeline = GreenfieldPipeline()
    tasks = [
        ImplementTaskRef(task_id="expose_entrypoint", design_node="docs/infra/build.md"),
        ImplementTaskRef(task_id="ci_release", design_node="docs/operations/runbook.md"),
    ]
    owner_paths = pipeline._resolve_owner_uniqueness_config_paths(tasks, {})
    assert owner_paths == {}, owner_paths
    # The gate does NOT raise (the regression was a false-RED here)...
    pipeline._certify_output_owner_uniqueness(tasks, owner_paths)  # no raise
    # ...but the oracle-scope resolution DOES still carry the permissive fallback
    # (it needs to know where the SUT may have written) — that's the asymmetry.
    oracle_paths = pipeline._resolve_oracle_config_output_paths(tasks, {})
    assert any(oracle_paths.get(t.task_id) for t in tasks), oracle_paths


def test_owner_uniqueness_keeps_config_declared_claims():
    """Config-DECLARED ``implement.default_output_paths`` remain EXCLUSIVE claims
    the gate reasons over — the fix drops only the permissive fallback, never a
    genuine declared claim (so a real declared nesting conflict still honest-fails)."""
    pipeline = GreenfieldPipeline()
    tasks = [
        ImplementTaskRef(task_id="a", design_node="docs/x.md"),
        ImplementTaskRef(task_id="b", design_node="docs/y.md"),
    ]
    config = {
        "implement": {
            "default_output_paths": {"docs/x.md": ["src/pkg"], "docs/y.md": ["src"]}
        }
    }
    owner_paths = pipeline._resolve_owner_uniqueness_config_paths(tasks, config)
    assert owner_paths == {"a": ["src/pkg"], "b": ["src"]}, owner_paths
    # DECLARED nesting dirs owned by DIFFERENT tasks is a real conflict -> honest-fail.
    with pytest.raises(StageError):
        pipeline._certify_output_owner_uniqueness(tasks, owner_paths)


# ─────────────────────────────────────────────────────────────
# task.declared_output_completeness — warn (default) vs enforce (GPT §3.4)
# ─────────────────────────────────────────────────────────────


def test_declared_output_completeness_warn_does_not_raise(tmp_path):
    """GPT §3.4: declared src/cli.ts + tests/cli.e2e.test.ts, only src/cli.ts made.

    Default (warn) must NOT raise — it only echoes. (This is the rollout-safe
    behaviour: the new check never hard-fails existing runs.)
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "cli.ts").write_text("export const x = 1;\n", encoding="utf-8")
    task = ImplementTaskRef(
        task_id="cli",
        design_node="n",
        expected_outputs=("src/cli.ts", "tests/cli.e2e.test.ts"),
    )
    results = [_Result(generated_files=[str(tmp_path / "src" / "cli.ts")])]
    messages: list[str] = []
    # warn = default config → no raise, message emitted.
    _check_declared_output_completeness(
        task, results, tmp_path, {}, echo=messages.append
    )
    assert any("declared-output-completeness (warn)" in m for m in messages)
    assert any("tests/cli.e2e.test.ts" in m for m in messages)


def test_declared_output_completeness_enforce_raises(tmp_path):
    """With ``implement.declared_output_completeness: enforce`` a missing declared
    EXACT output is a hard StageError."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "cli.ts").write_text("export const x = 1;\n", encoding="utf-8")
    task = ImplementTaskRef(
        task_id="cli",
        design_node="n",
        expected_outputs=("src/cli.ts", "tests/cli.e2e.test.ts"),
    )
    results = [_Result(generated_files=[str(tmp_path / "src" / "cli.ts")])]
    config = {"implement": {"declared_output_completeness": "enforce"}}
    with pytest.raises(StageError) as exc:
        _check_declared_output_completeness(
            task, results, tmp_path, config, echo=lambda _m: None
        )
    assert "tests/cli.e2e.test.ts" in str(exc.value)


def test_declared_output_completeness_ignores_symbol_declarations(tmp_path):
    """Symbol-style declared outputs (``Version.__str__`` / ``Range.matches(version)`` /
    ``module:range``) are NOT file paths — they must produce no spurious warn and, under
    ``enforce``, no false-RED. Regression: the old ``PurePosixPath(out).suffix``-non-empty
    test mis-classified a dotted symbol as a file (suffix ``.__str__`` / ``.matches(version)``)."""
    from codd.greenfield.pipeline import _declared_output_is_file_path

    assert _declared_output_is_file_path("token_bucket.py") is True
    assert _declared_output_is_file_path("src/app/clock.py") is True
    assert _declared_output_is_file_path("Version.__str__") is False
    assert _declared_output_is_file_path("Range.matches(version)") is False
    assert _declared_output_is_file_path("module:range") is False
    # node-id with a dotted alphanumeric method (a colon marks it a node-id, not a path)
    assert _declared_output_is_file_path("module:parser.parse") is False
    assert _declared_output_is_file_path("test:test-strategy") is False

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "version.py").write_text("class Version: ...\n", encoding="utf-8")
    task = ImplementTaskRef(
        task_id="impl_version",
        design_node="n",
        expected_outputs=("Version.__str__", "Range.matches(version)", "module:range"),
    )
    results = [_Result(generated_files=[str(tmp_path / "src" / "version.py")])]
    messages: list[str] = []
    # enforce mode + only SYMBOL declarations → no StageError and no warn (nothing is a file).
    _check_declared_output_completeness(
        task, results, tmp_path,
        {"implement": {"declared_output_completeness": "enforce"}},
        echo=messages.append,
    )
    assert messages == []


def test_declared_output_completeness_satisfied_ok(tmp_path):
    """When every declared EXACT output exists, neither warn nor enforce fires."""
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "cli.ts").write_text("export const x = 1;\n", encoding="utf-8")
    (tmp_path / "tests" / "cli.e2e.test.ts").write_text("// t\n", encoding="utf-8")
    task = ImplementTaskRef(
        task_id="cli",
        design_node="n",
        expected_outputs=("src/cli.ts", "tests/cli.e2e.test.ts"),
    )
    results = [
        _Result(
            generated_files=[
                str(tmp_path / "src" / "cli.ts"),
                str(tmp_path / "tests" / "cli.e2e.test.ts"),
            ]
        )
    ]
    msgs: list[str] = []
    config = {"implement": {"declared_output_completeness": "enforce"}}
    # enforce mode, but all present → no raise, no warn message.
    _check_declared_output_completeness(task, results, tmp_path, config, echo=msgs.append)
    assert not msgs


def test_declared_output_completeness_present_on_disk_counts(tmp_path):
    """A declared output that exists ON DISK (even if not in this task's generated
    list) counts as produced — no false warn for a file another task wrote."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "cli.ts").write_text("export const x = 1;\n", encoding="utf-8")
    task = ImplementTaskRef(
        task_id="cli", design_node="n", expected_outputs=("src/cli.ts",)
    )
    results = [_Result(generated_files=[])]  # not in generated, but on disk
    msgs: list[str] = []
    _check_declared_output_completeness(task, results, tmp_path, {}, echo=msgs.append)
    assert not msgs


def test_declared_output_completeness_off_is_silent(tmp_path):
    """``off`` disables the check entirely."""
    task = ImplementTaskRef(
        task_id="cli", design_node="n", expected_outputs=("src/missing.ts",)
    )
    results = [_Result(generated_files=[])]
    config = {"implement": {"declared_output_completeness": "off"}}
    msgs: list[str] = []
    _check_declared_output_completeness(task, results, tmp_path, config, echo=msgs.append)
    assert not msgs


def test_declared_output_completeness_directory_decl_not_checked(tmp_path):
    """A DIRECTORY/bare declaration is left to the kind gate, not this exact check."""
    task = ImplementTaskRef(
        task_id="cli", design_node="n", expected_outputs=("src/",)
    )
    results = [_Result(generated_files=[])]
    config = {"implement": {"declared_output_completeness": "enforce"}}
    # No exact file path declared → no raise even in enforce mode.
    _check_declared_output_completeness(task, results, tmp_path, config, echo=lambda _m: None)


def test_verify_task_contract_runs_completeness_in_warn_without_breaking(tmp_path):
    """The full ``_verify_task_contract`` still no-ops for a task with no required
    KIND, while the warn completeness check runs harmlessly (no raise)."""
    task = ImplementTaskRef(
        task_id="cli", design_node="n", expected_outputs=("src/missing.ts",)
    )
    results = [_Result(generated_files=[])]
    # default config → warn only; no required kind for a bare .ts under no roots →
    # the kind gate is a no-op, the completeness check warns (echo discarded).
    _verify_task_contract(task, results, tmp_path, {}, echo=lambda _m: None)
