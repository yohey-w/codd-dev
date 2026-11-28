"""W21 writing guidance follows the effective write scope."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from codd import implementer
from codd.greenfield.implement_test_feedback import DeclaredOutputs
from codd.greenfield.pipeline import GreenfieldPipeline, ImplementTaskRef


DESIGN = "design:report"
TEMPLATE = "views/report.html"
CONFIG = {"scan": {"test_dirs": ["checks"]}}


def build_prompt(spec, *, feedback=None, config=CONFIG):
    return implementer._build_implementation_prompt(
        config=config,
        design_context=implementer.DesignContext(DESIGN, Path("docs/design/report.md"), "# Report\n"),
        spec=spec,
        dependency_documents=[],
        conventions=[],
        coding_principles=None,
        feedback=feedback,
    )


def test_y5_writing_guidance_follows_write_scope():
    test_scope = build_prompt(implementer.ImplementSpec(DESIGN, ["views", "checks"], expected_outputs=[TEMPLATE]))
    source_scope = build_prompt(implementer.ImplementSpec(DESIGN, ["views"], expected_outputs=[TEMPLATE]))

    for heading in ("Elapsed-time measurements:", "Test-only certificates:", "Delayed effects:"):
        assert test_scope.count(heading) == 1
        assert heading not in source_scope


@pytest.mark.parametrize(("output_paths", "expected_outputs"), [
    pytest.param(["src"], ["src/test_report.py"], id="expected-outputs"),
    pytest.param(["src/test_report.py"], ["src/report.py"], id="output-paths-with-expected-outputs"),
])
def test_writing_guidance_reaches_declared_colocated_python_test(output_paths, expected_outputs):
    spec = implementer.ImplementSpec(DESIGN, output_paths, expected_outputs=expected_outputs)
    prompt = build_prompt(spec, config={**CONFIG, "project": {"language": "python"}})

    for heading in ("Elapsed-time measurements:", "Test-only certificates:", "Delayed effects:"):
        assert prompt.count(heading) == 1


@pytest.mark.parametrize("granted", [True, False], ids=["test-scope", "source-scope"])
def test_y5_feedback_rerun_uses_its_granted_write_scope(tmp_path, monkeypatch, granted):
    task = ImplementTaskRef("report", DESIGN, ("views", "checks"), expected_outputs=(TEMPLATE,))
    scope = DeclaredOutputs(tmp_path, [TEMPLATE, "checks/"] if granted else [TEMPLATE])
    prompts = []

    def fake_implement(root, *, design, output_paths, expected_outputs, feedback, **kwargs):
        spec = implementer.ImplementSpec(design, output_paths, expected_outputs=expected_outputs)
        prompts.append(build_prompt(spec, feedback=feedback))

    monkeypatch.setattr(implementer, "implement_tasks", fake_implement)
    pipeline = SimpleNamespace(ai_command="mock", echo=lambda _: None)
    GreenfieldPipeline._reimplement_tasks(
        pipeline, tmp_path, [task], "C feedback", CONFIG, feedback_output_paths=scope,
    )

    [prompt] = prompts
    assert "C feedback" in prompt
    for heading in ("Elapsed-time measurements:", "Test-only certificates:", "Delayed effects:"):
        assert (heading in prompt) == granted


def test_y6_delayed_effects_wait_then_assert():
    prompt = build_prompt(implementer.ImplementSpec(DESIGN, ["checks/report.test.txt"]))
    assert prompt.count("Delayed effects:") == 1
    line_end, bullet, spacing = prompt.split("Delayed effects:", 1)[1].splitlines()[:3]

    assert line_end == spacing == ""
    assert bullet.startswith("- ")
    assert "appears later or in another process" in bullet
    assert "wait with a timeout for the expected state before asserting it" in bullet
    assert "A single read immediately after the action" in bullet
    assert "Keep the direct assertion and put the wait before it" in bullet


def test_y12_delayed_effects_wait_for_state_after_action_returns():
    prompt = build_prompt(implementer.ImplementSpec(DESIGN, ["checks/report.test.txt"]))
    delayed_effects = prompt.split("Delayed effects:\n", 1)[1].split("\n\n", 1)[0]
    assert (
        "Waiting for a response, request completion, or an action to return does not wait for the expected state."
        in delayed_effects
    )
