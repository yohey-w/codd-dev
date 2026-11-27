"""Exercise the saved lineup through plan, derivation and generation."""

from copy import deepcopy
from hashlib import sha256
import json

from click.testing import CliRunner
import pytest
import yaml

from codd import generator
from codd.cli import main
from codd.required_artifacts_deriver import DEFAULTS_DIR, RequiredArtifactsDeriver


REQUIREMENT = "docs/requirements/project.md"
REQ_NODE = "req:project"
PRIMARY = "design:record_rules"
REGISTRY = "test:test_strategy"
CATALOG_SCOPE = "Holds normalization rules. Does not hold storage layouts."
PROJECT_SCOPE = "Define normalization for the imported records."
RATIONALE = "Readers need a single normalization specification."


def _project(root, catalog=None):
    config = {
        "project": {"type": "generic"},
        "scan": {"doc_dirs": ["docs/requirements", "docs/design", "docs/test"]},
        "plan": {"init_retry": {"max_retries": 1}},
    }
    if catalog is not None:
        config["required_artifacts"] = {"default_artifacts": catalog}
    path = root / ".codd/codd.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    doc = root / REQUIREMENT
    doc.parent.mkdir(parents=True)
    doc.write_text(
        "---\ncodd:\n  node_id: req:project\n  type: requirement\n---\n\n"
        "# Project requirements\n\n- Normalize imported records.\n",
        encoding="utf-8",
    )
    return path


def _catalog_row(artifact_id, scope):
    return {"id": artifact_id, "title": artifact_id, "scope": scope}


def _select(root, monkeypatch, catalog, selected, *, rederive=False):
    response = {
        "required_artifacts": [
            {
                "id": row["id"], "title": row["title"], "scope": PROJECT_SCOPE,
                "rationale": RATIONALE, "source": "ai_derived", "requirement_ids": ["R1"],
                "depends_on": row.get("depends_on", []),
            }
            for row in catalog if row["id"] in selected
        ],
        "omitted_artifacts": [
            {"id": row["id"], "rationale": "There is no separate responsibility."}
            for row in catalog if row["id"] not in selected
        ],
        "unassigned_requirements": [] if selected else [
            {"id": "R1", "rationale": "No new design document is needed."}
        ],
    }
    def invoke(self, prompt, *, allow_empty):
        assert self.ai_command == "mock-ai"
        return json.dumps(response)

    monkeypatch.setattr(RequiredArtifactsDeriver, "_call_ai", invoke)
    args = ["--derive", "--account-decisions"]
    if rederive:
        args.append("--rederive")
    result = _cli(root, *args)
    assert result.exit_code == 0, result.output


def _cli(root, *args):
    return CliRunner().invoke(main, ["plan", *args, "--path", str(root), "--ai-cmd", "mock-ai"])


def _entry(row=PRIMARY, node="design:rules", output="docs/design/rules.md", *dependencies):
    return {
        "node_id": node, "output": output, "title": "Rules",
        "required_artifact_id": row,
        "depends_on": [{"id": dep, "relation": "depends_on"} for dep in dependencies or (REQ_NODE,)],
        "behavior_owners": [],
        "owns_operation_flow": False,
    }


def _saved_plan(waves, reasons=None):
    if reasons is None:
        reasons = {
            "owns_operation_flow": "Normalization is a pure transformation.",
            "owns_actor_facing": "The requirements specify no actor-facing surface.",
        }
    return {"wave_config": waves, "ownership_not_applicable": reasons}


def _replies(monkeypatch, *responses):
    prompts = []
    replies = iter(responses)

    def invoke(command, prompt):
        assert command == "mock-ai"
        prompts.append(prompt)
        response = next(replies)
        return response if isinstance(response, str) else yaml.safe_dump(response)

    monkeypatch.setattr(generator, "_invoke_ai_command", invoke)
    return prompts


def _entries(path):
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [entry for entries in config["wave_config"].values() for entry in entries]


def test_saved_lineup_rejects_reselection_during_planning(tmp_path, monkeypatch):
    replacement = "design:boundaries"
    catalog = [_catalog_row(PRIMARY, CATALOG_SCOPE), _catalog_row(replacement, "Boundary decisions.")]
    config_path = _project(tmp_path, catalog)
    _select(tmp_path, monkeypatch, catalog, {PRIMARY})
    original = config_path.read_bytes()

    def invoke(command, prompt):
        assert command == "mock-ai"
        _select(tmp_path, monkeypatch, catalog, {replacement}, rederive=True)
        return yaml.safe_dump(_saved_plan({"1": [_entry()]}))

    monkeypatch.setattr(generator, "_invoke_ai_command", invoke)
    result = _cli(tmp_path, "--init", "--use-required-artifacts", "--no-contract-gate")

    assert result.exit_code != 0, result.output
    assert "The saved artifact selection changed during planning." in result.output
    assert config_path.read_bytes() == original
    lexicon = yaml.safe_load((tmp_path / "project_lexicon.yaml").read_bytes())
    assert [row["id"] for row in lexicon["required_artifacts"]] == [replacement]


def test_unselected_lineup_plan_and_generate_prompt_bytes(tmp_path, monkeypatch):
    catalog = [_catalog_row(PRIMARY, CATALOG_SCOPE)]
    config_path = _project(tmp_path, catalog)
    _select(tmp_path, monkeypatch, catalog, {PRIMARY})
    prompts = _replies(
        monkeypatch, {"1": [_entry()]},
        "## Overview\n\nNormalize records.\n\n```codd-behavior\nowns: {}\ncases: []\n```",
    )
    result = _cli(tmp_path, "--init", "--no-contract-gate")
    assert result.exit_code == 0, result.output
    assert "required_artifact_id" not in _entries(config_path)[0]
    generator.generate_wave(tmp_path, 1, ai_command="mock-ai")
    assert [sha256(prompt.encode()).hexdigest() for prompt in prompts] == [
        "2332483f8fd749676b4724d82f8e85111e4fcf17936caa7ab8dec58cecf08d24",
        "32cd202eee40538216f791289b9f467bb7f54f91f6b56afbc7260b95471968e2",
    ]


@pytest.mark.parametrize("problem", ["extra", "missing", "duplicate", "duplicate_wave"])
def test_saved_lineup_retries_with_specific_feedback(tmp_path, monkeypatch, problem):
    extra = "design:extra"
    second = "design:boundaries"
    catalog = [_catalog_row(PRIMARY, CATALOG_SCOPE), _catalog_row(second, "Boundary decisions.")]
    config_path = _project(tmp_path, catalog)
    _select(tmp_path, monkeypatch, catalog, {PRIMARY, second})
    correct = [_entry(), _entry(second, "design:boundaries", "docs/design/boundaries.md")]
    unexpected = _entry(extra, "design:extra", "docs/design/extra.md")
    duplicate = _entry(PRIMARY, "design:repeated", "docs/design/repeated.md")
    invalid, evidence = {
        "extra": (correct + [unexpected], ("extra", unexpected["output"])),
        "missing": (correct[:1], ("missing", second)),
        "duplicate": (correct + [duplicate], ("duplicate", PRIMARY)),
        "duplicate_wave": (
            yaml.safe_dump({"1": [unexpected]}) + yaml.safe_dump({"1": correct}),
            ("duplicate", "1"),
        ),
    }[problem]
    prompts = _replies(
        monkeypatch, _saved_plan({"1": invalid}) if isinstance(invalid, list) else invalid,
        _saved_plan({"1": correct}),
    )

    result = _cli(tmp_path, "--init", "--use-required-artifacts", "--no-contract-gate")

    assert result.exit_code == 0, result.output
    assert len(prompts) == 2 and "PREVIOUS RESPONSE WAS REJECTED" in prompts[1]
    feedback = prompts[1].split("PREVIOUS RESPONSE WAS REJECTED", 1)[1].lower()
    assert all(item in feedback for item in evidence)
    assert [entry["required_artifact_id"] for entry in _entries(config_path)[:2]] == [PRIMARY, second]


def test_saved_lineup_retry_exhaustion_preserves_config(tmp_path, monkeypatch):
    catalog = [_catalog_row(PRIMARY, CATALOG_SCOPE)]
    config_path = _project(tmp_path, catalog)
    _select(tmp_path, monkeypatch, catalog, {PRIMARY})
    original = config_path.read_bytes()
    invalid = _saved_plan({"1": [_entry("design:extra")]})
    prompts = _replies(monkeypatch, invalid, invalid)

    result = _cli(tmp_path, "--init", "--use-required-artifacts")

    assert result.exit_code != 0
    assert len(prompts) == 2
    assert PRIMARY in result.output and "docs/design/rules.md" in result.output
    assert config_path.read_bytes() == original


def test_bundled_lineup_uses_existing_requirements_and_reports_registry(tmp_path, monkeypatch):
    catalog = yaml.safe_load((DEFAULTS_DIR / "generic.yaml").read_text())["default_artifacts"]
    chosen = {row["id"] for row in catalog if row.get("always_required")}
    config_path = _project(tmp_path)
    _select(tmp_path, monkeypatch, catalog, chosen)
    docs = [
        _entry(row["id"], row["id"].replace("_", "-"), f"docs/design/{row['id'].split(':')[1]}.md")
        for row in catalog if row["id"] in chosen - {"design:requirements"}
    ]
    prompts = _replies(monkeypatch, _saved_plan({"1": docs}))

    result = _cli(tmp_path, "--init", "--use-required-artifacts", "--no-contract-gate")

    assert result.exit_code == 0, result.output
    entries = _entries(config_path)
    assert len(entries) == len(docs) + 1
    assert entries[-1]["output"] == "docs/test/test_strategy.md"
    assert "required_artifact_id" not in entries[-1]
    assert REQUIREMENT in prompts[0] and "design:requirements" in prompts[0]
    assert "Existing requirements" in result.output and REQUIREMENT in result.output
    assert "CoDD-required outside the selection" in result.output
    assert "There is no separate responsibility." in result.output


def test_saved_lineup_replans_the_previously_pinned_canonical_registry(tmp_path, monkeypatch):
    catalog = [_catalog_row(PRIMARY, CATALOG_SCOPE)]
    config_path = _project(tmp_path, catalog)
    _select(tmp_path, monkeypatch, catalog, {PRIMARY})
    response = _saved_plan({"1": [_entry()]})
    _replies(monkeypatch, response, response)

    first = _cli(tmp_path, "--init", "--use-required-artifacts", "--no-contract-gate")
    second = _cli(tmp_path, "--init", "--force", "--use-required-artifacts", "--no-contract-gate")

    assert first.exit_code == second.exit_code == 0, second.output
    assert [entry["output"] for entry in _entries(config_path)] == [
        "docs/design/rules.md", "docs/test/test_strategy.md",
    ]
    assert "docs/test/test_strategy.md — canonical verifiable-behavior registry" in second.output


def test_generate_all_waves_preserves_an_intentionally_empty_saved_lineup(tmp_path, monkeypatch):
    catalog = [_catalog_row(PRIMARY, CATALOG_SCOPE)]
    path = _project(tmp_path, catalog)
    config = yaml.safe_load(path.read_bytes())
    config["test_coverage"] = {"gate": False}
    path.write_text(yaml.safe_dump(config))
    _select(tmp_path, monkeypatch, catalog, set())
    prompts = _replies(monkeypatch, _saved_plan({}))
    planned = _cli(tmp_path, "--init", "--use-required-artifacts", "--no-contract-gate")
    assert planned.exit_code == 0, planned.output

    generated = CliRunner().invoke(main, [
        "generate", "--all-waves", "--path", str(tmp_path), "--ai-cmd", "mock-ai", "--no-contract-gate",
    ])

    assert generated.exit_code == 0, generated.output
    assert "0 generated" in generated.output
    assert len(prompts) == 1


@pytest.mark.parametrize("registry_id", [REGISTRY, "verification:charter"])
def test_custom_lineup_registry_and_scope_survive_catalog_edits(tmp_path, monkeypatch, registry_id):
    registry = _catalog_row(registry_id, "Verification boundaries.")
    if registry_id != REGISTRY:
        registry["output"] = "docs/test/test_strategy.md"
    catalog = [_catalog_row(PRIMARY, CATALOG_SCOPE), registry]
    config_path = _project(tmp_path, catalog)
    _select(tmp_path, monkeypatch, catalog, {PRIMARY, registry_id})
    config = yaml.safe_load(config_path.read_bytes())
    edited = deepcopy(config)
    edited["required_artifacts"]["default_artifacts"][0]["scope"] = "Changed catalog scope."
    config_path.write_text(yaml.safe_dump(edited), encoding="utf-8")
    docs = {
        "1": [_entry()],
        "2": [_entry(registry_id, "test:test-strategy", "docs/test/test_strategy.md", "design:rules")],
    }
    prompts = _replies(
        monkeypatch, _saved_plan(docs),
        "## Overview\n\nNormalize records.\n\n```codd-behavior\nowns: {}\ncases: []\n```",
    )

    result = _cli(tmp_path, "--init", "--use-required-artifacts", "--no-contract-gate")

    assert result.exit_code == 0, result.output
    entries = _entries(config_path)
    assert [entry["required_artifact_id"] for entry in entries] == [PRIMARY, registry_id]
    assert "docs/test/test_strategy.md" in result.output
    assert entries[0]["required_artifact_scope"]["catalog_scope"] == CATALOG_SCOPE
    generator.generate_wave(tmp_path, 1, ai_command="mock-ai")
    assert len(prompts) == 2
    for snippet in (CATALOG_SCOPE, PROJECT_SCOPE, RATIONALE):
        assert snippet in prompts[0] and snippet in prompts[1]
    assert "cite" in prompts[1].lower()
    assert "Changed catalog scope." not in prompts[1]


@pytest.mark.parametrize("change", ["heading", "line_endings", "new_document", "removed_document", "legacy"])
def test_saved_lineup_rejects_stale_or_unverifiable_requirements(tmp_path, monkeypatch, change):
    catalog = [_catalog_row(PRIMARY, CATALOG_SCOPE)]
    config_path = _project(tmp_path, catalog)
    _select(tmp_path, monkeypatch, catalog, {PRIMARY})
    requirement = tmp_path / REQUIREMENT
    if change == "heading":
        requirement.write_text(requirement.read_text().replace("# Project", "# Revised project"))
    elif change == "line_endings":
        requirement.write_bytes(requirement.read_bytes().replace(b"\n", b"\r\n"))
    elif change == "new_document":
        requirement.with_name("other.md").write_text(requirement.read_text().replace(REQ_NODE, "req:other"))
    elif change == "removed_document":
        requirement.unlink()
    else:
        lexicon = tmp_path / "project_lexicon.yaml"
        data = yaml.safe_load(lexicon.read_bytes())
        data.pop("required_artifact_snapshot", None)
        lexicon.write_text(yaml.safe_dump(data))
    original = config_path.read_bytes()
    prompts = _replies(monkeypatch)

    result = _cli(tmp_path, "--init", "--use-required-artifacts")

    assert result.exit_code != 0
    assert "--rederive" in result.output
    assert config_path.read_bytes() == original
    assert not prompts


def test_lineup_keeps_the_planned_roles_with_their_own_documents(tmp_path, monkeypatch):
    second = "design:boundaries"
    catalog = [_catalog_row(PRIMARY, CATALOG_SCOPE), _catalog_row(second, "Boundary decisions.")]
    path = _project(tmp_path, catalog)
    config = yaml.safe_load(path.read_bytes())
    config["project"]["capabilities"] = {"user_interface": True}
    path.write_text(yaml.safe_dump(config))
    _select(tmp_path, monkeypatch, catalog, {PRIMARY, second})
    model = _entry() | {"owns_operation_flow": True, "behavior_owners": ["normalize-record"]}
    surfaces = _entry(second, "design:boundaries", "docs/design/boundaries.md", "design:rules")
    surfaces["owns_actor_facing"] = True
    _replies(
        monkeypatch,
        _saved_plan({"1": [model, surfaces | {"owns_operation_flow": True}]}, {}),
        _saved_plan({"1": [model], "2": [surfaces]}, {}),
    )

    result = _cli(tmp_path, "--init", "--use-required-artifacts", "--no-contract-gate")

    assert result.exit_code == 0, result.output
    first, second, _ = generator._load_wave_artifacts(yaml.safe_load(path.read_bytes()))
    assert first.behavior_owners == ("normalize-record",) and first.owns_operation_flow
    assert second.owns_actor_facing and not second.owns_operation_flow
    first_prompt = generator._build_generation_prompt(first, [], [], behavior_contract=True)
    second_prompt = generator._build_generation_prompt(
        second, [], [], behavior_contract=True, operation_flow_owner_elsewhere=True,
    )
    assert "Operational Behavior Model (DESIGN-TIME" in first_prompt
    assert "Actor-Facing Surface/Copy Obligations (DESIGN-TIME" not in first_prompt
    assert "Actor-Facing Surface/Copy Obligations (DESIGN-TIME" in second_prompt
    assert "Cite its operation ids" in second_prompt


def test_accounted_lineup_discovers_requirements_from_the_configured_scan(tmp_path, monkeypatch):
    catalog = [_catalog_row(PRIMARY, CATALOG_SCOPE)]
    _project(tmp_path, catalog)
    requirement = tmp_path / REQUIREMENT
    custom_dir = tmp_path / "docs/design"
    custom_dir.mkdir()
    requirement.rename(custom_dir / "spec.md")

    _select(tmp_path, monkeypatch, catalog, {PRIMARY})
    _replies(monkeypatch, _saved_plan({"1": [_entry()]}))

    result = _cli(tmp_path, "--init", "--use-required-artifacts", "--no-contract-gate")
    assert result.exit_code == 0, result.output


def test_saved_lineup_rejects_artifacts_edited_after_derivation(tmp_path, monkeypatch):
    catalog = [_catalog_row(PRIMARY, CATALOG_SCOPE)]
    config_path = _project(tmp_path, catalog)
    _select(tmp_path, monkeypatch, catalog, {PRIMARY})
    lexicon_path = tmp_path / "project_lexicon.yaml"
    lexicon = yaml.safe_load(lexicon_path.read_bytes())
    lexicon["required_artifacts"][0]["scope"] = "Include record validation as well as normalization."
    lexicon_path.write_text(yaml.safe_dump(lexicon), encoding="utf-8")
    original = config_path.read_bytes()
    prompts = _replies(monkeypatch, {"1": [_entry()]})

    result = _cli(tmp_path, "--init", "--use-required-artifacts", "--no-contract-gate")

    assert result.exit_code != 0
    assert "no matching derivation snapshot" in result.output and "--rederive" in result.output
    assert not prompts
    assert config_path.read_bytes() == original


def test_saved_lineup_only_prompts_the_flow_owner_to_define_the_model(tmp_path, monkeypatch):
    consumer_id = "design:boundaries"
    catalog = [_catalog_row(PRIMARY, CATALOG_SCOPE), _catalog_row(consumer_id, "Boundary decisions.")]
    _project(tmp_path, catalog)
    _select(tmp_path, monkeypatch, catalog, {PRIMARY, consumer_id})
    owner = _entry() | {"owns_operation_flow": True}
    consumer = _entry(consumer_id, "design:boundaries", "docs/design/boundaries.md", owner["node_id"])
    behavior = "\n\n```codd-behavior\nowns: {}\ncases: []\n```"
    prompts = _replies(
        monkeypatch,
        _saved_plan({"1": [owner], "2": [consumer]}, {"owns_actor_facing": "There is no actor-facing surface."}),
        "## Overview\n\nNormalize records." + behavior
        + "\n\n```yaml\noperation_flow:\n  operations:\n    - id: normalize-record\n```",
        "## Overview\n\nApply normalize-record from docs/design/rules.md." + behavior,
    )

    result = _cli(tmp_path, "--init", "--use-required-artifacts", "--no-contract-gate")
    assert result.exit_code == 0, result.output
    owner_result, = generator.generate_wave(tmp_path, 1, ai_command="mock-ai")
    consumer_result, = generator.generate_wave(tmp_path, 2, ai_command="mock-ai")

    assert owner_result.status == consumer_result.status == "generated"
    _, owner_prompt, consumer_prompt = prompts
    guidance = "Operational Behavior Model (DESIGN-TIME"
    assert guidance in owner_prompt
    assert guidance not in consumer_prompt
    assert "Cite its operation ids. Do not emit operation_flow here." in consumer_prompt


def test_saved_lineup_retries_when_the_selected_registry_is_not_canonical(tmp_path, monkeypatch):
    catalog = [_catalog_row(REGISTRY, "Verification boundaries.")]
    config_path = _project(tmp_path, catalog)
    _select(tmp_path, monkeypatch, catalog, {REGISTRY})
    invalid = _entry(REGISTRY, "test:verification", "docs/test/verification.md")
    canonical = _entry(REGISTRY, "test:test-strategy", "docs/test/test_strategy.md")
    prompts = _replies(monkeypatch, _saved_plan({"1": [invalid]}), _saved_plan({"1": [canonical]}))

    result = _cli(tmp_path, "--init", "--use-required-artifacts", "--no-contract-gate")

    assert result.exit_code == 0, result.output
    assert len(prompts) == 2
    retry_prompt = prompts[1]
    assert "PREVIOUS RESPONSE WAS REJECTED" in retry_prompt
    assert f"{invalid['output']}: row {REGISTRY} must use {canonical['output']}" in retry_prompt
    entry, = _entries(config_path)
    assert entry["required_artifact_id"] == REGISTRY
    assert entry["output"] == canonical["output"]


def test_saved_lineup_retries_when_two_documents_claim_actor_facing_ownership(tmp_path, monkeypatch):
    consumer_id = "design:boundaries"
    catalog = [_catalog_row(PRIMARY, CATALOG_SCOPE), _catalog_row(consumer_id, "Boundary decisions.")]
    config_path = _project(tmp_path, catalog)
    config = yaml.safe_load(config_path.read_bytes())
    config["project"]["capabilities"] = {"user_interface": True}
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    _select(tmp_path, monkeypatch, catalog, {PRIMARY, consumer_id})
    owner = _entry() | {"owns_actor_facing": True}
    consumer = _entry(consumer_id, "design:boundaries", "docs/design/boundaries.md")
    reasons = {"owns_operation_flow": "Normalization is a pure transformation."}
    prompts = _replies(
        monkeypatch,
        _saved_plan({"1": [owner, consumer | {"owns_actor_facing": True}]}, reasons),
        _saved_plan({"1": [owner, consumer]}, reasons),
    )

    result = _cli(tmp_path, "--init", "--use-required-artifacts", "--no-contract-gate")

    assert result.exit_code == 0, result.output
    assert len(prompts) == 2
    retry_prompt = prompts[1]
    assert "PREVIOUS RESPONSE WAS REJECTED" in retry_prompt
    assert f"multiple actor-facing surface/copy owners: {owner['output']}, {consumer['output']}" in retry_prompt
    owners = [entry["output"] for entry in _entries(config_path) if entry.get("owns_actor_facing")]
    assert owners == [owner["output"]]


@pytest.mark.parametrize("custom_vocabulary", [False, True])
def test_saved_lineup_fills_missing_lexicon_sections_before_plan_and_generate(tmp_path, monkeypatch, custom_vocabulary):
    catalog = [_catalog_row(PRIMARY, CATALOG_SCOPE)]
    _project(tmp_path, catalog)
    lexicon_path = tmp_path / "project_lexicon.yaml"
    custom = {"id": "record_rule", "description": "A human-authored vocabulary entry."}
    if custom_vocabulary:
        lexicon_path.write_text(yaml.safe_dump({"node_vocabulary": [custom], "project_id": "my-project"}))
    _select(tmp_path, monkeypatch, catalog, {PRIMARY})
    saved = yaml.safe_load(lexicon_path.read_bytes())
    prompts = _replies(
        monkeypatch, _saved_plan({"1": [_entry()]}),
        "## Overview\n\nNormalize records.\n\n```codd-behavior\nowns: {}\ncases: []\n```",
    )

    planned = _cli(tmp_path, "--init", "--use-required-artifacts", "--no-contract-gate")
    assert planned.exit_code == 0, planned.output
    generated, = generator.generate_wave(tmp_path, 1, ai_command="mock-ai")

    assert generated.status == "generated"
    filled = yaml.safe_load(lexicon_path.read_bytes())
    for key in ("required_artifacts", "required_artifact_decisions", "required_artifact_snapshot"):
        assert filled[key] == saved[key]
    if custom_vocabulary:
        assert filled["node_vocabulary"] == [custom]
        assert filled["project_id"] == "my-project"
    else:
        assert len(filled["node_vocabulary"]) == 7
    assert len(filled["naming_conventions"]) == 4
    assert len(filled["design_principles"]) == 3
    assert len(filled["failure_modes"]) == 3
    assert len(prompts) == 2
    for prompt in prompts:
        assert (custom["description"] if custom_vocabulary else "Persistent database table") in prompt
        assert "Use one canonical name for the same domain concept" in prompt


def test_saved_lineup_retries_when_required_roles_are_unassigned(tmp_path, monkeypatch):
    catalog = [_catalog_row(PRIMARY, CATALOG_SCOPE)]
    config_path = _project(tmp_path, catalog)
    _select(tmp_path, monkeypatch, catalog, {PRIMARY})
    unassigned = _entry()
    del unassigned["owns_operation_flow"]
    owner = unassigned | {"owns_operation_flow": True, "owns_actor_facing": True}
    prompts = _replies(monkeypatch, _saved_plan({"1": [unassigned]}, {}), _saved_plan({"1": [owner]}, {}))

    result = _cli(tmp_path, "--init", "--use-required-artifacts", "--no-contract-gate")

    assert result.exit_code == 0, result.output
    assert len(prompts) == 2
    feedback = prompts[1].split("PREVIOUS RESPONSE WAS REJECTED", 1)[1]
    assert "owns_operation_flow" in feedback and "owns_actor_facing" in feedback
    assert "ownership_not_applicable" in feedback
    planned = _entries(config_path)[0]
    assert planned["owns_operation_flow"] is planned["owns_actor_facing"] is True


@pytest.mark.parametrize("requirement_text", [
    "- Expose a pure function that normalizes records.",
    "| Excluded features |\n| --- |\n| state transitions, access control and UI screens |",
    "## 対象外\n\n- 状態遷移、永続化、画面と表示文言",
    "- La bibliothèque transforme les données sans interaction.",
])
def test_saved_lineup_stateless_library_does_not_need_roles(tmp_path, monkeypatch, requirement_text):
    catalog = [_catalog_row(PRIMARY, CATALOG_SCOPE)]
    _project(tmp_path, catalog)
    requirement = tmp_path / REQUIREMENT
    requirement.write_text(requirement.read_text().replace("- Normalize imported records.", requirement_text))
    _select(tmp_path, monkeypatch, catalog, {PRIMARY})
    reasons = {
        "owns_operation_flow": "The library only transforms its inputs.",
        "owns_actor_facing": "The library exposes a programming interface only.",
    }
    prompts = _replies(
        monkeypatch, {"wave_config": {"1": [_entry()]}, "ownership_not_applicable": reasons},
        "## Overview\n\nNormalize records.\n\n```codd-behavior\nowns: {}\ncases: []\n```",
        "## Overview\n\nVerify normalization.\n\n```codd-behavior\nowns: {}\ncases: []\n```"
        "\n\n```codd-test-environment\nversion: 1\npackages: []\nassets: []\n```",
    )

    result = _cli(tmp_path, "--init", "--use-required-artifacts", "--no-contract-gate")
    assert result.exit_code == 0, result.output
    for reason in reasons.values():
        assert f"Not applicable: {reason}" in result.output
    for wave in (1, 2):
        generated, = generator.generate_wave(tmp_path, wave, ai_command="mock-ai")
        assert generated.status == "generated"

    assert len(prompts) == 3
    for prompt in prompts[1:]:
        assert "Operational Behavior Model (DESIGN-TIME" not in prompt
        assert "Actor-Facing Surface/Copy Obligations (DESIGN-TIME" not in prompt


@pytest.mark.parametrize("source", [
    "config", "config_consistency_disabled", "frontmatter", "codd_frontmatter", "body", "body_ui_pattern",
])
def test_saved_lineup_retries_when_declared_operation_flow_is_marked_unnecessary(tmp_path, monkeypatch, source):
    catalog = [_catalog_row(PRIMARY, CATALOG_SCOPE)]
    config_path = _project(tmp_path, catalog)
    requirement = tmp_path / REQUIREMENT
    flow = {"operations": [{"id": "normalize"}]}
    if source == "body_ui_pattern":
        flow["operations"][0]["ui_pattern"] = "single_form"
    declaration = yaml.safe_dump({"operation_flow": flow})
    if source.startswith("config"):
        config = yaml.safe_load(config_path.read_bytes())
        if source == "config_consistency_disabled":
            config["generate"] = {"design_consistency": False}
        config_path.write_text(yaml.safe_dump(config | {"operation_flow": flow}))
    elif source == "frontmatter":
        requirement.write_text(requirement.read_text().replace("codd:\n", declaration + "codd:\n", 1))
    elif source == "codd_frontmatter":
        addition = "".join(f"  {line}\n" for line in declaration.splitlines())
        requirement.write_text(requirement.read_text().replace("codd:\n", "codd:\n" + addition, 1))
    else:
        requirement.write_text(requirement.read_text().replace(
            "- Normalize imported records.", f"```yaml\n{declaration}```",
        ))
    _select(tmp_path, monkeypatch, catalog, {PRIMARY})
    actor_reason = {"owns_actor_facing": "There is no actor-facing surface."}
    owner = _entry() | {"owns_operation_flow": True}
    if source == "body_ui_pattern":
        owner["owns_actor_facing"] = True
    prompts = _replies(
        monkeypatch,
        _saved_plan({"1": [_entry()]}, actor_reason | {
            "owns_operation_flow": "No model needed.",
        }),
        _saved_plan({"1": [owner]}, {} if source == "body_ui_pattern" else actor_reason),
        "## Overview\n\nNormalize records.\n\n```yaml\noperation_flow:\n  operations:\n  - id: normalize\n```",
    )

    result = _cli(tmp_path, "--init", "--use-required-artifacts", "--no-contract-gate")

    assert result.exit_code == 0, result.output
    assert len(prompts) == 2
    feedback = prompts[1].split("PREVIOUS RESPONSE WAS REJECTED", 1)[1]
    assert ("codd.yaml" if source.startswith("config") else REQUIREMENT) in feedback
    assert "declares operation_flow" in feedback
    assert _entries(config_path)[0]["owns_operation_flow"] is True
    assert f"Operational model — {owner['output']}" in result.output
    if source == "body_ui_pattern":
        assert "declares operation_flow with ui_pattern" in feedback
        assert f"Actor-facing surfaces and copy — {owner['output']}" in result.output
    if source == "config_consistency_disabled":
        generated, = generator.generate_wave(tmp_path, 1, ai_command="mock-ai")
        assert generated.status == "generated"
        assert "Operational Behavior Model (DESIGN-TIME" in prompts[2]


@pytest.mark.parametrize("problem", ["both", "blank_reason", "unknown_owner", "unknown_role"])
def test_saved_lineup_retries_invalid_ownership_decisions(tmp_path, monkeypatch, problem):
    catalog = [_catalog_row(PRIMARY, CATALOG_SCOPE)]
    _project(tmp_path, catalog)
    _select(tmp_path, monkeypatch, catalog, {PRIMARY})
    actor_reason = {"owns_actor_facing": "There is no actor-facing surface."}
    owner = _entry() | {"owns_operation_flow": True}
    entry, reasons, evidence = {
        "both": (owner, actor_reason | {"owns_operation_flow": "No model needed."}, "both"),
        "blank_reason": (_entry(), actor_reason | {"owns_operation_flow": " "}, "non-blank reason"),
        "unknown_owner": (owner | {"required_artifact_id": "design:absent"}, actor_reason, "design:absent"),
        "unknown_role": (owner, actor_reason | {"owns_everything": "Not a role."}, "owns_everything"),
    }[problem]
    prompts = _replies(
        monkeypatch,
        {"wave_config": {"1": [entry]}, "ownership_not_applicable": reasons},
        {"wave_config": {"1": [owner]}, "ownership_not_applicable": actor_reason},
    )

    result = _cli(tmp_path, "--init", "--use-required-artifacts", "--no-contract-gate")

    assert result.exit_code == 0, result.output
    assert len(prompts) == 2
    assert evidence in prompts[1].split("PREVIOUS RESPONSE WAS REJECTED", 1)[1]
