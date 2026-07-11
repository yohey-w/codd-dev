"""Tests for the VB coverage gate's TARGETED-test-rerun scope derivation.

The VB gate's rerun must target TEST tasks (not source) and fence writes to the
TEST surface (so a coverage rerun can never rewrite production code). These guard
that scope derivation: test-task detection, source-doc targeting, batching when
no per-doc match, the test-only write-fence, and the broad fallback.
"""

from __future__ import annotations

from dataclasses import dataclass

from codd.vb_rerun_scope import (
    SCOPE_VB_BROAD,
    SCOPE_VB_TARGETED,
    derive_vb_rerun_scope,
    task_is_test_task,
)


@dataclass(frozen=True)
class _Task:
    task_id: str
    design_node: str = ""
    output_paths: tuple[str, ...] = ()
    expected_outputs: tuple[str, ...] = ()
    test_kinds: tuple[str, ...] = ()


CONFIG = {"scan": {"source_dirs": ["src/"], "test_dirs": ["tests/"]}}


# ---------------------------------------------------------------------------
# task_is_test_task
# ---------------------------------------------------------------------------


def test_task_is_test_task_by_output_path():
    assert task_is_test_task(_Task("t", output_paths=("tests/test_x.py",)), config=CONFIG)
    assert not task_is_test_task(_Task("s", output_paths=("src/app/x.py",)), config=CONFIG)


def test_task_is_test_task_ignores_test_kinds_alone():
    """Regression: ``test_kinds`` is V-model coverage-layer metadata, not a
    deliverable-kind signal — a derived SOURCE task carries it exactly as often
    as a derived TEST task (every derived task records which layer verifies
    it). A task with no OTHER test signal must stay classified as source even
    when ``test_kinds`` is non-empty.

    Found live in the 2026-07-03 ExprCalc Python greenfield dogfood: the
    derived task ``implement_tokenizer_module`` (a pure source task,
    ``expected_outputs=["src/exprcalc/tokenizer.py"]``) carries
    ``test_kinds=("unit",)`` like every other derived task. Treating that
    alone as "is a test task" pulled it into the VB coverage gate's
    test-only rerun scope alongside the real test task
    (``write_tokenizer_unit_tests``), so it received a prompt scoped to one
    source file PLUS test-coverage gap feedback for VBs owned by unrelated
    modules — a conflict the model could not resolve into a valid response.
    """
    assert not task_is_test_task(
        _Task("s", output_paths=("src/app/x.py",), test_kinds=("unit",)), config=CONFIG
    )
    # A genuine test task is still detected via its OWN output-path shape,
    # independent of test_kinds.
    assert task_is_test_task(
        _Task("t", output_paths=("tests/test_x.py",), test_kinds=("unit",)), config=CONFIG
    )


def test_task_is_test_task_by_design_node():
    assert task_is_test_task(_Task("t", design_node="test:test-strategy"), config=CONFIG)
    assert task_is_test_task(_Task("t", design_node="docs/test/strategy.md"), config=CONFIG)


def test_task_is_test_task_by_filename_pattern():
    assert task_is_test_task(_Task("t", output_paths=("foo/bar.spec.ts",)), config=CONFIG)


# ---------------------------------------------------------------------------
# Scope derivation
# ---------------------------------------------------------------------------


def test_scope_targets_only_test_tasks_and_fences_to_tests():
    tasks = [
        _Task("src_task", output_paths=("src/app/",)),
        _Task("test_task", output_paths=("tests/",), test_kinds=("unit",)),
    ]
    scope = derive_vb_rerun_scope(["docs/test/test_strategy.md"], tasks, config=CONFIG)
    assert scope.rung == SCOPE_VB_TARGETED
    assert "test_task" in scope.task_ids
    assert "src_task" not in scope.task_ids
    # Write-fence is the test surface only — no source dir.
    assert "tests" in scope.allowed_paths
    assert not any(p.startswith("src") for p in scope.allowed_paths)


def test_scope_batches_all_test_tasks_when_no_doc_match():
    tasks = [
        _Task("test_a", output_paths=("tests/a/",)),
        _Task("test_b", output_paths=("tests/b/",)),
    ]
    # Uncovered doc matches neither task's design node → batch all test tasks.
    scope = derive_vb_rerun_scope(["docs/test/unrelated.md"], tasks, config=CONFIG)
    assert scope.rung == SCOPE_VB_TARGETED
    assert set(scope.task_ids) == {"test_a", "test_b"}


def test_scope_targets_by_source_doc_when_node_matches():
    tasks = [
        _Task("strategy_tests", design_node="docs/test/test_strategy.md", output_paths=("tests/strategy/",)),
        _Task("other_tests", design_node="docs/test/other.md", output_paths=("tests/other/",)),
    ]
    scope = derive_vb_rerun_scope(["docs/test/test_strategy.md"], tasks, config=CONFIG)
    # Only the strategy test task is targeted (its node matches the uncovered doc).
    assert scope.task_ids == ("strategy_tests",)


def test_scope_broad_fallback_when_no_test_task():
    tasks = [
        _Task("src_a", output_paths=("src/a/",)),
        _Task("src_b", output_paths=("src/b/",)),
    ]
    scope = derive_vb_rerun_scope(["docs/test/test_strategy.md"], tasks, config=CONFIG)
    assert scope.rung == SCOPE_VB_BROAD
    assert scope.is_broad()
    assert scope.allowed_paths == ()  # empty ⇒ no fence (broad)
    assert set(scope.task_ids) == {"src_a", "src_b"}


def test_scope_excludes_source_task_that_merely_carries_test_kinds():
    """Regression: reproduces the ``implement_tokenizer_module`` /
    ``write_tokenizer_unit_tests`` pairing from the 2026-07-03 ExprCalc Python
    greenfield dogfood. Both tasks share one design doc and both carry
    ``test_kinds=("unit",)`` (V-model metadata, not a deliverable signal — see
    ``test_task_is_test_task_ignores_test_kinds_alone``); only the one whose
    OWN declared output is a test file may enter the VB gate's rerun scope or
    write-fence.
    """
    tasks = [
        _Task(
            "implement_tokenizer_module",
            design_node="docs/detailed_design/tokenizer_design.md",
            output_paths=("src/exprcalc/tokenizer.py",),
            expected_outputs=("src/exprcalc/tokenizer.py",),
            test_kinds=("unit",),
        ),
        _Task(
            "write_tokenizer_unit_tests",
            design_node="docs/detailed_design/tokenizer_design.md",
            output_paths=("tests/unit/test_tokenizer.py",),
            expected_outputs=("tests/unit/test_tokenizer.py",),
            test_kinds=("unit",),
        ),
    ]
    scope = derive_vb_rerun_scope(["docs/test/test_strategy.md"], tasks, config=CONFIG)
    assert scope.task_ids == ("write_tokenizer_unit_tests",)
    assert not any(p.startswith("src") for p in scope.allowed_paths)


def test_scope_uses_path_resolver_for_tasks_without_inline_paths():
    tasks = [_Task("test_task", design_node="docs/test/test_strategy.md")]

    def resolver(config, task):
        return ["tests/derived/"]

    scope = derive_vb_rerun_scope(
        ["docs/test/test_strategy.md"], tasks, config=CONFIG, path_resolver=resolver
    )
    assert scope.rung == SCOPE_VB_TARGETED
    assert "tests/derived" in scope.allowed_paths


def test_scope_falls_through_when_doc_matches_only_no_authored_artifact_task():
    """Un-inert the repair loop (2026-07 S3 StockRoom-mini burn).

    The canonical registry task (design_node ``docs/test/test_strategy.md``,
    expected_outputs = the DOC itself) is classified a "test task" by its node,
    and stage-1 doc-matching resolves to it because its node IS the uncovered VB
    source doc. But it authors NO test file — a rerun of it is inert ("scope
    contained only no-authored-artifact task(s) — nothing to repair"), so the
    residual VBs never get authored. The scope must DROP the doc-only task and
    fall through to the test tasks that actually author test files.
    """
    tasks = [
        # The doc-only canonical registry task — matches the uncovered doc but authors nothing.
        _Task(
            "document_test_strategy",
            design_node="docs/test/test_strategy.md",
            output_paths=("docs/test/test_strategy.md",),
            expected_outputs=("docs/test/test_strategy.md",),
        ),
        # A real test-authoring task (does NOT match the uncovered doc by node).
        _Task(
            "products_http",
            design_node="docs/detailed_design/product_catalog_module.md",
            output_paths=("tests/products/create.test.ts",),
            expected_outputs=("src/products/http.ts", "tests/products/create.test.ts"),
        ),
    ]
    scope = derive_vb_rerun_scope(["docs/test/test_strategy.md"], tasks, config=CONFIG)
    # Falls through to the authoring test task; the inert doc-only task is dropped.
    assert "document_test_strategy" not in scope.task_ids
    assert "products_http" in scope.task_ids
    # Still test-only fenced (never source).
    assert not any(p.startswith("src") for p in scope.allowed_paths)


def test_scope_targets_authoring_task_over_doc_only_when_both_match():
    """When both a doc-only registry task AND a real authoring test task match the
    uncovered doc, the authoring one is targeted (the synthesized VB coverage-
    closure task shares the canonical doc as its design node)."""
    tasks = [
        _Task(
            "document_test_strategy",
            design_node="docs/test/test_strategy.md",
            expected_outputs=("docs/test/test_strategy.md",),
        ),
        _Task(
            "verifiable_behavior_coverage",
            design_node="docs/test/test_strategy.md",
            output_paths=("tests/e2e/coverage.e2e.test.ts",),
            expected_outputs=("tests/e2e/coverage.e2e.test.ts",),
        ),
    ]
    scope = derive_vb_rerun_scope(["docs/test/test_strategy.md"], tasks, config=CONFIG)
    assert scope.task_ids == ("verifiable_behavior_coverage",)
