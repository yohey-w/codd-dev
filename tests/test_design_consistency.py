from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from codd.cli import main
from codd.config import load_project_config
from codd.design_consistency import enforce_design_consistency, without_behavior_contract
from codd.generator import generate_wave, regenerate_artifact
from codd.greenfield.pipeline import GreenfieldPipeline, StageError
from codd.planner import plan_init


OWNER = "docs/design/import.md"
CONSUMER = "docs/test/acceptance.md"
SPEC = "import-header-only"
ALLOW = "The importer accepts a file with a header and no rows."
DENY = "The test expects the importer to reject a file with a header and no rows."
EXPECTED = "The test expects the importer to accept a file with a header and no rows."


def _project(tmp_path: Path, *, managed: bool | None = True) -> Path:
    config = {
        "project": {"name": "importer"},
        "ai_command": "fake-ai",
        "generate": {"design_consistency": managed, "body_retry": {"max_retries": 1}},
        "wave_config": {
            "1": [{"node_id": "design:import", "output": OWNER, "title": "Importer",
                   "modules": ["importer"]}],
            "2": [{"node_id": "test:acceptance", "output": CONSUMER, "title": "Import tests",
                   "modules": ["importer"], "depends_on": ["design:import"]}],
        },
    }
    if managed:
        config["wave_config"]["1"][0].update(behavior_owners=[SPEC], owns_operation_flow=False)
        config["wave_config"]["2"][0].update(behavior_owners=[], owns_operation_flow=False)
    (tmp_path / "codd").mkdir()
    (tmp_path / "codd/codd.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return tmp_path


def _block(quote: str, outcome: str, *, owner=False, conditions=None, sample=None) -> str:
    data = {
        "owns": {SPEC: quote} if owner else {},
        "cases": [{
            "spec": SPEC,
            "surface": "import-file",
            "input": ["id\n"] if sample is None else sample,
            "conditions": {"format": "csv"} if conditions is None else conditions,
            "outcome": outcome,
            "quote": quote,
        }],
    }
    return f"```codd-behavior\n{yaml.safe_dump(data, sort_keys=False)}```\n"


def _document(quote: str, block: str = "") -> str:
    return f"# Import\n\n## Overview\n\n{quote}\n\n{block}"


def _write(project, name, content):
    path = project / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _assign_operation_flow_to_peer(project):
    config = load_project_config(project)
    output = "docs/design/operations.md"
    config["wave_config"]["1"].append({
        "node_id": "design:operations",
        "output": output,
        "title": "Operations",
        "behavior_owners": [],
        "owns_operation_flow": True,
    })
    _write(project, "codd/codd.yaml", yaml.safe_dump(config))
    _write(
        project, output,
        "# Operations\n\n```yaml\noperation_flow:\n  operations: [{id: import-file}]\n```\n\n"
        "```codd-behavior\nowns: {}\ncases: []\n```\n",
    )


def test_generation_retries_just_derivative_using_established_owner(tmp_path, monkeypatch):
    project = _project(tmp_path)
    _assign_operation_flow_to_peer(project)
    prompts = []

    def generate(_command, prompt):
        prompts.append(prompt)
        if "Node ID: design:import" in prompt:
            return _document(ALLOW, _block(ALLOW, "accept", owner=True))
        if "REVIEW FEEDBACK" not in prompt:
            return _document(DENY, _block(DENY, "reject"))
        assert ALLOW in prompt and DENY in prompt
        assert OWNER in prompt and CONSUMER in prompt
        return _document(EXPECTED, _block(EXPECTED, "accept"))

    monkeypatch.setattr("codd.generator._invoke_ai_command", generate)
    generate_wave(project, 1)
    owner_before = (project / OWNER).read_bytes()
    generate_wave(project, 2)

    assert len(prompts) == 3
    assert "Do not emit operation_flow here" in prompts[0]
    assert owner_before == (project / OWNER).read_bytes()
    assert EXPECTED in (project / CONSUMER).read_text(encoding="utf-8")
    enforce_design_consistency(project, load_project_config(project), complete=True)


def test_scoped_regeneration_preserves_existing_file_on_repeated_disagreement(tmp_path, monkeypatch):
    project = _project(tmp_path)
    _write(project, OWNER, _document(ALLOW, _block(ALLOW, "accept", owner=True)))
    consumer = _write(project, CONSUMER, _document(EXPECTED, _block(EXPECTED, "accept")))
    before = consumer.read_bytes()
    calls = []

    def disagree(_command, prompt):
        calls.append(prompt)
        return _document(DENY, _block(DENY, "reject"))

    monkeypatch.setattr("codd.generator._invoke_ai_command", disagree)
    with pytest.raises(ValueError, match=r"import\.md:5 .* conflicts with .*acceptance\.md:\d+"):
        regenerate_artifact(project, node_id="test:acceptance")
    assert len(calls) == 2
    assert consumer.read_bytes() == before


def test_force_all_waves_can_save_owner_and_resume_later_consumers(tmp_path, monkeypatch):
    project = _project(tmp_path)
    second = "docs/test/integration.md"
    config_path = project / "codd/codd.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["wave_config"]["3"] = [{
        **config["wave_config"]["2"][0],
        "node_id": "test:integration", "output": second, "title": "Integration",
    }]
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    owner = _write(project, OWNER, _document(ALLOW, _block(ALLOW, "accept", owner=True)))
    first = _write(project, CONSUMER, _document(EXPECTED, _block(EXPECTED, "accept")))
    later = _write(project, second, _document(EXPECTED, _block(EXPECTED, "accept")))
    reject_owner = "The importer rejects a file with a header and no rows."
    fail_second = True
    calls = []

    def generate(_command, prompt):
        if "Node ID: design:import" in prompt:
            calls.append("owner")
            return _document(reject_owner, _block(reject_owner, "reject", owner=True))
        assert reject_owner in prompt
        if "Node ID: test:integration" in prompt and fail_second:
            calls.append("integration")
            return _document(EXPECTED, _block(EXPECTED, "accept"))
        calls.append("consumer")
        return _document(DENY, _block(DENY, "reject"))

    monkeypatch.setattr("codd.generator._invoke_ai_command", generate)
    runner = CliRunner()
    interrupted = runner.invoke(main, ["generate", "--all-waves", "--force", "--path", str(project)])
    assert interrupted.exit_code == 1
    assert "Stopped at wave 3" in interrupted.output
    assert "conflicts with" in interrupted.output and second in interrupted.output
    assert reject_owner in owner.read_text(encoding="utf-8")
    assert DENY in first.read_text(encoding="utf-8")
    assert EXPECTED in later.read_text(encoding="utf-8")
    assert calls == ["owner", "consumer", "integration", "integration"]

    blocked = runner.invoke(main, ["implement", "--design", "design:import", "--path", str(project)])
    assert blocked.exit_code == 1 and "conflicts with" in blocked.output
    fail_second = False
    resumed = runner.invoke(main, ["generate", "--wave", "3", "--force", "--path", str(project)])
    assert resumed.exit_code == 0, resumed.output
    assert calls.count("owner") == 1
    enforce_design_consistency(project, load_project_config(project), complete=True)


def test_existing_conflict_on_other_conditions_does_not_block_local_regeneration(tmp_path, monkeypatch):
    project = _project(tmp_path)
    other = "docs/test/csv.md"
    config_path = project / "codd/codd.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["wave_config"]["2"].append({
        **config["wave_config"]["2"][0],
        "node_id": "test:csv", "output": other, "title": "CSV tests",
    })
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    _write(project, OWNER, _document(ALLOW, _block(ALLOW, "accept", owner=True)))
    _write(project, CONSUMER, _document(DENY, _block(DENY, "reject")))
    quote = "The importer rejects an empty TSV file."
    doc = _document(quote, _block(quote, "reject", conditions={"format": "tsv"}, sample=[""]))
    monkeypatch.setattr("codd.generator._invoke_ai_command", lambda *_: doc)

    regenerate_artifact(project, node_id="test:csv")
    assert quote in (project / other).read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="conflicts with"):
        enforce_design_consistency(project, config, complete=True)


@pytest.mark.parametrize(
    ("conditions", "sample"),
    [({"format": "tsv"}, None), ({"format": "csv"}, ["id\r\n"])],
)
def test_different_conditions_or_exact_input_do_not_conflict(tmp_path, conditions, sample):
    project = _project(tmp_path)
    _write(project, OWNER, _document(ALLOW, _block(ALLOW, "accept", owner=True)))
    quote = "This other example is rejected."
    _write(project, CONSUMER, _document(quote, _block(quote, "reject", conditions=conditions, sample=sample)))
    enforce_design_consistency(project, load_project_config(project), complete=True)


def test_legacy_migration_keeps_original_text_and_blocks_implementation(tmp_path, monkeypatch):
    project = _project(tmp_path, managed=None)
    owner_text, consumer_text = _document(ALLOW), _document(DENY)
    owner = _write(project, OWNER, owner_text)
    consumer = _write(project, CONSUMER, consumer_text)
    cli = CliRunner()

    def invoke(*args):
        return cli.invoke(main, [*args, "--path", str(project)])

    initial = invoke("generate", "--check-design")
    assert initial.exit_code == 1
    assert f"{OWNER}: missing codd-behavior declarations" in initial.output

    def extract(_command, prompt):
        if f"--- BEGIN SOURCE {OWNER} ---" in prompt:
            assert owner_text in prompt
            return _block(ALLOW, "accept", owner=True)
        assert SPEC in prompt and "import-file" in prompt
        if DENY in prompt:
            assert consumer_text in prompt
            return _block(DENY, "reject")
        assert EXPECTED in prompt
        return _block(EXPECTED, "accept")

    monkeypatch.setattr("codd.generator._invoke_ai_command", extract)
    assert invoke("generate", "--reconcile-design", "design:import").exit_code == 0
    conflict = invoke("generate", "--reconcile-design", "test:acceptance")
    assert conflict.exit_code == 1
    assert f"{OWNER}:5 (accept) conflicts with {CONSUMER}:5 (reject)" in conflict.output
    assert owner.read_text(encoding="utf-8").startswith(owner_text)
    assert consumer.read_text(encoding="utf-8").startswith(consumer_text)

    implementation = invoke("implement", "--design", "design:import", "--no-contract-gate")
    assert implementation.exit_code == 1
    assert "Unresolved design consistency issues" in implementation.output
    pipeline = GreenfieldPipeline(task_lister=lambda _: pytest.fail("implementation started"))
    with pytest.raises(StageError, match="Unresolved design consistency issues"):
        pipeline._stage_implement(project, {"units": {"old-task": "done"}}, {})

    consumer.write_text(consumer.read_text(encoding="utf-8").replace(DENY, EXPECTED, 1), encoding="utf-8")
    stale = invoke("generate", "--check-design")
    assert stale.exit_code == 1
    assert "quote is absent" in stale.output
    repaired = invoke("generate", "--reconcile-design", "test:acceptance", "--force")
    assert repaired.exit_code == 0, repaired.output
    assert without_behavior_contract(consumer.read_text(encoding="utf-8")) == _document(EXPECTED)
    assert invoke("generate", "--check-design").exit_code == 0


def test_owner_declaration_and_operation_role_cannot_be_bypassed(tmp_path, monkeypatch):
    project = _project(tmp_path)
    _assign_operation_flow_to_peer(project)
    no_owner = _document(ALLOW, _block(ALLOW, "accept"))
    monkeypatch.setattr("codd.generator._invoke_ai_command", lambda *_: no_owner)
    with pytest.raises(ValueError, match="behavior owners differ from wave_config"):
        generate_wave(project, 1)
    assert not (project / OWNER).exists()

    body = _document(ALLOW, _block(ALLOW, "accept", owner=True))
    body += "\n```yaml\noperation_flow:\n  operations: [{id: import-file}]\n```\n"
    monkeypatch.setattr("codd.generator._invoke_ai_command", lambda *_: body)
    with pytest.raises(ValueError, match="must reference the operation_flow owner"):
        generate_wave(project, 1)
    assert not (project / OWNER).exists()


def test_two_operation_flow_blocks_fail_even_when_first_agrees_with_frontmatter(tmp_path, monkeypatch):
    project = _project(tmp_path)
    config_path = project / "codd/codd.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["wave_config"]["1"][0]["owns_operation_flow"] = True
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    body = _document(ALLOW, _block(ALLOW, "accept", owner=True))
    body += "\n```yaml\noperation_flow:\n  operations: [{id: import-file}]\n```\n"
    body += "\n```yaml\noperation_flow:\n  operations: [{id: import-conflict}]\n```\n"
    monkeypatch.setattr("codd.generator._invoke_ai_command", lambda *_: body)

    with pytest.raises(ValueError, match="Only one operation_flow body declaration"):
        generate_wave(project, 1)
    assert not (project / OWNER).exists()


def test_conflicting_duplicate_operation_flow_in_frontmatter_is_rejected(tmp_path):
    project = _project(tmp_path)
    frontmatter = (
        "---\ncodd:\n"
        "  operation_flow:\n    operations: [{id: import-file, expected_outcomes: [reject]}]\n"
        "  operation_flow:\n    operations: [{id: import-file, expected_outcomes: [accept]}]\n"
        "---\n"
    )
    _write(project, OWNER, frontmatter + _document(ALLOW, _block(ALLOW, "accept", owner=True)))
    _write(project, CONSUMER, _document(EXPECTED, _block(EXPECTED, "accept")))

    with pytest.raises(ValueError, match="duplicate key 'operation_flow'"):
        enforce_design_consistency(project, load_project_config(project), complete=True)


def test_sample_inside_long_fence_does_not_activate_legacy_checks_or_regeneration(tmp_path, monkeypatch):
    project = _project(tmp_path, managed=False)
    example = _document(ALLOW, "````markdown\n" + _block(ALLOW, "accept", owner=True) + "````\n")
    _write(project, OWNER, example)
    _write(project, CONSUMER, _document(EXPECTED))
    config = load_project_config(project)
    enforce_design_consistency(project, config, complete=True)

    def generate(_command, prompt):
        assert "Behavior ownership and concrete examples:" not in prompt
        return example

    monkeypatch.setattr("codd.generator._invoke_ai_command", generate)
    generate_wave(project, 1, force=True)
    enforce_design_consistency(project, config, complete=True)


def test_reported_open_disagreement_fails_without_two_normalized_cases(tmp_path):
    project = _project(tmp_path)
    _write(project, OWNER, _document(ALLOW, _block(ALLOW, "accept", owner=True)))
    quote = "The design and acceptance criteria disagree on header-only imports. Hold release pending reconciliation."
    block = {
        "owns": {},
        "cases": [],
        "unresolved": [{"documents": [OWNER, CONSUMER], "quote": quote}],
    }
    _write(project, CONSUMER, _document(quote, f"```codd-behavior\n{yaml.safe_dump(block)}```\n"))

    with pytest.raises(ValueError, match=f"{CONSUMER}:5: unresolved disagreement between {OWNER} and {CONSUMER}"):
        enforce_design_consistency(project, load_project_config(project), complete=True)


def test_legacy_extraction_retries_invalid_test_ownership_and_uses_focus(tmp_path, monkeypatch):
    project = _project(tmp_path, managed=False)
    _write(project, OWNER, _document(ALLOW, _block(ALLOW, "accept", owner=True)))
    _write(project, CONSUMER, _document(EXPECTED))
    calls = []
    focus = "Extract the header-only behavior under CSV conditions."

    def extract(_command, prompt):
        calls.append(prompt)
        assert "Emit owns: {}" in prompt and focus in prompt
        if len(calls) == 1:
            return _block(EXPECTED, "accept", owner=True)
        assert "test/plan documents must reference" in prompt
        return _block(EXPECTED, "accept")

    monkeypatch.setattr("codd.generator._invoke_ai_command", extract)
    result = CliRunner().invoke(main, [
        "generate", "--reconcile-design", "test:acceptance", "--path", str(project),
        "--feedback", focus,
    ])
    assert result.exit_code == 0, result.output
    assert len(calls) == 2
    enforce_design_consistency(project, load_project_config(project), complete=True)


def test_fresh_consumer_must_reach_owner_via_dependency(tmp_path):
    project = _project(tmp_path)
    config_path = project / "codd/codd.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["wave_config"]["2"][0].pop("depends_on")
    _write(project, OWNER, _document(ALLOW, _block(ALLOW, "accept", owner=True)))
    _write(project, CONSUMER, _document(EXPECTED, _block(EXPECTED, "accept")))

    with pytest.raises(ValueError, match=f"{CONSUMER}:5: add a dependency on {OWNER}:5"):
        enforce_design_consistency(project, config, complete=True)


def test_new_plan_retries_forward_owner_dependency(tmp_path, monkeypatch):
    project = _project(tmp_path, managed=None)
    _write(project, "docs/requirements/import.md", "---\ncodd:\n  node_id: req:import\n  type: requirement\n---\n\n# Import requirements\n")
    config = load_project_config(project)
    valid = config["wave_config"]
    valid["1"][0]["behavior_owners"] = [SPEC]
    valid["2"][0]["behavior_owners"] = []
    invalid = {"1": valid["2"], "2": valid["1"]}
    prompts = []

    def plan(_command, prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return yaml.safe_dump(invalid)
        assert "place behavior owner 'design:import' before its dependent" in prompt
        return yaml.safe_dump(valid)

    monkeypatch.setattr("codd.generator._invoke_ai_command", plan)
    result = plan_init(project, force=True)
    assert len(prompts) == 2
    assert result.wave_config["1"][0]["behavior_owners"] == [SPEC]


@pytest.mark.parametrize("peer_outcome", ["reject", "accept"])
def test_different_spec_owners_can_declare_same_case(tmp_path, monkeypatch, peer_outcome):
    project = _project(tmp_path)
    peer = "docs/design/parser.md"
    peer_spec = "parser-syntax"
    config = load_project_config(project)
    config["wave_config"]["2"][0].update(
        node_id="design:parser", output=peer, behavior_owners=[peer_spec],
    )
    _write(project, "codd/codd.yaml", yaml.safe_dump(config))
    owner = _document(DENY, _block(DENY, "reject", owner=True))
    quote = f"The parser outcome is {peer_outcome}."
    peer_body = _document(quote, _block(quote, peer_outcome, owner=True).replace(SPEC, peer_spec))
    monkeypatch.setattr(
        "codd.generator._invoke_ai_command",
        lambda _, prompt: owner if "Node ID: design:import" in prompt else peer_body,
    )

    generate_wave(project, 1)
    if peer_outcome == "reject":
        generate_wave(project, 2)
        enforce_design_consistency(project, config, complete=True)
    else:
        with pytest.raises(ValueError, match="conflicts with") as error:
            generate_wave(project, 2)
        assert "both claim ownership" not in str(error.value)
        assert 'import-file' in str(error.value)
        assert r'["id\n"]' in str(error.value)
        assert '{"format": "csv"}' in str(error.value)
        assert not (project / peer).exists()


def test_duplicate_spec_ownership_reports_case_details(tmp_path):
    project = _project(tmp_path)
    peer = "docs/design/parser.md"
    config = load_project_config(project)
    config["wave_config"]["2"][0].update(node_id="design:parser", output=peer, behavior_owners=[SPEC])
    _write(project, OWNER, _document(ALLOW, _block(ALLOW, "accept", owner=True)))
    _write(project, peer, _document(EXPECTED, _block(EXPECTED, "accept", owner=True)))
    with pytest.raises(ValueError, match="both own specification") as exc:
        enforce_design_consistency(project, config, complete=True)
    ownership_issue = next(line for line in str(exc.value).splitlines() if "both own specification" in line)
    assert 'import-file' in ownership_issue
    assert r'["id\n"]' in ownership_issue
    assert '{"format": "csv"}' in ownership_issue


@pytest.mark.parametrize("surface", ["IMPORT FILE()", "import_file", "Import-File"])
def test_case_surface_variants_conflict_and_keep_original_spelling(tmp_path, surface):
    project = _project(tmp_path)
    _write(project, OWNER, _document(ALLOW, _block(ALLOW, "accept", owner=True)))
    _write(project, CONSUMER, _document(DENY, _block(DENY, "reject").replace("import-file", surface)))
    with pytest.raises(ValueError, match="conflicts with") as exc:
        enforce_design_consistency(project, load_project_config(project), complete=True)
    assert "import-file" in str(exc.value)
    assert surface in str(exc.value)


@pytest.mark.parametrize("surface", ["import.file", "importfile", "import-file(arg)", "import-file()()"])
def test_other_surface_spellings_remain_distinct(tmp_path, surface):
    project = _project(tmp_path)
    _write(project, OWNER, _document(ALLOW, _block(ALLOW, "accept", owner=True)))
    _write(project, CONSUMER, _document(DENY, _block(DENY, "reject").replace("import-file", surface)))
    enforce_design_consistency(project, load_project_config(project), complete=True)


def test_scalar_input_is_rejected_and_retried_with_feedback(tmp_path, monkeypatch):
    project = _project(tmp_path)
    _write(project, OWNER, _document(ALLOW, _block(ALLOW, "accept", owner=True)))
    _write(project, CONSUMER, _document(DENY, _block(DENY, "reject", sample="id\n")))
    with pytest.raises(ValueError, match="input must be a list"):
        enforce_design_consistency(project, load_project_config(project), complete=True)

    prompts = []

    def derive(_, prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return _document(DENY, _block(DENY, "reject", sample="id\n"))
        assert "input must be a list" in prompt
        return _document(EXPECTED, _block(EXPECTED, "accept"))

    monkeypatch.setattr("codd.generator._invoke_ai_command", derive)
    generate_wave(project, 2, force=True)
    assert len(prompts) == 2
    assert EXPECTED in (project / CONSUMER).read_text(encoding="utf-8")


@pytest.mark.parametrize(("role", "owner_elsewhere"), [(False, False), (None, False), (None, True)])
def test_unassigned_operation_flow_can_be_declared(tmp_path, monkeypatch, role, owner_elsewhere):
    project = _project(tmp_path)
    if owner_elsewhere:
        _assign_operation_flow_to_peer(project)
    config = load_project_config(project)
    if role is None:
        config["wave_config"]["1"][0].pop("owns_operation_flow")
    _write(project, "codd/codd.yaml", yaml.safe_dump(config))
    body = _document(ALLOW, _block(ALLOW, "accept", owner=True))
    body += "\n```yaml\noperation_flow:\n  operations: [{id: import-file}]\n```\n"
    monkeypatch.setattr("codd.generator._invoke_ai_command", lambda *_: body)

    generate_wave(project, 1)
    assert "operation_flow" in (project / OWNER).read_text(encoding="utf-8")
    _write(project, CONSUMER, _document(EXPECTED, _block(EXPECTED, "accept")))
    enforce_design_consistency(project, config, complete=True)


@pytest.mark.parametrize("problem", ["wave-key", "missing-title", "outside-output"])
@pytest.mark.parametrize("entrypoint", ["normal", "chunk", "greenfield"])
def test_unmanaged_implement_bypasses_strict_wave_checks(tmp_path, monkeypatch, problem, entrypoint):
    from codd.cli import _run_chunked_implementation
    from codd.implementer import implement_tasks

    project = _project(tmp_path, managed=False)
    config = load_project_config(project)
    config["ai_command"] = "sh -c 'cat >/dev/null; exit 3'"
    wave = config["wave_config"]
    wave.pop("2")
    if problem == "wave-key":
        wave["waves"] = wave.pop("1")
    elif problem == "missing-title":
        wave["1"][0].pop("title")
    else:
        wave["1"].append({
            "node_id": "design:external", "output": "docs/design/../../../outside.md", "title": "External",
        })
    _write(project, "codd/codd.yaml", yaml.safe_dump(config))
    _write(
        project, OWNER,
        "---\ncodd:\n  node_id: design:import\n  type: design\n---\n\n" + _document(ALLOW),
    )

    def fake_implementation(*_args):
        return "implementation entry reached"

    if entrypoint == "normal":
        with pytest.raises(ValueError, match="AI command failed: exit code 3"):
            implement_tasks(project, design="design:import", output_paths=["src"])
    elif entrypoint == "chunk":
        monkeypatch.setattr("codd.cli._chunked_task_and_steps", lambda *_: ("task", ["step"]))
        monkeypatch.setattr("codd.implementer.chunked_runner.ChunkedRunner.run_steps", fake_implementation)
        assert _run_chunked_implementation(
            project_root=project, task_id="task", ai_cmd=None, chunk_size=1, timeout_per_chunk=1, history=None,
        ) == "implementation entry reached"
    else:
        def list_tasks(_):
            raise RuntimeError(fake_implementation())

        with pytest.raises(RuntimeError, match="implementation entry reached"):
            GreenfieldPipeline(task_lister=list_tasks)._stage_implement(project, {}, {})


@pytest.mark.parametrize(("key", "value"), [("behavior_owners", []), ("owns_operation_flow", False)])
def test_planned_ownership_alone_activates_implementation_check(tmp_path, monkeypatch, key, value):
    project = _project(tmp_path, managed=None)
    config = load_project_config(project)
    config["wave_config"]["2"][0][key] = value
    _write(project, "codd/codd.yaml", yaml.safe_dump(config))
    _write(project, OWNER, _document(ALLOW))
    _write(project, CONSUMER, _document(EXPECTED))
    monkeypatch.setattr(
        "codd.implementer.Implementer.run_implement",
        lambda *_args, **_kwargs: pytest.fail("implementation started"),
    )

    result = CliRunner().invoke(main, [
        "implement", "--design", "design:import", "--output", "src", "--no-contract-gate",
        "--path", str(project),
    ])

    assert result.exit_code == 1
    assert f"{OWNER}: missing codd-behavior declarations" in result.output
    assert f"{CONSUMER}: missing codd-behavior declarations" in result.output


@pytest.fixture
def opted_out_with_declarations(tmp_path):
    project = _project(tmp_path)
    config = load_project_config(project)
    config["generate"]["design_consistency"] = False
    _write(project, "codd/codd.yaml", yaml.safe_dump(config))
    _write(project, OWNER, _document(ALLOW, _block(ALLOW, "accept", owner=True)))
    _write(project, CONSUMER, _document(DENY, _block(DENY, "reject")))
    return project


@pytest.mark.parametrize("entrypoint", ["generate", "implement", "chunk", "greenfield"])
def test_explicit_opt_out_bypasses_automatic_design_checks(
    opted_out_with_declarations, monkeypatch, capsys, entrypoint,
):
    project = opted_out_with_declarations
    if entrypoint == "generate":
        result = CliRunner().invoke(main, [
            "generate", "--all-waves", "--no-contract-gate", "--path", str(project),
        ])
        assert result.exit_code == 0, result.output
        assert "0 generated, 2 skipped" in result.output
        output = result.output
    elif entrypoint == "implement":
        def run_implement(*_args, **_kwargs):
            raise ValueError("AI command failed: exit code 3")

        monkeypatch.setattr("codd.implementer.Implementer.run_implement", run_implement)
        result = CliRunner().invoke(main, [
            "implement", "--design", "design:import", "--output", "src", "--no-contract-gate",
            "--path", str(project),
        ])
        assert result.exit_code == 1
        assert "AI command failed: exit code 3" in result.output
        output = result.output
    elif entrypoint == "chunk":
        from codd.cli import _run_chunked_implementation

        monkeypatch.setattr("codd.cli._chunked_task_and_steps", lambda *_: ("task", ["step"]))
        monkeypatch.setattr(
            "codd.implementer.chunked_runner.ChunkedRunner.run_steps", lambda *_: "implementation started",
        )
        assert _run_chunked_implementation(
            project_root=project, task_id="task", ai_cmd=None, chunk_size=1, timeout_per_chunk=1, history=None,
        ) == "implementation started"
        output = capsys.readouterr().out
    else:
        def list_tasks(_):
            raise RuntimeError("implementation started")

        messages = []
        pipeline = GreenfieldPipeline(task_lister=list_tasks, echo=messages.append)
        with pytest.raises(RuntimeError, match="implementation started"):
            pipeline._stage_implement(project, {}, {})
        output = "\n".join(messages)

    assert "case declarations:" not in output


def test_explicit_design_commands_still_check_opted_out_project(opted_out_with_declarations, monkeypatch):
    project = opted_out_with_declarations
    cli = CliRunner()
    path_args = ["--path", str(project)]
    result = cli.invoke(main, ["generate", "--check-design", *path_args])
    assert result.exit_code == 1
    assert f"{OWNER}:5 (accept) conflicts with {CONSUMER}:5 (reject)" in result.output

    def extract(_command, prompt):
        assert f"Planned specification ids owned here: {[SPEC]}" in prompt
        return _block(ALLOW, "accept", owner=True)

    monkeypatch.setattr("codd.generator._invoke_ai_command", extract)
    result = cli.invoke(main, ["generate", "--reconcile-design", "design:import", "--force", *path_args])
    assert result.exit_code == 1
    assert f"Behavior declarations written to {OWNER}" in result.output
    assert f"{OWNER}:5 (accept) conflicts with {CONSUMER}:5 (reject)" in result.output


@pytest.mark.parametrize(
    ("scenario", "counts"),
    [
        ("no-cases", (2, 1, 0, 0)),
        ("one-document", (2, 1, 1, 0)),
        ("no-documents", (0, 0, 0, 0)),
        ("cross-document", (2, 1, 2, 1)),
    ],
)
@pytest.mark.parametrize("command", ["generate-check", "generate-all", "implement", "greenfield"])
def test_full_design_check_reports_comparison_counts(tmp_path, monkeypatch, scenario, counts, command):
    project = _project(tmp_path)
    config = load_project_config(project)
    if scenario == "no-documents":
        for entries in config["wave_config"].values():
            for entry in entries:
                entry["output"] = entry["output"].replace("docs/", "other/")
                _write(project, entry["output"], _document(ALLOW))
    else:
        owner = _block(ALLOW, "accept", owner=True)
        consumer = _block(EXPECTED, "accept")
        if scenario == "no-cases":
            owner = f"```codd-behavior\nowns: {{{SPEC}: {ALLOW}}}\ncases: []\n```\n"
        if scenario != "cross-document":
            consumer = "```codd-behavior\nowns: {}\ncases: []\n```\n"
        _write(project, OWNER, _document(ALLOW, owner))
        _write(project, CONSUMER, _document(EXPECTED, consumer))
    _write(project, "codd/codd.yaml", yaml.safe_dump(config))
    if command == "greenfield":
        messages = []
        GreenfieldPipeline(echo=messages.append)._check_design_consistency(project)
        output = "\n".join(messages)
    else:
        if command == "implement":
            def run_implement(*_args, **_kwargs):
                raise ValueError("AI command failed: exit code 3")

            monkeypatch.setattr("codd.implementer.Implementer.run_implement", run_implement)
            args = ["implement", "--design", "design:import", "--output", "src", "--no-contract-gate"]
        else:
            args = ["generate", "--check-design"] if command == "generate-check" else ["generate", "--all-waves"]
        result = CliRunner().invoke(main, [*args, "--path", str(project)])
        if command == "implement":
            assert result.exit_code == 1 and "AI command failed: exit code 3" in result.output
        else:
            assert result.exit_code == 0, result.output
        output = result.output

    expected = "Declared design behaviors are consistent" if counts[-1] else "No design cases were compared across documents"
    assert expected in output
    for label, count in zip(("documents", "owned specifications", "case declarations", "cross-document cases"), counts):
        assert f"{label}: {count}" in output


def test_explicit_opt_out_ignores_malformed_leftover_ownership_keys(opted_out_with_declarations):
    project = opted_out_with_declarations
    config = load_project_config(project)
    config["wave_config"]["1"][0]["behavior_owners"] = "not-a-list"
    config["wave_config"]["2"][0]["owns_operation_flow"] = "yes"
    _write(project, "codd/codd.yaml", yaml.safe_dump(config))

    result = CliRunner().invoke(main, [
        "generate", "--all-waves", "--no-contract-gate", "--path", str(project),
    ])

    assert result.exit_code == 0, result.output
    assert "0 generated, 2 skipped" in result.output
