"""FIX-3 / FIX-4 / FIX-5 + campaign journal — the independent secondary fixes of
the Fable5 ts-v9 ruling (``dogfood/fable5_reply_2026-07-10_ts-v9.md``, Rulings
§"Secondary 1/2/3" and §(e)/(f) items 4-6).

* FIX-3 (Secondary 2 — rerun-scope hygiene): a task that authors no artifact can
  repair nothing, so it may never occupy a REPAIR scope. The first-pass
  short-circuit (``pipeline.py`` ``_default_implement_task_runner``) already skips
  such tasks; this closes the RERUN path (``_reimplement_tasks`` /
  ``_rerun_tasks_with_feedback``), which re-ran the two doc tasks 4x each = 8
  wasted AI calls emitting "outside output paths ['src','tests']".
* FIX-4 (Secondary 1 — plan-intake grounding): every ``expected_outputs`` entry
  must be a concrete path/glob OR a genuine prose-gate / non-codebase declaration.
  A PROSE entry describing authored codebase files in a code-authoring task (the
  ts-v9 ``implement_ci_dependency_purity_gates`` case) is UNGROUNDED — it reaches
  disk as an orphan the gate correctly refuses to own — so plan-intake demands a
  bounded re-derivation with concrete paths, then honest ``StageError``.
* FIX-5 (Secondary 3 — config default): ``ai_commands.impl_step_derive`` defaults
  to the session/base ``ai_command`` when unset; the warning fires ONLY when
  neither exists.
* Item 6: the implement-oracle campaign journal is persisted to
  ``<session>/implement_oracle_campaign.yaml`` (evidence-only).

Every fix is graph/data/config-layer — no ``language ==`` / per-symbol branch.
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest

import codd.greenfield.pipeline as pipeline_mod
from codd.greenfield.pipeline import GreenfieldPipeline, ImplementTaskRef, StageError

# The verbatim ts-v9 offender: ``implement_ci_dependency_purity_gates`` mixes a
# concrete authored test file with a PROSE string that describes authored files.
_CI_PROSE = "CI dependency-manifest and import-graph check scripts (exact path not specified by design)"

_TS_CONFIG = {
    "project": {"name": "exprcalc", "language": "typescript"},
    "scan": {"source_dirs": ["src"], "test_dirs": ["tests"]},
}


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _capture_implement_tasks(monkeypatch) -> list[str]:
    """Patch ``implement_tasks`` to RECORD (never run) the design nodes re-run."""
    seen: list[str] = []

    def fake_implement_tasks(project_root, *, design=None, **kwargs):
        seen.append(design)

        class _R:
            error = None
            generated_files: list = []

        return [_R()]

    import codd.implementer as implementer_mod

    monkeypatch.setattr(implementer_mod, "implement_tasks", fake_implement_tasks)
    return seen


# ═════════════════════════════════════════════════════════════
# FIX-3 — no-authored-artifact tasks never occupy a REPAIR scope.
# ═════════════════════════════════════════════════════════════


def _doc_task() -> ImplementTaskRef:
    # ``document_requirements_traceability``: its only output is a docs/ file the
    # config-wide output_paths ['src','tests'] excludes → authors nothing.
    return ImplementTaskRef(
        task_id="document_requirements_traceability",
        design_node="docs/requirements/requirements.md",
        output_paths=("docs/requirements",),
        expected_outputs=("docs/requirements/requirements.md",),
    )


def _src_task() -> ImplementTaskRef:
    return ImplementTaskRef(
        task_id="implement_expr_error_base_class",
        design_node="docs/design/errors.md",
        output_paths=("src/errors.ts",),
        expected_outputs=("src/errors.ts",),
    )


def test_fix3_reimplement_makes_zero_ai_calls_for_doc_task(tmp_path: Path, monkeypatch) -> None:
    seen = _capture_implement_tasks(monkeypatch)
    msgs: list[str] = []
    tasks = [_doc_task(), _src_task()]

    GreenfieldPipeline(echo=msgs.append)._reimplement_tasks(
        tmp_path, tasks, "feedback", _TS_CONFIG
    )

    # The doc task made ZERO AI calls; only the real source task re-ran.
    assert seen == ["docs/design/errors.md"], seen
    assert any(
        "document_requirements_traceability" in m and "skip" in m.lower() for m in msgs
    ), msgs


def test_fix3_campaign_scope_broad_skips_doc_tasks(tmp_path: Path, monkeypatch) -> None:
    # A broad campaign scope spans ALL tasks (the chunked_broad phase). The doc
    # tasks in it must make zero AI calls.
    import codd.config as _config_mod

    monkeypatch.setattr(_config_mod, "load_project_config", lambda root: _TS_CONFIG)
    monkeypatch.setattr(
        pipeline_mod, "_output_paths_for_task", lambda config, task: list(task.output_paths or ())
    )
    seen = _capture_implement_tasks(monkeypatch)

    tasks = [_doc_task(), _src_task()]
    GreenfieldPipeline(echo=lambda _m: None)._rerun_tasks_with_feedback(
        tmp_path, tasks, "feedback", _TS_CONFIG, scope=None
    )
    assert seen == ["docs/design/errors.md"], seen


# ═════════════════════════════════════════════════════════════
# FIX-4 — plan-intake grounding: prose that names authored files.
# ═════════════════════════════════════════════════════════════


def _ci_task_prose() -> ImplementTaskRef:
    return ImplementTaskRef(
        task_id="implement_ci_dependency_purity_gates",
        design_node="docs/infra/build_ci_setup.md",
        source="derived",
        expected_outputs=("tests/purity.test.ts", _CI_PROSE),
        test_kinds=("integration",),
    )


def _ci_task_pinned() -> ImplementTaskRef:
    return ImplementTaskRef(
        task_id="implement_ci_dependency_purity_gates",
        design_node="docs/infra/build_ci_setup.md",
        source="derived",
        output_paths=(
            ".github/scripts/check-deps.mjs",
            ".github/scripts/import-graph.mjs",
            "tests/purity.test.ts",
        ),
        expected_outputs=(
            "tests/purity.test.ts",
            ".github/scripts/check-deps.mjs",
            ".github/scripts/import-graph.mjs",
        ),
    )


def test_fix4_prose_mixed_task_exhausts_to_stageerror_naming_task(tmp_path: Path, monkeypatch) -> None:
    import codd.config as _config_mod

    monkeypatch.setattr(_config_mod, "load_project_config", lambda root: _TS_CONFIG)

    feedbacks: list[str] = []

    def deriver(project_root, *, ai_command=None, force=False, feedback=None):
        feedbacks.append(feedback or "")
        return 1

    # The deriver never fixes it — the lister keeps returning the prose task.
    lister = lambda project_root: [_ci_task_prose()]

    pipe = GreenfieldPipeline(task_deriver=deriver, task_lister=lister, echo=lambda _m: None)
    with pytest.raises(StageError) as exc:
        pipe._enforce_plan_intake_grounding(tmp_path, [_ci_task_prose()])

    assert "implement_ci_dependency_purity_gates" in str(exc.value), exc.value
    # The re-derivation feedback demanded concrete paths and named the prose entry.
    assert feedbacks, "expected at least one bounded re-derivation attempt"
    assert any("concrete" in fb.lower() and _CI_PROSE in fb for fb in feedbacks), feedbacks


def test_fix4_rederivation_converges_when_paths_pinned(tmp_path: Path, monkeypatch) -> None:
    import codd.config as _config_mod

    monkeypatch.setattr(_config_mod, "load_project_config", lambda root: _TS_CONFIG)

    calls = {"n": 0}

    def deriver(project_root, *, ai_command=None, force=False, feedback=None):
        calls["n"] += 1
        return 1

    # After the first re-derivation the deriver produces a path-pinned task.
    lister = lambda project_root: [_ci_task_pinned()]

    pipe = GreenfieldPipeline(task_deriver=deriver, task_lister=lister, echo=lambda _m: None)
    out = pipe._enforce_plan_intake_grounding(tmp_path, [_ci_task_prose()])

    assert calls["n"] == 1, "exactly one bounded re-derivation should have converged it"
    assert [t.task_id for t in out] == ["implement_ci_dependency_purity_gates"]


def test_fix4_pure_gate_task_is_not_flagged(tmp_path: Path, monkeypatch) -> None:
    # A task whose outputs are ALL prose/non-codebase (a verification/gate task) is
    # a legitimate no-op declaration and must NOT trip the grounding gate.
    import codd.config as _config_mod

    monkeypatch.setattr(_config_mod, "load_project_config", lambda root: _TS_CONFIG)
    gate_task = ImplementTaskRef(
        task_id="run_full_pytest_release_gate",
        design_node="docs/test/release_gate.md",
        source="derived",
        expected_outputs=("pytest -q output", "vitest run output"),
    )
    doc_task = _doc_task()

    pipe = GreenfieldPipeline(
        task_deriver=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not re-derive")),
        task_lister=lambda root: [gate_task, doc_task],
        echo=lambda _m: None,
    )
    out = pipe._enforce_plan_intake_grounding(tmp_path, [gate_task, doc_task])
    assert {t.task_id for t in out} == {"run_full_pytest_release_gate", "document_requirements_traceability"}


def test_fix4_path_pinned_ci_task_makes_github_scripts_orphan_gate_green(tmp_path: Path) -> None:
    from codd.implement_oracle_scope import build_path_owner_index, find_orphan_artifacts

    _write(tmp_path, ".github/scripts/check-deps.mjs", "// dep-purity check\n")
    _write(tmp_path, ".github/scripts/import-graph.mjs", "// import graph\n")

    index = build_path_owner_index([_ci_task_pinned()], project_root=tmp_path)
    orphans = find_orphan_artifacts(index, tmp_path)
    assert orphans == [], orphans


def test_fix4_prose_only_ci_task_leaves_github_scripts_orphaned(tmp_path: Path) -> None:
    # Characterization of the ROOT the fix addresses: with only prose (no concrete
    # path), the .github/scripts/*.mjs files have no owner → the orphan gate reds.
    from codd.implement_oracle_scope import build_path_owner_index, find_orphan_artifacts

    _write(tmp_path, ".github/scripts/check-deps.mjs", "// dep-purity check\n")
    # The task's OUTPUT sources carry only src/tests (config-wide), never .github.
    prose = ImplementTaskRef(
        task_id="implement_ci_dependency_purity_gates",
        design_node="docs/infra/build_ci_setup.md",
        output_paths=("src", "tests"),
        expected_outputs=("tests/purity.test.ts", _CI_PROSE),
    )
    index = build_path_owner_index([prose], project_root=tmp_path)
    orphans = find_orphan_artifacts(index, tmp_path)
    assert any(o.path == ".github/scripts/check-deps.mjs" for o in orphans), orphans


# ═════════════════════════════════════════════════════════════
# FIX-5 — impl_step_derive defaults to the base ai_command.
# ═════════════════════════════════════════════════════════════


def _op_flow_node():
    from codd.dag import Node

    return Node(
        id="docs/requirements/sample.md",
        kind="design_doc",
        path="docs/requirements/sample.md",
        attributes={
            "operation_flow": {
                "operations": [
                    {"id": "create_x", "actor": "admin", "verb": "create", "target": "x", "ui_pattern": "single_form"}
                ]
            }
        },
    )


def test_fix5_no_warning_when_base_ai_command_set_but_impl_step_derive_unset() -> None:
    from codd.llm.criteria_expander import warn_if_operation_flow_unused

    config = {"ai_command": "claude --print"}  # base set, impl_step_derive UNSET
    stream = io.StringIO()
    emitted = warn_if_operation_flow_unused(config, [_op_flow_node()], stream=stream)
    assert emitted is False, stream.getvalue()
    assert stream.getvalue() == ""


def test_fix5_warns_only_when_both_absent() -> None:
    from codd.llm.criteria_expander import warn_if_operation_flow_unused

    stream = io.StringIO()
    emitted = warn_if_operation_flow_unused({"ai_commands": {}}, [_op_flow_node()], stream=stream)
    assert emitted is True
    assert "impl_step_derive" in stream.getvalue()


def test_fix5_deriver_runs_with_base_ai_command_fallback(tmp_path: Path, monkeypatch) -> None:
    from pathlib import Path as _P

    import codd.deployment.providers.ai_command as ai_command_mod
    import codd.llm.impl_step_deriver as deriver_mod
    from codd.generator import DependencyDocument
    from codd.implementer import ImplementSpec, _load_or_derive_implementation_steps

    captured: dict[str, object] = {}

    class FakeSubprocessAiCommand:
        def __init__(self, *, command, project_root, config):
            captured["command"] = command

    class FakeDeriver:
        def __init__(self, ai_command):
            captured["ai"] = ai_command

        def derive_steps(self, spec, nodes, context):
            captured["derived"] = True
            return []

    monkeypatch.setattr(ai_command_mod, "SubprocessAiCommand", FakeSubprocessAiCommand)
    monkeypatch.setattr(deriver_mod, "SubprocessAiCommandImplStepDeriver", FakeDeriver)

    config = {"project": {"language": "python"}, "ai_command": "base-ai --print"}
    spec = ImplementSpec(design_node="docs/design/x.md", output_paths=["src"])
    docs = [DependencyDocument(node_id="d", path=_P("docs/design/x.md"), content="# x")]

    _load_or_derive_implementation_steps(config, spec, docs, tmp_path)

    assert captured.get("derived") is True, "deriver must run with the base ai_command fallback"
    assert captured.get("command") == "base-ai --print", captured


# ═════════════════════════════════════════════════════════════
# Item 6 — session-level campaign journal.
# ═════════════════════════════════════════════════════════════


def test_item6_campaign_record_persisted_to_session_yaml(tmp_path: Path) -> None:
    import yaml

    from codd.implement_oracle import _append_campaign_record

    _append_campaign_record(
        tmp_path,
        event="oracle_broad_phase",
        phase="supplier_first",
        focus_paths=("src/parser.ts",),
        task_ids=("implement_parser_entrypoint_and_error_sites",),
        before_signature=(("TS2305", "src/index.ts"),),
        after_signature=(),
        elapsed=1.5,
        status="progress",
        echo=lambda _m: None,
    )

    journal = tmp_path / ".codd" / "implement_oracle_campaign.yaml"
    assert journal.exists(), "session-level campaign journal must be written"
    data = yaml.safe_load(journal.read_text(encoding="utf-8"))
    records = data.get("records") if isinstance(data, dict) else None
    assert records and records[0]["phase"] == "supplier_first", data
    assert records[0]["status"] == "progress"
