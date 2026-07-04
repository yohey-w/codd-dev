"""Integration: the JS greenfield ExprCalc test-root placement bug, end to end.

Reproduces the exact shape that hard-failed the JS greenfield at implement: a task
declares ``['src/errors.js', 'test/errors.test.js']`` (the LLM freelanced the
singular ``test/`` for unit specs) while the harness owns ``tests/``. The
output-path fence used to DROP ``test/errors.test.js``, so the declared 'test'
deliverable read as "produced only source" → a hard ``StageError`` at the kind
gate (``pipeline._verify_task_contract``).

With the fix, the fake AI's misplaced ``test/errors.test.js`` is re-keyed under the
owned test root (B2), so the file exists at ``tests/errors.test.js`` (where the
verify runner discovers it) AND the kind gate passes — WITHOUT relaxing the fence
(the false-green vector: ``_produced_kinds`` counts a test-shaped file as kind
'test' wherever it sits, so an in-place accept would green the gate for a file the
runner never runs).
"""

from __future__ import annotations

from pathlib import Path

from codd.greenfield.pipeline import (
    ImplementTaskRef,
    _produced_kinds,
    _required_kinds,
)
from codd.implementer import (
    DesignContext,
    ImplementSpec,
    _normalize_declared_test_outputs,
    _parse_file_payloads,
    _test_root_from_config,
    _write_generated_files,
)

_CONFIG = {
    "project": {"name": "ExprCalc", "language": "typescript"},
    "scan": {"source_dirs": ["src"], "test_dirs": ["tests"]},
}

_RAW = (
    "=== FILE: src/errors.js ===\n"
    "```js\nexport class AppError extends Error {}\n```\n"
    "=== FILE: test/errors.test.js ===\n"  # freelanced singular dir
    "```js\nimport { AppError } from '../src/errors.js';\n"
    "test('constructs', () => { new AppError('x'); });\n```\n"
)


def _spec() -> ImplementSpec:
    return ImplementSpec(
        "test:errors",
        ["src", "tests"],  # the fence roots
        expected_outputs=["src/errors.js", "test/errors.test.js"],
    )


def _design_context() -> DesignContext:
    return DesignContext(
        node_id="test:errors",
        path=Path("docs/test/test_strategy.md"),
        content="# Test Strategy\n",
    )


def test_control_pre_fix_shape_drops_test_and_fails_kind_gate(tmp_path):
    """Without the test-root thread the misplaced test file is DROPPED and the kind
    gate would fail — this documents the bug the fix closes."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    # No test_root threaded → the misplaced test file is dropped (pre-fix behavior).
    dropped = {p for p, _ in _parse_file_payloads(_RAW, ["src", "tests"], "javascript")}
    assert dropped == {"src/errors.js"}
    produced_paths = [project_root / rel for rel in dropped]
    produced = _produced_kinds(produced_paths, project_root, _CONFIG)
    task = ImplementTaskRef(
        task_id="test:errors",
        design_node="docs/test/test_strategy.md",
        expected_outputs=("src/errors.js", "test/errors.test.js"),
    )
    required = _required_kinds(task, _CONFIG)
    assert required == {"source", "test"}
    # The bug: 'test' is required but never produced (the file was dropped).
    assert not required.issubset(produced)
    assert "test" not in produced


def test_b1_normalizes_declared_output_at_task_load():
    normalized = _normalize_declared_test_outputs(
        ["src/errors.js", "test/errors.test.js"], _CONFIG
    )
    assert normalized == ["src/errors.js", "tests/errors.test.js"]


def test_fix_rekeys_misplaced_test_and_kind_gate_passes(tmp_path):
    project_root = tmp_path / "proj"
    project_root.mkdir()

    generated = _write_generated_files(
        project_root=project_root,
        design_context=_design_context(),
        spec=_spec(),
        dependency_documents=[],
        language="javascript",
        raw_output=_RAW,
        syntax_gate=False,
        confusable_check=False,
        test_root=_test_root_from_config(_CONFIG),
    )

    rels = {p.relative_to(project_root).as_posix() for p in generated}
    # The misplaced test file now lives under the OWNED test root, not ``test/``.
    assert "tests/errors.test.js" in rels
    assert "test/errors.test.js" not in rels
    assert (project_root / "tests" / "errors.test.js").exists()
    assert not (project_root / "test" / "errors.test.js").exists()

    # Kind gate passes: the declared 'test' deliverable is produced where the runner
    # discovers it. Required kinds come from the declared outputs (verbatim).
    task = ImplementTaskRef(
        task_id="test:errors",
        design_node="docs/test/test_strategy.md",
        expected_outputs=("src/errors.js", "test/errors.test.js"),
    )
    required = _required_kinds(task, _CONFIG)
    produced = _produced_kinds(list(generated), project_root, _CONFIG)
    assert required == {"source", "test"}
    assert required.issubset(produced)
