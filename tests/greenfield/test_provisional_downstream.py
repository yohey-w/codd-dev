"""Protect the editable decisions appendix at the plan and generate boundaries."""

from pathlib import Path

from click.testing import CliRunner
import pytest
import yaml

from codd import generator
from codd.cli import main
from codd.planner import plan_init
from tests.greenfield.test_provisional_decisions import (
    APPENDIX,
    DECISIONS,
    DESIGN,
    DESIGN_BODY,
    DESK_LIMIT,
    WAVE,
    decision_row,
    decisions,
    discovery,
    finding,
    make_project,
    PROVISIONAL,
    run_design_cli,
    run_elicit,
    script_design,
    script_elicit,
)


@pytest.mark.parametrize("invalid_field", ["output", "depends_on"])
def test_plan_retries_when_ai_references_appendix_as_a_graph_document(tmp_path, monkeypatch, invalid_field):
    project = make_project(tmp_path)
    (project / DECISIONS).write_text(APPENDIX, encoding="utf-8")
    invalid_wave = yaml.safe_load(WAVE)
    artifact = invalid_wave["1"][0]
    if invalid_field == "output":
        artifact["output"] = str(project / DECISIONS)
    else:
        artifact["depends_on"] = ["./docs/requirements/../requirements/provisional-decisions.md"]
    responses = iter([yaml.safe_dump(invalid_wave), WAVE])
    prompts = []

    def invoke(command, prompt):
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr(generator, "_invoke_ai_command", invoke)
    result = plan_init(project, ai_command="fake")

    assert len(prompts) == 2
    assert "YOUR PREVIOUS RESPONSE WAS REJECTED" in prompts[1]
    assert DECISIONS.as_posix() in prompts[1]
    assert result.wave_config["1"][0] == {
        **yaml.safe_load(WAVE)["1"][0],
        "depends_on": [{"id": "req:reading-room", "relation": "implements", "semantic": "governance"}],
    }


@pytest.mark.parametrize("entrypoint", ["force", "regenerate"])
@pytest.mark.parametrize("managed", [True, False], ids=["managed", "ordinary"])
def test_generation_respects_the_managed_appendix(tmp_path, monkeypatch, entrypoint, managed):
    project = make_project(tmp_path)
    appendix = project / DECISIONS
    original = APPENDIX if managed else APPENDIX.split("\n", 1)[1]
    appendix.write_text(original, encoding="utf-8")
    config_path = project / "codd/codd.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["wave_config"] = yaml.safe_load(WAVE)
    # This spelling resolves to the appendix without matching its literal path.
    config["wave_config"]["1"][0]["output"] = str(project / DECISIONS)
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setattr(generator, "_invoke_ai_command", lambda *args, **kwargs: DESIGN_BODY)

    if entrypoint == "force":
        command = CliRunner().invoke(main, [
            "generate", "--path", str(project), "--wave", "1", "--force", "--ai-cmd", "fake",
        ])
        assert command.exit_code == int(managed), command.output + repr(command.exception)
        if managed:
            assert DECISIONS.as_posix() in command.output
    elif managed:
        with pytest.raises(ValueError, match=DECISIONS.as_posix()):
            generator.regenerate_artifact(project, node_id="design:reservations", ai_command="fake")
    else:
        result = generator.regenerate_artifact(project, node_id="design:reservations", ai_command="fake")
        assert result.status == "generated"

    current = appendix.read_text(encoding="utf-8")
    if managed:
        assert current == original
    else:
        assert "node_id: design:reservations" in current


@pytest.mark.parametrize("managed", [True, False], ids=["managed", "ordinary"])
def test_extracted_plan_receives_only_a_marked_appendix(tmp_path, monkeypatch, managed):
    project = make_project(tmp_path)
    (project / "docs/requirements/requirements.md").unlink()
    (project / "requirements.md").write_text("Visitors can reserve desks.\n", encoding="utf-8")
    extracted = Path(".codd/extract/reservations.md")
    (project / extracted).parent.mkdir(parents=True)
    (project / extracted).write_text(
        "---\ncodd:\n  node_id: design:extracted\n  type: design\n  source: extracted\n---\n\n"
        "Current booking behavior.\n",
        encoding="utf-8",
    )
    appendix = APPENDIX if managed else APPENDIX.split("\n", 1)[1]
    (project / DECISIONS).write_text(appendix, encoding="utf-8")
    prompts = []

    def invoke(command, prompt):
        prompts.append(prompt)
        return WAVE.replace("req:reading-room", "design:extracted")

    monkeypatch.setattr(generator, "_invoke_ai_command", invoke)
    result = plan_init(project, ai_command="fake")

    assert result.requirement_paths == [extracted.as_posix()]
    assert len(prompts) == 1
    assert "Current booking behavior." in prompts[0]
    assert prompts[0].count(DESK_LIMIT) == int(managed)


def test_freeform_appendix_is_read_verbatim_without_opt_in(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    known = finding("desk-duration")
    script_elicit(monkeypatch, discovery(known), decisions(decision_row(known["id"], DESK_LIMIT)))
    assert run_elicit(project, PROVISIONAL).exit_code == 0
    appendix = (
        "# Reading-room operating notes\r\n\r\n"
        "Visitors may reserve desks in half-hour blocks.  \r\n\r\n"
        "```md\r\n## desk-duration\r\n- Decision: Sample entry.\r\n```\r\n"
        "A staffed room may extend a reservation.\r\n\r\n  \r\n"
    )
    (project / DECISIONS).write_bytes(appendix.encode())
    design_prompts = script_design(monkeypatch)
    run_design_cli(project)
    implementation_prompts = []
    source = "src/reservations.py"

    def implement(command, prompt, *args, **kwargs):
        implementation_prompts.append(prompt)
        return f"=== FILE: {source} ===\n```python\nMAX_RESERVATION_MINUTES = 30\n```\n"

    monkeypatch.setattr(generator, "_invoke_ai_command", implement)
    result = CliRunner().invoke(main, [
        "implement", "--path", str(project), "--design", DESIGN.as_posix(),
        "--output", source, "--use-derived-steps", "false",
    ])

    assert result.exit_code == 0, result.output + repr(result.exception)
    for prompts in (*design_prompts.values(), implementation_prompts):
        assert len(prompts) == 1
        assert prompts[0].count(appendix) == 1
    assert (project / DECISIONS).read_bytes() == appendix.encode()
