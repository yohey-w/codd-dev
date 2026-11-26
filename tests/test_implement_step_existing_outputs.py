"""Full existing-file context from approved implementation steps."""

import pytest

from codd import implementer
from codd.greenfield import test_rederivation
from codd.implementer import Implementer, ImplementSpec
from codd.llm.impl_step_deriver import (
    ImplStep,
    ImplStepCacheRecord,
    impl_step_cache_path,
    write_impl_step_cache,
)
from codd.llm.plan_deriver import DerivedTask, DerivedTaskCacheRecord, write_derived_task_cache


DESIGN = "docs/design.md"
OTHER_DESIGN = "docs/other-design.md"
OUTPUT = "artifacts/current.txt"
NESTED = "artifacts/parts/deep/member.txt"
OTHER = "artifacts/parts-other/unrelated.txt"
ORIGINAL = {
    OUTPUT: "Keep the first line.\n\n  Keep the last line.\n",
    NESTED: "Keep the nested first line.\n\n  Keep the nested last line.\n",
    OTHER: "Keep the unrelated first line.\n\n  Keep the unrelated last line.\n",
}


def _write(root, path, body):
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(body, encoding="utf-8")


def _task(task_id, outputs, *, design=DESIGN, approved=True):
    return DerivedTask(
        task_id, task_id, "Revise the assigned files.", design, "detailed",
        expected_outputs=list(outputs), approved=approved,
    )


def _run(
    tmp_path, monkeypatch, *, declarations, output_paths=("artifacts",), standard_output=OUTPUT,
    files=ORIGINAL, steps=None, tasks=None, include_current=True, one_shot_response=None,
    output_budget=None, scan_roots=("artifacts",),
):
    _write(tmp_path, DESIGN, "# Design\n\nRevise the declared output.\n")
    for path, body in files.items():
        _write(tmp_path, path, body)
    spec = ImplementSpec(DESIGN, list(output_paths), expected_outputs=list(declarations))
    step = ImplStep(
        id="revise_outputs", kind="implementation", rationale="Revise existing outputs.",
        source_design_section="Revise the declared output.",
        expected_outputs=list(files if steps is None else steps), approved=True, inferred=False,
    )
    write_impl_step_cache(
        impl_step_cache_path(spec, {"project_root": tmp_path}),
        ImplStepCacheRecord("fixture", "key", spec.task_id, "design", "template", "now", [DESIGN], [step]),
    )
    if tasks is not None:
        _write(tmp_path, ".codd/codd.yaml", "{}\n")
        plan = list(tasks)
        if include_current:
            plan.append(_task("current", declarations))
        write_derived_task_cache(
            tmp_path / ".codd/derived_tasks/plan.yaml",
            DerivedTaskCacheRecord("fixture", "key", "design", "template", "now", [DESIGN], plan),
        )

    calls = []

    def invoke(command, prompt, **kwargs):
        calls.append((command, prompt))
        if command == "one-shot":
            return one_shot_response
        assert command == "standard"
        return f"=== FILE: {standard_output} ===\nUpdated text.\n"

    monkeypatch.setattr(implementer.generator_module, "_invoke_ai_command", invoke)
    config = {"ai_commands": {"implement": "standard"}, "scan": {"source_dirs": list(scan_roots)}}
    if output_budget is not None:
        config["implement"] = {"existing_output_files_prompt_limit": output_budget}
    if one_shot_response is not None:
        config["ai_commands"]["implement_one_shot"] = "one-shot"
    result = Implementer(tmp_path, config=config, use_derived_steps=True, sleep=lambda _: None).run_implement(spec)
    return calls, result


def _existing_step_block(path, body=None):
    text = (ORIGINAL[path] if body is None else body).rstrip()
    return f"=== EXISTING FILE: {path} ===\n{text}\n=== END EXISTING FILE ==="


def test_removes_only_approved_sibling_outputs_from_shared_design_steps(tmp_path, monkeypatch):
    unresolved = "artifacts/notes.txt"
    header = f"@generated-from: {DESIGN} (design:primary)\n"
    files = {path: header + body for path, body in ORIGINAL.items()}
    files[unresolved] = "These notes have no approved owner.\n"
    calls, _ = _run(
        tmp_path, monkeypatch, declarations=[OUTPUT, "artifacts/parts/"],
        output_paths=(OUTPUT, "artifacts/parts"), files=files,
        tasks=[_task("sibling", [OTHER]), _task("unapproved", [unresolved], approved=False)],
    )

    [(_, prompt)] = calls
    assert _existing_step_block(OUTPUT, files[OUTPUT]) in prompt
    assert _existing_step_block(NESTED, files[NESTED]) in prompt
    assert _existing_step_block(unresolved, files[unresolved]) in prompt
    assert f"- FULL: {OUTPUT} (see {OUTPUT} in Implementation steps above)" in prompt
    assert prompt.count(ORIGINAL[OUTPUT]) == 1
    assert ORIGINAL[OTHER] not in prompt
    assert OTHER in prompt  # The approved step itself is still rendered.


def test_one_shot_cannot_replace_sibling_output_under_a_shared_write_directory(tmp_path, monkeypatch, capsys):
    extra = "artifacts/new.txt"
    response = f"=== FILE: {extra} ===\nNew text.\n=== FILE: {OTHER} ===\nReplacement text.\n"
    calls, result = _run(
        tmp_path, monkeypatch, declarations=[OUTPUT], tasks=[_task("sibling", [OTHER])],
        one_shot_response=response,
    )

    assert [command for command, _ in calls] == ["one-shot", "standard"]
    assert all(ORIGINAL[OTHER].rstrip() not in prompt for _, prompt in calls)
    assert "route=standard reason=unseen_existing_output fallback=true" in capsys.readouterr().err
    assert result.generated_files == [tmp_path / OUTPUT]
    assert (tmp_path / OUTPUT).read_text(encoding="utf-8") == "Updated text.\n"
    assert (tmp_path / OTHER).read_text(encoding="utf-8") == ORIGINAL[OTHER]
    assert not (tmp_path / extra).exists()


def test_keeps_an_output_with_tied_ownership_even_when_sibling_is_listed_first(tmp_path, monkeypatch):
    calls, _ = _run(
        tmp_path, monkeypatch, declarations=[OUTPUT, OTHER],
        tasks=[_task("sibling", [OTHER])],
    )

    [(_, prompt)] = calls
    assert _existing_step_block(OTHER) in prompt


def test_excludes_sibling_output_with_matching_provenance(tmp_path, monkeypatch):
    header = f"@generated-from: {OTHER_DESIGN} (design:other)\n"
    files = {OUTPUT: ORIGINAL[OUTPUT], OTHER: header + ORIGINAL[OTHER]}
    calls, _ = _run(
        tmp_path, monkeypatch, declarations=[OUTPUT], tasks=[_task("sibling", [OTHER], design=OTHER_DESIGN)],
        files=files,
    )

    [(_, prompt)] = calls
    assert _existing_step_block(OUTPUT) in prompt
    assert ORIGINAL[OTHER].rstrip() not in prompt


def test_keeps_unowned_outputs_inside_and_outside_the_write_directory(tmp_path, monkeypatch):
    calls, _ = _run(
        tmp_path, monkeypatch, declarations=[OUTPUT], output_paths=(OUTPUT, "artifacts/parts"),
        tasks=[_task("sibling", ["artifacts/another.txt"])],
    )

    [(_, prompt)] = calls
    assert _existing_step_block(NESTED) in prompt
    assert _existing_step_block(OTHER) in prompt


@pytest.mark.parametrize("declarations", [
    [], ["artifact:current"], ["Record.__internal__"], ["artifacts/*.txt"],
    ["Revise the current output"], ["Revise the text in artifacts/current.txt"],
    [OUTPUT], ["artifacts/future/"], ["Place the new record in artifacts/"],
])
def test_keeps_all_step_bodies_without_an_approved_plan(tmp_path, monkeypatch, declarations):
    calls, _ = _run(tmp_path, monkeypatch, declarations=declarations, output_paths=(OUTPUT,))

    [(_, prompt)] = calls
    assert all(_existing_step_block(path) in prompt for path in ORIGINAL)


@pytest.mark.parametrize("ambiguous", [False, True], ids=["unmatched", "ambiguous"])
def test_keeps_all_step_bodies_when_current_task_cannot_be_identified(tmp_path, monkeypatch, ambiguous):
    tasks = [_task("sibling", [OTHER])]
    if ambiguous:
        tasks.extend([_task("first", [OUTPUT]), _task("second", [OUTPUT])])
    calls, _ = _run(
        tmp_path, monkeypatch, declarations=[OUTPUT], output_paths=(OUTPUT,), tasks=tasks, include_current=False,
    )

    [(_, prompt)] = calls
    assert all(_existing_step_block(path) in prompt for path in ORIGINAL)


def test_keeps_all_step_bodies_when_ownership_cannot_be_checked(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("ownership unavailable")

    monkeypatch.setattr(test_rederivation, "owning_task_for_path", unavailable)
    calls, _ = _run(
        tmp_path, monkeypatch, declarations=[OUTPUT], output_paths=(OUTPUT,), tasks=[_task("sibling", [OTHER])],
    )

    [(_, prompt)] = calls
    assert all(_existing_step_block(path) in prompt for path in ORIGINAL)


def test_keeps_unreadable_step_output_routing(tmp_path, monkeypatch, capsys):
    unreadable = tmp_path / OTHER
    unreadable.parent.mkdir(parents=True)
    unreadable.write_bytes(b"\xff\xfe")
    calls, _ = _run(
        tmp_path, monkeypatch, declarations=[OUTPUT], output_paths=(OUTPUT,), files={OUTPUT: ORIGINAL[OUTPUT]},
        steps=[OUTPUT, OTHER], tasks=[_task("sibling", [OTHER])],
        one_shot_response=f"=== FILE: {OUTPUT} ===\nUpdated text.\n",
    )

    [(command, prompt)] = calls
    assert command == "standard"
    assert "reason=incomplete_existing_output " in capsys.readouterr().err
    assert f"=== EXISTING FILE: {OTHER} ===" not in prompt


@pytest.mark.parametrize("step_uses_alias", [False, True], ids=["resolved-step", "alias-step"])
def test_one_shot_cannot_replace_excluded_sibling_output_through_an_in_scope_alias(
    tmp_path, monkeypatch, capsys, step_uses_alias,
):
    alias = "artifacts/writable/alias.txt"
    extra = "artifacts/writable/new.txt"
    (tmp_path / alias).parent.mkdir(parents=True)
    (tmp_path / alias).symlink_to(tmp_path / OTHER)
    response = f"=== FILE: {extra} ===\nNew text.\n=== FILE: {alias} ===\nReplacement text.\n"

    calls, result = _run(
        tmp_path, monkeypatch, declarations=[OUTPUT], output_paths=(OUTPUT, "artifacts/writable"),
        steps=[OUTPUT, alias if step_uses_alias else OTHER], tasks=[_task("sibling", [OTHER])],
        one_shot_response=response,
    )

    assert [command for command, _ in calls] == ["one-shot", "standard"]
    assert ORIGINAL[OTHER] not in calls[0][1]
    assert "route=standard reason=unseen_existing_output fallback=true" in capsys.readouterr().err
    assert result.generated_files == [tmp_path / OUTPUT]
    assert (tmp_path / OTHER).read_text(encoding="utf-8") == ORIGINAL[OTHER]
    assert not (tmp_path / extra).exists()


def test_shows_full_explicit_write_path_even_over_output_budget(tmp_path, monkeypatch, capsys):
    new = "artifacts/new.txt"
    calls, result = _run(
        tmp_path, monkeypatch, declarations=[new], output_paths=(new, OUTPUT), output_budget=8,
        standard_output=OUTPUT, tasks=[_task("sibling", [OTHER])],
        one_shot_response=f"=== FILE: {OUTPUT} ===\nUpdated text.\n",
    )

    [(command, prompt)] = calls
    assert command == "standard"
    assert "reason=existing_output_over_budget " in capsys.readouterr().err
    assert _existing_step_block(OUTPUT) in prompt
    assert result.generated_files == [tmp_path / OUTPUT]
    assert (tmp_path / OUTPUT).read_text(encoding="utf-8") == "Updated text.\n"


def test_shows_unowned_root_artifact_before_writer_reroots_a_globbed_payload(tmp_path, monkeypatch):
    root_output = "README.txt"
    body = "Keep these root file lines.\n\n  Keep this ending too.\n"
    calls, result = _run(
        tmp_path, monkeypatch, declarations=["artifacts/*.txt"],
        files={root_output: body, OTHER: ORIGINAL[OTHER]},
        standard_output=f"artifacts/{root_output}", tasks=[_task("sibling", [OTHER], design=OTHER_DESIGN)],
    )

    [(command, prompt)] = calls
    assert command == "standard"
    assert _existing_step_block(root_output, body) in prompt
    assert result.generated_files == [tmp_path / root_output]
    assert (tmp_path / root_output).read_text(encoding="utf-8") == "Updated text.\n"


def test_one_shot_cannot_replace_sibling_root_artifact_through_rerooting(tmp_path, monkeypatch, capsys):
    root_output = "README.txt"
    body = "Keep these root file lines.\n\n  Keep this ending too.\n"
    calls, result = _run(
        tmp_path, monkeypatch, declarations=[OUTPUT], files={OUTPUT: ORIGINAL[OUTPUT], root_output: body},
        tasks=[_task("sibling", [root_output], design=OTHER_DESIGN)],
        one_shot_response=f"=== FILE: artifacts/{root_output} ===\nReplacement text.\n",
    )

    assert [command for command, _ in calls] == ["one-shot", "standard"]
    assert all(body.rstrip() not in prompt for _, prompt in calls)
    assert "route=standard reason=unseen_existing_output fallback=true" in capsys.readouterr().err
    assert result.generated_files == [tmp_path / OUTPUT]
    assert (tmp_path / OUTPUT).read_text(encoding="utf-8") == "Updated text.\n"
    assert (tmp_path / root_output).read_text(encoding="utf-8") == body
    assert not (tmp_path / "artifacts" / root_output).exists()


@pytest.mark.parametrize(("declaration", "target"), [
    ("artifacts/**/*.txt", OUTPUT), ("artifacts/[slot]/*.txt", "artifacts/[slot]/entry.txt"),
], ids=["recursive-glob", "literal-brackets"])
def test_keeps_full_body_when_existing_glob_matchers_disagree(tmp_path, monkeypatch, declaration, target):
    body = "Keep this matched file.\n\n  Keep its ending.\n"
    calls, result = _run(
        tmp_path, monkeypatch, declarations=[declaration], files={target: body, OTHER: ORIGINAL[OTHER]},
        standard_output=target, tasks=[_task("sibling", [OTHER], design=OTHER_DESIGN)],
    )

    [(command, prompt)] = calls
    assert command == "standard"
    assert _existing_step_block(target, body) in prompt
    assert result.generated_files == [tmp_path / target]
    assert (tmp_path / target).read_text(encoding="utf-8") == "Updated text.\n"


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_keeps_unowned_link_target_before_writer_updates_it_through_alias(tmp_path, monkeypatch, kind):
    alias = "artifacts/parts/entry.txt"
    target = "artifacts/shared.txt"
    body = "Keep this existing shared body.\n"
    _write(tmp_path, target, body)
    destination = tmp_path / alias
    destination.parent.mkdir(parents=True)
    if kind == "symlink":
        destination.symlink_to(tmp_path / target)
        declaration = "artifacts/parts/"
    else:
        destination.hardlink_to(tmp_path / target)
        declaration = "artifacts/parts/*.txt"

    calls, result = _run(
        tmp_path, monkeypatch, declarations=[declaration], output_paths=("artifacts/parts",),
        files={target: body, OTHER: ORIGINAL[OTHER]}, standard_output=alias, tasks=[_task("sibling", [OTHER])],
    )

    [(command, prompt)] = calls
    assert command == "standard"
    assert _existing_step_block(target, body) in prompt
    assert result.generated_files == [tmp_path / alias]
    assert (tmp_path / target).read_text(encoding="utf-8") == "Updated text.\n"


def test_keeps_unowned_files_with_prose_declaration_and_dot_relative_scan_root(tmp_path, monkeypatch):
    calls, result = _run(
        tmp_path, monkeypatch, declarations=["artifacts/ should contain the revised record/"],
        scan_roots=["./artifacts"], tasks=[_task("sibling", ["artifacts/another.txt"])],
    )

    [(command, prompt)] = calls
    assert command == "standard"
    assert all(_existing_step_block(path) in prompt for path in ORIGINAL)
    assert result.generated_files == [tmp_path / OUTPUT]
    assert (tmp_path / OUTPUT).read_text(encoding="utf-8") == "Updated text.\n"
