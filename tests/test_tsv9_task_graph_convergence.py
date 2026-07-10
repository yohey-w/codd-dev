"""FIX-1 + FIX-2 — implement consumes the planner's task-level ``dependencies``
graph for execution ORDER, producer-content INJECTION, and the repair CAMPAIGN
order; and the repair feedback names the REAL exporter of a missing symbol.

Fable5 ruling ``dogfood/fable5_reply_2026-07-10_ts-v9.md`` (the "shared root of A
and B" convergence fix). The planner already emits a task-level ``dependencies``
graph (the correct production DAG — e.g. ``bundle_c14186dabdb1033c.yaml``), but
at HEAD 29ed6cf NOTHING consumes it: ordering, (B') injection, and the campaign
all derive their notion of "dependency" from the design-elaboration DAG, whose
edges point OPPOSITE to module imports (a public-API barrel is shallow in the
V-model but is the LAST thing to build). Result: a consumer artifact is generated
+ repaired before its producers exist on disk, transcribes a design-embedded
symbol spelling, and the tsc oracle correctly (and permanently, under starved
context) reds.

These tests use the ACTUAL failed run's artifacts as fixtures
(``tests/fixtures/fable5_tsv9/`` = a verbatim copy of the ts-v9 bundle + the
generated ``src/*.ts``). Every fix is graph/data-layer — no ``language ==`` /
per-symbol / per-framework branch.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import yaml

_FIXTURE = Path(__file__).parent / "fixtures" / "fable5_tsv9"


# ---------------------------------------------------------------------------
# Fixtures — a hermetic tmp project seeded from the REAL ts-v9 artifacts.
# ---------------------------------------------------------------------------


def _seed_tsv9_project(
    tmp_path: Path, *, with_sources: bool = False, with_docs: bool = True
) -> Path:
    """A tmp project carrying the real ts-v9 derived-task bundle (and, when
    ``with_sources``, the generated ``src/*.ts`` producers on disk; when
    ``with_docs``, the design-doc frontmatter stubs whose ``depends_on`` edges
    reproduce the V-model inversion)."""
    project = tmp_path / "tsv9"
    (project / "codd").mkdir(parents=True, exist_ok=True)
    (project / "codd" / "codd.yaml").write_text(
        yaml.safe_dump(
            {
                "project": {"name": "exprcalc", "language": "typescript"},
                "scan": {"source_dirs": ["src"], "test_dirs": ["tests"]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (project / ".codd" / "derived_tasks").mkdir(parents=True, exist_ok=True)
    shutil.copy(_FIXTURE / "bundle.yaml", project / ".codd" / "derived_tasks" / "bundle.yaml")
    # The real design docs (frontmatter-only stubs) — their ``depends_on`` edges
    # are the V-model elaboration DAG that INVERTS module-import order (a shallow
    # public-API barrel outranks the deep detailed-design producers). Present on
    # disk so the HEAD design-rank ordering reproduces the actual inversion.
    if with_docs:
        for doc in (_FIXTURE / "docs_root").rglob("*.md"):
            rel = doc.relative_to(_FIXTURE / "docs_root")
            (project / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(doc, project / rel)
    if with_sources:
        (project / "src").mkdir(parents=True, exist_ok=True)
        for src in (_FIXTURE / "src").glob("*.ts"):
            shutil.copy(src, project / "src" / src.name)
    return project


def _order(project: Path) -> list[str]:
    from codd.implementer import list_implement_tasks

    return [entry["task_id"] for entry in list_implement_tasks(project)]


# ---------------------------------------------------------------------------
# FIX-1 ORDERING — rank = cycle-safe longest-chain over task ``dependencies``.
# ---------------------------------------------------------------------------


def test_producer_precedes_its_test_via_task_dependencies(tmp_path: Path) -> None:
    # ``write_error_hierarchy_unit_tests`` declares a task-level dependency on
    # ``implement_expr_error_base_class`` (edge already in the bundle). The
    # design-elaboration DAG never expressed this (the test's design node is the
    # governance taxonomy doc), so at HEAD the errors TEST was authored 20 min
    # before the errors.ts it imports. The producer must now precede it.
    project = _seed_tsv9_project(tmp_path)
    order = _order(project)
    assert order.index("implement_expr_error_base_class") < order.index(
        "write_error_hierarchy_unit_tests"
    ), order


def test_all_barrel_producers_and_transitive_ast_precede_the_barrel(tmp_path: Path) -> None:
    # The barrel's four DECLARED producers, PLUS ``implement_ast_node_hierarchy``
    # which the barrel reaches only TRANSITIVELY (barrel → parser_entrypoint →
    # grammar_descent → ast), must all precede it. Transitivity is the crux: the
    # barrel's own edge list omits ast entirely.
    project = _seed_tsv9_project(tmp_path)
    order = _order(project)
    barrel = order.index("implement_public_api_barrel")
    for producer in (
        "implement_tokenizer_scan_state_machine",
        "implement_parser_entrypoint_and_error_sites",
        "implement_evaluator_tree_walk",
        "implement_expr_error_base_class",
        "implement_ast_node_hierarchy",  # transitive-only
    ):
        assert order.index(producer) < barrel, (producer, order)


def test_ordering_is_deterministic_across_identical_input(tmp_path: Path) -> None:
    # Resume determinism: ordering is a pure function of static bundle data, so
    # the same bundle produces the identical order twice (no disk-state input).
    project = _seed_tsv9_project(tmp_path)
    assert _order(project) == _order(project)


def test_dependency_cycle_degrades_to_design_rank_without_raising(tmp_path: Path) -> None:
    # A task-dependency CYCLE must degrade to the current (design-rank) order,
    # never raise. We inject a 2-cycle into a copy of the bundle and assert the
    # call returns (and stays a pure permutation of the task set).
    project = _seed_tsv9_project(tmp_path)
    bundle_path = project / ".codd" / "derived_tasks" / "bundle.yaml"
    data = yaml.safe_load(bundle_path.read_text(encoding="utf-8"))
    by_id = {t["id"]: t for t in data["tasks"]}
    # Force a cycle: ast ⇄ errors (neither legitimately depends on the other).
    by_id["implement_ast_node_hierarchy"]["dependencies"] = ["implement_expr_error_base_class"]
    by_id["implement_expr_error_base_class"]["dependencies"] = ["implement_ast_node_hierarchy"]
    bundle_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    order = _order(project)  # must not raise
    assert set(order) == set(by_id), "ordering must stay a pure permutation of the task set"


def test_ordering_byte_identical_when_no_task_has_dependencies(tmp_path: Path) -> None:
    # The edge-less fallback: strip EVERY task-level dependency (legacy/configured
    # bundle shape) and the order must collapse to the SAME order the shipped
    # (design-rank, is_test, enum-index) triple produced — i.e. cache/declaration
    # order here (no design docs on disk ⇒ design-rank 0 for all, so the triple is
    # is_test then enumeration index). (The design-rank TIEBREAK itself is guarded
    # by the existing ``test_dependency_artifact_coherence`` ordering tests, which
    # have no task edges and so must stay byte-identical after this fix.)
    project = _seed_tsv9_project(tmp_path, with_docs=False)
    bundle_path = project / ".codd" / "derived_tasks" / "bundle.yaml"
    data = yaml.safe_load(bundle_path.read_text(encoding="utf-8"))
    for task in data["tasks"]:
        task["dependencies"] = []
    bundle_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    order = _order(project)
    # Reference = the shipped triple over the SAME edge-less entries: stable sort
    # by (design-rank=0, is_test, enumeration-index) — no design docs on disk, so
    # design-rank is 0 for all and only (is_test, index) discriminates.
    tasks = data["tasks"]
    reference = [
        tasks[i]["id"]
        for i in sorted(
            range(len(tasks)),
            key=lambda i: (1 if tasks[i].get("test_kinds") else 0, i),
        )
    ]
    assert order == reference, order


# ---------------------------------------------------------------------------
# FIX-1 (B') INJECTION — producer FILES = task ``dependencies`` transitive
# closure (nearest-first), unioned with the design-closure, on disk.
# ---------------------------------------------------------------------------


def _build_task_prompt(project: Path, *, design_node: str, expected_output: str) -> str:
    from codd.implementer import (
        DesignContext,
        ImplementSpec,
        _build_implementation_prompt,
        _load_project_config,
    )

    config = _load_project_config(project)
    return _build_implementation_prompt(
        config=config,
        design_context=DesignContext(
            node_id=design_node,
            path=Path(design_node),
            content=f"# {design_node}\n",
        ),
        spec=ImplementSpec(
            design_node=design_node,
            output_paths=["src", "tests"],
            expected_outputs=[expected_output],
        ),
        # Empty design-closure ⇒ the ONLY way ast/parser/errors content can appear
        # is the NEW task-graph producer injection (clean behavior-RED at HEAD).
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
        project_root=project,
    )


def test_barrel_prompt_injects_ast_and_parser_via_task_graph(tmp_path: Path) -> None:
    # The barrel's prompt must carry the ON-DISK content of ast.ts + parser.ts —
    # reached through the task-``dependencies`` transitive closure (parser is a
    # direct producer; ast is transitive-only). At HEAD the barrel's context had
    # neither, so index.ts re-exported the AST types from the wrong module.
    project = _seed_tsv9_project(tmp_path, with_sources=True)
    prompt = _build_task_prompt(
        project,
        design_node="docs/design/public_api_design.md",
        expected_output="src/index.ts",
    )
    # A sentinel that lives ONLY in the producer files, never in a design doc.
    assert "makeNumberNode" in prompt, "ast.ts producer content missing from barrel prompt"
    assert "export function parse" in prompt, "parser.ts producer content missing from barrel prompt"


def test_errors_test_prompt_injects_errors_ts_abstract_line(tmp_path: Path) -> None:
    # The errors-test task's design closure is ``requirements`` only — errors.ts
    # was NEVER injectable for it. Its task-level dependency
    # (implement_expr_error_base_class) makes errors.ts (with the ``abstract``
    # line) part of its producer context, so the test can bind to the concrete
    # subtypes instead of transcribing ``toThrow(ExprError)``.
    project = _seed_tsv9_project(tmp_path, with_sources=True)
    prompt = _build_task_prompt(
        project,
        design_node="docs/governance/error_taxonomy_decision.md",
        expected_output="tests/errors.test.ts",
    )
    assert "abstract class ExprError" in prompt, "errors.ts abstract line missing from errors-test prompt"


# ---------------------------------------------------------------------------
# FIX-1 CAMPAIGN — the repair campaign's dependency order uses the SAME rank
# (longest-chain over task ``dependencies``), byte-identical when edge-less.
# ---------------------------------------------------------------------------


def _task_stub(task_id: str, output: str, deps: tuple[str, ...]) -> SimpleNamespace:
    return SimpleNamespace(task_id=task_id, output_paths=(output,), dependencies=deps)


def test_campaign_order_regenerates_producers_before_consumers(tmp_path: Path) -> None:
    from codd.implement_oracle_scope import build_path_owner_index, task_dependency_order

    # Declaration order deliberately lists the CONSUMER first (the bundle's own
    # inverted shape). The campaign must still emit producer before consumer.
    tasks = [
        _task_stub("consumer_barrel", "src/index.ts", ("producer_ast",)),
        _task_stub("producer_ast", "src/ast.ts", ()),
    ]
    index = build_path_owner_index(tasks, project_root=tmp_path)
    order = task_dependency_order([t.task_id for t in tasks], index)
    assert order == ("producer_ast", "consumer_barrel"), order


def test_campaign_order_is_transitive(tmp_path: Path) -> None:
    from codd.implement_oracle_scope import build_path_owner_index, task_dependency_order

    tasks = [
        _task_stub("barrel", "src/index.ts", ("entry",)),
        _task_stub("entry", "src/parser.ts", ("ast",)),
        _task_stub("ast", "src/ast.ts", ()),
    ]
    index = build_path_owner_index(tasks, project_root=tmp_path)
    order = task_dependency_order([t.task_id for t in tasks], index)
    assert order.index("ast") < order.index("entry") < order.index("barrel"), order


def test_campaign_order_byte_identical_without_task_dependencies(tmp_path: Path) -> None:
    # The edge-less fallback: no dependencies ⇒ pure declaration order (the shipped
    # behavior), regardless of the requested subset's own order.
    from codd.implement_oracle_scope import build_path_owner_index, task_dependency_order

    tasks = [
        _task_stub("a", "src/a.ts", ()),
        _task_stub("b", "src/b.ts", ()),
        _task_stub("c", "src/c.ts", ()),
    ]
    index = build_path_owner_index(tasks, project_root=tmp_path)
    assert task_dependency_order(["c", "a", "b"], index) == ("a", "b", "c")


# ---------------------------------------------------------------------------
# FIX-2 — repair feedback names the REAL exporter of a missing symbol.
# ---------------------------------------------------------------------------


def test_feedback_names_the_real_symbol_owner(tmp_path: Path) -> None:
    from codd.implement_oracle import _exporter_surface_block
    from codd.implement_oracle_scope import StructuredDiagnostic

    # ast.ts (the real owner) on disk; parser.ts exports only ``parse``; index.ts
    # imports the AST types from parser (the defect). The feedback must state where
    # ExprNode ACTUALLY lives so the mechanical rewrite to ./ast is derivable.
    (tmp_path / "src").mkdir()
    shutil.copy(_FIXTURE / "src" / "ast.ts", tmp_path / "src" / "ast.ts")
    shutil.copy(_FIXTURE / "src" / "parser.ts", tmp_path / "src" / "parser.ts")
    shutil.copy(_FIXTURE / "src" / "index.ts", tmp_path / "src" / "index.ts")

    result = SimpleNamespace(
        diagnostics=[
            StructuredDiagnostic(
                code="TS2305",
                primary_path="src/index.ts",
                symbol="ExprNode",
                module_specifier="./parser.js",
                related_path="src/parser.ts",
            )
        ]
    )
    block = _exporter_surface_block(result, tmp_path)
    assert "src/ast.ts" in block, block
    assert "ExprNode" in block, block


def test_feedback_symbol_owner_silent_degrade_when_no_owner(tmp_path: Path) -> None:
    from codd.implement_oracle import _exporter_surface_block
    from codd.implement_oracle_scope import StructuredDiagnostic

    # No file exports ``TotallyMissing`` — the owner scan must degrade silently
    # (no fabricated owner line), never raise.
    (tmp_path / "src").mkdir()
    shutil.copy(_FIXTURE / "src" / "parser.ts", tmp_path / "src" / "parser.ts")
    result = SimpleNamespace(
        diagnostics=[
            StructuredDiagnostic(
                code="TS2305",
                primary_path="src/index.ts",
                symbol="TotallyMissing",
                module_specifier="./parser.js",
                related_path="src/parser.ts",
            )
        ]
    )
    block = _exporter_surface_block(result, tmp_path)  # must not raise
    assert "is exported by" not in block or "TotallyMissing" not in block
