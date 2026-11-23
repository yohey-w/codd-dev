"""Bounded existing-file context must never authorize a blind whole-file rewrite."""
from __future__ import annotations

import copy
import hashlib
import pickle
import subprocess
from pathlib import Path

import pytest
import yaml

import codd.ai_invoke as ai_invoke
import codd.implementer as implementation
from codd.cli import CoddCLIError
from codd.implementer import (
    DesignContext,
    EXISTING_OUTPUT_FILES_PROMPT_LIMIT,
    Implementer,
    ImplementSpec,
    _build_implementation_prompt,
    _existing_output_files_context,
)


LIMIT = 20000
UNSET = object()


def put(root: Path, path: str, body: str) -> Path:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def render(root: Path, outputs: list[str], expected: list[str] | None = None, *, limit=UNSET) -> str:
    config = {"project": {"name": "fixture", "language": "python", "frameworks": []}}
    if limit is not UNSET:
        config["implement"] = {"existing_output_files_prompt_limit": limit}
    return _build_implementation_prompt(
        config=config,
        design_context=DesignContext("design:fixture", Path("docs/design.md"), "Preserve existing behavior.\n"),
        spec=ImplementSpec("docs/design.md", outputs, expected_outputs=expected or []),
        dependency_documents=[], conventions=[], coding_principles=None, project_root=root,
    )


def project(root: Path, *, syntax_gate: bool = True, retries: int = 0, limit=UNSET) -> Path:
    settings = {"syntax_gate": syntax_gate, "confusable_check": syntax_gate,
                "no_usable_file_retries": retries}
    if limit is not UNSET:
        settings["existing_output_files_prompt_limit"] = limit
    put(root, "codd/codd.yaml", yaml.safe_dump({
        "project": {"name": "fixture", "language": "python", "frameworks": []},
        "ai_command": "test-agent",
        "implement": settings,
        "scan": {"source_dirs": ["src/"], "doc_dirs": ["docs/"],
                 "config_files": [], "exclude": []},
    }))
    put(root, "docs/design.md", "---\ncodd:\n  node_id: design:fixture\n  type: design\n---\nPreserve existing behavior.\n")
    return root


def block(path: str, body: str = "value = 2\n") -> str:
    return f"=== FILE: {path} ===\n{body}\n"


def test_implement_under_cap_prompt_fixed_baseline_sha256(tmp_path):
    put(tmp_path, "src/unit.py", "def preserve():\n    return 7\n")
    prompt = render(tmp_path, ["src/unit.py"], ["src/unit.py"])
    # Computed from the unmodified production implementation before this fix.
    assert len(prompt) == 6706
    assert hashlib.sha256(prompt.encode()).hexdigest() == "3385abb87e56448703f85099ae3f19f3da2b1dd1134a0420dd30f3bd2ad16397"


@pytest.mark.parametrize("limit", [None, -1, 0, "invalid", float("inf")],
                         ids=["null", "negative", "zero", "non-numeric", "infinite"])
def test_implement_invalid_or_null_limit_matches_unset(tmp_path, limit):
    path = "src/unit.py"
    put(tmp_path, path, "value = 1\n")
    assert render(tmp_path, [path], limit=limit) == render(tmp_path, [path])

    put(tmp_path, path, "#" + "界" * LIMIT + "\n")
    default_prompt = render(tmp_path, [path])
    assert f"content limit: {LIMIT} characters" in default_prompt
    assert f"PARTIAL: {path}" in default_prompt
    assert render(tmp_path, [path], limit=limit) == default_prompt


def test_implement_under_cap_existing_context_is_legacy_identical(tmp_path):
    put(tmp_path, "src/one.py", "first = 1\n\n")
    put(tmp_path, "src/two.py", "second = 2\n")
    assert _existing_output_files_context(tmp_path, ["src/one.py", "src/two.py"]) == (
        "--- BEGIN EXISTING FILE src/one.py ---\nfirst = 1\n--- END EXISTING FILE src/one.py ---\n\n"
        "--- BEGIN EXISTING FILE src/two.py ---\nsecond = 2\n--- END EXISTING FILE src/two.py ---"
    )


def test_implement_overflow_inventory_includes_full_partial_and_every_omitted(tmp_path):
    put(tmp_path, "src/full.py", "# full\n")
    put(tmp_path, "src/partial.py", "#" + "x" * LIMIT + "\nHIDDEN_PARTIAL_TAIL\n")
    put(tmp_path, "src/omitted.py", "HIDDEN_OMITTED_BODY\n")
    put(tmp_path, "src/later.py", "HIDDEN_LATER_BODY\n")
    prompt = render(tmp_path, ["src"], ["src/full.py", "src/partial.py", "src/omitted.py", "src/later.py"])
    assert "FULL: src/full.py" in prompt
    assert "PARTIAL: src/partial.py" in prompt
    assert "OMITTED: src/omitted.py" in prompt
    assert "OMITTED: src/later.py" in prompt
    assert "read the full file" in prompt.lower()
    assert "overrides" in prompt.lower()
    assert "HIDDEN_PARTIAL_TAIL" not in prompt
    assert "HIDDEN_OMITTED_BODY" not in prompt
    assert "HIDDEN_LATER_BODY" not in prompt
    context = _existing_output_files_context(tmp_path, ["src/full.py", "src/partial.py"])
    shown_partial = context.split("--- BEGIN EXISTING FILE src/partial.py ---\n", 1)[1].split("\n--- END EXISTING FILE", 1)[0]
    shown_characters = LIMIT - len("# full\n")
    total_characters = len((tmp_path / "src/partial.py").read_text())
    assert shown_partial == (
        "#" + "x" * (shown_characters - 1)
        + f"\n[PARTIAL EXISTING FILE: showing {shown_characters} of {total_characters} characters]"
    )
    assert EXISTING_OUTPUT_FILES_PROMPT_LIMIT == LIMIT


def test_implement_incomplete_prompt_instructs_text_only_models_to_omit(tmp_path):
    path = "src/partial.py"
    put(tmp_path, path, "x" * (LIMIT + 1))
    instructions = render(tmp_path, [path]).split("Safety rule", 1)[1]
    assert "If you can read and edit files on disk, read the full file before editing it" in instructions
    assert "write the complete updated file directly to disk" in instructions
    assert "If you cannot read and edit files on disk, omit those files from your response" in instructions
    assert "report" not in instructions.lower()
    # A disk-editing agent must not split one task between disk edits and text
    # blocks: the capture keeps what changed on disk and drops its text blocks.
    assert "Deliver every other file of this task the same way — written to disk, not as text blocks" in instructions


@pytest.mark.parametrize("extra", [False, True], ids=["exact-cap", "following-file"])
def test_implement_exact_cap_boundary(tmp_path, extra):
    put(tmp_path, "src/full.py", "x" * LIMIT)
    outputs = ["src/full.py"]
    if extra:
        put(tmp_path, "src/next.py", "next_body\n")
        outputs.append("src/next.py")
    context = _existing_output_files_context(tmp_path, outputs)
    assert "x" * LIMIT in context
    assert "PARTIAL:" not in context
    if extra:
        assert "FULL: src/full.py" in context
        assert "OMITTED: src/next.py" in context
    else:
        assert context == f"--- BEGIN EXISTING FILE src/full.py ---\n{'x' * LIMIT}\n--- END EXISTING FILE src/full.py ---"


@pytest.mark.parametrize("path", ["src/cli.py", "src/library.py", "src/pipeline.py"], ids=["cli", "library", "pipeline"])
@pytest.mark.parametrize("direct", [False, True], ids=["expected", "direct"])
@pytest.mark.parametrize("syntax_gate", [False, True], ids=["gates-off", "gates-on"])
@pytest.mark.parametrize("smaller", [False, True], ids=["longer", "shorter"])
def test_implement_blind_partial_rewrite_is_rejected(tmp_path, monkeypatch, path, direct, syntax_gate, smaller):
    root = project(tmp_path, syntax_gate=syntax_gate)
    original = "#" + "p" * LIMIT + "\npreserve = 1\n"
    target = put(root, path, original)
    replacement = "replacement = 2\n" if smaller else "#" + "q" * (LIMIT + 100) + "\nreplacement = 2\n"
    response = block(path, replacement)
    monkeypatch.setattr(implementation.generator_module, "_invoke_ai_command", lambda *a, **k: response)
    spec = ImplementSpec("docs/design.md", [path] if direct else ["src"], expected_outputs=[] if direct else [path])
    with pytest.raises(CoddCLIError) as exc:
        Implementer(root, sleep=lambda _: None).run_implement(spec)
    assert path in str(exc.value)
    assert "incomplete" in str(exc.value).lower()
    assert target.read_text() == original


@pytest.mark.parametrize("alias", ["none", "response-symlink", "response-hardlink"])
def test_implement_omitted_rewrite_rejects_entire_mixed_batch(tmp_path, monkeypatch, alias):
    root = project(tmp_path, syntax_gate=False)
    put(root, "src/full.py", "#" + "f" * (LIMIT - 2) + "\n")
    target = put(root, "src/omitted.py", "preserve = 1\n")
    response_path = "src/alias.py" if alias != "none" else "src/omitted.py"
    if alias == "response-symlink":
        (root / response_path).symlink_to("omitted.py")
    elif alias == "response-hardlink":
        (root / response_path).hardlink_to(target)
    response = block("src/new.py") + block(response_path)
    monkeypatch.setattr(implementation.generator_module, "_invoke_ai_command", lambda *a, **k: response)
    with pytest.raises(CoddCLIError) as exc:
        Implementer(root, sleep=lambda _: None).run_implement(ImplementSpec(
            "docs/design.md", ["src"], expected_outputs=["src/full.py", "src/omitted.py"]))
    assert response_path in str(exc.value)
    assert target.read_text() == "preserve = 1\n"
    assert not (root / "src/new.py").exists()


def test_implement_omitted_declared_symlink_protects_its_target(tmp_path, monkeypatch):
    root = project(tmp_path, syntax_gate=False)
    put(root, "src/full.py", "x" * LIMIT)
    original = "preserve = 1\n"
    target = put(root, "src/canonical.py", original)
    (root / "src/declared.py").symlink_to(target.name)

    def invoke(*args, **kwargs):
        # An atomic save changes the inode but leaves the resolved path in place.
        put(root, "src/temporary.py", original).replace(target)
        return block("src/canonical.py")

    monkeypatch.setattr(implementation.generator_module, "_invoke_ai_command", invoke)
    with pytest.raises(CoddCLIError, match="OMITTED: src/declared.py"):
        Implementer(root).run_implement(ImplementSpec(
            "docs/design.md", ["src"], expected_outputs=["src/full.py", "src/declared.py"]))
    assert target.read_text() == original


@pytest.mark.parametrize("unreadable", ["binary", "permission-denied"])
def test_implement_unreadable_declared_file_is_listed_and_protected(tmp_path, monkeypatch, unreadable):
    root = project(tmp_path, syntax_gate=False)
    path = "src/unreadable.py"
    target = put(root, path, "preserve = 1\n")
    original = b"\xff\xfe\x80\n" if unreadable == "binary" else target.read_bytes()
    target.write_bytes(original)
    if unreadable == "permission-denied":
        real_read_text = Path.read_text

        def read_text(self, *args, **kwargs):
            if self == target:
                raise PermissionError("test fixture denies this read")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read_text)

    prompts = []

    def invoke(command, prompt, **kwargs):
        prompts.append(prompt)
        return block("src/new.py") + block(path)

    monkeypatch.setattr(implementation.generator_module, "_invoke_ai_command", invoke)
    with pytest.raises(CoddCLIError, match=f"OMITTED: {path} \\(unreadable\\)") as error:
        Implementer(root).run_implement(ImplementSpec("docs/design.md", ["src"], expected_outputs=[path]))
    assert "Unreadable files are excluded from the character count." in str(error.value)
    assert f"OMITTED: {path} (unreadable)" in prompts[0]
    assert f"BEGIN EXISTING FILE {path}" not in prompts[0]
    assert target.read_bytes() == original
    assert not (root / "src/new.py").exists()


def test_implement_with_full_context_keeps_syntax_gate_before_path_validation(tmp_path, monkeypatch):
    root = project(tmp_path / "project")
    original = "preserve = 1\n"
    existing = put(root, "src/unit.py", original)
    outside = put(tmp_path, "outside.py", original)
    (root / "src/alias.py").symlink_to(outside)
    prompts = []

    def invoke(command, prompt, **kwargs):
        prompts.append(prompt)
        return block("src/unit.py", "def broken(:\n") + block("src/alias.py")

    monkeypatch.setattr(implementation.generator_module, "_invoke_ai_command", invoke)
    with pytest.raises(CoddCLIError, match=r"produced invalid file\(s\) after 3 attempt\(s\): src/unit.py"):
        Implementer(root).run_implement(ImplementSpec("docs/design.md", ["src"], expected_outputs=["src/unit.py"]))
    assert len(prompts) == 3
    assert all("--- REVIEW FEEDBACK" in prompt for prompt in prompts[1:])
    assert existing.read_text() == outside.read_text() == original


def test_implement_omitted_existing_files_warn_once_on_success(tmp_path, monkeypatch, capsys):
    root = project(tmp_path)
    full = put(root, "src/full.py", "value = 1\n")
    partial = put(root, "src/partial.py", "x" * (LIMIT + 1))
    omitted = put(root, "src/omitted.py", "preserve = 1\n")
    unreadable = put(root, "src/unreadable.py", "")
    unreadable.write_bytes(b"\xff")
    responses = iter([block("src/full.py", "def broken(:\n"), block("src/full.py") + block("src/new.py")])
    monkeypatch.setattr(implementation.generator_module, "_invoke_ai_command", lambda *a, **k: next(responses))
    expected = [full, partial, omitted, unreadable]
    result = Implementer(root).run_implement(ImplementSpec(
        "docs/design.md", ["src"], expected_outputs=[path.relative_to(root).as_posix() for path in expected]))
    assert result.generated_files == [full, root / "src/new.py"]
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1
    warning = lines[0]
    assert "PARTIAL: src/partial.py" in warning
    assert "OMITTED: src/omitted.py" in warning
    assert "OMITTED: src/unreadable.py (unreadable)" in warning
    assert "src/full.py" not in warning


def test_implement_overflow_still_accepts_fully_shown_and_new_files(tmp_path, monkeypatch):
    root = project(tmp_path)
    put(root, "src/full.py", "value = 1\n")
    original = "#" + "p" * LIMIT + "\n"
    partial = put(root, "src/partial.py", original)
    monkeypatch.setattr(implementation.generator_module, "_invoke_ai_command", lambda *a, **k: block("src/full.py") + block("src/new.py"))
    result = Implementer(root).run_implement(ImplementSpec(
        "docs/design.md", ["src"], expected_outputs=["src/full.py", "src/partial.py"]))
    assert result.generated_files == [root / "src/full.py", root / "src/new.py"]
    assert "value = 2" in (root / "src/full.py").read_text()
    assert partial.read_text() == original


@pytest.mark.parametrize("writes_disk", [False, True], ids=["stdout-fallback", "disk-capture"])
@pytest.mark.parametrize("smaller", [False, True], ids=["preserving", "legitimately-smaller"])
def test_implement_real_agent_capture_provenance_after_baseline_restore(tmp_path, monkeypatch, capsys, writes_disk, smaller):
    root = project(tmp_path)
    original = "#" + "p" * LIMIT + "\npreserve = 1\n"
    target = put(root, "src/unit.py", original)
    replacement = "value = 2\n" if smaller else original + "added = 2\n"
    real_run = subprocess.run
    real_run(["git", "init", "-q", str(root)], check=True)
    observed: list[str] = []

    def fake_run(command, **kwargs):
        if command[0] == "git":
            return real_run(command, **kwargs)
        assert command == ["test-agent"]
        if writes_disk:
            target.write_text(replacement, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, block("src/unit.py", replacement), "")

    def invoke(*args, **kwargs):
        captured = ai_invoke.invoke_ai("test-agent", args[1], project_root=root)
        assert isinstance(captured, str)
        assert target.read_text() == original  # capture cleanup already restored it
        observed.append(captured)
        return captured

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)
    monkeypatch.setattr(ai_invoke, "is_file_writing_agent", lambda command: command == ["test-agent"])
    monkeypatch.setattr(implementation.generator_module, "_invoke_ai_command", invoke)
    spec = ImplementSpec("docs/design.md", ["src/unit.py"])
    if writes_disk:
        result = Implementer(root).run_implement(spec)
        assert result.generated_files == [target]
        assert replacement.rstrip() in target.read_text()
        assert "no accepted output for incompletely shown existing files" not in capsys.readouterr().err
    else:
        with pytest.raises(CoddCLIError, match="incomplete"):
            Implementer(root).run_implement(spec)
        assert target.read_text() == original
    assert len(observed) == 1


def test_implement_incomplete_guard_retries_bounded_with_clear_reason(tmp_path, monkeypatch):
    root = project(tmp_path, retries=3)
    original = "#" + "界" * LIMIT + "\n"
    target = put(root, "src/unit.py", original)
    full = put(root, "src/full.py", "keep = 0\n")
    omitted = put(root, "src/other.py", "keep = 1\n")
    prompts: list[str] = []

    def invoke(command, prompt, **kwargs):
        prompts.append(prompt)
        return block("src/unit.py")

    monkeypatch.setattr(implementation.generator_module, "_invoke_ai_command", invoke)
    with pytest.raises(CoddCLIError, match="incomplete") as error:
        Implementer(root, sleep=lambda _: None).run_implement(ImplementSpec(
            "docs/design.md", ["src"], expected_outputs=["src/full.py", "src/unit.py", "src/other.py"]))
    assert len(prompts) == 2
    assert "incomplete" in prompts[1].split("--- REVIEW FEEDBACK", 1)[1].lower()
    message = str(error.value)
    assert "PARTIAL: src/unit.py" in message
    assert "OMITTED: src/other.py" in message
    total = len(full.read_text()) + len(original) + len(omitted.read_text())
    assert f"{total} characters" in message
    assert f"limit: {LIMIT} characters" in message
    assert "implement.existing_output_files_prompt_limit" in message
    assert "AI command" in message and "directly on disk" in message
    assert target.read_text() == original


def test_implement_incomplete_guard_retry_can_omit_untouched_file(tmp_path, monkeypatch):
    root = project(tmp_path, retries=3)
    original = "#" + "p" * LIMIT + "\n"
    target = put(root, "src/large.py", original)
    new_path = "src/new.py"
    prompts: list[str] = []

    def invoke(command, prompt, **kwargs):
        prompts.append(prompt)
        if len(prompts) == 1:
            return block(new_path) + block("src/large.py")
        assert not (root / new_path).exists()
        assert "incomplete" in prompt.split("--- REVIEW FEEDBACK", 1)[1].lower()
        return block(new_path)

    monkeypatch.setattr(implementation.generator_module, "_invoke_ai_command", invoke)
    result = Implementer(root, sleep=lambda _: None).run_implement(ImplementSpec(
        "docs/design.md", ["src"], expected_outputs=["src/large.py", new_path]))
    assert len(prompts) == 2
    assert result.generated_files == [root / new_path]
    assert "value = 2" in (root / new_path).read_text()
    assert target.read_text() == original


def test_implement_incomplete_guard_preserves_general_retry_budget(tmp_path, monkeypatch):
    root = project(tmp_path, retries=3)
    put(root, "src/large.py", "#" + "p" * LIMIT + "\n")
    new_path = "src/new.py"
    responses = iter([block("src/large.py"), "", "", "", block(new_path)])
    monkeypatch.setattr(implementation.generator_module, "_invoke_ai_command", lambda *a, **k: next(responses))
    result = Implementer(root, sleep=lambda _: None).run_implement(ImplementSpec(
        "docs/design.md", ["src"], expected_outputs=["src/large.py", new_path]))
    assert result.generated_files == [root / new_path]


@pytest.mark.parametrize("limit", [30000, "30000"], ids=["integer", "numeric-string"])
def test_implement_raised_limit_allows_text_replacement_of_newly_full_file(tmp_path, monkeypatch, limit):
    root = project(tmp_path, limit=limit)
    target = put(root, "src/large.py", "#" + "p" * LIMIT + "\nexisting_tail = 1\n")
    untouched = put(root, "src/later.py", "#" + "q" * LIMIT + "\n")
    paths = ["src/large.py", "src/later.py"]
    default_prompt = render(root, ["src"], paths)
    assert "PARTIAL: src/large.py" in default_prompt
    assert "OMITTED: src/later.py" in default_prompt

    def invoke(command, prompt, **kwargs):
        assert "content limit: 30000 characters" in prompt
        assert "FULL: src/large.py" in prompt
        assert "PARTIAL: src/later.py" in prompt
        assert "existing_tail = 1" in prompt
        return block("src/large.py")

    monkeypatch.setattr(implementation.generator_module, "_invoke_ai_command", invoke)
    result = Implementer(root).run_implement(ImplementSpec("docs/design.md", ["src"], expected_outputs=paths))
    assert result.generated_files == [target]
    assert "value = 2" in target.read_text()
    assert untouched.read_text() == "#" + "q" * LIMIT + "\n"


def test_implement_incomplete_coverage_is_snapshotted_before_invocation(tmp_path, monkeypatch):
    root = project(tmp_path)
    target = put(root, "src/unit.py", "#" + "p" * LIMIT + "\n")

    def invoke(*args, **kwargs):
        # An invocation can change file size. Coverage must still describe the
        # input the model received, not a later re-read of the working tree.
        target.write_text("temporary_agent_state = 1\n", encoding="utf-8")
        return block("src/unit.py")

    monkeypatch.setattr(implementation.generator_module, "_invoke_ai_command", invoke)
    with pytest.raises(CoddCLIError, match="incomplete"):
        Implementer(root).run_implement(ImplementSpec("docs/design.md", ["src/unit.py"]))
    assert target.read_text() == "temporary_agent_state = 1\n"


@pytest.mark.parametrize(
    "round_trip",
    [copy.copy, copy.deepcopy, lambda output: pickle.loads(pickle.dumps(output))],
    ids=["copy", "deepcopy", "pickle"],
)
def test_capture_round_trip_preserves_per_file_origin(tmp_path, monkeypatch, round_trip):
    original = "value = 1\n"
    changed = put(tmp_path, "src/changed.py", original)
    untouched = put(tmp_path, "src/untouched.py", original)
    new_path = tmp_path / "src/carrier.txt"
    embedded_block = block("src/untouched.py")
    real_run = subprocess.run
    real_run(["git", "init", "-q", str(tmp_path)], check=True)

    def fake_run(command, **kwargs):
        if command[0] == "git":
            return real_run(command, **kwargs)
        assert command == ["test-agent"]
        changed.write_text("value = 2\n", encoding="utf-8")
        new_path.write_text("carrier\n" + embedded_block, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)
    captured = ai_invoke.invoke_file_writing_agent(["test-agent"], "prompt", tmp_path)

    assert changed.read_text() == untouched.read_text() == original
    assert not new_path.exists()
    assert embedded_block in captured
    expected_paths = frozenset({changed.resolve(), new_path.resolve()})
    assert captured.captured_paths == expected_paths

    restored = round_trip(captured)

    assert isinstance(restored, str)
    assert restored == str(captured)
    assert restored.captured_paths == expected_paths


def test_implement_capture_origin_is_per_parsed_destination(tmp_path, monkeypatch):
    root = project(tmp_path, syntax_gate=False)
    original = "#" + "p" * LIMIT + "\n"
    protected = put(root, "src/protected.py", original)
    real_run = subprocess.run
    real_run(["git", "init", "-q", str(root)], check=True)

    def fake_run(command, **kwargs):
        if command[0] == "git":
            return real_run(command, **kwargs)
        # Only carrier.txt was actually written. Its body happens to contain
        # another parsed file block, which must not inherit carrier's origin.
        put(root, "src/carrier.txt", "carrier\n" + block("src/protected.py"))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)
    monkeypatch.setattr(implementation.generator_module, "_invoke_ai_command", lambda *a, **k:
                        ai_invoke.invoke_file_writing_agent(["test-agent"], a[1], root))
    with pytest.raises(CoddCLIError, match="src/protected.py"):
        Implementer(root).run_implement(ImplementSpec("docs/design.md", ["src"], expected_outputs=["src/protected.py"]))
    assert protected.read_text() == original
    assert not (root / "src/carrier.txt").exists()


@pytest.mark.parametrize("size,status", [(12000, "PARTIAL"), (LIMIT, "OMITTED")])
@pytest.mark.parametrize("response_path", ["src/canonical.py", "src/alias.py"])
@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_implement_full_copy_remains_writable_through_alias(tmp_path, monkeypatch, capsys, size, status, response_path, alias_kind):
    root = project(tmp_path)
    canonical = put(root, "src/canonical.py", "#" + "f" * (size - 2) + "\n")
    alias = root / "src/alias.py"
    if alias_kind == "symlink":
        alias.symlink_to(canonical.name)
    else:
        alias.hardlink_to(canonical)
    outputs = ["src/canonical.py", "src/alias.py"]
    prompt = render(root, ["src"], outputs)
    assert "FULL: src/canonical.py" in prompt
    assert f"{status}: src/alias.py" in prompt
    monkeypatch.setattr(implementation.generator_module, "_invoke_ai_command", lambda *a, **k: block(response_path))
    result = Implementer(root).run_implement(ImplementSpec("docs/design.md", ["src"], expected_outputs=outputs))
    assert result.generated_files == [root / response_path]
    assert "value = 2" in canonical.read_text()
    assert capsys.readouterr().err == ""


def test_implement_real_agent_atomic_save_of_symlink_is_accepted(tmp_path, monkeypatch):
    root = project(tmp_path)
    original = "#" + "p" * LIMIT + "\npreserve = 1\n"
    canonical = put(root, "src/canonical.py", original)
    alias = root / "src/alias.py"
    alias.symlink_to("canonical.py")
    replacement = original + "added = 2\n"
    real_run = subprocess.run
    real_run(["git", "init", "-q", str(root)], check=True)

    def fake_run(command, **kwargs):
        if command[0] == "git":
            return real_run(command, **kwargs)
        # Editors often atomically save by replacing the directory entry.
        alias.unlink()
        alias.write_text(replacement, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    def invoke(*args, **kwargs):
        captured = ai_invoke.invoke_file_writing_agent(["test-agent"], args[1], root)
        assert alias.is_symlink()
        assert canonical.read_text() == original
        return captured

    monkeypatch.setattr(ai_invoke.subprocess, "run", fake_run)
    monkeypatch.setattr(implementation.generator_module, "_invoke_ai_command", invoke)
    result = Implementer(root).run_implement(ImplementSpec("docs/design.md", ["src/alias.py"]))
    assert result.generated_files == [alias]
    assert replacement.rstrip() in canonical.read_text()
