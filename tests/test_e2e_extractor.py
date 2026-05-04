from codd.e2e_extractor import ScenarioCollection, ScenarioExtractor


def test_extract_empty_files_returns_empty_collection(tmp_path):
    collection = ScenarioExtractor(tmp_path).extract()

    assert collection.scenarios == []
    assert collection.source_screen_flow is None
    assert collection.source_requirements is None


def test_parse_screen_flow_basic(tmp_path):
    screen_flow = tmp_path / "screen-flow.md"
    screen_flow.write_text(
        """# Screen Flow

## Route: /login
**Component**: LoginPage
**User Action**: Enter email/password, click Login
**Transitions**: /dashboard

## Route: /dashboard
**Component**: Dashboard
**User Action**: Select a course
""",
        encoding="utf-8",
    )

    routes = ScenarioExtractor(tmp_path)._parse_screen_flow(screen_flow)

    login = next(route for route in routes if route["route"] == "/login")
    assert login["component"] == "LoginPage"
    assert login["actions"] == ["Enter email/password", "click Login"]
    assert login["transitions"] == ["/dashboard"]
    assert {route["route"] for route in routes} == {"/login", "/dashboard"}


def test_parse_mermaid_screen_flow_routes(tmp_path):
    screen_flow = tmp_path / "screen-flow.md"
    screen_flow.write_text(
        """```mermaid
graph LR
  subgraph learner["Learner"]
    "/login"
    "/courses/:id"
  end
```
""",
        encoding="utf-8",
    )

    routes = ScenarioExtractor(tmp_path)._parse_screen_flow(screen_flow)

    assert [route["route"] for route in routes] == ["/login", "/courses/:id"]


def test_parse_requirements_basic(tmp_path):
    requirements = tmp_path / "requirements.md"
    requirements.write_text(
        """# Requirements

### FR-AUTH-1: Learner login
User Story: As a learner I want to log in so that I can access my courses.
Priority: high

Acceptance Criteria:
- The learner shall enter email and password.
- The dashboard shall be shown after login.
""",
        encoding="utf-8",
    )

    parsed = ScenarioExtractor(tmp_path)._parse_requirements(requirements)

    assert parsed[0]["id"] == "FR-AUTH-1"
    assert parsed[0]["title"] == "Learner login"
    assert parsed[0]["user_story"].startswith("As a learner")
    assert parsed[0]["priority"] == "high"
    assert parsed[0]["acceptance_criteria"] == [
        "The learner shall enter email and password.",
        "The dashboard shall be shown after login.",
    ]


def test_generate_scenarios_combines_routes_and_requirements(tmp_path):
    routes = [
        {
            "route": "/login",
            "title": "",
            "component": "LoginPage",
            "actions": ["Enter credentials", "Submit the login form"],
            "transitions": ["/dashboard"],
        }
    ]
    requirements = [
        {
            "id": "FR-AUTH-1",
            "title": "Learner login",
            "user_story": "As a learner I want to log in.",
            "acceptance_criteria": ["The dashboard shall be shown after login."],
            "priority": "high",
        }
    ]

    scenarios = ScenarioExtractor(tmp_path)._generate_scenarios(routes, requirements)

    assert len(scenarios) == 1
    assert scenarios[0].name == "Learner login via /login"
    assert scenarios[0].priority == "high"
    assert scenarios[0].routes == ["/login", "/dashboard"]
    assert scenarios[0].acceptance_criteria == ["The dashboard shall be shown after login."]
    assert "Submit the login form." in scenarios[0].steps


def test_save_scenarios_md(tmp_path):
    collection = ScenarioCollection(
        scenarios=[
            ScenarioExtractor(tmp_path)._generate_scenarios(
                [
                    {
                        "route": "/login",
                        "title": "",
                        "component": "LoginPage",
                        "actions": ["Enter credentials"],
                        "transitions": [],
                    }
                ],
                [],
            )[0]
        ],
        source_screen_flow="/tmp/screen-flow.md",
        source_requirements="/tmp/requirements.md",
    )

    output_path = ScenarioExtractor(tmp_path).save_scenarios(collection)

    content = output_path.read_text(encoding="utf-8")
    assert output_path == tmp_path / "docs" / "e2e" / "scenarios.md"
    assert content.startswith("# E2E Scenarios")
    assert "## 1. LoginPage user journey" in content
    assert "- Routes: `/login`" in content


def test_generate_scenarios_skips_api_routes(tmp_path):
    routes = [
        {"route": "/api/health", "title": "", "component": "", "actions": [], "transitions": []},
        {"route": "/login", "title": "", "component": "LoginPage", "actions": [], "transitions": []},
    ]

    scenarios = ScenarioExtractor(tmp_path)._generate_scenarios(routes, [])

    assert [scenario.routes[0] for scenario in scenarios] == ["/login"]
