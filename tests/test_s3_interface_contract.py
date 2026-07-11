"""S3-mini Root B — "convergence granularity ≤ circulated contract granularity".

Fable5 ruling ``dogfood/fable5_reply_2026-07-12_s3-interface-contract.md``. When
independently-generated units share an EMERGENT surface (a producer's public
interface), they converge only to the granularity of the contract information the
loop circulates. Today NAMES circulate (PART-2 name-degradation, exporter-surface,
symbol-owners) → names converged; SHAPE (signatures) is delivered NOWHERE → shapes
oscillated (positional-3 vs object-1 vs positional-4 for one ``request(...)``).

The fix raises the DELIVERY granularity to match the existing prompt promise
("bind ... signatures ... VERBATIM"):

  1. ``plan_derive_meta.md`` defines ``dependencies`` as the build/run IMPORT edge
     (incl. the harness a test drives) — so the plan carries the distance-1 edge.
  2. PART-2 gains a THIRD (observed-import) producer source + a TWO-TIER budget
     whose Tier-1 (distance-1 producers) is rendered FULL, and on budget overflow
     degrades to SIGNATURE granularity and NEVER below (the distance-1 floor).
  3. a signature-level renderer (``render_public_surface``) feeds BOTH the PART-2
     Tier-1 degradation AND the exporter-surface repair feedback. Ladder:
     signature → names → paths → None.

Every fix is data/graph/granularity: no ``language ==``, no per-project signature
pin, no new artifact/node/gate. The renderer dispatches on file EXTENSION only, so
a language without a renderer preserves current behavior (generality).
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import yaml


# ── shared scaffolding ────────────────────────────────────────────────────────


def _seed(project: Path, tasks: list[dict], files: dict[str, str]) -> None:
    (project / "codd").mkdir(parents=True, exist_ok=True)
    (project / "codd" / "codd.yaml").write_text(
        yaml.safe_dump(
            {
                "project": {"name": "demo", "language": "typescript"},
                "scan": {"source_dirs": ["src"], "test_dirs": ["tests"]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (project / ".codd" / "derived_tasks").mkdir(parents=True, exist_ok=True)
    bundle = {
        "provider_id": "p",
        "cache_key": "k",
        "design_doc_sha": "d",
        "prompt_template_sha": "t",
        "generated_at": "now",
        "design_docs": [],
        "tasks": tasks,
    }
    (project / ".codd" / "derived_tasks" / "bundle.yaml").write_text(
        yaml.safe_dump(bundle, sort_keys=False), encoding="utf-8"
    )
    for rel, content in files.items():
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _task(
    task_id: str,
    outputs: list[str],
    deps: tuple[str, ...] = (),
    *,
    doc: str | None = None,
    test: bool = False,
) -> dict:
    return {
        "id": task_id,
        "title": task_id.replace("_", " "),
        "description": "Derived task.",
        "source_design_doc": doc or f"docs/design/{task_id}.md",
        "v_model_layer": "detailed",
        "expected_outputs": list(outputs),
        "test_kinds": ["unit"] if test else [],
        "dependencies": list(deps),
        "approved": True,
    }


def _dep_context(project: Path, *, design_node: str, expected_output: str) -> str | None:
    from codd.implementer import (
        ImplementSpec,
        _dependency_artifact_files_context,
        _load_project_config,
    )

    config = _load_project_config(project)
    return _dependency_artifact_files_context(
        project,
        config,
        [],
        ImplementSpec(
            design_node=design_node,
            output_paths=["src", "tests"],
            expected_outputs=[expected_output],
        ),
    )


# The concrete 現物 (generic form): a producer whose EMERGENT surface is an
# interface whose method carries a POSITIONAL-3 signature. Names alone ("request",
# "close") never disambiguate the call SHAPE — signatures do.
_APP_HANDLE = (
    "export interface AppResponse {\n"
    "  status: number;\n"
    "  body: unknown;\n"
    "}\n"
    "export interface AppHandle {\n"
    "  request(method: string, path: string, options?: RequestOptions): Promise<AppResponse>;\n"
    "  close(): Promise<void>;\n"
    "}\n"
    "export async function createApp(options: CreateAppOptions): Promise<AppHandle> {\n"
    "  return null as unknown as AppHandle;\n"
    "}\n"
)


# ═════════════════════════════════════════════════════════════════════════════
# Increment 3 — the signature renderer (render_public_surface)
# ═════════════════════════════════════════════════════════════════════════════


def test_render_public_surface_emits_interface_signature_not_just_names(tmp_path: Path) -> None:
    from codd.implement_oracle_scope import extract_public_surface, render_public_surface

    (tmp_path / "src" / "core").mkdir(parents=True)
    (tmp_path / "src" / "core" / "server.ts").write_text(_APP_HANDLE, encoding="utf-8")

    sig = render_public_surface("src/core/server.ts", tmp_path)
    assert sig is not None
    # The positional-3 request SIGNATURE — the SHAPE, not just the member name.
    assert "request(method: string, path: string" in sig
    assert "Promise<AppResponse>" in sig
    assert "close(): Promise<void>" in sig
    # Strictly MORE than the name list: ``request``/``close`` are interface MEMBERS
    # that never appear in the export-NAME surface at all.
    names = extract_public_surface("src/core/server.ts", tmp_path)
    assert names is not None
    assert set(names) >= {"AppHandle", "AppResponse", "createApp"}
    assert "request" not in names, "the NAME surface cannot carry a member's shape"
    # NOT the full function body (signature-level, not body-level).
    assert "return null as unknown" not in sig


def test_render_public_surface_strips_function_body(tmp_path: Path) -> None:
    from codd.implement_oracle_scope import render_public_surface

    (tmp_path / "m.ts").write_text(
        "export function add(a: number, b: number): number {\n  return a + b;\n}\n",
        encoding="utf-8",
    )
    sig = render_public_surface("m.ts", tmp_path)
    assert sig is not None
    assert "add(a: number, b: number): number" in sig
    assert "return a + b" not in sig


def test_render_public_surface_class_keeps_member_signatures_strips_bodies(tmp_path: Path) -> None:
    from codd.implement_oracle_scope import render_public_surface

    (tmp_path / "c.ts").write_text(
        "export class Repo {\n"
        "  find(id: string): number {\n"
        "    return this.rows[id];\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    sig = render_public_surface("c.ts", tmp_path)
    assert sig is not None
    assert "class Repo" in sig
    assert "find(id: string): number" in sig
    assert "return this.rows" not in sig


def test_render_public_surface_unknown_language_is_none(tmp_path: Path) -> None:
    """Generality: a file kind with no signature extractor degrades to None so the
    caller falls back to the name-level surface (or paths), never crashes."""
    from codd.implement_oracle_scope import render_public_surface

    (tmp_path / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    assert render_public_surface("mod.py", tmp_path) is None
    (tmp_path / "thing.faketon").write_text("PROC DIVIDE (X, Y)\n", encoding="utf-8")
    assert render_public_surface("thing.faketon", tmp_path) is None


# ═════════════════════════════════════════════════════════════════════════════
# Increment 2 — PART-2 two-tier budget + observed-import third source
# ═════════════════════════════════════════════════════════════════════════════


def test_tier1_distance1_producer_degrades_to_signature_never_below(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """The distance-1 signature FLOOR (Root B's exact violation): when a distance-1
    producer's FULL content overflows the budget, its SIGNATURE is still carried —
    never dropped to name-only/path-only/truncated — and the degradation is echoed
    (no silent cap)."""
    import codd.implementer as impl

    monkeypatch.setattr(impl, "DEPENDENCY_ARTIFACT_FILES_PROMPT_LIMIT", 200)
    padding = "// filler that must NOT reach the prompt when degraded\n" * 400
    producer = _APP_HANDLE + padding + "// OVERFLOW_TAIL_SENTINEL\n"
    _seed(
        tmp_path,
        tasks=[
            _task("build_server", ["src/core/server.ts"]),
            _task(
                "write_tenant_crud_test",
                ["tests/tenant-crud.test.ts"],
                deps=("build_server",),
                test=True,
            ),
        ],
        files={"src/core/server.ts": producer},
    )
    with caplog.at_level(logging.INFO):
        ctx = _dep_context(
            tmp_path,
            design_node="docs/design/write_tenant_crud_test.md",
            expected_output="tests/tenant-crud.test.ts",
        )
    assert ctx is not None
    # Floor honored: the positional-3 signature is present even though the full
    # file overflowed the budget.
    assert "request(method: string, path: string" in ctx
    assert "Promise<AppResponse>" in ctx
    # Degraded (not full) and the overflow tail never leaked verbatim.
    assert "OVERFLOW_TAIL_SENTINEL" not in ctx
    # Telemetry: the distance-1 signature degradation is logged, not silent.
    assert any(
        "distance-1" in r.getMessage().lower() and "signature" in r.getMessage().lower()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_observed_import_source_pulls_undeclared_producer_surface(tmp_path: Path) -> None:
    """The THIRD source: a consumer whose file EXISTS on disk and imports a producer
    for which the planner declared NO task-level dependency edge (the Root B
    first-pass gap) still receives that producer's surface — the observed import is
    measured and unioned into Tier-1."""
    consumer = (
        'import { createApp, AppHandle } from "../src/core/server.js";\n'
        "const h: AppHandle = null as unknown as AppHandle;\n"
        "void createApp;\n"
    )
    _seed(
        tmp_path,
        tasks=[
            _task("build_server", ["src/core/server.ts"]),
            # NOTE: no deps=(...) — the import edge is UNDECLARED in the plan.
            _task("write_key_crud_test", ["tests/key-crud.test.ts"], test=True),
        ],
        files={
            "src/core/server.ts": _APP_HANDLE,
            "tests/key-crud.test.ts": consumer,
        },
    )
    ctx = _dep_context(
        tmp_path,
        design_node="docs/design/write_key_crud_test.md",
        expected_output="tests/key-crud.test.ts",
    )
    # At HEAD this is ``None`` (no declared edge, no design-closure) — the producer
    # surface never reached the consumer. The observed-import source delivers it.
    assert ctx is not None
    assert "AppHandle" in ctx
    assert "interface AppHandle" in ctx  # full surface (budget ample) — shape visible


def test_dependency_context_none_without_edges_or_observed_imports(tmp_path: Path) -> None:
    """Generality no-op: a task with no declared dependency edge, no design-closure,
    and whose own file imports nothing gets NO dependency block (byte-identical to
    the shipped edge-less behavior)."""
    _seed(
        tmp_path,
        tasks=[_task("solo", ["src/solo.ts"])],
        files={"src/solo.ts": "export const x = 1;\n"},
    )
    ctx = _dep_context(
        tmp_path, design_node="docs/design/solo.md", expected_output="src/solo.ts"
    )
    assert ctx is None


# ═════════════════════════════════════════════════════════════════════════════
# Increment 3 wiring ② — exporter-surface repair feedback raised to signature
# ═════════════════════════════════════════════════════════════════════════════


def test_exporter_surface_feedback_carries_interface_signature(tmp_path: Path) -> None:
    """The #1 anti-oscillation lever (exporter surface in repair feedback) now
    carries the SIGNATURE of the demanded module, not just its export NAMES — so a
    TS2554/TS2353/TS2339 shape error becomes reconcilable, not re-guessable."""
    from codd.implement_oracle import _exporter_surface_block
    from codd.implement_oracle_scope import StructuredDiagnostic

    (tmp_path / "src" / "core").mkdir(parents=True)
    (tmp_path / "src" / "core" / "server.ts").write_text(_APP_HANDLE, encoding="utf-8")
    (tmp_path / "src" / "consumer.ts").write_text(
        'import { AppHandle } from "./core/server.js";\n', encoding="utf-8"
    )

    result = SimpleNamespace(
        diagnostics=[
            StructuredDiagnostic(
                code="TS2305",
                primary_path="src/consumer.ts",
                symbol="AppHandle",
                module_specifier="./core/server.js",
            )
        ]
    )
    block = _exporter_surface_block(result, tmp_path)
    assert "CURRENT PUBLIC INTERFACE" in block
    assert "server.ts" in block
    # The signature (positional-3 request), not just the name ``AppHandle``.
    assert "request(method: string, path: string" in block
    assert "Promise<AppResponse>" in block


# ═════════════════════════════════════════════════════════════════════════════
# Increment 1 — plan_derive_meta dependency semantics + cache invalidation
# ═════════════════════════════════════════════════════════════════════════════


def test_plan_template_defines_build_run_dependency_semantics() -> None:
    """Increment 1: the deriver template must DEFINE ``dependencies`` as the
    build/run IMPORT edge (incl. the harness a test drives), not merely tasks the
    task follows — so the distance-1 edge is present in the plan."""
    import re as _re

    from codd.llm.plan_deriver import DEFAULT_TEMPLATE_PATH

    # Whitespace-normalized so the check is robust to line wrapping.
    text = _re.sub(r"\s+", " ", DEFAULT_TEMPLATE_PATH.read_text(encoding="utf-8").lower())
    assert "import, include, or invoke" in text
    assert "harness" in text
    assert "not merely tasks it conceptually verifies or follows" in text


def test_template_edit_invalidates_derived_task_cache(tmp_path: Path) -> None:
    """The ``prompt_template_sha`` folds into the derived-task cache key, so editing
    ``plan_derive_meta.md`` auto-invalidates the cache (no migration). This is the
    mechanism increment 1 relies on."""
    from codd.dag import Node
    from codd.llm.plan_deriver import SubprocessAiCommandPlanDeriver

    class _Fake:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, prompt: str, model: str | None = None) -> str:
            self.calls += 1
            return '{"tasks": []}'

        def provider_id(self, model: str | None = None) -> str:
            return "fake"

    project = tmp_path / "proj"
    (project / "codd").mkdir(parents=True)
    (project / "codd" / "codd.yaml").write_text(
        yaml.safe_dump({"project": {"name": "demo", "language": "python"}}, sort_keys=False),
        encoding="utf-8",
    )
    node = Node(
        id="docs/design/x.md",
        kind="design_doc",
        path="docs/design/x.md",
        attributes={"content": "Body", "frontmatter": {}},
    )
    tmpl = tmp_path / "tmpl.md"
    tmpl.write_text(
        "V1 {design_doc_bundle} {v_model_layer} {project_context}\n", encoding="utf-8"
    )
    ctx = {"project_root": project}
    fake = _Fake()

    SubprocessAiCommandPlanDeriver(fake, template_path=tmpl).derive_tasks([node], "detailed", ctx)
    # Same template → cache hit (no re-invoke).
    SubprocessAiCommandPlanDeriver(fake, template_path=tmpl).derive_tasks([node], "detailed", ctx)
    assert fake.calls == 1

    # Edit the template → prompt_template_sha changes → cache miss → re-invoke.
    tmpl.write_text(
        "V2 CHANGED {design_doc_bundle} {v_model_layer} {project_context}\n", encoding="utf-8"
    )
    SubprocessAiCommandPlanDeriver(fake, template_path=tmpl).derive_tasks([node], "detailed", ctx)
    assert fake.calls == 2
