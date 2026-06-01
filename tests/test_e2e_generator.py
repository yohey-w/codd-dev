from click.testing import CliRunner

from codd.cli import main
from codd.e2e_extractor import ScenarioCollection, UserScenario
from codd.e2e_generator import ASSERTION_GUARD_MESSAGE, TestGenerator, load_scenarios_from_markdown


def _scenario() -> UserScenario:
    return UserScenario(
        name="Login & Complete!",
        steps=["Enter credentials", "Submit the login form"],
        routes=["/login", "/dashboard"],
        acceptance_criteria=["The dashboard shall be shown after login."],
        priority="high",
    )


def _collection(*scenarios: UserScenario) -> ScenarioCollection:
    return ScenarioCollection(scenarios=list(scenarios))


def test_generate_filename_sanitizes_scenario_name(tmp_path):
    generator = TestGenerator(tmp_path)

    assert generator._scenario_to_filename("Login & Complete!") == "test_login_complete.spec.ts"


def test_cypress_filename_uses_cy_suffix(tmp_path):
    generator = TestGenerator(tmp_path, framework="cypress")

    assert generator._scenario_to_filename("Login & Complete!") == "test_login_complete.cy.ts"


def test_render_playwright_test_contains_import_test_and_scenario(tmp_path):
    content = TestGenerator(tmp_path)._render_playwright_test(_scenario())

    assert 'import { test, expect } from "@playwright/test";' in content
    assert 'test("Login & Complete!", async ({ page }) => {' in content
    assert 'await page.goto("http://localhost:3000/login");' in content
    assert "// Step 1: Enter credentials" in content


def test_acceptance_criteria_in_output(tmp_path):
    content = TestGenerator(tmp_path)._render_playwright_test(_scenario())

    assert "// - The dashboard shall be shown after login." in content


def test_generate_empty_collection_returns_empty_list(tmp_path):
    generated = TestGenerator(tmp_path).generate(ScenarioCollection(), output_dir=tmp_path / "tests")

    assert generated == []
    assert (tmp_path / "tests").is_dir()


def test_generate_writes_files(tmp_path):
    generated = TestGenerator(tmp_path).generate(_collection(_scenario()), output_dir=tmp_path / "e2e")

    assert len(generated) == 1
    assert generated[0].file_name == "test_login_complete.spec.ts"
    output = tmp_path / "e2e" / generated[0].file_name
    assert output.exists()
    content = output.read_text(encoding="utf-8")
    assert "Login & Complete!" in content
    assert ASSERTION_GUARD_MESSAGE in content
    assert "TODO: Add assertions" not in content


def test_generate_dedupes_duplicate_file_names(tmp_path):
    generated = TestGenerator(tmp_path).generate(
        _collection(_scenario(), _scenario()),
        output_dir=tmp_path / "e2e",
    )

    assert [test.file_name for test in generated] == [
        "test_login_complete.spec.ts",
        "test_login_complete_2.spec.ts",
    ]


def test_load_scenarios_from_markdown_reads_extractor_output(tmp_path):
    scenarios = tmp_path / "docs" / "e2e" / "scenarios.md"
    scenarios.parent.mkdir(parents=True)
    scenarios.write_text(
        """# E2E Scenarios

- Source screen flow: /tmp/screen-flow.md
- Source requirements: /tmp/requirements.md

## 1. Learner login via /login
- Priority: high
- Routes: `/login` -> `/dashboard`

### Steps
1. Open /login.
2. Submit credentials.

### Acceptance Criteria
- The dashboard shall be shown after login.
""",
        encoding="utf-8",
    )

    collection = load_scenarios_from_markdown(scenarios)

    assert collection.source_screen_flow == "/tmp/screen-flow.md"
    assert collection.source_requirements == "/tmp/requirements.md"
    assert collection.scenarios[0].name == "Learner login via /login"
    assert collection.scenarios[0].routes == ["/login", "/dashboard"]
    assert collection.scenarios[0].steps == ["Open /login.", "Submit credentials."]


def test_load_operational_scenarios_from_markdown_reads_metadata(tmp_path):
    scenarios = tmp_path / "docs" / "e2e" / "operational-scenarios.md"
    scenarios.parent.mkdir(parents=True)
    scenarios.write_text(
        """# Operational E2E Scenarios

## 1. operator assign_item readback
- Kind: operational
- Priority: medium
- Actor: operator
- Coverage Axis: persistence_readback
- Source Operation: codd.yaml.operation_flow#assign_item
- Trigger: assign work_item.
- Routes: `/work-items`

### Preconditions
- workspace exists

### Steps
1. Act as operator.
2. Trigger assign work_item.

### Observable Outcomes
- assignment persists

### Acceptance Criteria
- assign_item state change is still observable after readback.

### DoD Obligations
- scenario_state: scenario state is reset
- durable_readback: persisted state is visible after reload
""",
        encoding="utf-8",
    )

    collection = load_scenarios_from_markdown(scenarios)
    scenario = collection.scenarios[0]

    assert scenario.kind == "operational"
    assert scenario.actor == "operator"
    assert scenario.coverage_axis == "persistence_readback"
    assert scenario.source == "codd.yaml.operation_flow"
    assert scenario.operation_id == "assign_item"
    assert scenario.preconditions == ["workspace exists"]
    assert scenario.observable_outcomes == ["assignment persists"]
    assert [item.id for item in scenario.dod_obligations] == ["scenario_state", "durable_readback"]


def test_load_operational_scenarios_ignores_intro_sections(tmp_path):
    scenarios = tmp_path / "docs" / "e2e" / "operational-scenarios.md"
    scenarios.parent.mkdir(parents=True)
    scenarios.write_text(
        """# Operational E2E Scenarios

## MECE Coverage Axes
- happy_path

## Sources
- codd.yaml.operation_flow

## 1. operator assign_item success
- Kind: operational
- Priority: medium
- Actor: operator
- Coverage Axis: happy_path
- Source Operation: codd.yaml.operation_flow#assign_item
- Trigger: assign work_item.
- Routes: `/work-items`

### Steps
1. Act as operator.

### Acceptance Criteria
- operator can complete assign_item.
""",
        encoding="utf-8",
    )

    collection = load_scenarios_from_markdown(scenarios)

    assert [scenario.name for scenario in collection.scenarios] == ["operator assign_item success"]


def test_cli_e2e_generate_help():
    result = CliRunner().invoke(main, ["e2e-generate", "--help"])

    assert result.exit_code == 0
    assert "Generate Playwright or Cypress test files" in result.output


def test_cli_e2e_generate_group_help():
    result = CliRunner().invoke(main, ["e2e", "generate", "--help"])

    assert result.exit_code == 0
    assert "--framework" in result.output
    assert "operational" in result.output


def test_cli_e2e_extract_group_help():
    result = CliRunner().invoke(main, ["e2e", "extract", "--help"])

    assert result.exit_code == 0
    assert "Scenario catalog to extract" in result.output


def test_cli_generates_from_scenarios_markdown(tmp_path):
    scenarios = tmp_path / "docs" / "e2e" / "scenarios.md"
    scenarios.parent.mkdir(parents=True)
    scenarios.write_text(
        """# E2E Scenarios

## 1. Learner login via /login
- Priority: high
- Routes: `/login`

### Steps
1. Open /login.

### Acceptance Criteria
- The login page shall be shown.
""",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        main,
        ["e2e", "generate", "--path", str(tmp_path), "--output", "generated", "--base-url", "http://app.test"],
    )

    assert result.exit_code == 0
    assert "Generated 1 test file(s):" in result.output
    content = (tmp_path / "generated" / "test_learner_login_via_login.spec.ts").read_text(encoding="utf-8")
    assert 'await page.goto("http://app.test/login");' in content


def test_cli_generates_operational_scenarios_from_operation_flow(tmp_path):
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        """operation_flow:
  operations:
    - id: assign_item
      actor: operator
      verb: assign
      target: work_item
      route: /work-items
      expected_outcomes: [assignment persists]
""",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        main,
        [
            "e2e",
            "generate",
            "--path",
            str(tmp_path),
            "--mode",
            "operational",
            "--output",
            "generated",
            "--base-url",
            "http://app.test",
        ],
    )

    assert result.exit_code == 0
    assert "Generated 2 test file(s):" in result.output
    content = (tmp_path / "generated" / "test_operator_assign_item_readback.spec.ts").read_text(encoding="utf-8")
    assert "// Kind: operational" in content
    assert "// Coverage axis: persistence_readback" in content
    assert "// codd: covers operation=codd.yaml.operation_flow#assign_item axis=persistence_readback" in content
    assert "// Evidence policy: exercise the actor-facing public trigger" in content
    assert "// DoD obligations:" in content
    assert "// DoD marker format: codd: dod operation=<source_operation> axis=<coverage_axis> obligation=<obligation_id>" in content
    assert "collect all failures" in content
    assert 'await page.goto("http://app.test/work-items");' in content
    assert ASSERTION_GUARD_MESSAGE in content
    assert "TODO: Add assertions" not in content


def test_operational_scenarios_require_public_trigger_and_chain_readback(tmp_path):
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        """operation_flow:
  operations:
    - id: save_resume_state
      actor: operator
      verb: update
      target: work_item_state
      route: /work-items/:id
      expected_outcomes:
        - latest state is restored after reopen
      visible_to:
        - reviewer
""",
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["e2e", "extract", "--path", str(tmp_path), "--mode", "operational"])

    assert result.exit_code == 0
    content = (tmp_path / "docs" / "e2e" / "operational-scenarios.md").read_text(encoding="utf-8")
    assert "Evidence exercises the actor-facing public trigger" in content
    assert "mutable shared seed state is not trusted" in content
    assert "Evidence verifies producer -> durable state/event -> readback/consumer reflection" in content
    assert "### DoD Obligations" in content
    assert "durable_readback" in content
    assert "reviewer observes the result" in content


def test_operational_scenarios_render_derived_state_axes(tmp_path):
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        """operation_flow:
  operations:
    - id: track_completion_metric
      actor: operator
      verb: update
      target: process_progress
      route: /processes/:id
      trigger: timer event from the public work surface
      measurement_source: elapsed_seconds
      durable_state: progress_events.elapsed_seconds
      consumer_surfaces: [manager dashboard]
      threshold: 80% of required duration
      expected_outcomes:
        - dashboard shows derived completion percentage
""",
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["e2e", "extract", "--path", str(tmp_path), "--mode", "operational"])

    assert result.exit_code == 0
    content = (tmp_path / "docs" / "e2e" / "operational-scenarios.md").read_text(encoding="utf-8")
    assert "derived_state_chain" in content
    assert "threshold_boundary" in content
    assert "partial_signal_contract" in content
    assert "scenario-owned or idempotently reset state" in content
    assert "Evidence verifies measured or observed input -> durable state/event" in content
    assert "Evidence covers behavior below, at, and above" in content
    assert "all-fields-present ideal stub" in content


def test_operational_scenarios_do_not_treat_provider_word_as_partial_signal(tmp_path):
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        """operation_flow:
  operations:
    - id: password_login
      actor: operator
      verb: login
      target: dashboard
      route: /login
      trigger: submit credentials through an authentication provider
      expected_outcomes: [operator reaches dashboard]
""",
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["e2e", "extract", "--path", str(tmp_path), "--mode", "operational"])

    assert result.exit_code == 0
    content = (tmp_path / "docs" / "e2e" / "operational-scenarios.md").read_text(encoding="utf-8")
    assert "Extracted 1 operational scenario(s)" in result.output
    assert "partial_signal_contract" in content
    assert "password_login partial source signal" not in content


def test_cli_extracts_operational_catalog(tmp_path):
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        """operation_flow:
  operations:
    - id: submit_request
      actor: operator
      verb: submit
      target: request
      route: /requests
      expected_outcomes: [request is submitted]
""",
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["e2e", "extract", "--path", str(tmp_path), "--mode", "operational"])

    assert result.exit_code == 0
    assert "Extracted 2 operational scenario(s)" in result.output
    assert (tmp_path / "docs" / "e2e" / "operational-scenarios.md").exists()


def test_design_and_lexicon_hints_are_included(tmp_path):
    (tmp_path / "DESIGN.md").write_text(
        """---
colors:
  Primary:
    value: "#1A73E8"
typography:
  Body:
    value: "16px"
---
""",
        encoding="utf-8",
    )
    (tmp_path / "project_lexicon.yaml").write_text(
        """node_vocabulary:
  - id: test_user
    description: Stable E2E test user naming.
naming_conventions: []
design_principles:
  - Prefer role-based selectors.
failure_modes: []
extractor_registry: {}
""",
        encoding="utf-8",
    )

    content = TestGenerator(tmp_path)._render_playwright_test(_scenario())

    assert "// DESIGN.md assertion candidates:" in content
    assert "colors.Primary = #1A73E8" in content
    assert "// Project lexicon hints:" in content
    assert "Stable E2E test user naming." in content


def test_invalid_project_lexicon_does_not_block_e2e_generation(tmp_path):
    (tmp_path / "project_lexicon.yaml").write_text("required_artifacts: bad\n", encoding="utf-8")

    content = TestGenerator(tmp_path)._render_playwright_test(_scenario())

    assert 'test("Login & Complete!", async ({ page }) => {' in content
    assert "// Project lexicon hints:" not in content
