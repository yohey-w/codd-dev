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


def test_extract_operational_scenarios_from_operation_flow(tmp_path):
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        """scan:
  doc_dirs:
    - docs/
operation_flow:
  actors: [operator, reviewer]
  preconditions: [workspace exists]
  operations:
    - id: assign_item
      actor: operator
      verb: assign
      target: work_item
      route: /work-items
      expected_outcomes:
        - assignee sees the item
        - assignment persists
      visible_to: reviewer
    - id: close_item
      actor: operator
      verb: complete
      target: work_item
      route: /work-items/:id
      expected_outcomes: [item is closed]
      forbidden_actors: [reviewer]
""",
        encoding="utf-8",
    )

    collection = ScenarioExtractor(tmp_path).extract_operational()
    axes = {scenario.coverage_axis for scenario in collection.scenarios}

    assert "codd.yaml.operation_flow" in collection.source_operation_flows
    assert {
        "happy_path",
        "persistence_readback",
        "cross_actor_reflection",
        "permission_boundary",
        "terminal_state_guard",
    }.issubset(axes)
    readback = next(scenario for scenario in collection.scenarios if scenario.name == "operator assign_item readback")
    assert readback.actor == "operator"
    assert readback.routes == ["/work-items"]
    assert "assignment persists" in readback.observable_outcomes
    assert "workspace exists" in readback.preconditions
    assert any("mutable shared seed state is not trusted" in item for item in readback.acceptance_criteria)


def test_extract_operational_scenarios_cover_derived_state_and_thresholds(tmp_path):
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        """operation_flow:
  operations:
    - id: record_activity_progress
      actor: operator
      verb: update
      target: work_item_progress
      route: /work-items/:id
      trigger: automatic activity event emitted by the work surface
      measurement_source: active_seconds emitted by the work surface
      durable_state: progress_events.active_seconds
      readback: latest active_seconds is restored when the work item is reopened
      consumer_surfaces:
        - reviewer progress dashboard
      threshold: 90% of required duration
      boundary_cases: [89%, 90%, missing duration]
      expected_outcomes:
        - progress percentage is derived from active_seconds / required_duration
        - reviewer dashboard reflects the derived progress percentage
""",
        encoding="utf-8",
    )

    collection = ScenarioExtractor(tmp_path).extract_operational()
    axes = {scenario.coverage_axis for scenario in collection.scenarios}

    assert {"derived_state_chain", "threshold_boundary"}.issubset(axes)
    derived = next(
        scenario
        for scenario in collection.scenarios
        if scenario.coverage_axis == "derived_state_chain"
    )
    threshold = next(
        scenario
        for scenario in collection.scenarios
        if scenario.coverage_axis == "threshold_boundary"
    )
    assert "active_seconds emitted by the work surface" in derived.observable_outcomes
    assert any("scenario-owned or idempotently reset" in step for step in derived.steps)
    assert any("measured or observed input -> durable state/event" in item for item in derived.acceptance_criteria)
    assert any("below, at, and above" in item for item in threshold.acceptance_criteria)
    assert "90% of required duration" in threshold.observable_outcomes


def test_extract_operational_preserves_commas_inside_yaml_list_items(tmp_path):
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        """operation_flow:
  operations:
    - id: review_items
      actor: operator
      verb: read
      target: assigned_items
      route: /items
      expected_outcomes:
        - operator cannot create, edit, or delete records from this screen
""",
        encoding="utf-8",
    )

    collection = ScenarioExtractor(tmp_path).extract_operational()

    assert collection.scenarios[0].observable_outcomes == [
        "operator cannot create, edit, or delete records from this screen"
    ]


def test_extract_operational_dedupes_overlapping_doc_dirs(tmp_path):
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        """scan:
  doc_dirs:
    - docs/
    - docs/requirements/
""",
        encoding="utf-8",
    )
    req = tmp_path / "docs" / "requirements" / "ops.md"
    req.parent.mkdir(parents=True)
    req.write_text(
        """---
codd:
  operation_flow:
    operations:
      - id: assign_item
        actor: operator
        verb: assign
        target: work_item
        route: /work-items
---
# Ops
""",
        encoding="utf-8",
    )

    collection = ScenarioExtractor(tmp_path).extract_operational()

    assert collection.source_operation_flows == ["docs/requirements/ops.md.codd.operation_flow"]


def test_save_operational_scenarios_md(tmp_path):
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        """operation_flow:
  operations:
    - id: approve_request
      actor: operator
      verb: approve
      target: request
      route: /requests
      expected_outcomes: [request is approved]
""",
        encoding="utf-8",
    )

    extractor = ScenarioExtractor(tmp_path)
    output_path = extractor.save_operational_scenarios(extractor.extract_operational())
    content = output_path.read_text(encoding="utf-8")

    assert output_path == tmp_path / "docs" / "e2e" / "operational-scenarios.md"
    assert content.startswith("# Operational E2E Scenarios")
    assert "## MECE Coverage Axes" in content
    assert "- Coverage Axis: happy_path" in content
    assert "Run the whole suite, collect all failures" in content
