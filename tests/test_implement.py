"""Tests for codd implement direct design-node API."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

from click.testing import CliRunner
import yaml

import codd.implementer as implementer_module
from codd.cli import main
from codd.implementer import (
    DesignContext,
    ImplementSpec,
    _build_implementation_prompt,
    _parse_file_payloads,
)


def _write_doc(
    project: Path,
    relative_path: str,
    *,
    node_id: str,
    body: str,
    depends_on: list[dict] | None = None,
    conventions: list[dict] | None = None,
) -> None:
    codd = {"node_id": node_id, "type": "design"}
    if depends_on is not None:
        codd["depends_on"] = depends_on
    if conventions is not None:
        codd["conventions"] = conventions
    path = project / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"{yaml.safe_dump({'codd': codd}, sort_keys=False, allow_unicode=True)}"
        "---\n\n"
        f"{body.rstrip()}\n",
        encoding="utf-8",
    )


def _setup_project(
    tmp_path: Path,
    *,
    language: str = "typescript",
    include_coding_principles: bool = False,
) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "codd").mkdir()
    config = {
        "project": {"name": "demo", "language": language, "frameworks": ["nextjs"]},
        "ai_command": "mock-ai --print",
        "scan": {
            "source_dirs": ["src/"],
            "test_dirs": ["tests/"],
            "doc_dirs": ["docs/design/"],
            "config_files": [],
            "exclude": [],
        },
        "conventions": [{"targets": ["db:rls_policies"], "reason": "Tenant isolation is mandatory."}],
        "implement": {"default_output_paths": {"docs/design/auth.md": ["src/auth"]}},
    }
    if include_coding_principles:
        config["coding_principles"] = "docs/governance/coding_principles.md"
        principles_path = project / "docs" / "governance" / "coding_principles.md"
        principles_path.parent.mkdir(parents=True)
        principles_path.write_text(
            "# Coding Principles\n\n- Prefer pure helper functions.\n- Make tenant checks explicit.\n",
            encoding="utf-8",
        )
    (project / "codd" / "codd.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    _write_doc(
        project,
        "docs/design/shared.md",
        node_id="design:shared",
        body="# Shared Design\n\nUse shared request context.\n",
    )
    _write_doc(
        project,
        "docs/design/auth.md",
        node_id="design:auth",
        depends_on=[{"id": "design:shared", "relation": "depends_on"}],
        conventions=[{"targets": ["module:auth"], "reason": "Role checks are release-blocking."}],
        body="# Auth Design\n\nImplement auth service with tenant-aware checks.\n",
    )
    return project


def _mock_implement_ai(monkeypatch, *, stdout: str | None = None) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def fake_run(command, *, input, capture_output, text, check, **kwargs):
        match = re.search(r"Output paths: (?P<output>[^\n,]+)", input)
        assert match is not None
        output_dir = match.group("output")
        calls.append({"command": command, "input": input, "output_dir": output_dir})
        body = stdout
        if body is None:
            body = (
                f"=== FILE: {output_dir}/index.ts ===\n"
                "```ts\n"
                "export type AuthContext = { ready: true };\n"
                "export function buildAuth(): AuthContext {\n"
                "  return { ready: true };\n"
                "}\n"
                "```\n"
            )
        return subprocess.CompletedProcess(args=command, returncode=0, stdout=body, stderr="")

    monkeypatch.setattr(implementer_module.generator_module.subprocess, "run", fake_run)
    return calls


def test_implement_command_generates_files_with_traceability_comments(tmp_path, monkeypatch):
    project = _setup_project(tmp_path, include_coding_principles=True)
    calls = _mock_implement_ai(monkeypatch)

    result = CliRunner().invoke(
        main,
        ["implement", "--path", str(project), "--design", "docs/design/auth.md", "--output", "src/auth"],
    )

    assert result.exit_code == 0, result.output
    generated_file = project / "src" / "auth" / "index.ts"
    assert generated_file.exists()
    content = generated_file.read_text(encoding="utf-8")
    assert content.startswith("// @generated-by: codd implement")
    assert "// @generated-from: docs/design/auth.md (design:auth)" in content
    assert "// @generated-from: docs/design/shared.md (design:shared)" in content
    assert "// @design-node: docs/design/auth.md" in content
    assert "1 files generated across 1 task(s)" in result.output

    prompt = calls[0]["input"]
    assert "Project coding principles" in prompt
    assert "Prefer pure helper functions." in prompt
    assert "Tenant isolation is mandatory." in prompt
    assert calls[0]["command"] == ["mock-ai", "--print"]


def test_implement_uses_configured_output_paths(tmp_path, monkeypatch):
    project = _setup_project(tmp_path)
    _mock_implement_ai(monkeypatch)

    result = CliRunner().invoke(main, ["implement", "--path", str(project), "--design", "docs/design/auth.md"])

    assert result.exit_code == 0, result.output
    assert (project / "src" / "auth" / "index.ts").is_file()


def test_implement_respects_python_project_language(tmp_path, monkeypatch):
    project = _setup_project(tmp_path, language="python")
    calls = _mock_implement_ai(
        monkeypatch,
        stdout=(
            "=== FILE: src/auth/service.py ===\n"
            "```python\n"
            "def build_service() -> bool:\n"
            "    return True\n"
            "```\n"
        ),
    )

    result = CliRunner().invoke(
        main,
        ["implement", "--path", str(project), "--design", "docs/design/auth.md", "--output", "src/auth"],
    )

    assert result.exit_code == 0, result.output
    generated_file = project / "src" / "auth" / "service.py"
    assert generated_file.exists()
    assert generated_file.read_text(encoding="utf-8").startswith("# @generated-by: codd implement")
    prompt = calls[0]["input"]
    assert "Primary language: python" in prompt
    assert "Generate concrete production-oriented Python source files." in prompt
    assert "=== FILE: src/auth/<filename>.py ===" in prompt
    assert "```python" in prompt


def test_implement_fallback_uses_rust_extension(tmp_path, monkeypatch):
    project = _setup_project(tmp_path, language="rust")
    calls = _mock_implement_ai(
        monkeypatch,
        stdout=(
            "```rust\n"
            "pub fn build_authentication() -> bool {\n"
            "    true\n"
            "}\n"
            "```\n"
        ),
    )

    result = CliRunner().invoke(
        main,
        ["implement", "--path", str(project), "--design", "docs/design/auth.md", "--output", "src/auth"],
    )

    assert result.exit_code == 0, result.output
    generated_file = project / "src" / "auth" / "index.rs"
    assert generated_file.exists()
    assert generated_file.read_text(encoding="utf-8").startswith("// @generated-by: codd implement")
    assert "Primary language: rust" in calls[0]["input"]
    assert "=== FILE: src/auth/<filename>.rs ===" in calls[0]["input"]


def test_parse_file_payloads_unheadered_unfenced_output_raises():
    """No '=== FILE: ... ===' header AND no complete code fence must raise.

    Regression for the 2026-06-30 java_v2 greenfield dogfood: this exact
    content shape (a duplicated pom.xml tail fragment sliced mid-token,
    starting "in>" instead of "<plugin>") had zero FILE-header matches and no
    fence, yet the old fallback silently accepted it as one implicit file and
    wrote it to disk as a bogus "index.java" with a full traceability header —
    indistinguishable from genuine generated output. It must raise instead, so
    the caller's existing ValueError -> NoUsableGeneratedFiles retry applies.
    """
    garbled = (
        "in>\n                <groupId>org.jacoco</groupId>\n            </plugin>\n"
        "        </plugins>\n    </build>\n</project>\n"
    )

    try:
        _parse_file_payloads(garbled, ["."], "java")
    except ValueError as exc:
        assert "no complete code fence" in str(exc)
    else:
        raise AssertionError("expected ValueError for unheadered, unfenced output")


def test_parse_file_payloads_unheadered_but_fenced_output_still_falls_back():
    """A single COMPLETE code fence with no FILE header is still accepted.

    Distinguishes the fix from an over-broad one: the fallback that
    `test_implement_fallback_uses_rust_extension` relies on (an AI that
    skipped the FILE header but did wrap its one file in a real fence) must
    keep working.
    """
    fenced = "```python\nvalue = 1\n```\n"

    payloads = _parse_file_payloads(fenced, ["src/auth"], "python")

    assert payloads == [("src/auth/index.py", "value = 1\n")]


def test_parse_file_payloads_unheadered_fenced_truncated_fragment_raises():
    """A single COMPLETE fence wrapping an unbalanced/truncated body must raise.

    Regression for the 2026-07-02 javascript Top-6 greenfield dogfood:
    tests/index.js was a truncated tail fragment of tests/e2e/tokenize.e2e.test.js
    (missing its opening imports/test-block, starting mid-statement with a stray
    orphaned `});`) that still happened to be wrapped start-to-end in one real
    fence, so the existing "is this a complete fence" guard let it through and it
    was written to disk as a bogus tests/index.js with a full traceability header
    — a `node --check` SyntaxError that then poisoned an unrelated, later task's
    coherence-oracle check. A complete fence is necessary but not sufficient: its
    body must also look like a whole file, not a mid-file fragment.
    """
    fenced = (
        "```javascript\n"
        "assert.strictEqual(json.input, '2 + 3 * 4');\n"
        "});\n"
        "\n"
        "test('does something else', () => {\n"
        "  assert.ok(true);\n"
        "});\n"
        "```\n"
    )

    try:
        _parse_file_payloads(fenced, ["tests"], "javascript")
    except ValueError as exc:
        assert "unbalanced" in str(exc)
    else:
        raise AssertionError("expected ValueError for a truncated fragment wrapped in one fence")


def test_parse_file_payloads_bare_basename_reroot_accepts_complete_file():
    """A bare-basename FILE header with genuinely complete content still reroots.

    Proves the new brace-balance guard on `_reroot_bare_basename` output is not
    over-broad: the documented codex use case (a bare `task_model.py`-style name
    rerooted under the single configured output prefix) must keep working when
    the content is a real, balanced, complete file.
    """
    headered = "=== FILE: index.py ===\n```python\ndef build():\n    return {'ok': True}\n```\n"

    payloads = _parse_file_payloads(headered, ["src/auth"], "python")

    assert payloads == [("src/auth/index.py", "def build():\n    return {'ok': True}\n")]


def test_parse_file_payloads_bare_basename_reroot_rejects_truncated_fragment():
    """A bare-basename FILE header with unbalanced/truncated content is skipped.

    Regression for the 2026-06-30/2026-07-03 cpp_v2 Top-6 greenfield dogfood:
    a mangled retry left a truncated tail fragment of a parser test (starting
    mid-token, missing #includes, unbalanced braces) under a bare `index.cpp`
    FILE header with a single configured output prefix (`.`); `_reroot_bare_basename`
    rerooted it to a real repo-root `index.cpp` with no content validation. The
    same fragment shape here must be skipped instead of written.
    """
    headered = (
        "=== FILE: index.cpp ===\n"
        "```cpp\n"
        "    const auto* left = dynamic_cast<const UnaryExpr*>(&root->left());\n"
        "    ASSERT_NE(left, nullptr);\n"
        "}\n"
        "```\n"
    )

    try:
        _parse_file_payloads(headered, ["."], "cpp")
    except ValueError as exc:
        assert "unbalanced" in str(exc)
    else:
        raise AssertionError("expected ValueError for a truncated fragment under a bare-basename header")


def test_implement_clean_removes_existing_output_path(tmp_path, monkeypatch):
    project = _setup_project(tmp_path)
    stale_dir = project / "src" / "auth"
    stale_dir.mkdir(parents=True)
    stale_file = stale_dir / "stale.ts"
    stale_file.write_text("// stale", encoding="utf-8")
    _mock_implement_ai(monkeypatch)

    result = CliRunner().invoke(
        main,
        ["implement", "--path", str(project), "--design", "docs/design/auth.md", "--output", "src/auth", "--clean"],
    )

    assert result.exit_code == 0, result.output
    assert "Cleaning requested output paths" in result.output
    assert not stale_file.exists()
    assert (project / "src" / "auth" / "index.ts").is_file()


def test_get_valid_task_slugs(tmp_path):
    from codd.implementer import get_valid_task_slugs

    project = _setup_project(tmp_path)

    assert get_valid_task_slugs(project) == {"auth"}


def test_get_valid_task_slugs_no_mapping(tmp_path):
    from codd.implementer import get_valid_task_slugs

    project = tmp_path / "empty"
    project.mkdir()
    (project / "codd").mkdir()
    (project / "codd" / "codd.yaml").write_text("project:\n  name: demo\n  language: typescript\n", encoding="utf-8")

    assert get_valid_task_slugs(project) == set()


def test_error_summaries_excluded_from_prompt():
    prompt = _build_implementation_prompt(
        config={"project": {"language": "typescript", "frameworks": ["next.js"]}},
        design_context=DesignContext(
            node_id="design:test",
            path=Path("docs/design/test.md"),
            content="# Test design\n",
        ),
        spec=ImplementSpec("docs/design/test.md", ["src/test"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
        prior_task_outputs=[
            {
                "task_id": "success",
                "task_title": "Successful Task",
                "directory": "src/test",
                "files": ["service.ts"],
                "exported_types": ["User"],
                "exported_functions": [],
                "exported_classes": [],
                "exported_values": [],
            },
            {
                "task_id": "failed",
                "task_title": "Failed Task",
                "directory": "src/failed",
                "files": [],
                "exported_types": [],
                "exported_functions": [],
                "exported_classes": [],
                "exported_values": [],
                "error": "AI command returned empty implementation output",
            },
        ],
    )

    assert "Successful Task" in prompt
    assert "Failed Task" not in prompt
    assert "empty implementation output" not in prompt


# ═══════════════════════════════════════════════════════════
# Regression: 2026-07-03 ExprCalc TypeScript greenfield dogfood. The
# ``infra:build-setup`` task (output_paths ``src`` + ``tests``, matching the
# real task shape exactly) generated tests/e2e/*.ts using Node's built-in
# ``node:test`` module, reasoning from a "no third-party dependencies"
# project convention. But the TS profile's scaffolded ``commands.verify``
# always runs Vitest directly (``npx vitest run``), and Vitest does not
# collect ``node:test``-style files — they fail verification with
# "No test suite found in file ...". Nothing in the implement prompt stated
# the project's ACTUAL test runner, so the AI had no ground truth to weigh
# against its own dependency-minimizing inference. See
# codd/languages/profile.py's ``TestFrameworkSpec`` for the full incident.
# ═══════════════════════════════════════════════════════════


def test_implementation_prompt_states_test_framework_for_typescript():
    prompt = _build_implementation_prompt(
        config={"project": {"language": "typescript"}},
        design_context=DesignContext(
            node_id="infra:build-setup",
            path=Path("docs/infra/build_setup.md"),
            content="# Build & Tooling Setup\n",
        ),
        spec=ImplementSpec("infra:build-setup", ["src", "tests"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
    )

    assert "Test framework (release-blocking" in prompt
    assert "Vitest" in prompt
    assert 'import { describe, it, expect } from "vitest"' in prompt
    assert "node:test" in prompt  # named explicitly as what NOT to use


def test_implementation_prompt_states_test_framework_for_python():
    prompt = _build_implementation_prompt(
        config={"project": {"language": "python"}},
        design_context=DesignContext(
            node_id="test:test-strategy",
            path=Path("docs/test/test_strategy.md"),
            content="# Test Strategy\n",
        ),
        spec=ImplementSpec("test:test-strategy", ["tests"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
    )

    assert "pytest" in prompt
    assert "import pytest" in prompt


def test_implementation_prompt_omits_test_framework_when_spec_is_not_test_related():
    prompt = _build_implementation_prompt(
        config={"project": {"language": "typescript"}},
        design_context=DesignContext(
            node_id="design:system",
            path=Path("docs/design/system_design.md"),
            content="# System Design\n",
        ),
        spec=ImplementSpec("design:system", ["src"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
    )

    assert "Test framework (release-blocking" not in prompt


# ═══════════════════════════════════════════════════════════
# Regression: 2026-07-02 ExprCalc Python greenfield dogfood, task
# ``add_doctest_worked_examples``. That task's declared expected_outputs are
# THREE ALREADY-EXISTING source files (its real job: add doctest examples to
# their docstrings — an EDIT, not a from-scratch generation) plus an unrelated
# test-wiring file it must also touch. On a retried/resumed invocation the
# three source files already carried correct doctest content from an earlier
# attempt, but this implement prompt never showed the model their current
# content (only design docs and terse prior-task summaries reach the prompt),
# so the model had no safe way to reproduce them complete-and-unchanged in a
# whole-file response and left them out — leaving
# ``_verify_task_contract`` to see produced={"test"} against a declared
# required={"source"} and hard-fail, even though the real deliverable was
# already complete on disk. The fix: thread the task's declared
# ``expected_outputs`` into the prompt and show the CURRENT content of any
# entry that already exists on disk.
# ═══════════════════════════════════════════════════════════

def test_build_implementation_prompt_includes_existing_output_file_content(tmp_path):
    existing = tmp_path / "src" / "auth" / "service.py"
    existing.parent.mkdir(parents=True)
    existing.write_text("def build_service() -> bool:\n    return True\n", encoding="utf-8")

    prompt = _build_implementation_prompt(
        config={"project": {"language": "python"}},
        design_context=DesignContext(
            node_id="design:test",
            path=Path("docs/design/test.md"),
            content="# Test design\n",
        ),
        spec=ImplementSpec(
            "docs/design/test.md",
            ["src/auth"],
            expected_outputs=["src/auth/service.py"],
        ),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
        project_root=tmp_path,
    )

    assert "Existing content of this task's declared output files" in prompt
    assert "def build_service() -> bool:" in prompt
    assert "EDIT them, not recreate them from scratch" in prompt


def test_build_implementation_prompt_omits_existing_content_without_project_root(tmp_path):
    """Backward compatibility: a caller that never passes ``project_root``
    (every call site that predates this fix) gets a prompt with no trace of
    the new section, even when ``expected_outputs`` names a real file."""
    existing = tmp_path / "src" / "auth" / "service.py"
    existing.parent.mkdir(parents=True)
    existing.write_text("def build_service() -> bool:\n    return True\n", encoding="utf-8")

    prompt = _build_implementation_prompt(
        config={"project": {"language": "python"}},
        design_context=DesignContext(
            node_id="design:test",
            path=Path("docs/design/test.md"),
            content="# Test design\n",
        ),
        spec=ImplementSpec(
            "docs/design/test.md",
            ["src/auth"],
            expected_outputs=["src/auth/service.py"],
        ),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
    )

    assert "Existing content of this task's declared output files" not in prompt
    assert "def build_service() -> bool:" not in prompt


def test_existing_output_files_context_skips_non_files_and_traversal(tmp_path):
    from codd.implementer import _existing_output_files_context

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.py").write_text("value = 1\n", encoding="utf-8")

    context = _existing_output_files_context(
        tmp_path,
        [
            "src/real.py",
            "src/does_not_exist.py",
            "module:Some.symbol",
            "../outside.py",
        ],
    )

    assert context is not None
    assert "value = 1" in context
    assert context.count("BEGIN EXISTING FILE") == 1


def test_existing_output_files_context_returns_none_when_nothing_exists(tmp_path):
    from codd.implementer import _existing_output_files_context

    assert _existing_output_files_context(tmp_path, ["src/brand_new.py"]) is None


def test_implement_tasks_shows_existing_content_for_edit_shaped_task(tmp_path, monkeypatch):
    """End-to-end version of the regression above: through the full
    ``implement_tasks`` public API (not the lower-level prompt-builder call),
    the AI command actually invoked must receive the pre-existing file's real
    content."""
    project = _setup_project(tmp_path, language="python")
    existing = project / "src" / "auth" / "service.py"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text(
        'def build_service() -> bool:\n'
        '    """Build it.\n\n'
        "    >>> build_service()\n"
        "    True\n"
        '    """\n'
        "    return True\n",
        encoding="utf-8",
    )
    calls = _mock_implement_ai(
        monkeypatch,
        stdout=(
            "=== FILE: src/auth/service.py ===\n"
            "```python\n"
            'def build_service() -> bool:\n'
            '    """Build it.\n\n'
            "    >>> build_service()\n"
            "    True\n"
            '    """\n'
            "    return True\n"
            "```\n"
        ),
    )

    results = implementer_module.implement_tasks(
        project,
        design="docs/design/auth.md",
        output_paths=["src/auth"],
        expected_outputs=["src/auth/service.py"],
    )

    assert not results[0].error
    prompt = calls[0]["input"]
    assert "Existing content of this task's declared output files" in prompt
    assert ">>> build_service()" in prompt


# ═══════════════════════════════════════════════════════════
# Regression: the SAME 2026-07-02 ExprCalc Python dogfood incident, second
# root cause. ``add_doctest_worked_examples``'s ``design_node`` (a derived
# task's ``source_design_doc``) is ``docs/infra/build_and_ci_setup.md`` — the
# SAME document backing a SIBLING task (``implement_zero_dependency_
# verification_script``). ``ImplementTaskRef`` carried no ``title``/
# ``description`` at all, so the implement prompt for EITHER task was
# byte-for-byte identical except for the (coarse, routing-only) output paths:
# the model had no way to tell which of the document's several described
# pieces of work was actually assigned to THIS invocation. Confirmed live: the
# model's response attempted to regenerate an UNRELATED sibling artifact
# (``scripts/verify_zero_dependencies.py`` — that sibling task's own output)
# while never touching this task's own declared source files. The fix adds a
# ``YOUR SPECIFIC TASK FOR THIS INVOCATION`` scope-boundary block from the
# DAG's own ``DerivedTask.title``/``description``, so the model is told
# in-band which piece of a possibly-multi-task document is its job right now.
# ═══════════════════════════════════════════════════════════

def test_build_implementation_prompt_includes_task_scope_boundary():
    prompt = _build_implementation_prompt(
        config={"project": {"language": "python"}},
        design_context=DesignContext(
            node_id="infra:build-and-ci",
            path=Path("docs/infra/build_and_ci_setup.md"),
            content="# Build and CI\n\nCovers doctests AND the zero-dependency script.\n",
        ),
        spec=ImplementSpec(
            "docs/infra/build_and_ci_setup.md",
            ["src"],
            task_title="Add doctest worked examples to public functions",
            task_description=(
                "Add doctest examples to tokenize()'s docstring, parse()'s docstring, "
                "and evaluate()'s docstring. Wire tests/__init__.py's load_tests hook."
            ),
        ),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
    )

    assert "YOUR SPECIFIC TASK FOR THIS INVOCATION" in prompt
    assert "Task: Add doctest worked examples to public functions" in prompt
    assert "Wire tests/__init__.py's load_tests hook" in prompt
    assert "a single design document commonly backs several separate tasks" in prompt
    # The scope boundary must appear BEFORE the raw design-doc dump, so the
    # model reads its actual assignment before the document's full text.
    assert prompt.index("YOUR SPECIFIC TASK FOR THIS INVOCATION") < prompt.index("Design document content:")


def test_build_implementation_prompt_omits_task_scope_boundary_when_untitled():
    """Backward compatibility: a task with no DAG-derived title/description
    (every call site that predates this fix, and configured
    ``implement_targets`` tasks that have no DerivedTask at all) gets a prompt
    with no trace of the new section."""
    prompt = _build_implementation_prompt(
        config={"project": {"language": "python"}},
        design_context=DesignContext(
            node_id="design:test",
            path=Path("docs/design/test.md"),
            content="# Test design\n",
        ),
        spec=ImplementSpec("docs/design/test.md", ["src/test"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
    )

    assert "YOUR SPECIFIC TASK FOR THIS INVOCATION" not in prompt


def test_implement_tasks_forwards_task_title_and_description(tmp_path, monkeypatch):
    """End-to-end: ``implement_tasks``'s new ``task_title``/``task_description``
    kwargs reach the actual prompt sent to the AI command."""
    project = _setup_project(tmp_path, language="python")
    calls = _mock_implement_ai(monkeypatch)

    implementer_module.implement_tasks(
        project,
        design="docs/design/auth.md",
        output_paths=["src/auth"],
        task_title="Add doctest worked examples to public functions",
        task_description="Add doctest examples to tokenize()'s docstring.",
    )

    prompt = calls[0]["input"]
    assert "YOUR SPECIFIC TASK FOR THIS INVOCATION" in prompt
    assert "Add doctest worked examples to public functions" in prompt
    assert "Add doctest examples to tokenize()'s docstring." in prompt


def test_implementation_prompt_forbids_meta_copy_in_user_facing_ui():
    prompt = _build_implementation_prompt(
        config={"project": {"language": "typescript", "frameworks": ["next.js"]}},
        design_context=DesignContext(
            node_id="design:login",
            path=Path("docs/design/login.md"),
            content="# Login design\n",
        ),
        spec=ImplementSpec("docs/design/login.md", ["src/app/login/page.tsx"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
    )

    assert "production user copy only" in prompt
    assert "never surface design rationale" in prompt
    assert "implementation assumptions" in prompt
    assert "environment notes as visible text" in prompt


def test_test_generation_prompt_guides_scoped_content_assertions():
    """Test-targeting implement runs must steer the model to scope content
    assertions to the subject under test (anti false-RED on incidental markup),
    while keeping the verifiable-behavior coverage requirement intact."""
    prompt = _build_implementation_prompt(
        config={"project": {"language": "python", "frameworks": ["flask"]}},
        design_context=DesignContext(
            node_id="test:acceptance",
            path=Path("docs/test/acceptance_criteria.md"),
            content="# Acceptance criteria\n",
        ),
        spec=ImplementSpec("docs/test/acceptance_criteria.md", ["src/tests"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
    )

    # The scoping principle is present and stated as precision, not weakening.
    assert "Scoped assertions" in prompt
    assert "subject under test" in prompt
    assert "not the whole response body" in prompt
    assert "entire response/document" in prompt
    # The existing coverage requirement must still be enforced (additive, not a
    # replacement): generated tests still assert every declared behavior.
    assert "codd: covers vb=<id>" in prompt
    assert "Still assert the constraint fully" in prompt
    # General principle only — no project/example literals leaked into the guidance.
    for literal in ("kakeibo", "viewport", "initial-scale", "summary"):
        assert literal not in prompt


def test_non_test_generation_prompt_omits_scoped_assertion_block():
    """The scoped-assertion guidance is gated to test-targeting runs only, so a
    plain source-implementation prompt must not carry it."""
    prompt = _build_implementation_prompt(
        config={"project": {"language": "python", "frameworks": ["flask"]}},
        design_context=DesignContext(
            node_id="design:summary",
            path=Path("docs/design/summary.md"),
            content="# Summary design\n",
        ),
        spec=ImplementSpec("docs/design/summary.md", ["src/app"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
    )

    assert "Scoped assertions" not in prompt
    assert "codd: covers vb=<id>" not in prompt


def test_test_generation_prompt_emits_e2e_no_runtime_import_rule_for_cli():
    """A test-targeting run on a CLI e2e modality (the default) must steer the
    model to keep the e2e layer subprocess-only and free of runtime imports — the
    e2e-no-runtime-import contract, with an AST-check preference."""
    from codd.project_types import ProjectCapabilities

    prompt = _build_implementation_prompt(
        config={"project": {"language": "python"}},
        design_context=DesignContext(
            node_id="test:acceptance",
            path=Path("docs/test/acceptance_criteria.md"),
            content="# Acceptance criteria\n",
        ),
        spec=ImplementSpec("docs/test/acceptance_criteria.md", ["src/tests"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
        capabilities=ProjectCapabilities(e2e_modality="cli"),
    )

    assert "E2E no-runtime-import contract" in prompt
    assert "as a SUBPROCESS" in prompt
    # In-process runtime-importing helpers belong in the unit tree, not e2e.
    assert "UNIT/integration helper" in prompt
    # Prefer an AST-based import check over brittle literal-string scanning.
    assert "prefer an AST-based import check" in prompt
    # Stack-neutral: no project/example literals leaked into the guidance.
    for literal in ("todo_cli", "todo-cli", "Playwright", "tests/e2e"):
        assert literal not in prompt


def test_test_generation_prompt_e2e_rule_defaults_on_when_untyped():
    """An untyped run (no capabilities) defaults to the conservative cli baseline,
    so the e2e-no-runtime-import guidance is emitted (matches the gate default)."""
    prompt = _build_implementation_prompt(
        config={"project": {"language": "python"}},
        design_context=DesignContext(
            node_id="test:acceptance",
            path=Path("docs/test/acceptance_criteria.md"),
            content="# Acceptance criteria\n",
        ),
        spec=ImplementSpec("docs/test/acceptance_criteria.md", ["src/tests"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
    )
    assert "E2E no-runtime-import contract" in prompt


def test_test_generation_prompt_omits_e2e_rule_for_browser_modality():
    """A browser e2e suite legitimately imports a client/runtime, so the
    no-runtime-import rule MUST be gated out (anti-false guidance)."""
    from codd.project_types import ProjectCapabilities

    prompt = _build_implementation_prompt(
        config={"project": {"language": "python"}},
        design_context=DesignContext(
            node_id="test:acceptance",
            path=Path("docs/test/acceptance_criteria.md"),
            content="# Acceptance criteria\n",
        ),
        spec=ImplementSpec("docs/test/acceptance_criteria.md", ["src/tests"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
        capabilities=ProjectCapabilities(e2e_modality="browser"),
    )
    assert "E2E no-runtime-import contract" not in prompt
    # The shared test-helper symbol rule (ungated) is still present.
    assert "Test-helper import coherence" in prompt


def test_non_test_generation_prompt_omits_e2e_no_runtime_import_rule():
    """The e2e-contract guidance is gated to test-targeting runs only."""
    prompt = _build_implementation_prompt(
        config={"project": {"language": "python"}},
        design_context=DesignContext(
            node_id="design:summary",
            path=Path("docs/design/summary.md"),
            content="# Summary design\n",
        ),
        spec=ImplementSpec("docs/design/summary.md", ["src/app"]),
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
    )
    assert "E2E no-runtime-import contract" not in prompt


# ── Provenance banner placement (shebang-aware) ─────────────────────────

_BANNER = "@generated-by: codd implement\n@generated-from: docs/design/x.md (design:x)"


def test_prepend_banner_keeps_node_shebang_on_line_one():
    """A `#!/usr/bin/env node` bin entry keeps its shebang on line 1; the
    banner goes immediately after (else `tsc` raises TS18026)."""
    content = '#!/usr/bin/env node\nimport { run } from "./run";\nrun();\n'
    out = implementer_module._prepend_traceability_comment("src/cli.ts", _BANNER, content)
    lines = out.splitlines()
    assert lines[0] == "#!/usr/bin/env node"
    assert lines[1] == "// @generated-by: codd implement"
    assert "// @generated-from: docs/design/x.md (design:x)" in out
    assert 'import { run } from "./run";' in out


def test_prepend_banner_keeps_python_shebang_on_line_one():
    """Language-agnostic: a Python shebang also stays on line 1."""
    content = "#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n"
    out = implementer_module._prepend_traceability_comment("src/tool.py", _BANNER, content)
    lines = out.splitlines()
    assert lines[0] == "#!/usr/bin/env python3"
    assert lines[1] == "# @generated-by: codd implement"
    assert "import sys" in out


def test_prepend_banner_non_shebang_unchanged():
    """Normal content (no shebang) → banner at the very top, as before."""
    content = 'import { run } from "./run";\nrun();\n'
    out = implementer_module._prepend_traceability_comment("src/cli.ts", _BANNER, content)
    lines = out.splitlines()
    assert lines[0] == "// @generated-by: codd implement"
    assert lines[1] == "// @generated-from: docs/design/x.md (design:x)"
    assert out.endswith('import { run } from "./run";\nrun();\n')


def test_prepend_banner_idempotent_for_shebang_file():
    """Applying the header twice to a shebang file must not duplicate the banner
    or push the shebang off line 1."""
    content = "#!/usr/bin/env node\nconsole.log(1);\n"
    once = implementer_module._prepend_traceability_comment("src/cli.ts", _BANNER, content)
    twice = implementer_module._prepend_traceability_comment("src/cli.ts", _BANNER, once)
    assert once == twice
    assert twice.splitlines()[0] == "#!/usr/bin/env node"
    assert twice.count("// @generated-by: codd implement") == 1


def test_prepend_banner_idempotent_for_non_shebang_file():
    """Idempotency for the ordinary top-of-file case is preserved."""
    content = "def f():\n    return 1\n"
    once = implementer_module._prepend_traceability_comment("src/m.py", _BANNER, content)
    twice = implementer_module._prepend_traceability_comment("src/m.py", _BANNER, once)
    assert once == twice
    assert once.count("# @generated-by: codd implement") == 1


def test_prepend_banner_shebang_ts_compiles_pattern():
    """A tiny TS bin file keeps the shebang on line 1, so `tsc` would accept it
    (no TS18026). Mirrors the codex2 dogfood regression."""
    content = "#!/usr/bin/env node\nconsole.log(1)\n"
    out = implementer_module._prepend_traceability_comment("src/cli.ts", _BANNER, content)
    assert out.startswith("#!/usr/bin/env node\n")
    # The shebang is the first line and nothing precedes it.
    assert out.index("#!/usr/bin/env node") == 0
    # Banner is still present (traceability preserved).
    assert "@generated-by: codd implement" in out
