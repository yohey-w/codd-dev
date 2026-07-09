"""Cross-artifact symbol/type coherence: producer-first ordering + dependency
artifact content injected as a binding import contract.

Fable5-designed increment (see
``dogfood/fable5_reply_2026-07-09_static-type-coherence.md``, Q2/Q3/Q5 and the
"Red-first DoD shape"). Two coupled defects that make independently-generated
files disagree on symbol names / signatures / module paths (native-oracle
typecheck RED on statically-typed stacks):

1. Implement tasks execute in cache/declaration order, so a consumer can run
   before its producer exists on disk (PART 1: topological, producer-first).
2. First generation never sees the ACTUAL content of the files it imports from,
   only the prose design docs — so it invents member spellings/signatures
   (PART 2: inject dependency-artifact content as a VERBATIM-binding contract,
   with design-wins-on-conflict anti-false-green wording).

Language-blind by construction (full file content, DAG-data-driven ordering);
the budget-overflow fallback dispatches through the language-adapter seam.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from codd.implementer import (
    DependencyDocument,
    DesignContext,
    ImplementSpec,
    _build_implementation_prompt,
    list_implement_tasks,
)


# Verbatim contract fragments the injected block MUST carry (asserted by tests).
_VERBATIM_BINDING = "bind to their exported symbols, signatures, and module paths VERBATIM"
_NO_INVENT = "do not re-declare, rename, or invent members not present"
_DESIGN_WINS = "follow the DESIGN and let the mismatch surface"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_config(project: Path, *, language: str = "python", name: str = "Demo") -> dict:
    (project / "codd").mkdir(parents=True, exist_ok=True)
    config = {"project": {"name": name, "language": language}}
    (project / "codd" / "codd.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )
    return config


def _write_design_doc(project: Path, rel: str, *, node_id: str, depends_on: list[str] | None = None) -> None:
    codd: dict = {"node_id": node_id, "type": "design"}
    if depends_on:
        codd["depends_on"] = [{"id": dep} for dep in depends_on]
    front = yaml.safe_dump({"codd": codd}, sort_keys=False)
    path = project / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{front}---\n\n# {node_id}\n", encoding="utf-8")


def _write_derived_cache(project: Path, cache_name: str, design_docs: list[str], tasks: list[dict]) -> None:
    from codd.llm.plan_deriver import (
        DerivedTask,
        DerivedTaskCacheRecord,
        write_derived_task_cache,
    )

    records = [DerivedTask.from_dict(task) for task in tasks]
    write_derived_task_cache(
        project / ".codd" / "derived_tasks" / cache_name,
        DerivedTaskCacheRecord("stub", "key", "sha", "tmpl", "now", design_docs, records),
    )


def _task(
    task_id: str,
    source_design_doc: str,
    *,
    expected_outputs: list[str] | None = None,
    test_kinds: list[str] | None = None,
    layer: str = "detailed",
) -> dict:
    return {
        "id": task_id,
        "title": task_id.replace("_", " "),
        "description": "Derived task.",
        "source_design_doc": source_design_doc,
        "v_model_layer": layer,
        "expected_outputs": expected_outputs or [],
        "test_kinds": test_kinds or [],
        "approved": True,
    }


# ---------------------------------------------------------------------------
# RED #1 — prompt contract: consumer prompt carries producer FILE content +
# the verbatim-binding contract + the design-wins-on-conflict sentence.
# ---------------------------------------------------------------------------


def test_consumer_prompt_injects_producer_file_content_and_binding_contract(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    config = _write_config(project, language="python")

    # producer design doc + consumer design doc (consumer depends_on producer).
    _write_design_doc(project, "docs/design/producer.md", node_id="design:producer")
    _write_design_doc(
        project,
        "docs/design/consumer.md",
        node_id="design:consumer",
        depends_on=["docs/design/producer.md"],
    )

    # A producer task OWNS the producer node and has a file ON DISK; a consumer
    # task owns the consumer node (its prompt is what we build below).
    _write_derived_cache(
        project,
        "cache.yaml",
        ["docs/design/producer.md", "docs/design/consumer.md"],
        [
            _task("build_producer", "docs/design/producer.md", expected_outputs=["src/producer.py"]),
            _task(
                "build_consumer",
                "docs/design/consumer.md",
                expected_outputs=["tests/test_consumer.py"],
                test_kinds=["unit"],
            ),
        ],
    )

    # The distinctive sentinel lives ONLY in the producer FILE, never in a design
    # doc — so a passing assertion proves file-content injection specifically.
    producer_file = project / "src" / "producer.py"
    producer_file.parent.mkdir(parents=True, exist_ok=True)
    producer_file.write_text(
        "def PRODUCER_SENTINEL_add(a: int, b: int) -> int:\n    return a + b\n",
        encoding="utf-8",
    )

    prompt = _build_implementation_prompt(
        config=config,
        design_context=DesignContext(
            node_id="design:consumer",
            path=Path("docs/design/consumer.md"),
            content="# Consumer\n",
        ),
        spec=ImplementSpec(
            "docs/design/consumer.md",
            ["tests"],
            dependency_design_nodes=["docs/design/producer.md"],
            expected_outputs=["tests/test_consumer.py"],
        ),
        dependency_documents=[
            DependencyDocument(
                node_id="design:producer",
                path=Path("docs/design/producer.md"),
                content="# Producer\n",
            )
        ],
        conventions=[],
        coding_principles=None,
        project_root=project,
    )

    # Producer FILE content is injected (not just the prose design doc).
    assert "PRODUCER_SENTINEL_add" in prompt
    # The binding import contract wording (conditional, not imperative).
    assert _VERBATIM_BINDING in prompt
    assert _NO_INVENT in prompt
    # The anti-false-green / design-wins-on-conflict discriminator.
    assert _DESIGN_WINS in prompt


# ---------------------------------------------------------------------------
# RED #2 — ordering: producer-first over the design-node depends_on DAG, and
# source-kind before test-kind within a single design doc.
# ---------------------------------------------------------------------------


def test_list_implement_tasks_orders_producer_before_consumer(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    _write_config(project)

    _write_design_doc(project, "docs/design/producer.md", node_id="design:producer")
    _write_design_doc(
        project,
        "docs/design/consumer.md",
        node_id="design:consumer",
        depends_on=["docs/design/producer.md"],
    )

    # Deliberately list the CONSUMER task BEFORE the producer task in cache order.
    _write_derived_cache(
        project,
        "cache.yaml",
        ["docs/design/producer.md", "docs/design/consumer.md"],
        [
            _task("consume", "docs/design/consumer.md", test_kinds=["unit"]),
            _task("produce", "docs/design/producer.md"),
        ],
    )

    order = [task["task_id"] for task in list_implement_tasks(project)]
    assert order == ["produce", "consume"], order


def test_list_implement_tasks_orders_source_before_test_within_doc(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    _write_config(project)

    # One design doc, two tasks: the TEST task is listed first in cache order.
    _write_derived_cache(
        project,
        "cache.yaml",
        ["docs/design/mod.md"],
        [
            _task("mod_tests", "docs/design/mod.md", test_kinds=["unit"]),
            _task("mod_source", "docs/design/mod.md"),
        ],
    )

    order = [task["task_id"] for task in list_implement_tasks(project)]
    assert order == ["mod_source", "mod_tests"], order


def test_ordering_is_deterministic_and_a_pure_permutation(tmp_path: Path) -> None:
    # Resume safety rests on this: ordering must be a STABLE, deterministic
    # permutation of the same task-id set (never disk-state dependent), so a
    # mid-run resume sees the identical order and the same membership.
    project = tmp_path / "proj"
    _write_config(project)
    _write_design_doc(project, "docs/design/producer.md", node_id="design:producer")
    _write_design_doc(
        project,
        "docs/design/consumer.md",
        node_id="design:consumer",
        depends_on=["docs/design/producer.md"],
    )
    _write_derived_cache(
        project,
        "cache.yaml",
        ["docs/design/producer.md", "docs/design/consumer.md"],
        [
            _task("consume", "docs/design/consumer.md", test_kinds=["unit"]),
            _task("produce", "docs/design/producer.md"),
        ],
    )

    first = [task["task_id"] for task in list_implement_tasks(project)]
    second = [task["task_id"] for task in list_implement_tasks(project)]
    assert first == second == ["produce", "consume"]
    # Pure permutation: no task added or dropped by reordering.
    assert set(first) == {"consume", "produce"}


# ---------------------------------------------------------------------------
# DoD #4 guards — language-blindness, budget-overflow degradation, resume.
# ---------------------------------------------------------------------------


def _consumer_prompt(
    project: Path,
    config: dict,
    *,
    dependency_path: str = "docs/design/producer.md",
    dependency_node: str = "design:producer",
) -> str:
    """Build the CONSUMER task's implement prompt (it depends on the producer)."""
    return _build_implementation_prompt(
        config=config,
        design_context=DesignContext(
            node_id="design:consumer",
            path=Path("docs/design/consumer.md"),
            content="# Consumer\n",
        ),
        spec=ImplementSpec(
            "docs/design/consumer.md",
            ["tests"],
            dependency_design_nodes=[dependency_path],
            expected_outputs=["tests/test_consumer.py"],
        ),
        dependency_documents=[
            DependencyDocument(
                node_id=dependency_node,
                path=Path(dependency_path),
                content="# Producer\n",
            )
        ],
        conventions=[],
        coding_principles=None,
        project_root=project,
    )


def _seed_producer(project: Path, producer_file: str, content: str, *, language: str) -> dict:
    """Config + design docs + a derived cache whose producer task owns
    ``producer_file``, and that file written on disk with ``content``."""
    config = _write_config(project, language=language)
    _write_design_doc(project, "docs/design/producer.md", node_id="design:producer")
    _write_design_doc(
        project,
        "docs/design/consumer.md",
        node_id="design:consumer",
        depends_on=["docs/design/producer.md"],
    )
    _write_derived_cache(
        project,
        "cache.yaml",
        ["docs/design/producer.md", "docs/design/consumer.md"],
        [
            _task("build_producer", "docs/design/producer.md", expected_outputs=[producer_file]),
            _task(
                "build_consumer",
                "docs/design/consumer.md",
                expected_outputs=["tests/test_consumer.py"],
                test_kinds=["unit"],
            ),
        ],
    )
    path = project / producer_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return config


def test_injection_is_language_blind_for_a_non_typescript_language(tmp_path: Path) -> None:
    # A wholly fabricated language with an unrecognized file extension: content
    # injection reads bytes off disk and never dispatches on language, so it must
    # still fire and carry the binding contract.
    project = tmp_path / "proj"
    config = _seed_producer(
        project,
        "src/thing.faketon",
        "PROC DIVIDE-BY (X, Y) RETURNS FAKETON_QUOTIENT\n",
        language="faketon",
    )
    prompt = _consumer_prompt(project, config)
    assert "FAKETON_QUOTIENT" in prompt
    assert _VERBATIM_BINDING in prompt
    assert _DESIGN_WINS in prompt


def test_budget_overflow_degrades_to_name_level_surface(tmp_path: Path) -> None:
    # A producer whose full content exceeds the budget degrades to a name-level
    # public surface via the shipped language-adapter extractor (TS has one) —
    # never a truncated mid-file fragment.
    from codd.implementer import DEPENDENCY_ARTIFACT_FILES_PROMPT_LIMIT

    project = tmp_path / "proj"
    padding = "// pad line kept from producer\n" * (DEPENDENCY_ARTIFACT_FILES_PROMPT_LIMIT // 10)
    body = (
        "export function bigProducerApi(): number {\n  return 1;\n}\n"
        + padding
        + "// OVERFLOW_TAIL_SENTINEL should never reach the prompt verbatim\n"
    )
    assert len(body) > DEPENDENCY_ARTIFACT_FILES_PROMPT_LIMIT
    config = _seed_producer(project, "src/producer.ts", body, language="typescript")

    prompt = _consumer_prompt(project, config)
    # Degraded, not truncated: the name-level surface fired, the full body did not.
    assert "public surface only" in prompt
    assert "bigProducerApi" in prompt
    assert "OVERFLOW_TAIL_SENTINEL" not in prompt


def test_budget_overflow_degrades_to_paths_only_without_an_extractor(tmp_path: Path) -> None:
    # No name-level extractor for this file kind → degrade to paths-only (never
    # crash, never inject a truncated fragment).
    from codd.implementer import DEPENDENCY_ARTIFACT_FILES_PROMPT_LIMIT

    project = tmp_path / "proj"
    body = "X" * (DEPENDENCY_ARTIFACT_FILES_PROMPT_LIMIT + 500) + "OVERFLOW_TAIL_SENTINEL\n"
    config = _seed_producer(project, "src/thing.faketon", body, language="faketon")

    prompt = _consumer_prompt(project, config)
    assert "path only" in prompt
    assert "OVERFLOW_TAIL_SENTINEL" not in prompt


def test_resume_under_reordering_neither_skips_nor_repeats_tasks(tmp_path: Path) -> None:
    # PART 1 alters greenfield execution order mid-pipeline. Verify the resume
    # path (record["units"] rebuild + DONE-skip) is safe under the new order: an
    # already-DONE producer is skipped (not repeated), the remaining consumer runs
    # exactly once, and no task-id is dropped or duplicated.
    from codd.greenfield.pipeline import STATUS_DONE, GreenfieldPipeline

    project = tmp_path / "proj"
    _write_config(project)
    _write_design_doc(project, "docs/design/producer.md", node_id="design:producer")
    _write_design_doc(
        project,
        "docs/design/consumer.md",
        node_id="design:consumer",
        depends_on=["docs/design/producer.md"],
    )
    # Consumer listed before producer in cache order; ordering reorders to
    # [produce, consume].
    _write_derived_cache(
        project,
        "cache.yaml",
        ["docs/design/producer.md", "docs/design/consumer.md"],
        [
            _task("consume", "docs/design/consumer.md", test_kinds=["unit"]),
            _task("produce", "docs/design/producer.md"),
        ],
    )

    calls: list[str] = []

    def recording_runner(project_root, task, **kwargs):
        calls.append(task.task_id)
        return "1 file(s) generated"

    pipeline = GreenfieldPipeline()
    pipeline.implement_task_runner = recording_runner
    # Isolate the unit-tracking loop from the surrounding gates/scaffold/session.
    pipeline._enforce_api_facade_coverage = lambda project_root, tasks: tasks
    pipeline._enforce_deliverable_surface_exclusion = lambda project_root, tasks: tasks
    pipeline._ensure_test_runner = lambda project_root: None
    pipeline._checkpoint = lambda project_root: None
    pipeline._finalize_dependency_lock_coherence = lambda project_root: None
    pipeline._enforce_implement_oracle_gate = lambda project_root, tasks, options: None

    # Resume state: the producer already completed on a prior (interrupted) run.
    record: dict = {"units": {"produce": STATUS_DONE}}
    pipeline._stage_implement(project, record, {"coverage_gate": False})

    # Producer skipped (not repeated); consumer ran exactly once.
    assert calls == ["consume"], calls
    # Every task present exactly once, producer preserved as done.
    assert record["units"] == {"produce": "done", "consume": "done"}
