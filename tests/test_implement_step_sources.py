"""Approved step citations in the implementation prompt and route selection."""

import json

import pytest

from codd import implementer
from codd.implementer import Implementer, ImplementSpec
from codd.llm.impl_step_deriver import (
    ImplStep,
    ImplStepCacheRecord,
    impl_step_cache_path,
    parse_impl_steps,
    write_impl_step_cache,
)
from codd.llm.plan_deriver import DerivedTask, DerivedTaskCacheRecord, write_derived_task_cache


DESIGN = "docs/design/primary.md"
SOURCE = "docs/design/downstream detail.md"
OUTPUT = "output/unit.txt"
BODY = "# Downstream detail\n\nFirst requirement.\n\n## Rules\n\nLast constraint.\n"
SOURCE_HEADING = "Design documents cited by implementation steps:"
SOURCE_LIMIT = 128 * 1024


def _put(root, path, body):
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(body, encoding="utf-8")


def _step(citation, *, step_id="apply_design", expected=(), approved=True, inferred=False):
    return ImplStep(
        id=step_id, kind="implementation", rationale="Apply the design.",
        source_design_section=citation, expected_outputs=list(expected), approved=approved, inferred=inferred,
    )


class Project:
    def __init__(self, root, monkeypatch):
        self.root = root
        self.calls = []
        self.config = {
            "ai_commands": {"implement": "standard", "implement_one_shot": "one-shot"},
            "scan": {"doc_dirs": ["docs"]},
        }
        self.register_designs(SOURCE)
        _put(root, DESIGN, "# Primary design\n\nBuild a deliverable.\n")
        monkeypatch.setattr(implementer.generator_module, "_invoke_ai_command", self.invoke)

    def register_designs(self, *paths):
        artifacts = self.config.setdefault("wave_config", {}).setdefault(1, [])
        for path in paths:
            artifacts.append({"node_id": f"design:fixture-{len(artifacts)}", "output": path, "title": path})

    def invoke(self, command, prompt, **kwargs):
        self.calls.append((command, prompt))
        return f"=== FILE: {OUTPUT} ===\nComplete.\n"

    def run(self, *steps, dependencies=(), outputs=(OUTPUT,), explicit_command=None):
        spec = ImplementSpec(DESIGN, list(outputs), list(dependencies), list(outputs))
        if steps:
            write_impl_step_cache(
                impl_step_cache_path(spec, {"project_root": self.root}),
                ImplStepCacheRecord("fixture", "key", spec.task_id, "design", "template", "now", [DESIGN], list(steps)),
            )
        result = Implementer(
            self.root, config=self.config, ai_command=explicit_command, use_derived_steps=True, sleep=lambda _: None,
        ).run_implement(spec)
        assert result.generated_files == [self.root / OUTPUT]
        return self.calls[-1]

    def plan_with_producer(self, produced_path=SOURCE):
        tasks = [
            DerivedTask.from_dict({
                "id": task_id, "title": task_id, "description": "Task.",
                "source_design_doc": design, "v_model_layer": "detailed",
                "expected_outputs": [output], "dependencies": dependencies, "approved": True,
            })
            for task_id, design, output, dependencies in (
                ("producer", "docs/design/producer.md", produced_path, []),
                ("consumer", DESIGN, OUTPUT, ["producer"]),
            )
        ]
        write_derived_task_cache(
            self.root / ".codd/derived_tasks/plan.yaml",
            DerivedTaskCacheRecord("fixture", "key", "design", "template", "now", [DESIGN], tasks),
        )


@pytest.fixture
def project(tmp_path, monkeypatch):
    return Project(tmp_path / "project", monkeypatch)


@pytest.mark.parametrize("command", [None, "explicit"], ids=["one-shot", "standard"])
def test_includes_full_downstream_step_sources_in_citation_order(project, command):
    _put(project.root, SOURCE, BODY)
    second = "notes/additional.md"
    project.register_designs(second)
    _put(project.root, second, "# Additional\n\nAn independent constraint.\n")
    steps = [
        _step(f"{SOURCE}#rules"),
        _step(second, step_id="apply_addition"),
        _step(f"./{SOURCE}#downstream-detail", step_id="apply_detail"),
    ]

    route, prompt = project.run(*steps, explicit_command=command)

    assert route == (command or "one-shot")
    assert prompt.count(BODY) == 1
    assert prompt.index("Design document content:") < prompt.index("Implementation steps to follow")
    assert prompt.index("Dependency documents:") < prompt.index(SOURCE_HEADING)
    assert prompt.index(SOURCE_HEADING) < prompt.index(BODY) < prompt.index("An independent constraint.")
    assert f"{SOURCE}#rules" in prompt and "apply_addition" in prompt


@pytest.mark.parametrize("separator", [", ", "、", " / ", "; "], ids=["comma", "japanese-comma", "slash", "semicolon"])
def test_sections_can_cite_multiple_files(project, separator):
    _put(project.root, SOURCE, BODY)
    second = "docs/design/other.md"
    project.register_designs(second)
    second_body = "# Other design\n\nFollow the other constraint.\n"
    _put(project.root, second, second_body)
    citation = f"{SOURCE} §3 First requirement、another heading clause{separator}{second} §2 Details"

    route, prompt = project.run(_step(citation))

    assert route == "one-shot"
    assert prompt.index(SOURCE_HEADING) < prompt.index(BODY) < prompt.index(second_body)


@pytest.mark.parametrize(
    "citation", [
        "{first} §2 Milestone 4／{second} §2 CI-01",
        '「{first}」と“{second}”',
        "[first]({first}#rules) and [second]({second})",
        "{first}（第２節）、{second} §2 Details",
        "Start with {first} and finish with {second}.",
        "参照{first}，別紙{second}；補足",
    ],
    ids=["fullwidth-slash", "quotes", "markdown-links", "section", "final-period", "fullwidth-punctuation"],
)
def test_registered_paths_are_literal_matches_in_citation_order(project, citation):
    first = "docs/first.md"
    second = "docs/second.md"
    project.register_designs(second, first)
    _put(project.root, first, "# First cited document\n")
    _put(project.root, second, "# Second cited document\n")

    route, prompt = project.run(_step(citation.format(first=first, second=second)))

    assert route == "one-shot"
    assert prompt.index("# First cited document") < prompt.index("# Second cited document")


@pytest.mark.parametrize(
    ("shorter", "longer"), [("docs/a.md", "x/docs/a.md"), ("design.md", "system_design.md")],
    ids=["qualified-paths", "basenames"],
)
@pytest.mark.parametrize("also_shorter", [False, True], ids=["long-only", "separate-short-mention"])
def test_prefers_longest_overlapping_citation(project, shorter, longer, also_shorter):
    project.register_designs(shorter, longer)
    _put(project.root, shorter, "# Shorter citation\n")
    _put(project.root, longer, "# Longer citation\n")
    citation = f"Use {longer}" + (f" and {shorter}" if also_shorter else "")

    route, prompt = project.run(_step(citation))

    assert route == "one-shot"
    assert "# Longer citation" in prompt
    assert ("# Shorter citation" in prompt) == also_shorter
    if also_shorter:
        assert prompt.index("# Longer citation") < prompt.index("# Shorter citation")


@pytest.mark.parametrize("source", ["contract.md", "docs/design/contract.md"], ids=["root", "nested"])
@pytest.mark.parametrize(
    "citation", ["Use the guidance in ‘contract.md’ here.", "[仕様](contract.md#rules)"],
    ids=["quoted", "markdown-link"],
)
def test_resolves_standalone_basename_with_one_distinct_registered_path(project, source, citation):
    project.register_designs(source, source)
    _put(project.root, source, BODY)

    route, prompt = project.run(_step(citation))

    assert route == "one-shot"
    assert prompt.count(BODY) == 1
    assert f"--- BEGIN STEP DESIGN SOURCE {source} ---" in prompt


@pytest.mark.parametrize("first", ["contract.md", "docs/contract.md"], ids=["root-and-nested", "two-nested"])
def test_ambiguous_standalone_basename_is_ignored(project, first):
    second = "other/contract.md"
    project.register_designs(first, second)
    _put(project.root, first, BODY)

    route, prompt = project.run(_step("contract.md#rules"))

    assert route == "one-shot"
    assert BODY not in prompt
    assert SOURCE_HEADING not in prompt


@pytest.mark.parametrize("absolute", [False, True], ids=["dot-relative", "absolute"])
def test_explicit_root_path_disambiguates_registered_basename(project, absolute):
    source = "contract.md"
    project.register_designs(source, "docs/design/contract.md")
    _put(project.root, source, BODY)
    citation = (project.root / source).as_posix() if absolute else f"./{source}"

    route, prompt = project.run(_step(f"{citation}#rules"))

    assert route == "one-shot"
    assert prompt.count(BODY) == 1
    assert f"--- BEGIN STEP DESIGN SOURCE {source} ---" in prompt


def test_ambiguous_basename_does_not_revive_shorter_match(project):
    source = "docs/contract.md"
    project.register_designs(source, "docs/service contract.md", "other/service contract.md")
    _put(project.root, source, BODY)

    route, prompt = project.run(_step("See ‘service contract.md’ for the rules."))

    assert route == "one-shot"
    assert BODY not in prompt
    assert SOURCE_HEADING not in prompt


@pytest.mark.parametrize("source", ["contract.md", "docs/design/contract.md"], ids=["root", "nested"])
@pytest.mark.parametrize("path", ["other/contract.md", "other/./contract.md"], ids=["folder", "folder-and-dot"])
def test_basename_at_end_of_unregistered_folder_path_is_ignored(project, source, path):
    project.register_designs(source)
    _put(project.root, source, BODY)

    route, prompt = project.run(_step(f"Use {path} for background."))

    assert route == "one-shot"
    assert BODY not in prompt
    assert SOURCE_HEADING not in prompt


@pytest.mark.parametrize("format_text", ["{path} General requirements", "General requirements in {path} apply here."])
def test_recognizes_file_path_with_free_text_around_it(project, format_text):
    _put(project.root, SOURCE, BODY)

    route, prompt = project.run(_step(format_text.format(path=SOURCE)))

    assert route == "one-shot"
    assert BODY in prompt


@pytest.mark.parametrize("separator", [" ", ":"], ids=["whitespace", "colon"])
def test_recognizes_bare_project_root_document_at_start_of_citation(project, separator):
    source = "contract.md"
    project.register_designs(source)
    _put(project.root, source, BODY)

    route, prompt = project.run(_step(f"{source}{separator}General requirements"))

    assert route == "one-shot"
    assert BODY in prompt


@pytest.mark.parametrize("exists", [True, False], ids=["readable", "missing"])
def test_registered_design_path_can_use_another_extension(project, capsys, exists):
    source = "docs/detailed records/contract.txt"
    project.register_designs(source)
    if exists:
        _put(project.root, source, BODY)

    route, prompt = project.run(_step(f"Cite ./{source} §2 Details"))

    assert route == ("one-shot" if exists else "standard")
    assert (BODY in prompt) == exists
    if not exists:
        assert "reason=incomplete_dependency_artifact " in capsys.readouterr().err


@pytest.mark.parametrize("location", [
    "primary", "dependency", "declared-output", "step-output", "producer-output", "coding-principles",
])
def test_leaves_one_full_copy_when_source_is_already_in_prompt(project, location):
    source = DESIGN if location == "primary" else SOURCE
    _put(project.root, source, BODY)
    dependencies = [source] if location == "dependency" else []
    outputs = (OUTPUT, source) if location == "declared-output" else (OUTPUT,)
    expected = [source] if location == "step-output" else []
    if location == "producer-output":
        project.plan_with_producer()
    if location == "coding-principles":
        project.config["coding_principles"] = source

    route, prompt = project.run(_step(f"./{source}#rules", expected=expected), dependencies=dependencies, outputs=outputs)

    assert route == "one-shot"
    assert prompt.count(BODY) == 1
    assert SOURCE_HEADING not in prompt


@pytest.mark.parametrize("excerpt", [False, True], ids=["full", "excerpt"])
def test_screen_flow_citation_is_added_only_if_its_full_body_is_missing(project, excerpt):
    screen_flow = "docs/extracted/screen-flow.md"
    project.register_designs(screen_flow)
    _put(project.root, DESIGN, "# Primary design\n\nDescribe the UI.\n")
    _put(project.root, "DESIGN.md", "---\ncolors:\n  primary: '#1a2b3c'\n---\n")
    body = BODY
    if excerpt:
        body += "x" * implementer.SCREEN_FLOW_PROMPT_LIMIT + "\nFinal constraint.\n"
    _put(project.root, screen_flow, body)

    route, prompt = project.run(_step(screen_flow))

    assert route == "one-shot"
    assert prompt.count(body) == 1
    assert "--- SCREEN-FLOW (UI ROUTE DEFINITIONS) ---" in prompt
    assert (SOURCE_HEADING in prompt) == excerpt


def test_adds_source_even_if_an_existing_output_excerpt_is_present(project, capsys):
    _put(project.root, SOURCE, BODY)
    project.config["implement"] = {"existing_output_files_prompt_limit": 28}

    route, prompt = project.run(_step(SOURCE), outputs=(OUTPUT, SOURCE))

    assert route == "standard"
    assert "reason=incomplete_existing_output " in capsys.readouterr().err
    assert "[PARTIAL EXISTING FILE:" in prompt
    assert prompt.count(BODY) == 1


@pytest.mark.parametrize("failure", [
    "missing", "unreadable", "over-budget", "registered-outside-project", "escaping-symlink",
])
def test_incomplete_step_source_disallows_complete_context(project, capsys, failure):
    citation = SOURCE
    if failure == "unreadable":
        _put(project.root, SOURCE, "")
        (project.root / SOURCE).write_bytes(b"\xff")
    if failure == "over-budget":
        _put(project.root, SOURCE, BODY + "x" * SOURCE_LIMIT + "\n")
    if failure in {"registered-outside-project", "escaping-symlink"}:
        outside = project.root.parent / "external.md"
        outside.write_text(BODY, encoding="utf-8")
        if failure == "registered-outside-project":
            citation = str(outside)
            project.register_designs(citation)
        else:
            (project.root / SOURCE).symlink_to(outside)

    route, prompt = project.run(_step(f"{citation}#rules"))

    assert route == "standard"
    assert "reason=incomplete_dependency_artifact " in capsys.readouterr().err
    assert BODY not in prompt


def test_unregistered_absolute_path_does_not_make_context_incomplete(project):
    outside = project.root.parent / "external.md"
    outside.write_text(BODY, encoding="utf-8")

    route, prompt = project.run(_step(f"{outside}#rules"))

    assert route == "one-shot"
    assert BODY not in prompt
    assert SOURCE_HEADING not in prompt


@pytest.mark.parametrize("larger_output", [False, True], ids=["source-only", "existing-output-priority"])
def test_switches_to_standard_when_a_source_disappears_during_retry(project, monkeypatch, capsys, larger_output):
    _put(project.root, SOURCE, BODY)
    existing = "output/existing.txt"
    _put(project.root, existing, "Prior content.\n")
    project.config["implement"] = {"no_usable_file_retries": 1, "existing_output_files_prompt_limit": 32}

    def invoke(command, prompt, **kwargs):
        response = project.invoke(command, prompt, **kwargs)
        if len(project.calls) == 1:
            (project.root / SOURCE).unlink()
            if larger_output:
                _put(project.root, existing, "New content.\n" * 5)
            return "No usable file."
        return response

    monkeypatch.setattr(implementer.generator_module, "_invoke_ai_command", invoke)
    route, prompt = project.run(_step(SOURCE), outputs=(OUTPUT, existing))

    assert [command for command, _prompt in project.calls] == ["one-shot", "standard"]
    assert BODY in project.calls[0][1]
    assert route == "standard" and BODY not in prompt
    expected_reason = "incomplete_existing_output" if larger_output else "incomplete_dependency_artifact"
    assert f"reason={expected_reason} fallback=true" in capsys.readouterr().err


def test_source_budget_is_independent_of_producer_artifacts(project):
    producer = "output/producer.txt"
    artifact_limit = implementer.DEPENDENCY_ARTIFACT_FILES_PROMPT_LIMIT
    _put(project.root, producer, "p" * artifact_limit)
    full_source = BODY + "x" * artifact_limit + "\nFinal constraint of the cited design.\n"
    _put(project.root, SOURCE, full_source)
    project.plan_with_producer(producer)

    route, prompt = project.run(_step(SOURCE))

    assert route == "one-shot"
    assert f"--- BEGIN DEPENDENCY ARTIFACT {producer} ---\n" + "p" * artifact_limit in prompt
    assert full_source in prompt


def test_total_source_budget_retains_earlier_complete_sources(project, capsys):
    second = "docs/design/second.md"
    project.register_designs(second)
    first_body = BODY + "x" * (SOURCE_LIMIT - len(BODY) - 2) + "\n"
    _put(project.root, SOURCE, first_body)
    _put(project.root, second, "# Source that cannot fit in full\n")

    route, prompt = project.run(_step(f"{SOURCE}, {second}"))

    assert route == "standard"
    assert "reason=incomplete_dependency_artifact " in capsys.readouterr().err
    assert first_body in prompt
    assert "# Source that cannot fit in full" not in prompt


@pytest.mark.parametrize("citation", [
    "Overview", "2. Input/output behavior", "best_practice_augmenter", "#rules", "design:primary",
    'Overview and the label "Reference.md"',
    "docs/other/unknown.txt §2 Details",
    "https://example.invalid/docs/remote.md#part",
    "See https://example.invalid/?source=docs/remote.md for background.",
    "See [background](https://example.invalid/docs/remote.md) for context.",
    "See https://example.invalid/(docs/remote.md) for context.",
])
def test_keeps_non_path_step_sources_as_labels(project, citation):
    route, prompt = project.run(_step(citation))

    assert route == "one-shot"
    assert citation in " ".join(prompt.split())
    assert SOURCE_HEADING not in prompt


@pytest.mark.parametrize("exists", [True, False], ids=["readable", "missing"])
def test_ignores_unregistered_path_in_explanatory_text(project, exists):
    source = "notes/readme.md"
    if exists:
        _put(project.root, source, BODY)

    route, prompt = project.run(_step(f"See {source} for context."))

    assert route == "one-shot"
    assert BODY not in prompt
    assert SOURCE_HEADING not in prompt


def test_default_source_keeps_primary_document_as_a_single_copy(project):
    _put(project.root, DESIGN, BODY)
    payload = _step(DESIGN).to_dict()
    del payload["source_design_section"]
    [step] = parse_impl_steps(
        json.dumps({"steps": [payload]}), provider_id="fixture", generated_at="now", default_source_design_section=DESIGN,
    )

    route, prompt = project.run(step)

    assert route == "one-shot"
    assert prompt.count(BODY) == 1
    assert SOURCE_HEADING not in prompt


@pytest.mark.parametrize(
    "steps", [[], [_step("")], [_step("docs/design/missing.md#rules", approved=False)]],
    ids=["no-citation", "empty", "unapproved"],
)
def test_does_not_use_citations_from_steps_absent_from_prompt(project, steps):
    project.register_designs("docs/design/missing.md")
    route, prompt = project.run(*steps)

    assert route == "one-shot"
    assert "Implementation steps to follow" not in prompt
    assert SOURCE_HEADING not in prompt


def test_uses_only_citations_from_rendered_steps_when_approvals_are_mixed(project):
    _put(project.root, SOURCE, BODY)
    missing = "docs/design/pending.md"
    project.register_designs(missing)
    steps = [
        _step(DESIGN, step_id="apply_primary"),
        _step(SOURCE, step_id="apply_approved_inference", inferred=True),
        _step(missing, step_id="exclude_pending_inference", inferred=True, approved=False),
    ]

    route, prompt = project.run(*steps)

    assert route == "one-shot"
    assert "apply_primary" in prompt and "apply_approved_inference" in prompt
    assert BODY in prompt
    assert "exclude_pending_inference" not in prompt and missing not in prompt
