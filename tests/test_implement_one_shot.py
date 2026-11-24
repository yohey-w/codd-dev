"""Per-task command selection for the shared implement generation path."""

import hashlib
import json
import shlex
import sys
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from codd import implementer
from codd.ai_usage import summarize_usage
from codd.cli import CoddCLIError, main
from codd.greenfield.pipeline import GreenfieldPipeline, ImplementTaskRef
from codd.implementer import Implementer, ImplementSpec
from codd.llm.impl_step_deriver import impl_step_cache_path, read_impl_step_cache
from codd.llm.plan_deriver import DerivedTask, DerivedTaskCacheRecord, write_derived_task_cache


DESIGN = "docs/design.md"
OUTPUT = "src/unit.py"
SUCCESS = f"=== FILE: {OUTPUT} ===\nvalue = 2\n"


class Project:
    def __init__(self, root):
        self.root = root
        agent = self.put("agent.stub", """
import json
import sys
from pathlib import Path

role = sys.argv[1]
calls = Path('calls.jsonl')
prior = [json.loads(line)[0] for line in calls.read_text().splitlines()] if calls.exists() else []
with calls.open('a') as stream:
    stream.write(json.dumps([role, sys.stdin.read()]) + '\\n')
response = json.loads(Path('responses.json').read_text())[role][prior.count(role)]
if isinstance(response, int):
    sys.exit(response)
if isinstance(response, dict):
    print(response['stderr'], file=sys.stderr)
    sys.exit(2)
print(response, end='')
""")
        self.command = shlex.join([sys.executable, str(agent)])
        self.config = {
            "project": {"name": "fixture", "language": "python", "frameworks": []},
            "ai_command": f"{self.command} default",
            "ai_commands": {"implement": f"{self.command} normal"},
            "ai": {"usage_log": "usage.jsonl"},
            "implement": {
                "default_output_paths": {DESIGN: [OUTPUT]},
                "existing_output_files_prompt_limit": 32,
                "no_usable_file_retries": 0,
            },
        }
        self.put(DESIGN, "---\ncodd:\n  node_id: design:fixture\n  type: design\n---\nBuild the unit.\n")

    def put(self, path, content):
        destination = self.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        return destination

    def configure(self, *, one_shot=True, **responses):
        if one_shot:
            self.config["ai_commands"]["implement_one_shot"] = f"{self.command} one-shot"
        self.put("codd/codd.yaml", yaml.safe_dump(self.config))
        self.put("responses.json", json.dumps(responses))

    def plan(self, *entries):
        tasks = [
            DerivedTask.from_dict({"id": task_id, "title": task_id, "description": "Task.",
                                   "source_design_doc": design, "v_model_layer": "detailed",
                                   "expected_outputs": outputs, "dependencies": dependencies,
                                   "approved": True})
            for task_id, design, outputs, dependencies in entries
        ]
        write_derived_task_cache(
            self.root / ".codd/derived_tasks/plan.yaml",
            DerivedTaskCacheRecord("stub", "key", "sha", "tmpl", "now", [DESIGN], tasks),
        )

    def run(self, *, output=OUTPUT, expected=(), feedback=None, derive=False):
        spec = ImplementSpec(DESIGN, [output], expected_outputs=list(expected))
        return Implementer(self.root, use_derived_steps=derive, sleep=lambda _: None).run_implement(
            spec, feedback=feedback,
        )

    def records(self, filename):
        path = self.root / filename
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    @property
    def calls(self):
        return self.records("calls.jsonl")

    @property
    def routing_events(self):
        return [row for row in self.records("usage.jsonl") if row.get("event") == "implement_routing"]

    @property
    def routing(self):
        return [
            (row["route"], row["reason"], row["fallback"])
            for row in self.routing_events
        ]


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv("CODD_AI_USAGE_LOG", raising=False)
    return Project(tmp_path)


def test_unset_command_matches_pre_routing_prompts_and_writes(project, capsys):
    project.config["implement"]["no_usable_file_retries"] = 1
    project.configure(one_shot=False, normal=["no files", SUCCESS])
    result = project.run(feedback="Repair the unit.")
    assert [role for role, _ in project.calls] == ["normal", "normal"]
    # Captured by running this fixture against the unmodified implementation.
    assert [hashlib.sha256(prompt.encode()).hexdigest() for _, prompt in project.calls] == [
        "38d3c2aa87ce465190c4f99276d1fcb595dc95b5b6ace35bc1067bfc87cd5944",
        "b1ed64c24e0f6d75c191c2cb1085f2f2acddb39084fb33c307de55af95a358e3",
    ]
    assert result.generated_files == [project.root / OUTPUT]
    assert hashlib.sha256(result.generated_files[0].read_bytes()).hexdigest() == (
        "1dea35e88ed46fdf4846464e854f88b43642d3b8b30143fb743ae73401405525"
    )
    assert project.routing == []
    assert capsys.readouterr().err == (
        "[codd] implement produced no usable files (no-usable retry 1/1): "
        "AI command returned output with no '=== FILE: ... ===' marker and no complete code fence to fall back to "
        "(8 unstructured chars); treating the response as unparseable rather than guessing it is one fil\n"
    )


@pytest.mark.parametrize("explicit", [False, True], ids=["eligible", "override"])
def test_implement_run_chooses_command(project, explicit):
    project.configure(normal=[SUCCESS], **{"one-shot": [SUCCESS], "override": [SUCCESS]})
    args = ["implement", "run", "--task", DESIGN, "--path", str(project.root),
            "--use-derived-steps", "false", "--no-coverage-gate"]
    role = "override" if explicit else "one-shot"
    if explicit:
        args.extend(["--ai-cmd", f"{project.command} override"])
    result = CliRunner().invoke(main, args, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert [called for called, _ in project.calls] == [role]
    routing = ("standard", "explicit_override", False) if explicit else ("one_shot", "complete_context", False)
    assert project.routing == [routing]
    assert f"implement route={routing[0]} reason={routing[1]}" in result.stderr


@pytest.mark.parametrize("size", [16, 80], ids=["full", "partial"])
def test_existing_output_uses_prompt_coverage(project, size, capsys):
    project.put(OUTPUT, "#" + "x" * size + "\n")
    project.configure(normal=[SUCCESS], **{"one-shot": [SUCCESS]})
    if size > 32:
        with pytest.raises(CoddCLIError, match="PARTIAL"):
            project.run()
        assert [role for role, _ in project.calls] == ["normal"]
        assert project.routing == [("standard", "incomplete_existing_output", False)]
        assert "reason=incomplete_existing_output" in capsys.readouterr().err
        assert (project.root / OUTPUT).read_text() == "#" + "x" * size + "\n"
    else:
        project.run(feedback="Keep the context visible in the repair.")
        assert [role for role, _ in project.calls] == ["one-shot"]
        assert "Keep the context visible in the repair." in project.calls[0][1]


@pytest.mark.parametrize("expected, role", [((), "normal"), ((OUTPUT,), "one-shot")])
def test_directory_scope_requires_concrete_deliverable(project, expected, role):
    project.configure(normal=[SUCCESS], **{"one-shot": [SUCCESS]})
    project.run(output="src", expected=expected)
    assert [called for called, _ in project.calls] == [role]
    if not expected:
        assert project.routing == [("standard", "non_concrete_output", False)]


@pytest.mark.parametrize("producer_body, producer_in_plan, role", [
    ("value = 1\n", True, "one-shot"), ("#" + "x" * 20_001, True, "normal"),
    (b"\xff", True, "normal"), (None, True, "normal"), (None, False, "normal"),
], ids=["full", "degraded", "unreadable", "missing", "unresolved-task"])
def test_dependency_artifact_coverage_routes_task(project, producer_body, producer_in_plan, role):
    producer = "src/producer.py"
    if isinstance(producer_body, bytes):
        project.put(producer, "").write_bytes(producer_body)
    elif producer_body is not None:
        project.put(producer, producer_body)
    task_entries = [("consume", DESIGN, [OUTPUT], ["produce"])]
    if producer_in_plan:
        task_entries.insert(0, ("produce", "docs/producer.md", [producer], []))
    project.plan(*task_entries)
    project.configure(normal=[SUCCESS], **{"one-shot": [SUCCESS]})
    project.run(expected=[OUTPUT])
    assert [called for called, _ in project.calls] == [role]
    if role == "normal":
        assert project.routing == [("standard", "incomplete_dependency_artifact", False)]


def test_new_source_and_test_route_with_full_near_and_incomplete_distant_artifacts(project, capsys):
    output_test = "tests/test_unit.py"
    shared_design = "docs/shared.md"
    project.put(shared_design, "---\ncodd:\n  node_id: design:shared\n  type: design\n---\nShared design.\n")
    near_a, near_b = "src/near_a.py", "src/near_b.py"
    far_full, far, future = "src/far_full.py", "src/far.ts", "src/future.py"
    project.put(near_a, "value_a = 1\n")
    project.put(near_b, "value_b = 2\n")
    project.put(far_full, "value_remote = 3\n")
    project.put(far, "export const remote = 3;\n// " + "x" * implementer.DEPENDENCY_ARTIFACT_FILES_PROMPT_LIMIT)
    project.plan(
        ("far_full", "docs/far_full.md", [far_full], []),
        ("far", "docs/far.md", [far], []),
        ("near_a", "docs/near_a.md", [near_a], ["far_full", "far"]),
        ("near_b", "docs/near_b.md", [near_b], []),
        ("later", shared_design, [future, OUTPUT], []),
        ("consume", DESIGN, [OUTPUT, output_test], ["near_a", "near_b"]),
    )
    response = SUCCESS + f"=== FILE: {output_test} ===\ndef test_unit():\n    assert True\n"
    project.configure(normal=[response], **{"one-shot": [response]})
    spec = ImplementSpec(DESIGN, [OUTPUT, output_test], [shared_design], [OUTPUT, output_test])
    assert not any((project.root / path).exists() for path in (OUTPUT, output_test, future))
    result = Implementer(project.root, use_derived_steps=False).run_implement(spec)
    [(role, prompt)] = project.calls
    assert f"--- BEGIN DEPENDENCY ARTIFACT {near_a} ---\nvalue_a = 1\n" in prompt
    assert f"--- BEGIN DEPENDENCY ARTIFACT {near_b} ---\nvalue_b = 2\n" in prompt
    assert f"--- BEGIN DEPENDENCY ARTIFACT {far_full} ---\nvalue_remote = 3\n" in prompt
    assert f"DEPENDENCY ARTIFACT {far} (public surface only" in prompt
    assert "Exported symbols: remote" in prompt
    assert f"BEGIN DEPENDENCY ARTIFACT {far}" not in prompt
    assert set(result.generated_files) == {project.root / OUTPUT, project.root / output_test}
    assert role == "one-shot"
    assert project.routing == [("one_shot", "complete_context", False)]
    [event] = project.routing_events
    assert event.keys() == {
        "timestamp", "stage", "event", "route", "reason", "detail", "fallback", "design_node", "outputs",
        "incomplete_distant_artifacts",
    }
    assert event["outputs"] == [OUTPUT, output_test]
    assert event["incomplete_distant_artifacts"] == 2
    assert "incomplete_distant_artifacts=2" in capsys.readouterr().err


def test_declared_output_in_immediate_producer_is_not_a_missing_dependency(project):
    producer = "src/producer.py"
    project.put(producer, "value = 1\n")
    project.plan(
        ("produce", "docs/producer.md", [producer, f"./{OUTPUT}"], []),
        ("consume", DESIGN, [OUTPUT], ["produce"]),
    )
    project.configure(normal=[SUCCESS], **{"one-shot": [SUCCESS]})
    project.run(expected=[OUTPUT])
    [(role, prompt)] = project.calls
    assert f"BEGIN DEPENDENCY ARTIFACT {producer}" in prompt
    assert role == "one-shot"
    assert project.routing_events[0]["incomplete_distant_artifacts"] == 0


@pytest.mark.parametrize("expected", [(OUTPUT,), ()], ids=["derived", "direct"])
def test_plan_without_matching_task_uses_standard(project, expected):
    producer = "src/producer.py"
    project.put(producer, "value = 1\n")
    project.plan(
        ("produce", "docs/producer.md", [producer], []),
        ("other", DESIGN, ["src/other.py"], ["produce"]),
    )
    project.configure(normal=[SUCCESS], **{"one-shot": [SUCCESS]})
    project.run(expected=expected)
    assert [role for role, _ in project.calls] == ["normal"]
    assert project.routing == [("standard", "unmatched_plan_task", False)]


def test_signature_only_observed_import_uses_standard_without_plan(project):
    project.config["implement"]["existing_output_files_prompt_limit"] = 1000
    consumer, producer = "src/unit.ts", "src/producer.ts"
    project.put(consumer, 'import { produce } from "./producer";\nexport const value = produce();\n')
    project.put(producer, "export function produce(): number { return 1; }\n// "
                + "x" * implementer.DEPENDENCY_ARTIFACT_FILES_PROMPT_LIMIT)
    response = f"=== FILE: {consumer} ===\nexport const value = 1;\n"
    project.configure(normal=[response], **{"one-shot": [response]})
    project.run(output=consumer, expected=[consumer])
    [(role, prompt)] = project.calls
    assert f"DEPENDENCY ARTIFACT {producer} (distance-1 producer — signature surface" in prompt
    assert role == "normal"
    assert project.routing == [("standard", "incomplete_dependency_artifact", False)]


@pytest.mark.parametrize("regular_succeeds", [True, False])
def test_exhausted_no_usable_output_falls_back_once(project, capsys, regular_succeeds):
    project.config["implement"]["no_usable_file_retries"] = 1
    rejected = "=== FILE: ../outside.py ===\nvalue = 1\n"
    normal = [SUCCESS] if regular_succeeds else [rejected, rejected]
    project.configure(normal=normal, **{"one-shot": ["no files", "still no files"]})
    if regular_succeeds:
        assert project.run(feedback="Caller feedback.").generated_files == [project.root / OUTPUT]
    else:
        with pytest.raises(CoddCLIError, match=r"outside\.py") as error:
            project.run(feedback="Caller feedback.")
        assert "unstructured chars" not in str(error.value)
        assert not (project.root / OUTPUT).exists()
    assert [role for role, _ in project.calls] == ["one-shot", "one-shot", *(["normal"] * len(normal))]
    assert project.calls[0][1] == project.calls[2][1]
    assert "A previous implementation attempt produced no usable files" in project.calls[1][1]
    assert project.routing == [("one_shot", "complete_context", False), ("standard", "no_usable_output", True)]
    fallback_lines = [
        line for line in capsys.readouterr().err.splitlines()
        if "reason=no_usable_output fallback=true" in line
    ]
    # The fallback notice repeats the first prompt's distant-artifact count.
    assert len(fallback_lines) == 1 and "incomplete_distant_artifacts=0" in fallback_lines[0]
    groups, invalid = summarize_usage(project.root / "usage.jsonl")
    assert invalid == 0
    assert sum(group["calls"] for group in groups) == len(project.calls)


@pytest.mark.parametrize("invalid", ["value = (\n", "valuе = 1\n"], ids=["syntax", "confusable"])
def test_exhausted_write_gate_falls_back(project, invalid):
    project.config["implement"]["syntax_gate_max_attempts"] = 2
    block = f"=== FILE: {OUTPUT} ===\n{invalid}"
    project.configure(normal=[SUCCESS], **{"one-shot": [block, block]})
    project.run()
    assert [role for role, _ in project.calls] == ["one-shot", "one-shot", "normal"]
    assert project.routing[-1] == ("standard", "syntax_gate", True)
    assert (project.root / OUTPUT).read_text().endswith("value = 2\n")


def test_replacement_guard_applies_before_and_after_fallback(project):
    original = "#" + "x" * 80 + "\n"
    project.config["implement"]["no_usable_file_retries"] = 1
    project.configure(normal=[SUCCESS, SUCCESS], **{"one-shot": ["no files", SUCCESS, SUCCESS]})
    # A file created after the first prompt becomes partial in the retry prompt.
    implementer = Implementer(project.root, use_derived_steps=False,
                              sleep=lambda _: project.put(OUTPUT, original))
    with pytest.raises(CoddCLIError, match="PARTIAL"):
        implementer.run_implement(ImplementSpec(DESIGN, [OUTPUT]))
    assert [role for role, _ in project.calls] == ["one-shot"] * 3 + ["normal"] * 2
    assert project.routing[-1] == ("standard", "incomplete_output_rejected", True)
    assert (project.root / OUTPUT).read_text() == original


@pytest.mark.parametrize("failure, detail", [
    (2, "exit code 2"), ({"stderr": "empty output from a failed helper"}, "empty output from a failed helper"),
])
def test_process_failure_does_not_fall_back(project, failure, detail):
    project.configure(normal=[SUCCESS], **{"one-shot": [failure]})
    with pytest.raises(ValueError, match=f"AI command failed: {detail}"):
        project.run()
    assert [role for role, _ in project.calls] == ["one-shot"]
    assert project.routing == [("one_shot", "complete_context", False)]


def test_unrelated_cli_error_propagates_without_fallback(project, monkeypatch):
    project.configure(normal=[SUCCESS], **{"one-shot": [SUCCESS]})
    invoke = implementer.generator_module._invoke_ai_command
    error = CoddCLIError("Unrelated implementation error")

    def invoke_then_raise(command, prompt, **kwargs):
        result = invoke(command, prompt, **kwargs)
        if command == project.config["ai_commands"]["implement_one_shot"]:
            raise error from RuntimeError("Unrelated cause")
        return result

    monkeypatch.setattr(implementer.generator_module, "_invoke_ai_command", invoke_then_raise)
    with pytest.raises(CoddCLIError) as raised:
        project.run()
    assert raised.value is error
    assert [role for role, _ in project.calls] == ["one-shot"]
    assert project.routing == [("one_shot", "complete_context", False)]
    assert not (project.root / OUTPUT).exists()


def test_usage_log_write_failure_does_not_interrupt_routing_or_fallback(project, capsys):
    (project.root / "usage.jsonl").mkdir()
    project.configure(normal=[SUCCESS], **{"one-shot": ["no files"]})
    assert project.run().generated_files == [project.root / OUTPUT]
    assert [role for role, _ in project.calls] == ["one-shot", "normal"]
    stderr = capsys.readouterr().err
    assert "route=one_shot reason=complete_context fallback=false" in stderr
    assert "route=standard reason=no_usable_output fallback=true" in stderr


def test_routing_setting_does_not_change_step_derivation_cache(project):
    project.config["ai_commands"]["impl_step_derive"] = f"{project.command} derive"
    project.config["implement"]["existing_output_files_prompt_limit"] = 1000
    steps = json.dumps({"steps": [{
        "id": "implement_unit", "kind": "implementation", "rationale": "Build the unit.",
        "source_design_section": "Build the unit.", "expected_outputs": [OUTPUT],
    }]})
    project.configure(one_shot=False, derive=[steps], normal=[SUCCESS])
    project.run(derive=True)
    cache = impl_step_cache_path(ImplementSpec(DESIGN, [OUTPUT]), {"project_root": project.root})
    baseline = read_impl_step_cache(cache)
    assert baseline and baseline.steps
    before = cache.read_bytes()
    project.configure(derive=[steps], normal=[SUCCESS, SUCCESS], **{"one-shot": [SUCCESS]})
    project.run(derive=True)
    assert [role for role, _ in project.calls] == ["derive", "normal", "one-shot"]
    assert cache.read_bytes() == before
    assert read_impl_step_cache(cache).cache_key == baseline.cache_key


def test_greenfield_uses_same_routing_for_generation_and_feedback(project):
    project.config["ai_commands"]["impl_step_derive"] = f"{project.command} derive"
    steps = json.dumps({"steps": [{
        "id": "implement_unit", "kind": "implementation", "rationale": "Build the unit.",
        "source_design_section": "Build the unit.", "expected_outputs": [],
    }]})
    existing = "#" + "x" * 80 + "\nvalue = 2\n"
    initial = f"=== FILE: {OUTPUT} ===\n{existing}"
    project.configure(derive=[steps], normal=[SUCCESS], **{"one-shot": [initial]})
    pipeline = GreenfieldPipeline()
    task = ImplementTaskRef("unit", DESIGN, ["src"], expected_outputs=(OUTPUT,))
    result = pipeline._default_implement_task_runner(
        project.root, task, ai_command=None, coverage_gate=False,
        chunk_size=None, timeout_per_chunk=600,
    )
    assert "1 file(s) generated" in result
    # Repair sees the file from the first pass and cannot replace it from partial context.
    pipeline._reimplement_tasks(project.root, [task], "Repair the unit.", project.config)
    assert [role for role, _ in project.calls] == ["derive", "one-shot", "normal"]
    assert "Repair the unit." in project.calls[-1][1]
    assert project.routing[-1] == ("standard", "incomplete_existing_output", False)
    assert (project.root / OUTPUT).read_text().endswith(existing)


def test_unreadable_existing_output_named_only_in_approved_steps_uses_standard(project):
    project.config["ai_commands"]["impl_step_derive"] = f"{project.command} derive"
    unreadable = project.put("src/legacy.py", "")
    unreadable.write_bytes(b"\xff")
    steps = json.dumps({"steps": [{
        "id": "implement_unit", "kind": "implementation", "rationale": "Build the unit.",
        "source_design_section": "Build the unit.", "expected_outputs": ["src/legacy.py"],
        "approved": True,
    }]})
    project.configure(derive=[steps], normal=[SUCCESS], **{"one-shot": [SUCCESS]})
    project.run(output="src", expected=[OUTPUT], derive=True)
    assert [role for role, _ in project.calls] == ["derive", "normal"]
    assert project.routing == [("standard", "incomplete_existing_output", False)]


def test_existing_output_over_budget_uses_standard_even_if_shown_in_steps(project):
    project.config["ai_commands"]["impl_step_derive"] = f"{project.command} derive"
    original = "#" + "x" * 80 + "\nvalue = 1\n"
    project.put(OUTPUT, original)
    steps = json.dumps({"steps": [{
        "id": "implement_unit", "kind": "implementation", "rationale": "Build the unit.",
        "source_design_section": "Build the unit.", "expected_outputs": [OUTPUT], "approved": True,
    }]})
    project.configure(derive=[steps], normal=[SUCCESS], **{"one-shot": [SUCCESS]})
    project.run(derive=True)
    assert [role for role, _ in project.calls] == ["derive", "normal"]
    assert original in project.calls[1][1]
    assert project.routing == [("standard", "existing_output_over_budget", False)]
    assert (project.root / OUTPUT).read_text().endswith("value = 2\n")
