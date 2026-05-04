from click.testing import CliRunner

from codd.cli import main
from codd.e2e_extractor import ScenarioCollection, UserScenario
from codd.e2e_generator import TestGenerator, load_scenarios_from_markdown


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
    assert "Login & Complete!" in output.read_text(encoding="utf-8")


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


def test_cli_e2e_generate_help():
    result = CliRunner().invoke(main, ["e2e-generate", "--help"])

    assert result.exit_code == 0
    assert "Generate Playwright or Cypress test files" in result.output


def test_cli_e2e_generate_group_help():
    result = CliRunner().invoke(main, ["e2e", "generate", "--help"])

    assert result.exit_code == 0
    assert "--framework" in result.output


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
