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


API_ROW = "design:api_design"
FLOW_ROW = "design:screen_flow_design"
API_NODE = "design:http-contract"
FLOW_NODE = "design:navigation"


def _ordering_project(root, *, web):
    catalog = None if web else [_catalog_row(API_ROW, "API contract"), _catalog_row(FLOW_ROW, "Navigation")]
    config_path = _project(root, catalog)
    if web:
        config = yaml.safe_load(config_path.read_bytes())
        config["project"]["type"] = "web"
        config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        catalog = RequiredArtifactsDeriver(root)._load_defaults("web")
    return config_path, catalog


def _ordering_selection(root, monkeypatch, catalog, selected, *, accounted=True):
    selected = selected | {row["id"] for row in catalog if row.get("always_required")}
    responses = {
        "required_artifacts": [
            {
                "id": row["id"], "title": row["title"], "scope": PROJECT_SCOPE,
                "source": "ai_derived", "rationale": RATIONALE, "depends_on": [], "requirement_ids": ["R1"],
            }
            for row in catalog if row["id"] in selected
        ],
        "omitted_artifacts": [
            {"id": row["id"], "rationale": "Separate coverage is unnecessary."}
            for row in catalog if row["id"] not in selected
        ],
        "unassigned_requirements": [],
    }
    calls = []

    def invoke(self, prompt, **kwargs):
        calls.append((self.ai_command, prompt))
        return json.dumps(responses)

    monkeypatch.setattr(RequiredArtifactsDeriver, "_call_ai", invoke)
    flags = ["--account-decisions"] if accounted else []
    result = _cli(root, "--derive", *flags)
    assert result.exit_code == 0, result.output
    assert len(calls) == 1 and calls[0][0] == "mock-ai"
    return selected, result.output, calls[0][1]


def _ordering_entry(artifact_id, *dependencies):
    node = {API_ROW: API_NODE, FLOW_ROW: FLOW_NODE}.get(artifact_id, artifact_id)
    return _entry(artifact_id, node, f"docs/design/{node.split(':')[1]}.md", *dependencies)


@pytest.mark.parametrize(("case", "expected"), [
    ("no_rule", "84d84012fd4c2eadb5468f502998411b9aa8e3c83b4048b386e1b8bd798835be"),
    ("api_only", "ac94ec501ad8df97cea8b7b533f5bc6ce72b302891dacd6d08990f949cf9c681"),
    ("flow_only", "faa66bc6fcc2bb626b00ec1e4678c6e6768883d34ac3b96de2717bba134420aa"),
    ("plain_init", "615a116a05a0047fa415303effa0d551b82b9bad64e1ec9eadf3d19c78dedcea"),
])
def test_ordering_unaffected_plans_match_badf2db(tmp_path, monkeypatch, case, expected):
    config_path, catalog = _ordering_project(tmp_path, web=case != "no_rule")
    # A complete fixture lexicon keeps draft timestamps out of the captured prompt.
    lexicon = {
        "node_vocabulary": [{"id": "record", "description": "Imported record"}],
        "naming_conventions": [{"id": "snake", "regex": "[a-z_]+"}],
        "design_principles": ["Keep transformations reproducible."],
        "failure_modes": [{"id": "bad_input", "description": "Malformed record"}],
    }
    (tmp_path / "project_lexicon.yaml").write_text(yaml.safe_dump(lexicon), encoding="utf-8")
    chosen = {API_ROW, FLOW_ROW}
    if case == "api_only":
        chosen = {API_ROW}
    elif case == "flow_only":
        chosen = {FLOW_ROW}
    selected, selection_output, selection_prompt = _ordering_selection(tmp_path, monkeypatch, catalog, chosen)
    waves = {"2": [_ordering_entry(row) for row in sorted(selected - {"design:requirements"})]}
    saved = case != "plain_init"
    prompts = _replies(monkeypatch, _saved_plan(waves) if saved else waves)
    flags = ["--use-required-artifacts"] if saved else []

    result = _cli(tmp_path, "--init", *flags, "--no-contract-gate")

    assert result.exit_code == 0, result.output
    assert len(prompts) == 1
    wave_bytes = config_path.read_bytes().split(b"\nwave_config:\n", 1)[1].split(b"\ntest_coverage:", 1)[0]
    # Captured on badf2db with these fixture inputs. No checkout, remote or Git config is needed.
    trace = [selection_prompt.encode(), selection_output.encode(), prompts[0].encode(), result.output.encode()]
    trace.append(wave_bytes)
    assert sha256(b"\0".join(trace)).hexdigest() == expected


def test_ordering_legacy_selection_prompt_matches_badf2db(tmp_path, monkeypatch):
    _, catalog = _ordering_project(tmp_path, web=True)
    _, _, prompt = _ordering_selection(tmp_path, monkeypatch, catalog, {API_ROW}, accounted=False)
    assert sha256(prompt.encode()).hexdigest() == "1ed9c183ad500327769d72c7ef3be3f566718b801f32a76f2f536ce360d8f615"


@pytest.mark.parametrize("dependency_already_planned", [False, True])
def test_web_ordering_moves_only_required_waves(tmp_path, monkeypatch, dependency_already_planned):
    config_path, catalog = _ordering_project(tmp_path, web=True)
    transitions = "design:screen_transitions"
    consumer = "design:auth_authorization_design"
    later = "design:database_design"
    selected, _, selection_prompt = _ordering_selection(
        tmp_path, monkeypatch, catalog, {API_ROW, FLOW_ROW, transitions, consumer, later},
    )
    api = _ordering_entry(API_ROW, *((FLOW_NODE,) if dependency_already_planned else ()))
    flow = _ordering_entry(FLOW_ROW) | {"owns_actor_facing": True, "owns_operation_flow": True}
    unaffected = selected - {"design:requirements", API_ROW, FLOW_ROW, transitions, consumer, later}
    prompts = _replies(monkeypatch, _saved_plan({
        "4": [api, flow, *(_ordering_entry(row) for row in sorted(unaffected))],
        "5": [_ordering_entry(transitions, API_NODE, FLOW_NODE)],
        "6": [_ordering_entry(consumer, transitions)],
        "8": [_ordering_entry(later, transitions)],
    }, {}))

    result = _cli(tmp_path, "--init", "--use-required-artifacts", "--no-contract-gate")

    assert result.exit_code == 0, result.output
    assert len(prompts) == 1
    assert all("ordered_after" not in prompt for prompt in [selection_prompt, *prompts])
    waves = yaml.safe_load(config_path.read_bytes())["wave_config"]
    assert [entry["node_id"] for entry in waves["4"]] == [FLOW_NODE, *sorted(unaffected)]
    api_entry, = waves["5"]
    assert api_entry["node_id"] == API_NODE
    assert [dep["id"] for dep in api_entry["depends_on"]].count(FLOW_NODE) == 1
    assert [entry["node_id"] for entry in waves["6"]] == [transitions]
    assert [entry["node_id"] for entry in waves["7"]] == [consumer]
    assert [entry["node_id"] for entry in waves["8"]] == [later]
    reports = [line for line in result.output.splitlines() if "ordered_after" in line]
    # The web catalog chains screen flow -> API -> screen transitions; both rules report.
    assert len(reports) == 2
    assert API_ROW in reports[0] and FLOW_ROW in reports[0]
    assert transitions in reports[1] and API_ROW in reports[1]


@pytest.mark.parametrize("has_dependency", [False, True])
def test_ordering_of_already_separated_waves(tmp_path, monkeypatch, has_dependency):
    base, final = "design:base", "design:final"
    catalog = [
        _catalog_row(base, "Base design"),
        _catalog_row(final, "Final design") | {"ordered_after": [base]},
    ]
    config_path = _project(tmp_path, catalog)
    _ordering_selection(tmp_path, monkeypatch, catalog, {base, final})
    final_dependencies = [base] if has_dependency else []
    prompts = _replies(monkeypatch, _saved_plan({
        "2": [_ordering_entry(base)], "5": [_ordering_entry(final, *final_dependencies)],
    }))

    result = _cli(tmp_path, "--init", "--use-required-artifacts", "--no-contract-gate")

    assert result.exit_code == 0, result.output
    reports = [line for line in result.output.splitlines() if "ordered_after" in line]
    assert len(reports) == (0 if has_dependency else 1)
    assert len(prompts) == 1
    waves = yaml.safe_load(config_path.read_bytes())["wave_config"]
    assert waves["2"][0]["node_id"] == base
    entry, = waves["5"]
    assert entry["node_id"] == final
    assert [dep["id"] for dep in entry["depends_on"]].count(base) == 1


@pytest.mark.parametrize("same_wave", [False, True], ids=["separated", "same-wave"])
def test_ordering_keeps_a_pair_already_ordered_the_other_way(tmp_path, monkeypatch, same_wave):
    base, middle, final, consumer = "design:base", "design:middle", "design:final", "design:consumer"
    catalog = [
        _catalog_row(base, "Base design"),
        _catalog_row(middle, "Intermediate design"),
        _catalog_row(final, "Final design") | {"ordered_after": [base]},
        _catalog_row(consumer, "Consumer design"),
    ]
    config_path = _project(tmp_path, catalog)
    _ordering_selection(tmp_path, monkeypatch, catalog, {base, middle, final, consumer})
    entries = [
        _ordering_entry(final), _ordering_entry(middle, final),
        _ordering_entry(base, middle), _ordering_entry(consumer, base),
    ]
    if same_wave:
        waves = {"1": entries[:3], "2": entries[3:]}
    else:
        waves = {str(index): [entry] for index, entry in enumerate(entries, start=1)}
    prompts = _replies(monkeypatch, _saved_plan(waves))

    result = _cli(tmp_path, "--init", "--use-required-artifacts", "--no-contract-gate")

    assert result.exit_code == 0, result.output
    assert len(prompts) == 1
    reports = [line for line in result.output.splitlines() if "ordered_after" in line]
    assert len(reports) == int(same_wave)
    waves = yaml.safe_load(config_path.read_bytes())["wave_config"]
    assert [entry["node_id"] for entry in waves["1"]] == ([final, middle] if same_wave else [final])
    assert waves["2" if same_wave else "3"][0]["node_id"] == base
    assert waves["3" if same_wave else "4"][0]["node_id"] == consumer
    assert [dep["id"] for dep in waves["2" if same_wave else "3"][0]["depends_on"]] == [middle]
    assert base not in [dep["id"] for dep in waves["1"][0]["depends_on"]]


def test_web_ordering_moves_reverse_same_wave_pair_and_consumers(tmp_path, monkeypatch):
    # reverse_same_wave: the flow already depends on the API, and both start in wave 4.
    config_path, catalog = _ordering_project(tmp_path, web=True)
    transitions, auth, infra = (
        "design:screen_transitions", "design:auth_authorization_design", "design:infrastructure_design",
    )
    selected, _, _ = _ordering_selection(tmp_path, monkeypatch, catalog, {API_ROW, FLOW_ROW, transitions, auth, infra})
    unaffected = sorted(selected - {"design:requirements", API_ROW, FLOW_ROW, transitions, auth, infra})
    prompts = _replies(monkeypatch, _saved_plan({
        "4": [_ordering_entry(API_ROW), _ordering_entry(FLOW_ROW, API_NODE), *map(_ordering_entry, unaffected)],
        "5": [_ordering_entry(transitions, API_NODE, FLOW_NODE)],
        "6": [_ordering_entry(auth, transitions)],
        "9": [_ordering_entry(infra, auth)],
    }))

    result = _cli(tmp_path, "--init", "--use-required-artifacts", "--no-contract-gate")

    assert result.exit_code == 0, result.output
    assert len(prompts) == 1
    waves = yaml.safe_load(config_path.read_bytes())["wave_config"]
    assert [entry["node_id"] for entry in waves["4"]] == [API_NODE, *unaffected]
    assert [dep["id"] for dep in waves["4"][0]["depends_on"]] == [REQ_NODE]
    assert [entry["node_id"] for entry in waves["5"]] == [FLOW_NODE]
    assert [dep["id"] for dep in waves["5"][0]["depends_on"]] == [API_NODE]
    assert [waves[wave][0]["node_id"] for wave in ("6", "7", "9")] == [transitions, auth, infra]
    report, = [line for line in result.output.splitlines() if "ordered_after" in line]
    assert f"{FLOW_NODE}, wave 5 after {API_NODE}, wave 4" in report


@pytest.mark.parametrize("old_snapshot", [True, False], ids=["missing-field", "explicit-empty"])
def test_web_ordering_loads_default_rule_for_saved_legacy_selection(tmp_path, monkeypatch, old_snapshot):
    # old_selection: candidates saved before ordered_after had the same IDs and scopes.
    config_path, catalog = _ordering_project(tmp_path, web=True)
    selected, _, _ = _ordering_selection(tmp_path, monkeypatch, catalog, {API_ROW, FLOW_ROW})
    lexicon_path = tmp_path / "project_lexicon.yaml"
    lexicon = yaml.safe_load(lexicon_path.read_bytes())
    snapshot = lexicon["required_artifact_snapshot"]
    saved_api = snapshot["candidates"][API_ROW]
    del saved_api["ordered_after"]
    if not old_snapshot:
        saved_api["ordered_after"] = []
    lexicon_path.write_text(yaml.safe_dump(lexicon), encoding="utf-8")
    prompts = _replies(monkeypatch, _saved_plan({
        "4": [_ordering_entry(row) for row in sorted(selected - {"design:requirements"})],
    }))

    result = _cli(tmp_path, "--init", "--use-required-artifacts", "--no-contract-gate")

    assert result.exit_code == 0, result.output
    assert len(prompts) == 1 and "ordered_after" not in prompts[0]
    waves = yaml.safe_load(config_path.read_bytes())["wave_config"]
    assert FLOW_NODE in [entry["node_id"] for entry in waves["4"]]
    api = next(entry for entry in waves["5" if old_snapshot else "4"] if entry["node_id"] == API_NODE)
    assert (FLOW_NODE in [dep["id"] for dep in api["depends_on"]]) == old_snapshot
    assert api["required_artifact_scope"]["catalog_scope"] == saved_api["scope"]
    assert yaml.safe_load(lexicon_path.read_bytes())["required_artifact_snapshot"] == snapshot


def test_ordering_leaves_an_existing_unrelated_plan_cycle_to_downstream_checks(tmp_path, monkeypatch):
    # unrelated_ai_cycle: the catalog orders A and B, while the plan cycles C and D.
    a, b, c, d = "design:a", "design:b", "design:c", "design:d"
    catalog = [
        _catalog_row(a, "Design A") | {"ordered_after": [b]},
        _catalog_row(b, "Design B"), _catalog_row(c, "Design C"), _catalog_row(d, "Design D"),
    ]
    config_path = _project(tmp_path, catalog)
    _ordering_selection(tmp_path, monkeypatch, catalog, {a, b, c, d})
    entries = [_ordering_entry(a), _ordering_entry(b), _ordering_entry(c, d), _ordering_entry(d, c)]
    prompts = _replies(monkeypatch, _saved_plan({"4": entries}))

    result = _cli(tmp_path, "--init", "--use-required-artifacts", "--no-contract-gate")

    assert result.exit_code == 0, result.output
    assert len(prompts) == 1
    assert "ordered_after" not in result.output
    waves = yaml.safe_load(config_path.read_bytes())["wave_config"]
    actual = {entry["node_id"]: [dep["id"] for dep in entry["depends_on"]] for entry in waves["4"]}
    assert actual == {a: [REQ_NODE], b: [REQ_NODE], c: [d], d: [c]}


def test_ordering_respects_an_indirect_order_created_by_another_rule(tmp_path, monkeypatch):
    # combined_cycle: two compatible catalog rules encounter cross-dependencies in the plan.
    a, b, c, d = "design:a", "design:b", "design:c", "design:d"
    catalog = [
        _catalog_row(a, "Design A") | {"ordered_after": [b]},
        _catalog_row(b, "Design B"),
        _catalog_row(c, "Design C") | {"ordered_after": [d]},
        _catalog_row(d, "Design D"),
    ]
    config_path = _project(tmp_path, catalog)
    _ordering_selection(tmp_path, monkeypatch, catalog, {a, b, c, d})
    prompts = _replies(monkeypatch, _saved_plan({
        "4": [_ordering_entry(a), _ordering_entry(b, c), _ordering_entry(c), _ordering_entry(d, a)],
    }))

    result = _cli(tmp_path, "--init", "--use-required-artifacts", "--no-contract-gate")

    assert result.exit_code == 0, result.output
    assert len(prompts) == 1
    waves = yaml.safe_load(config_path.read_bytes())["wave_config"]
    assert [[entry["node_id"] for entry in waves[wave]] for wave in ("4", "5", "6")] == [[b, c], [a], [d]]
    assert [dep["id"] for dep in waves["6"][0]["depends_on"]] == [a]
    assert [dep["id"] for dep in waves["4"][1]["depends_on"]] == [REQ_NODE]


def test_saved_selection_does_not_borrow_contradicting_current_rules(tmp_path, monkeypatch):
    # Planning from a saved selection only borrows the current catalog's rules; a catalog
    # problem there never stops planning (main has no rules). Selection still rejects it.
    base, final = "design:base", "design:final"
    catalog = [_catalog_row(base, "Base design"), _catalog_row(final, "Final design")]
    config_path = _project(tmp_path, catalog)
    _ordering_selection(tmp_path, monkeypatch, catalog, {base, final})
    config = yaml.safe_load(config_path.read_bytes())
    config["required_artifacts"]["default_artifacts"][0]["ordered_after"] = [final]
    config["required_artifacts"]["default_artifacts"][1]["ordered_after"] = [base]
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    prompts = _replies(monkeypatch, _saved_plan({"2": [_ordering_entry(base), _ordering_entry(final)]}))

    result = _cli(tmp_path, "--init", "--use-required-artifacts", "--no-contract-gate")

    assert result.exit_code == 0, result.output
    assert len(prompts) == 1
    assert not [line for line in result.output.splitlines() if "ordered_after" in line]
    waves = yaml.safe_load(config_path.read_bytes())["wave_config"]
    assert sorted(entry["node_id"] for entry in waves["2"]) == sorted(
        [_ordering_entry(base)["node_id"], _ordering_entry(final)["node_id"]]
    )


@pytest.mark.parametrize(("target", "other_field"), [
    ("design:a", None), ("design:b", "ordered_after"), ("design:b", "depends_on"),
], ids=["self", "mutual-ordering", "mixed-dependencies"])
def test_derivation_rejects_catalog_ordering_cycles_before_using_a_response(tmp_path, monkeypatch, target, other_field):
    # catalog_self_cycle and catalog_cycle_with_prior_edge: a reply cannot excuse a bad catalog.
    a, b = "design:a", "design:b"
    catalog = [_catalog_row(a, "Design A") | {"ordered_after": [target]}, _catalog_row(b, "Design B")]
    if other_field:
        catalog[1][other_field] = [a]
    config_path = _project(tmp_path, catalog)
    original_config = config_path.read_bytes()
    calls = []

    def invoke(self, prompt, **kwargs):
        calls.append(prompt)
        pytest.fail("The catalog must be validated before asking for a selection")

    monkeypatch.setattr(RequiredArtifactsDeriver, "_call_ai", invoke)

    result = _cli(tmp_path, "--derive", "--account-decisions")

    assert result.exit_code == 1, result.output
    assert f"{a!r} ordered_after {target!r} creates a dependency cycle" in result.output
    assert calls == []
    assert config_path.read_bytes() == original_config
    assert not (tmp_path / "project_lexicon.yaml").exists()


def test_saved_selection_skips_a_borrowed_rule_that_would_close_a_cycle(tmp_path, monkeypatch):
    # cycle_only_when_mixing_catalog_versions: each catalog version alone is valid.
    a, b = "design:a", "design:b"
    catalog = [_catalog_row(a, "Design A"), _catalog_row(b, "Design B") | {"depends_on": [a]}]
    config_path = _project(tmp_path, catalog)
    _ordering_selection(tmp_path, monkeypatch, catalog, {a, b})
    config = yaml.safe_load(config_path.read_bytes())
    config["required_artifacts"]["default_artifacts"][0]["ordered_after"] = [b]
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    prompts = _replies(monkeypatch, _saved_plan({"2": [_ordering_entry(a), _ordering_entry(b)]}))

    result = _cli(tmp_path, "--init", "--use-required-artifacts", "--no-contract-gate")

    assert result.exit_code == 0, result.output
    assert len(prompts) == 1
    assert not [line for line in result.output.splitlines() if "ordered_after" in line]


def test_saved_selection_ignores_an_unreadable_current_catalog(tmp_path, monkeypatch):
    # unrelated_malformed_override: an unselected row's defect must not stop planning.
    a, b, c = "design:a", "design:b", "design:c"
    catalog = [_catalog_row(a, "Design A"), _catalog_row(b, "Design B"), _catalog_row(c, "Design C")]
    config_path = _project(tmp_path, catalog)
    _ordering_selection(tmp_path, monkeypatch, catalog, {a, b})
    config = yaml.safe_load(config_path.read_bytes())
    del config["required_artifacts"]["default_artifacts"][2]["scope"]
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    prompts = _replies(monkeypatch, _saved_plan({"2": [_ordering_entry(a), _ordering_entry(b)]}))

    result = _cli(tmp_path, "--init", "--use-required-artifacts", "--no-contract-gate")

    assert result.exit_code == 0, result.output
    assert len(prompts) == 1


def test_web_catalog_orders_flow_api_and_transitions_in_one_chain():
    # Screen flow decides where an action leads, the API realizes it in its responses,
    # and screen transitions record the concrete edges per response: no two of these
    # describe the same outcome from the same wave.
    from codd.required_artifacts_deriver import RequiredArtifactsDeriver

    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "codd/required_artifacts/defaults/web.yaml"
    rows = {row["id"]: row for row in yaml.safe_load(path.read_text(encoding="utf-8"))["default_artifacts"]}
    assert rows["design:api_design"]["ordered_after"] == ["design:screen_flow_design"]
    assert rows["design:screen_transitions"]["ordered_after"] == ["design:api_design"]
    assert RequiredArtifactsDeriver  # the catalog loads through the same deriver
