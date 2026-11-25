import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from codd.cli import main
from codd.config import load_project_config
from codd.design_consistency import enforce_design_consistency, without_behavior_contract
from codd.generator import WaveArtifact, _apply_reconciliation_scope, _DesignRewrite, generate_wave, regenerate_artifact
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


def _same_wave_without_dependency(project, *, owner_first=True, owner_only_plan=False):
    config = load_project_config(project)
    waves = config["wave_config"]
    owner = waves["1"][0]
    consumer = waves.pop("2")[0]
    consumer.pop("depends_on")
    if owner_only_plan:
        config["generate"]["design_consistency"] = None
        consumer.pop("behavior_owners")
        consumer.pop("owns_operation_flow")
    waves["1"] = [owner, consumer] if owner_first else [consumer, owner]
    _write(project, "codd/codd.yaml", yaml.safe_dump(config))


@pytest.mark.parametrize("owner_first", [True, False])
@pytest.mark.parametrize("owner_only_plan", [False, True])
def test_same_wave_case_without_dependency_retries_before_write(tmp_path, monkeypatch, owner_first, owner_only_plan):
    project = _project(tmp_path)
    _same_wave_without_dependency(project, owner_first=owner_first, owner_only_plan=owner_only_plan)
    calls = []

    def generate(_command, prompt):
        if "Node ID: design:import" in prompt:
            calls.append("owner")
            return _document(ALLOW, _block(ALLOW, "accept", owner=True))
        if "REVIEW FEEDBACK" not in prompt:
            calls.append("consumer")
            return _document(EXPECTED, _block(EXPECTED, "accept"))
        calls.append("retry")
        assert not (project / CONSUMER).exists()
        assert SPEC in prompt and OWNER in prompt
        assert "remove the case" in prompt and "cite the owner" in prompt
        return _document(f"See {OWNER} for the decision.", "```codd-behavior\nowns: {}\ncases: []\n```\n")

    monkeypatch.setattr("codd.generator._invoke_ai_command", generate)
    generate_wave(project, 1)

    assert calls == (["owner", "consumer", "retry"] if owner_first else ["consumer", "retry", "owner"])
    enforce_design_consistency(project, load_project_config(project), complete=True)


def test_dependency_retry_cannot_drop_the_behavior_block(tmp_path, monkeypatch):
    project = _project(tmp_path)
    _same_wave_without_dependency(project, owner_first=False, owner_only_plan=True)
    responses = iter([
        _document(EXPECTED, _block(EXPECTED, "accept")),
        _document(f"See {OWNER} for the decision."),
    ])

    def generate(_command, prompt):
        assert "Node ID: test:acceptance" in prompt
        return next(responses)

    monkeypatch.setattr("codd.generator._invoke_ai_command", generate)
    with pytest.raises(ValueError, match="missing codd-behavior declarations"):
        generate_wave(project, 1)
    assert not (project / CONSUMER).exists()


def _run_through_design_check(project):
    from codd.greenfield.pipeline import new_session, save_session

    session = new_session({"ai_command": "fake-ai"})
    for stage in ("init", "elicit", "plan"):
        session["stages"][stage]["status"] = "done"
    save_session(project, session)
    return GreenfieldPipeline(coverage_gate=False).run(project, resume=True)


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_greenfield_resume_repairs_only_consumer_after_full_design_check(tmp_path, monkeypatch, repair_succeeds):
    from codd.greenfield.pipeline import format_greenfield_result, load_session

    project = _project(tmp_path)
    _same_wave_without_dependency(project)
    owner = _write(project, OWNER, _document(ALLOW, _block(ALLOW, "accept", owner=True)))
    consumer = _write(project, CONSUMER, _document(EXPECTED, _block(EXPECTED, "accept")))
    before_owner, before_consumer = owner.read_bytes(), consumer.read_bytes()
    prompts = []

    def generate(command, prompt):
        prompts.append(prompt)
        assert command == "fake-ai"
        assert "Node ID: test:acceptance" in prompt
        assert SPEC in prompt and OWNER in prompt and "remove the case" in prompt
        if repair_succeeds:
            return _document(f"See {OWNER} for the decision.", "```codd-behavior\nowns: {}\ncases: []\n```\n")
        return _document(EXPECTED, _block(EXPECTED, "accept"))

    def stop_at_implementation(_project):
        raise StageError("implementation reached")

    monkeypatch.setattr("codd.generator._invoke_ai_command", generate)
    failed = _run_through_design_check(project)
    assert failed.failed_stage == "generate" and failed.resume_command
    assert "add a dependency on" in failed.error
    assert load_session(project)["stages"]["generate"]["units"] == {"1": "done"}
    assert not prompts

    resumed = GreenfieldPipeline(coverage_gate=False, task_lister=stop_at_implementation).run(project, resume=True)
    assert owner.read_bytes() == before_owner
    if repair_succeeds:
        assert len(prompts) == 1
        assert resumed.failed_stage == "implement" and resumed.error == "implementation reached"
        assert load_session(project)["stages"]["generate"]["status"] == "done"
        assert consumer.read_bytes() != before_consumer
    else:
        assert len(prompts) == 2
        assert resumed.failed_stage == "generate" and resumed.resume_command is None
        assert "--resume" not in format_greenfield_result(resumed, "text")
        assert consumer.read_bytes() == before_consumer


@pytest.mark.parametrize("issue", ["conflict", "unresolved"])
def test_greenfield_complete_disagreement_requires_manual_resolution(tmp_path, monkeypatch, issue):
    from codd.greenfield.pipeline import format_greenfield_result

    project = _project(tmp_path)
    _same_wave_without_dependency(project)
    _write(project, OWNER, _document(ALLOW, _block(ALLOW, "accept", owner=True)))
    block = _block(DENY, "reject" if issue == "conflict" else "accept")
    if issue == "unresolved":
        data = yaml.safe_load(block.split("\n", 1)[1].rsplit("```", 1)[0])
        data["unresolved"] = [{"documents": [OWNER, CONSUMER], "quote": DENY}]
        block = f"```codd-behavior\n{yaml.safe_dump(data)}```\n"
    _write(project, CONSUMER, _document(DENY, block))

    def unexpected_ai_call(*_args):
        pytest.fail("a reported disagreement must be reconciled before regeneration")

    monkeypatch.setattr("codd.generator._invoke_ai_command", unexpected_ai_call)
    result = _run_through_design_check(project)

    assert result.failed_stage == "generate"
    assert ("conflicts with" if issue == "conflict" else "unresolved disagreement") in result.error
    assert result.resume_command is None
    assert "--resume" not in format_greenfield_result(result, "text")
    assert "codd generate --check-design" in result.inspect_command


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


def test_explicit_check_on_opted_out_project_skips_dependency_reachability(tmp_path):
    project = _project(tmp_path)
    config = load_project_config(project)
    config["generate"]["design_consistency"] = False
    config["wave_config"]["2"][0].pop("depends_on")
    _write(project, "codd/codd.yaml", yaml.safe_dump(config))
    _write(project, OWNER, _document(ALLOW, _block(ALLOW, "accept", owner=True)))
    _write(project, CONSUMER, _document(EXPECTED, _block(EXPECTED, "accept")))

    result = CliRunner().invoke(main, ["generate", "--check-design", "--path", str(project)])

    assert result.exit_code == 0, result.output
    assert "add a dependency" not in result.output


PEER = "docs/design/validation.md"
DEPENDENT = "docs/design/service.md"
FOLLOW_UP = "docs/plan/implementation.md"
DISPUTE = "Input validation accepts files larger than the importer's 100 byte limit."
LIMIT = "The importer accepts files of at most 100 bytes."
UNBOUNDED = "Input validation accepts files of any size."
ALIGNED = "Input validation rejects files above the importer's 100 byte limit."
RECONCILIATIONS = ".codd/audit/design-reconciliations.jsonl"
RECONCILIATION_FAILURES = ".codd/audit/design-reconciliation-failures.jsonl"


def _declaration(quote, *, spec=None, documents=()):
    data = {"owns": {spec: quote} if spec else {}, "cases": [],
            "unresolved": [{"documents": list(documents), "quote": quote}] if documents else []}
    return _document(quote, f"```codd-behavior\n{yaml.safe_dump(data)}```\n")


def _upstream_disagreement_project(tmp_path):
    project = _project(tmp_path)
    config = load_project_config(project)
    reporter = config["wave_config"].pop("2")[0]
    reporter["depends_on"] = ["design:import", "design:validation"]
    config["wave_config"].update({
        "2": [{"node_id": "design:validation", "output": PEER, "title": "Validation",
               "behavior_owners": ["input-validation"]}],
        "3": [{"node_id": "design:service", "output": DEPENDENT, "title": "Service",
               "depends_on": ["design:validation"]}],
        "4": [reporter],
        "5": [{"node_id": "plan:implementation", "output": FOLLOW_UP, "title": "Implementation",
               "depends_on": ["test:acceptance"]}],
    })
    _write(project, "codd/codd.yaml", yaml.safe_dump(config))
    _write(project, OWNER, _declaration(LIMIT, spec=SPEC))
    _write(project, PEER, _declaration(UNBOUNDED, spec="input-validation"))
    _write(project, DEPENDENT, _declaration("The service delegates input validation."))
    return project


@pytest.mark.parametrize(("failure", "stage", "reason", "attempt_calls"), [
    ("scope", "select edit scope", "invalid line numbers in replace_lines", ["scope"]),
    ("outside_scope", "rewrite", "outside the approved edit scope", ["rewrite"]),
    ("unchanged", "rewrite", "reconciliation did not change the document body", ["rewrite"]),
    ("stale_dependent", "check rewritten documents", "conflicts with", ["rewrite"]),
    ("still_reported", "regenerate reporter", "unresolved disagreement between", ["rewrite", "reporter"]),
])
def test_reconciliation_reports_and_records_each_failed_attempt(
    tmp_path, monkeypatch, failure, stage, reason, attempt_calls,
):
    from codd.greenfield.pipeline import format_greenfield_result

    project = _upstream_disagreement_project(tmp_path)
    config = load_project_config(project)
    config["generate"]["body_retry"]["max_retries"] = 2
    _write(project, "codd/codd.yaml", yaml.safe_dump(config))

    original = _declaration(UNBOUNDED, spec="input-validation")
    replacement = _declaration(ALIGNED, spec="input-validation")
    if failure in {"scope", "outside_scope"}:
        note = "Keep the original file name in the import log."
        original = original.replace("```codd-behavior", f"{note}\n\n```codd-behavior")
        replacement = replacement.replace("```codd-behavior", "Discard the original file name.\n\n```codd-behavior")
    if failure == "unchanged":
        replacement = original
    if failure == "stale_dependent":
        block = _block(UNBOUNDED, "accept").replace(SPEC, "input-validation")
        _write(project, DEPENDENT, _document(UNBOUNDED, block))
        block = _block(ALIGNED, "reject", owner=True).replace(SPEC, "input-validation")
        replacement = _document(ALIGNED, block)
    peer = _write(project, PEER, original)
    calls = []

    def fake(command, prompt):
        assert command == "fake-ai"
        if "Select reconciliation edit locations" in prompt:
            calls.append("scope")
            allowed = [i for i, line in enumerate(original.splitlines(), 1) if UNBOUNDED in line]
            return json.dumps({"replace_lines": [999] if failure == "scope" else allowed, "insert_after": []})
        if "Node ID: design:validation" in prompt:
            calls.append("rewrite")
            if failure == "outside_scope" and calls.count("rewrite") == 2:
                return replacement.replace("Discard the original file name.", note).replace("## Overview", "## Details")
            return replacement
        assert "Node ID: test:acceptance" in prompt
        calls.append("reporter")
        return _declaration(DISPUTE, documents=(OWNER, PEER))

    monkeypatch.setattr("codd.generator._invoke_ai_command", fake)
    if failure == "still_reported":
        result = _run_through_design_check(project)
        assert result.failed_stage == "generate"
        output = format_greenfield_result(result, "text")
    else:
        result = CliRunner().invoke(main, [
            "generate", "--wave", "4", "--no-contract-gate", "--path", str(project),
        ])
        assert result.exit_code == 1
        output = result.output

    expected_calls = ["reporter"] + attempt_calls * 2
    if failure == "outside_scope":
        expected_calls.insert(1, "scope")
    assert calls == expected_calls
    assert peer.read_text(encoding="utf-8") == original
    assert not (project / CONSUMER).exists()
    assert "Provisional design decisions:" not in output
    assert "codd generate --check-design" in output
    assert "Automatic design reconciliation failed after 2 attempts." in output
    assert f"Rewrite {PEER} to follow {OWNER}." in output
    entry = json.loads((project / RECONCILIATION_FAILURES).read_text(encoding="utf-8"))
    assert entry["outcome"] == "failed"
    assert entry["disagreements"] == [{"reporter": CONSUMER, "documents": [OWNER, PEER], "quote": DISPUTE}]
    assert entry["rewrites"] == [{"document": PEER, "follow": [OWNER]}]
    assert [attempt["stage"] for attempt in entry["attempts"]] == [stage, stage]
    assert [attempt["attempt"] for attempt in entry["attempts"]] == [1, 2]
    document = {"still_reported": CONSUMER, "stale_dependent": None}.get(failure, PEER)
    phase = f"{stage} {document}" if document else stage
    for attempt in entry["attempts"]:
        assert attempt["document"] == document
        assert reason in attempt["reason"]
        line = f"Attempt {attempt['attempt']} ({phase}): {attempt['reason']}"
        assert " ".join(line.split()) in " ".join(output.split())
    if failure == "outside_scope":
        assert entry["attempts"][0]["reason"] != entry["attempts"][1]["reason"]
    assert not (project / RECONCILIATIONS).exists()


@pytest.mark.parametrize("ineligible", ["zero_budget", "later_document", "cited_reporter"])
def test_reconciliation_reports_and_records_why_it_was_skipped(tmp_path, monkeypatch, ineligible):
    project = _upstream_disagreement_project(tmp_path)
    config = load_project_config(project)
    if ineligible == "zero_budget":
        config["generate"]["body_retry"]["max_retries"] = 0
        reason = "generate.body_retry.max_retries is 0"
    elif ineligible == "later_document":
        config["wave_config"]["4"].extend(config["wave_config"].pop("2"))
        reason = f"{PEER} is not earlier than reporter {CONSUMER}"
    else:
        _write(project, PEER, _declaration(
            UNBOUNDED, spec="input-validation", documents=(OWNER, DEPENDENT),
        ))
        reason = f"{PEER} reports its own unresolved disagreement"
    _write(project, "codd/codd.yaml", yaml.safe_dump(config))
    calls = []

    def fake(command, prompt):
        assert command == "fake-ai"
        assert "Node ID: test:acceptance" in prompt
        calls.append("reporter")
        return _declaration(DISPUTE, documents=(OWNER, PEER))

    monkeypatch.setattr("codd.generator._invoke_ai_command", fake)
    result = CliRunner().invoke(main, [
        "generate", "--wave", "4", "--no-contract-gate", "--path", str(project),
    ])

    assert result.exit_code == 1
    assert calls == ["reporter"]
    assert f"Automatic design reconciliation skipped: {reason}." in result.output.splitlines()
    assert "Review the reported design documents, then run 'codd generate --check-design'." in result.output
    assert "Provisional design decisions:" not in result.output
    entry = json.loads((project / RECONCILIATION_FAILURES).read_text(encoding="utf-8"))
    assert entry["outcome"] == "skipped"
    assert entry["reason"] == f"{reason}."
    assert entry["attempts"] == []
    assert not (project / RECONCILIATIONS).exists()


def test_successful_reconciliation_keeps_the_original_output(tmp_path, monkeypatch):
    project = _upstream_disagreement_project(tmp_path)
    calls = []

    def fake(command, prompt):
        assert command == "fake-ai"
        if "Node ID: design:validation" in prompt:
            calls.append("rewrite")
            return _declaration(ALIGNED, spec="input-validation")
        assert "Node ID: test:acceptance" in prompt
        calls.append("reporter")
        if ALIGNED not in prompt:
            return _declaration(DISPUTE, documents=(OWNER, PEER))
        return _declaration("Files above 100 bytes are rejected.")

    monkeypatch.setattr("codd.generator._invoke_ai_command", fake)
    result = CliRunner().invoke(main, [
        "generate", "--wave", "4", "--no-contract-gate", "--path", str(project),
    ])

    assert result.exit_code == 0
    assert calls == ["reporter", "rewrite", "reporter"]
    assert result.output == (
        f"Generated: {CONSUMER} (test:acceptance)\n"
        "Wave 4: 1 generated, 0 skipped\n"
        f"Provisional design decisions: {RECONCILIATIONS}\n"
        f"Review existing dependent documents: {DEPENDENT}\n"
    )
    entry = json.loads((project / RECONCILIATIONS).read_text(encoding="utf-8"))
    assert entry.keys() == {"reporter", "documents", "quote", "rewritten", "change", "review_dependents"}
    assert not (project / RECONCILIATION_FAILURES).exists()


@pytest.mark.parametrize("entrypoint", ["generate", "greenfield", "single-wave"])
@pytest.mark.parametrize("on_disk", [False, True])
def test_reporter_reconciles_accepted_upstream_designs(tmp_path, monkeypatch, entrypoint, on_disk):
    from codd.greenfield.pipeline import format_greenfield_result

    project = _upstream_disagreement_project(tmp_path)
    owner_before = (project / OWNER).read_bytes()
    dependent_before = (project / DEPENDENT).read_bytes()
    calls = []
    if on_disk:
        _write(project, CONSUMER, _declaration(DISPUTE, documents=(PEER, OWNER)))
        if entrypoint != "single-wave":
            _write(project, FOLLOW_UP, _declaration("The plan follows the acceptance criteria."))

    def fake(command, prompt):
        assert command == "fake-ai"
        if "Node ID: design:validation" in prompt:
            calls.append("rewrite")
            assert all(value in prompt for value in (OWNER, LIMIT, PEER, UNBOUNDED, DISPUTE))
            return _declaration(ALIGNED, spec="input-validation")
        if "Node ID: test:acceptance" in prompt:
            calls.append("reporter")
            if ALIGNED not in prompt:
                return _declaration(DISPUTE, documents=(PEER, OWNER))
        else:
            assert "Node ID: plan:implementation" in prompt
            calls.append("later")
        return _declaration("Files above 100 bytes are rejected.")

    monkeypatch.setattr("codd.generator._invoke_ai_command", fake)
    if entrypoint != "greenfield":
        selection = ["--wave", "4"] if entrypoint == "single-wave" else ["--all-waves"]
        result = CliRunner().invoke(main, [
            "generate", *selection, "--no-contract-gate", "--path", str(project),
        ])
        output = result.output
        assert result.exit_code == 0, output
    else:
        def stop_at_implementation(_project):
            raise StageError("implementation reached")

        from codd.greenfield.pipeline import new_session, save_session

        session = new_session({"ai_command": "fake-ai"})
        for stage in ("init", "elicit", "plan"):
            session["stages"][stage]["status"] = "done"
        if on_disk:
            session["stages"]["generate"]["units"] = dict.fromkeys(map(str, range(1, 6)), "done")
        save_session(project, session)
        pipeline = GreenfieldPipeline(coverage_gate=False, task_lister=stop_at_implementation)
        result = pipeline.run(project, resume=True)
        output = format_greenfield_result(result, "text")
        assert result.failed_stage == "implement", output

    expected_calls = ["rewrite", "reporter"]
    if not on_disk:
        expected_calls.insert(0, "reporter")
        if entrypoint != "single-wave":
            expected_calls.append("later")
    assert calls == expected_calls
    assert (project / OWNER).read_bytes() == owner_before
    assert (project / DEPENDENT).read_bytes() == dependent_before
    assert ALIGNED in (project / PEER).read_text(encoding="utf-8")
    if entrypoint != "single-wave":
        assert (project / FOLLOW_UP).is_file()
        enforce_design_consistency(project, load_project_config(project), complete=True)
    assert RECONCILIATIONS in output
    assert DEPENDENT in output
    log = project / RECONCILIATIONS
    before = log.read_bytes()
    entries = [json.loads(line) for line in before.splitlines()]
    assert len(entries) == 1
    assert entries[0]["reporter"] == CONSUMER
    assert set(entries[0]["documents"]) == {OWNER, PEER}
    assert entries[0]["quote"] == DISPUTE
    assert entries[0]["rewritten"] == PEER
    assert ALIGNED in entries[0]["change"]
    if entrypoint == "greenfield":
        resumed = pipeline.run(project, resume=True)
        assert resumed.failed_stage == "implement"
        assert RECONCILIATIONS in format_greenfield_result(resumed, "text")
        assert log.read_bytes() == before
        assert calls == expected_calls


@pytest.mark.parametrize("budget", [0, 2])
def test_reporter_reconciliation_exhaustion_keeps_design_unresolved(tmp_path, monkeypatch, budget):
    from codd.greenfield.pipeline import format_greenfield_result

    project = _upstream_disagreement_project(tmp_path)
    config = load_project_config(project)
    config["generate"]["body_retry"]["max_retries"] = budget
    _write(project, "codd/codd.yaml", yaml.safe_dump(config))
    before = (project / PEER).read_bytes()
    calls = []

    def fake(_command, prompt):
        if "Node ID: design:validation" in prompt:
            calls.append("rewrite")
            return _declaration(ALIGNED, spec="input-validation")
        assert "Node ID: test:acceptance" in prompt
        calls.append("reporter")
        return _declaration(DISPUTE, documents=(OWNER, PEER))

    monkeypatch.setattr("codd.generator._invoke_ai_command", fake)
    result = _run_through_design_check(project)
    assert result.failed_stage == "generate"
    assert DISPUTE in result.error and "unresolved disagreement" in result.error
    assert "codd generate --check-design" in result.inspect_command
    assert "--resume" not in format_greenfield_result(result, "text")
    assert calls == ["reporter"] + ["rewrite", "reporter"] * budget
    assert (project / PEER).read_bytes() == before
    assert not (project / CONSUMER).exists()
    assert not (project / RECONCILIATIONS).exists()


@pytest.mark.parametrize("failure", ["unchanged", "stale-dependent"])
def test_reporter_cannot_clear_an_unreconciled_design(tmp_path, monkeypatch, failure):
    project = _upstream_disagreement_project(tmp_path)
    calls = []
    if failure == "stale-dependent":
        block = _block(UNBOUNDED, "accept").replace(SPEC, "input-validation")
        _write(project, DEPENDENT, _document(UNBOUNDED, block))
        repair = _block(ALIGNED, "reject", owner=True).replace(SPEC, "input-validation")
        replacement = _document(ALIGNED, repair)
    else:
        replacement = _declaration(UNBOUNDED, spec="input-validation")

    def fake(_command, prompt):
        if "Node ID: design:validation" in prompt:
            calls.append("rewrite")
            return replacement
        calls.append("reporter")
        return _declaration(DISPUTE, documents=(OWNER, PEER))

    monkeypatch.setattr("codd.generator._invoke_ai_command", fake)
    before = (project / PEER).read_bytes()
    with pytest.raises(ValueError, match="unresolved disagreement"):
        generate_wave(project, 4)
    assert calls == ["reporter", "rewrite"]
    assert (project / PEER).read_bytes() == before
    assert not (project / CONSUMER).exists()
    assert not (project / RECONCILIATIONS).exists()


def test_generation_without_reported_disagreement_keeps_one_call_per_new_document(tmp_path, monkeypatch):
    project = _upstream_disagreement_project(tmp_path)
    calls = []

    def fake(_command, prompt):
        calls.append(next(line for line in prompt.splitlines() if line.startswith("Node ID: ")))
        return _declaration("Current behavior is documented.")

    monkeypatch.setattr("codd.generator._invoke_ai_command", fake)
    result = CliRunner().invoke(main, [
        "generate", "--all-waves", "--no-contract-gate", "--path", str(project),
    ])
    assert result.exit_code == 0, result.output
    assert calls == ["Node ID: test:acceptance", "Node ID: plan:implementation"]
    assert RECONCILIATIONS not in result.output
    assert not (project / RECONCILIATIONS).exists()


def test_reconciliation_preserves_unrelated_prose_from_full_rewrite(tmp_path, monkeypatch):
    project = _upstream_disagreement_project(tmp_path)
    config = load_project_config(project)
    config["generate"]["body_retry"]["max_retries"] = 2
    _write(project, "codd/codd.yaml", yaml.safe_dump(config))
    unrelated = "The original file name is retained in the import log."
    paraphrase = "The import log keeps the source file name."

    def with_log_note(document, note):
        return document.replace("```codd-behavior", f"{note}\n\n```codd-behavior", 1)

    before_body = with_log_note(_declaration(UNBOUNDED, spec="input-validation"), unrelated)
    after = with_log_note(_declaration(ALIGNED, spec="input-validation"), unrelated)
    boundary = "A 100 byte file is accepted and a 101 byte file is rejected."
    after = after.replace(f"{ALIGNED}\n", f"{ALIGNED}\n{boundary}\n", 1)
    frontmatter = "---\ncodd:\n  node_id: design:validation  # An existing comment.\n---\n\n"
    before = frontmatter + before_body
    peer = _write(project, PEER, before)
    original_bytes = before.replace("\n", "\r\n").encode("utf-8")
    peer.write_bytes(original_bytes)
    calls = []

    def fake(_command, prompt):
        if "Select reconciliation edit locations" in prompt:
            calls.append("scope")
            assert DISPUTE in prompt and UNBOUNDED in prompt
            allowed = [i for i, line in enumerate(before_body.splitlines(), 1) if UNBOUNDED in line]
            return json.dumps({"replace_lines": allowed, "insert_after": allowed[:1]})
        if "Node ID: design:validation" in prompt:
            calls.append("rewrite")
            if calls.count("rewrite") == 1:
                return after.replace(unrelated, paraphrase)
            assert "outside the approved edit scope" in prompt
            assert peer.read_bytes() == original_bytes
            assert not (project / RECONCILIATIONS).exists()
            return after
        assert "Node ID: test:acceptance" in prompt
        if ALIGNED not in prompt:
            return _declaration(DISPUTE, documents=(OWNER, PEER))
        return _declaration("Files above 100 bytes are rejected.")

    monkeypatch.setattr("codd.generator._invoke_ai_command", fake)
    generate_wave(project, 4)

    assert paraphrase not in peer.read_text(encoding="utf-8")
    assert peer.read_bytes() == (frontmatter + after).replace("\n", "\r\n").encode("utf-8")
    assert calls == ["scope", "rewrite", "rewrite"]
    entry = json.loads((project / RECONCILIATIONS).read_text(encoding="utf-8"))
    assert entry["quote"] == DISPUTE
    assert entry["rewritten"] == PEER
    assert f"-{UNBOUNDED}" in entry["change"]
    assert f"+{ALIGNED}" in entry["change"]
    assert f"+{boundary}" in entry["change"]
    assert paraphrase not in entry["change"]


@pytest.mark.parametrize("fault", ["insert_outside_scope", "unknown_line_number", "empty_scope"])
def test_reconciliation_rejects_an_unusable_edit_scope_and_out_of_scope_insertions(tmp_path, monkeypatch, fault):
    project = _upstream_disagreement_project(tmp_path)
    config = load_project_config(project)
    config["generate"]["body_retry"]["max_retries"] = 2
    _write(project, "codd/codd.yaml", yaml.safe_dump(config))
    unrelated = "The original file name is retained in the import log."

    def with_log_note(document):
        return document.replace("```codd-behavior", f"{unrelated}\n\n```codd-behavior", 1)

    before = with_log_note(_declaration(UNBOUNDED, spec="input-validation"))
    replaced = with_log_note(_declaration(ALIGNED, spec="input-validation"))
    boundary = "A 100 byte file is accepted and a 101 byte file is rejected."
    with_insertion = replaced.replace(f"{ALIGNED}\n", f"{ALIGNED}\n{boundary}\n", 1)
    peer = _write(project, PEER, before)
    target = [i for i, line in enumerate(before.splitlines(), 1) if UNBOUNDED in line]
    calls = []

    def fake(_command, prompt):
        if "Select reconciliation edit locations" in prompt:
            calls.append("scope")
            if calls.count("scope") == 1 and fault == "unknown_line_number":
                return json.dumps({"replace_lines": [len(before.splitlines()) + 50], "insert_after": []})
            if calls.count("scope") == 1 and fault == "empty_scope":
                return json.dumps({"replace_lines": [], "insert_after": []})
            return json.dumps({"replace_lines": target, "insert_after": []})
        if "Node ID: design:validation" in prompt:
            calls.append("rewrite")
            assert f"Reported disagreements:\n- {OWNER} and {PEER}: {DISPUTE}" in prompt
            if fault == "insert_outside_scope" and calls.count("rewrite") == 1:
                return with_insertion
            return replaced
        assert "Node ID: test:acceptance" in prompt
        if ALIGNED not in prompt:
            return _declaration(DISPUTE, documents=(OWNER, PEER))
        return _declaration("Files above 100 bytes are rejected.")

    monkeypatch.setattr("codd.generator._invoke_ai_command", fake)
    generate_wave(project, 4)

    text = peer.read_text(encoding="utf-8")
    assert ALIGNED in text and UNBOUNDED not in text
    assert boundary not in text
    expected = ["scope", "rewrite", "rewrite"] if fault == "insert_outside_scope" else ["scope", "scope", "rewrite"]
    assert calls == expected
    entry = json.loads((project / RECONCILIATIONS).read_text(encoding="utf-8"))
    assert entry["rewritten"] == PEER
    assert boundary not in entry["change"]


@pytest.mark.parametrize(("proposal", "position"), [
    ("P1\n\n### Heading\n\nP2\n\nAddition\n\nP3", 5),
    ("P1\n\n### Heading\n\nP2\n\nAddition\n\nP3", 6),
    ("P1\n\nAddition\n\n### Heading\n\nP2\n\nP3", 1),
    ("P1\n\nAddition\n\n### Heading\n\nP2\n\nP3", 2),
])
def test_reconciliation_accepts_either_side_of_an_identical_blank_line(proposal, position):
    source = "P1\n\n### Heading\n\nP2\n\nP3"

    assert _apply_reconciliation_scope(PEER, source, proposal.splitlines(), (set(), {position})) == proposal


@pytest.mark.parametrize(("position", "expected"), [
    (1, "Start\r\nRepeat\r\nAdded\r\nRepeat\nRepeat\r\nEnd  "),
    (2, "Start\r\nRepeat\nAdded\r\nRepeat\r\nRepeat\r\nEnd  "),
])
def test_reconciliation_preserves_the_in_scope_alignment_of_repeated_prose(position, expected):
    source = "Start\r\nRepeat\nRepeat\r\nEnd  "
    proposal = "Start\nRepeat\nAdded\nRepeat\nRepeat\nEnd  "

    updated = _apply_reconciliation_scope(PEER, source, proposal.splitlines(), (set(), {position}))

    assert updated == expected
    assert updated.splitlines() == proposal.splitlines()


@pytest.mark.parametrize(("source", "proposal", "scope", "operation"), [
    ("P1\nP2\nP3", "P1\nParaphrase\nP3", ({1}, set()), "replacement at line 2"),
    ("P1\nP2\nP3", "P1\nAdded\nP2\nP3", (set(), {2}), "insertion after line 1"),
    ("P1\nP2\nP3", "P1\nP3", (set(), {1}), "deletion at line 2"),
    ("P1\nP2\nP3", "P1\nChanged\nAdded\nP3", ({2}, set()), "insertion after line 2"),
    ("P1\nP2\nP3", "P1\nP3\nP2", (set(), set()), "insertion after line 1"),
    ("P1\n\n### Heading\n\nP2\n\nP3\nKeep this", "P1\n\n### Heading\n\nP2\n\nAddition\n\nP3\nParaphrased",
     (set(), {5}), "replacement at line 8"),
    ("P1\n\n### Heading\n\nP2\n\nP3\nKeep this\nTail", "P1\n\n### Heading\n\nP2\n\nAddition\n\nP3\nTail",
     (set(), {5}), "deletion at line 8"),
])
def test_reconciliation_explains_which_edit_exceeds_the_scope(source, proposal, scope, operation):
    with pytest.raises(ValueError) as failed:
        _apply_reconciliation_scope(PEER, source, proposal.splitlines(), scope)

    reason = str(failed.value)
    assert "outside the approved edit scope" in reason
    assert operation in reason
    assert f"replace_lines={sorted(scope[0])}" in reason
    assert f"insert_after={sorted(scope[1])}" in reason


def test_reconciliation_removes_an_approved_line_between_untouched_lines():
    source = "Start  \r\nDelete me\nKeep me\r\nEnd\t"

    assert _apply_reconciliation_scope(PEER, source, ["Start  ", "Keep me", "End\t"], ({2}, set())) == (
        "Start  \r\nKeep me\r\nEnd\t"
    )


@pytest.mark.parametrize(("replace_lines", "updated"), [
    ({1, 2}, "A\r\n\nA"),
    ({2}, "A\r\r\nA"),
])
def test_reconciliation_preserves_protected_bytes_when_cr_and_lf_meet(replace_lines, updated):
    proposal = ["A", "", "A"]

    result = _apply_reconciliation_scope(PEER, "A\rA\n\nA", proposal, (replace_lines, set()))

    assert result == updated
    assert result.splitlines() == proposal


@pytest.mark.parametrize(("body", "proposal", "scope", "updated"), [
    ("## Overview\r\nKeep  \nRemove\r\n\r\n", "## Overview\nKeep  \n", ({3}, set()),
     "## Overview\r\nKeep  \n\r\n"),
    ("## Overview\r\nKeep  \n\r\n", "## Overview\nKeep  \nAdded\n", (set(), {2}),
     "## Overview\r\nKeep  \nAdded\n\r\n"),
])
def test_reconciliation_draft_preserves_mixed_line_endings_at_the_body_boundary(
    tmp_path, monkeypatch, body, proposal, scope, updated,
):
    prefix = "---\r\ndescription: Example\n---\r\n\n"
    artifact = WaveArtifact(1, "design:example", PEER, "Example", [], [])
    rewrite = _DesignRewrite(prefix + body, "Example change", scope)
    monkeypatch.setattr("codd.generator._invoke_ai_command", lambda _command, _prompt: proposal)

    assert rewrite.draft(artifact, [], "fake-ai", None, tmp_path) == prefix + updated


@pytest.mark.parametrize("padding", [0, 4096])
def test_reconciliation_audits_each_proposal_and_frozen_scope_after_multi_target_failures(tmp_path, monkeypatch, padding):
    project = _upstream_disagreement_project(tmp_path)
    config = load_project_config(project)
    config["generate"]["body_retry"]["max_retries"] = 2
    config["wave_config"]["3"][0]["behavior_owners"] = ["service"]
    config["wave_config"]["4"][0]["depends_on"].append("design:service")
    _write(project, "codd/codd.yaml", yaml.safe_dump(config))

    note = "p" * padding + "Keep the existing audit entry." + "q" * padding
    changed_note = note.replace("Keep", "Discard")
    other_claim = "The service accepts any file size."
    other_repair = "The service applies the 100 byte limit."

    def with_notes(document, *notes):
        return document.replace("```codd-behavior", "\n".join(notes) + "\n\n```codd-behavior", 1)

    originals = {
        PEER: with_notes(_declaration(UNBOUNDED, spec="input-validation"), "Keep the design log."),
        DEPENDENT: with_notes(_declaration(other_claim, spec="service"), "a" * 35000, note, "b" * 35000),
    }
    for output, old in originals.items():
        _write(project, output, old)

    scopes = {
        output: {"replace_lines": [i for i, line in enumerate(old.splitlines(), 1) if claim in line],
                 "insert_after": []}
        for output, old, claim in [(PEER, originals[PEER], UNBOUNDED), (DEPENDENT, originals[DEPENDENT], other_claim)]
    }
    note_line = originals[DEPENDENT].splitlines().index(note) + 1
    scopes[DEPENDENT]["replace_lines"] = sorted(scopes[DEPENDENT]["replace_lines"] + [note_line - 1, note_line + 1])
    proposals = {
        PEER: [with_notes(_declaration(claim, spec="input-validation"), "Keep the design log.") for claim in (
            ALIGNED, "Input validation applies the same 100 byte limit as the importer.",
        )],
        DEPENDENT: [
            with_notes(_declaration(other_repair, spec="service"), "c" * 70000, changed_note, "d" * 70000),
            with_notes(_declaration(other_repair, spec="service"), "First note.", note, "Last note."),
        ],
    }
    still_disputed = f"# Import\n\n## Overview\n\n{DISPUTE}\n\n```codd-behavior\n" + yaml.safe_dump({
        "owns": {}, "cases": [], "unresolved": [
            {"documents": [OWNER, PEER], "quote": DISPUTE},
            {"documents": [OWNER, DEPENDENT], "quote": DISPUTE},
        ],
    }) + "```\n"
    calls = []

    def fake(_command, prompt):
        if prompt.startswith("Select reconciliation edit locations"):
            output = next(output for output in scopes if prompt.startswith(f"Select reconciliation edit locations in {output}."))
            calls.append(("scope", output))
            return json.dumps(scopes[output])
        for node, output in [("design:validation", PEER), ("design:service", DEPENDENT)]:
            if f"Node ID: {node}" in prompt:
                call = ("rewrite", output)
                if call in calls:
                    assert f"Lines allowed to be replaced or removed: {scopes[output]['replace_lines']}" in prompt
                    assert "outside the approved edit scope" in prompt
                proposal = proposals[output][calls.count(call)]
                calls.append(call)
                return proposal
        assert "Node ID: test:acceptance" in prompt
        calls.append(("reporter", CONSUMER))
        return still_disputed

    monkeypatch.setattr("codd.generator._invoke_ai_command", fake)
    with pytest.raises(ValueError, match="unresolved disagreement"):
        generate_wave(project, 4)

    assert calls == [
        ("reporter", CONSUMER), ("scope", PEER), ("rewrite", PEER), ("scope", DEPENDENT),
        ("rewrite", DEPENDENT), ("rewrite", PEER), ("rewrite", DEPENDENT), ("reporter", CONSUMER),
    ]
    entry = json.loads((project / RECONCILIATION_FAILURES).read_text(encoding="utf-8"))
    assert [attempt["stage"] for attempt in entry["attempts"]] == ["rewrite", "regenerate reporter"]
    for index, attempt in enumerate(entry["attempts"]):
        assert set(attempt["rewrites"]) == set(proposals)
        for output, rewrite in attempt["rewrites"].items():
            assert rewrite["scope"] == scopes[output]
            proposal = rewrite["proposal"]
            assert proposal["original_chars"] == len(proposals[output][index])
            if output == DEPENDENT and index == 0:
                assert proposal["truncated"] and len(proposal["text"]) <= 65536
                assert "Discard the existing audit entry." not in proposal["text"]
                assert "Discard the existing audit entry." not in proposal["change"]["text"]
                violation = proposal["violation"]
                assert violation["operation"] == "replacement"
                assert violation["original"]["line"] == violation["proposal"]["line"] == note_line
                assert "Keep the existing audit entry." in violation["original"]["text"]
                assert "Discard the existing audit entry." in violation["proposal"]["text"]
                assert all(len(violation[side]["text"]) <= 512 for side in ("original", "proposal"))
                assert all(violation[side]["truncated"] == bool(padding) for side in ("original", "proposal"))
            else:
                assert not proposal["truncated"] and proposal["text"] == proposals[output][index]
    assert all((project / output).read_text(encoding="utf-8") == old for output, old in originals.items())
    assert not (project / CONSUMER).exists()
    assert not (project / RECONCILIATIONS).exists()
