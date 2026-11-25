"""Exercise the artifact selection handoff through the greenfield CLI."""

from copy import deepcopy
import json
from types import SimpleNamespace

from click.testing import CliRunner
import pytest
import yaml

from codd.cli import main
from codd.greenfield import pipeline
from codd.lexicon import LEXICON_FILENAME
from codd.required_artifacts_deriver import RequiredArtifactsDeriver
from tests.greenfield.conftest import make_stub_project
from tests.greenfield.test_pipeline import _fake_runners


ARTIFACT_ID = "design:digest_rules"
ARTIFACT = {
    "id": ARTIFACT_ID,
    "title": "Digest Rules",
    "scope": "Numeric records and their command-line operation.",
    "depends_on": [],
}
SELECTION = {
    "required_artifacts": [
        {
            **ARTIFACT,
            "source": "ai_derived",
            "rationale": "One document covers both operations.",
            "requirement_ids": ["R1", "R2"],
        },
    ],
    "omitted_artifacts": [],
    "unassigned_requirements": [],
}
REPORT = "Required artifact lineup\n- design:digest_rules: Digest Rules"


@pytest.fixture
def greenfield(tmp_path, monkeypatch):
    root = make_stub_project(tmp_path, "mock-ai", greenfield_config={"use_required_artifacts": True})
    config_path = root / "codd" / "codd.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["required_artifacts"] = {"project_type": "generic", "default_artifacts": [ARTIFACT]}
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    prompts, responses, stages = [], [], []
    real_pipeline = pipeline.GreenfieldPipeline
    project = SimpleNamespace(
        root=root, prompts=prompts, responses=responses, stages=stages, actual_generation=False,
    )

    def build_pipeline(**kwargs):
        runners = _fake_runners(stages)
        del runners["plan_runner"]
        if project.actual_generation:
            del runners["wave_lister"], runners["generate_wave_runner"]
        return real_pipeline(**kwargs, **runners)

    def call_ai(self, prompt, *, allow_empty=False):
        assert self.ai_command == "mock-ai"
        assert allow_empty
        prompts.append(prompt)
        return json.dumps(responses.pop(0))

    monkeypatch.setattr(real_pipeline, "_classify_deliverable_surfaces", lambda self, project_root: None)
    monkeypatch.setattr(real_pipeline, "_provision_env", lambda self, project_root: None)
    monkeypatch.setattr(pipeline, "GreenfieldPipeline", build_pipeline)
    monkeypatch.setattr(RequiredArtifactsDeriver, "_call_ai", call_ai)

    def invoke(*flags):
        return CliRunner().invoke(main, ["greenfield", "--path", str(root), "--no-elicit", *flags])

    project.invoke = invoke
    return project


def test_opted_in_greenfield_retries_selection_on_resume_and_reports_the_plan(greenfield, monkeypatch):
    project = greenfield
    invalid = deepcopy(SELECTION)
    invalid["required_artifacts"][0]["requirement_ids"] = ["R999"]
    project.responses.extend([invalid, invalid, invalid, SELECTION])
    lexicon = project.root / LEXICON_FILENAME
    original = b"# Keep this author-maintained section\nnode_vocabulary: []\n"
    lexicon.write_bytes(original)
    planning_calls = []

    def plan_init(project_root, *, ai_command, force, use_required_artifacts):
        planning_calls.append(use_required_artifacts)
        assert force and ai_command == "mock-ai" and use_required_artifacts is True
        data = yaml.safe_load(lexicon.read_text(encoding="utf-8"))
        assert [item["id"] for item in data["required_artifacts"]] == [ARTIFACT_ID]
        assert data["required_artifact_decisions"]["artifact_evidence"] == {ARTIFACT_ID: ["R1", "R2"]}
        return SimpleNamespace(wave_config={"1": []}, required_artifact_report=REPORT)

    monkeypatch.setattr("codd.planner.plan_init", plan_init)
    failed = project.invoke("--use-required-artifacts", "--ai-cmd", "mock-ai")

    assert failed.exit_code == 1, failed.output
    assert "Failed stage: plan" in failed.output
    assert "R999" in failed.output and "Unaccounted requirement R2" in failed.output
    assert "codd greenfield --resume" in failed.output
    assert not planning_calls and not project.stages
    assert len(project.prompts) == 3
    assert lexicon.read_bytes() == original
    assert pipeline.load_session(project.root)["options"]["use_required_artifacts"] is True

    resumed = project.invoke("--resume", "--format", "json")

    assert resumed.exit_code == 0, resumed.output
    assert len(project.prompts) == 4
    assert planning_calls == [True]
    payload = json.loads(resumed.output[resumed.output.index('{\n  "project_root"'):])
    assert payload["status"] == "success"
    assert payload["required_artifact_report"] == REPORT

    replayed = project.invoke("--resume")

    assert replayed.exit_code == 0, replayed.output
    assert REPORT in replayed.output
    assert len(project.prompts) == 4 and planning_calls == [True]


def test_opted_in_greenfield_reuses_the_saved_accounted_empty_selection(greenfield, monkeypatch):
    project = greenfield
    project.actual_generation = True
    project.responses.append({
        "required_artifacts": [],
        "omitted_artifacts": [{"id": ARTIFACT_ID, "rationale": "The operations already exist."}],
        "unassigned_requirements": [
            {"id": item, "rationale": "No design changes are needed."} for item in ("R1", "R2")
        ],
    })
    selected = CliRunner().invoke(main, ["plan", "--path", str(project.root), "--derive", "--account-decisions"])
    assert selected.exit_code == 0, selected.output
    lexicon = project.root / LEXICON_FILENAME
    original = lexicon.read_bytes()
    data = yaml.safe_load(original)
    assert data["required_artifacts"] == []
    assert data["required_artifact_decisions"]["artifact_evidence"] == {}
    planning_calls = []

    def plan_ai(command, prompt):
        assert command == "mock-ai" and "No documents are selected for generation" in prompt
        planning_calls.append(prompt)
        return yaml.safe_dump({
            "wave_config": {},
            "ownership_not_applicable": {
                "owns_operation_flow": "Existing operations need no new design.",
                "owns_actor_facing": "Existing surfaces need no new design.",
            },
        })

    monkeypatch.setattr("codd.generator._invoke_ai_command", plan_ai)
    result = project.invoke("--use-required-artifacts")

    assert result.exit_code == 0, result.output
    assert len(planning_calls) == 1
    assert len(project.prompts) == 1
    assert lexicon.read_bytes() == original
    assert "0 waves generated" in result.output


def test_greenfield_passes_the_selected_rows_to_planning_and_generation(greenfield, monkeypatch):
    project = greenfield
    project.actual_generation = True
    project.responses.append(SELECTION)
    plan_prompts = []

    def plan_ai(command, prompt):
        assert command == "mock-ai"
        assert ARTIFACT_ID in prompt and ARTIFACT["scope"] in prompt
        plan_prompts.append(prompt)
        if prompt.startswith("## Project Lexicon") and "Saved scope for" in prompt:
            return "## Overview\n\nDefine the digest rules here.\n\n```codd-behavior\nowns: {}\ncases: []\n```"
        return yaml.safe_dump({
            "wave_config": {"1": [{
                "node_id": "design:digest",
                "output": "docs/design/digest.md",
                "title": ARTIFACT["title"],
                "required_artifact_id": ARTIFACT_ID,
                "depends_on": [{"id": "req:stub-app-requirements", "relation": "derives_from"}],
            }]},
            "ownership_not_applicable": {
                "owns_operation_flow": "The digest operation only transforms its inputs.",
                "owns_actor_facing": "The plan adds no actor-facing surface or copy.",
            },
        })

    monkeypatch.setattr("codd.generator._invoke_ai_command", plan_ai)
    result = project.invoke("--use-required-artifacts")

    assert result.exit_code == 0, result.output
    assert len(project.prompts) == 1 and len(plan_prompts) == 2
    assert "docs/design/digest.md" in result.output
    assert (project.root / "docs/design/digest.md").is_file()
    config = yaml.safe_load((project.root / "codd/codd.yaml").read_bytes())
    assert config["wave_config"]["1"][0]["required_artifact_id"] == ARTIFACT_ID


def test_greenfield_without_flag_preserves_the_legacy_plan_signature(greenfield, monkeypatch):
    project = greenfield
    planning_calls = []

    def plan_init(project_root, *, ai_command, force):
        planning_calls.append(project_root)
        assert force
        return SimpleNamespace(wave_config={"1": []})

    monkeypatch.setattr("codd.planner.plan_init", plan_init)
    result = project.invoke("--format", "json")

    assert result.exit_code == 0, result.output
    assert planning_calls == [project.root]
    assert not project.prompts
    payload = json.loads(result.output[result.output.index('{\n  "project_root"'):])
    assert "required_artifact_report" not in payload
    assert "use_required_artifacts" not in pipeline.load_session(project.root)["options"]


def test_greenfield_dry_run_announces_artifact_selection_without_deriving(greenfield):
    result = greenfield.invoke("--use-required-artifacts", "--dry-run")

    assert result.exit_code == 0, result.output
    assert "codd plan --derive --account-decisions" in result.output
    assert "codd plan --init --force --use-required-artifacts" in result.output
    assert not greenfield.prompts
    assert pipeline.load_session(greenfield.root) is None


def test_explicit_opt_in_on_resume_persists_while_the_plan_is_incomplete(greenfield, monkeypatch):
    def failed_plan(project_root, *, ai_command, force):
        raise ValueError("The original legacy plan failed.")

    project = greenfield
    monkeypatch.setattr("codd.planner.plan_init", failed_plan)
    first = project.invoke()
    assert first.exit_code == 1 and "The original legacy plan failed." in first.output
    assert "use_required_artifacts" not in pipeline.load_session(project.root)["options"]
    project.responses.extend([{}, {}, {}])

    opted_in = project.invoke("--resume", "--use-required-artifacts")

    assert opted_in.exit_code == 1 and "Artifact derivation failed" in opted_in.output
    assert len(project.prompts) == 3
    assert pipeline.load_session(project.root)["options"]["use_required_artifacts"] is True


def test_explicit_opt_in_cannot_replace_a_completed_legacy_plan_on_resume(greenfield, monkeypatch):
    def plan_init(project_root, *, ai_command, force):
        return SimpleNamespace(wave_config={"1": []})

    project = greenfield
    monkeypatch.setattr("codd.planner.plan_init", plan_init)
    first = project.invoke()
    assert first.exit_code == 0, first.output
    checkpoint = pipeline.session_path(project.root)
    original = checkpoint.read_bytes()

    switched = project.invoke("--resume", "--use-required-artifacts")

    assert switched.exit_code == 1, switched.output
    assert "codd greenfield --use-required-artifacts" in switched.output
    assert "without --resume" in switched.output
    assert not project.prompts
    assert checkpoint.read_bytes() == original
