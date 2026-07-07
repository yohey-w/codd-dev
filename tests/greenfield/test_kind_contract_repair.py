"""v3.17.0 — kind-contract envelope alignment + bounded feedback repair.

A fresh unattended Python greenfield halted at implement when the deriver bundled
source+test into one task and the implementer authored only the source: the mixed
task routes source-pure (its declared test falls outside the output fence and is
dropped), the prompt frames it as source-only, and the kind gate then hard-fails a
task the model could satisfy — with no feedback re-drive (the gate is verified by
the pipeline OUTSIDE the implementer's syntax/no-usable retry loop). This closes it
with: (1) envelope alignment — expose the test dirs when a task declares a test
kind; (2) a bounded feedback repair loop with UNION evaluation; (3) prompt
projection of the declared deliverables. The kind gate itself is byte-identical.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import codd.greenfield.pipeline as pipeline_mod
from codd.greenfield.pipeline import (
    GreenfieldPipeline,
    ImplementTaskRef,
    StageError,
    _augment_with_declared_test_roots,
)
from codd.implementer import (
    DesignContext,
    ImplementationResult,
    ImplementSpec,
    _build_implementation_prompt,
)
from tests.greenfield.conftest import make_stub_project


_CONFIG = {"project": {"language": "python"}, "scan": {"source_dirs": ["src"], "test_dirs": ["tests"]}}


def _task(*outs: str, task_id: str = "t") -> ImplementTaskRef:
    return ImplementTaskRef(task_id=task_id, design_node="docs/design/x.md", expected_outputs=tuple(outs))


# ── Edit 1: envelope alignment ────────────────────────────────────────────────


def test_augment_adds_test_root_for_declared_test_kind() -> None:
    """A task DECLARING a test kind whose resolved output paths are source-pure gets
    the test root appended (so the declared test is in-fence); a source-only task
    does NOT, and a base that already covers tests is returned unchanged (idempotent)."""
    mixed = _task("src/pkg/x.py", "tests/test_x.py")  # required kinds = {source, test}
    src_only = _task("src/pkg/x.py")  # required kinds = {source}

    # source-pure base + declared test kind → test root appended.
    assert "tests" in _augment_with_declared_test_roots(_CONFIG, mixed, ["src/pkg"])
    # source-only task → NEVER handed a test root, even with a source-pure base.
    assert "tests" not in _augment_with_declared_test_roots(_CONFIG, src_only, ["src/pkg"])
    # base already covers tests → returned unchanged (no duplicate, idempotent).
    assert _augment_with_declared_test_roots(_CONFIG, mixed, ["src/pkg", "tests"]) == ["src/pkg", "tests"]


def test_augment_excludes_dot_test_root() -> None:
    """A ``"."`` test root (a root-module language that colocates tests) is excluded
    — it needs no extra fence entry, so the base is returned unchanged."""
    config = {"project": {"language": "go"}, "scan": {"source_dirs": ["."], "test_dirs": ["."]}}
    mixed = _task("main.go", "main_test.go")
    assert _augment_with_declared_test_roots(config, mixed, ["pkg"]) == ["pkg"]


# ── Edit 2: declared-deliverable projection ───────────────────────────────────


def test_declared_deliverables_section_in_prompt(tmp_path) -> None:
    prompt = _build_implementation_prompt(
        config={"project": {"name": "demo", "language": "python"}, "scan": {"source_dirs": ["src/"], "test_dirs": ["tests/"]}},
        design_context=DesignContext(node_id="impl:tokenize", path=Path("docs/design/tokenizer.md"), content="# Tokenizer\n"),
        spec=ImplementSpec("impl:tokenize", ["src/demo"], expected_outputs=["src/demo/tokenizer.py", "tests/test_tokenizer.py"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
        project_root=tmp_path,
    )
    assert "DECLARED DELIVERABLES" in prompt
    assert "src/demo/tokenizer.py" in prompt
    assert "tests/test_tokenizer.py" in prompt


def test_prompt_omits_deliverables_section_without_declared_outputs(tmp_path) -> None:
    prompt = _build_implementation_prompt(
        config={"project": {"name": "demo", "language": "python"}, "scan": {"test_dirs": ["tests/"]}},
        design_context=DesignContext(node_id="design:system", path=Path("docs/design/system.md"), content="# System\n"),
        spec=ImplementSpec("design:system", ["src"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
        project_root=tmp_path,
    )
    assert "DECLARED DELIVERABLES" not in prompt


# ── Edit 3: bounded feedback repair (union evaluation) ────────────────────────


def _mixed_task() -> ImplementTaskRef:
    return ImplementTaskRef(
        task_id="implement_tokenize_scanner",
        design_node="docs/design/tokenizer.md",
        expected_outputs=("src/stub_app/tokenizer.py", "tests/test_tokenizer.py"),
    )


def _install_stub(monkeypatch, project: Path, writer):
    """Install a stub ``implement_tasks`` (via ``writer(attempt) -> relpath``) and a
    no-op step-deriver; returns the call-tracking dict."""
    calls: dict = {"n": 0, "feedbacks": []}

    def _stub(project_root, *, design=None, output_paths=None, expected_outputs=None,
              task_title=None, task_description=None, ai_command=None, use_derived_steps=None,
              feedback=None, **_kw):
        i = calls["n"]
        calls["n"] += 1
        calls["feedbacks"].append(feedback)
        written = []
        for rel in writer(i):
            path = Path(project_root) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            body = "def test_it():\n    assert True\n" if "test" in Path(rel).name else "def f():\n    return 1\n"
            path.write_text(body, encoding="utf-8")
            written.append(path)
        return [ImplementationResult(design_node=design or "n", output_paths=[Path(project_root) / "src"], generated_files=written)]

    monkeypatch.setattr("codd.implementer.implement_tasks", _stub)
    monkeypatch.setattr(pipeline_mod, "_derive_and_approve_steps", lambda *a, **k: 0)
    return calls


def _run(project: Path, task: ImplementTaskRef) -> str:
    return GreenfieldPipeline()._default_implement_task_runner(
        project, task, ai_command=None, coverage_gate=False, chunk_size=None, timeout_per_chunk=600,
    )


def test_kind_contract_repair_converges_on_second_attempt(tmp_path, monkeypatch) -> None:
    """attempt 1 emits only source → kind gate unmet → a feedback re-drive that names
    the missing kind + declared test path emits the test → UNION satisfies the gate."""
    project = make_stub_project(tmp_path, "stub-ai --print")
    calls = _install_stub(
        monkeypatch, project,
        writer=lambda i: ["src/stub_app/tokenizer.py"] if i == 0 else ["tests/test_tokenizer.py"],
    )
    _run(project, _mixed_task())
    assert calls["n"] == 2  # converged on the second attempt (not a hard fail)
    assert calls["feedbacks"][0] is None
    assert calls["feedbacks"][1] and "test" in calls["feedbacks"][1].lower()
    assert "tests/test_tokenizer.py" in calls["feedbacks"][1]


def test_kind_contract_green_path_single_attempt(tmp_path, monkeypatch) -> None:
    """A task that satisfies the contract on the first attempt costs exactly one
    implement call — the repair loop adds zero cost to the green path."""
    project = make_stub_project(tmp_path, "stub-ai --print")
    calls = _install_stub(
        monkeypatch, project,
        writer=lambda i: ["src/stub_app/tokenizer.py", "tests/test_tokenizer.py"],
    )
    _run(project, _mixed_task())
    assert calls["n"] == 1


def test_kind_contract_honest_exhaustion_raises_gate_error(tmp_path, monkeypatch) -> None:
    """If the model NEVER produces the declared test, the budget (1 + default 2)
    is spent and the gate itself raises the SAME hard StageError — anti-false-green
    intact, the task is never falsely marked done."""
    project = make_stub_project(tmp_path, "stub-ai --print")
    calls = _install_stub(
        monkeypatch, project,
        writer=lambda i: ["src/stub_app/tokenizer.py"],  # only ever source
    )
    with pytest.raises(StageError) as exc:
        _run(project, _mixed_task())
    assert calls["n"] == 3  # 1 initial + 2 retries, then honest fail
    assert "declared output kind" in str(exc.value)


def test_kind_contract_retries_disabled_by_knob(tmp_path, monkeypatch) -> None:
    """``implement.kind_contract_max_retries: 0`` restores the legacy hard-fail on
    the first mismatch (one attempt, no re-drive)."""
    project = make_stub_project(tmp_path, "stub-ai --print")
    # Write the knob into the project config.
    codd_yaml = project / "codd" / "codd.yaml"
    import yaml
    cfg = yaml.safe_load(codd_yaml.read_text(encoding="utf-8"))
    cfg.setdefault("implement", {})["kind_contract_max_retries"] = 0
    codd_yaml.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    calls = _install_stub(
        monkeypatch, project,
        writer=lambda i: ["src/stub_app/tokenizer.py"],  # only source
    )
    with pytest.raises(StageError):
        _run(project, _mixed_task())
    assert calls["n"] == 1  # no retry
