import json

from click.testing import CliRunner

from codd.cli import main
from codd.operational_e2e_audit import build_agent_workflow_plan, build_operational_e2e_audit


def test_operational_audit_requires_explicit_covers_marker(tmp_path):
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
    tests_dir = tmp_path / "tests" / "e2e"
    tests_dir.mkdir(parents=True)
    (tests_dir / "assign_item.spec.ts").write_text(
        """// operation assign_item
// axis persistence_readback
test('assign item persists', async () => {});
""",
        encoding="utf-8",
    )

    report = build_operational_e2e_audit(tmp_path)

    readback = next(row for row in report.rows if row.coverage_axis == "persistence_readback")
    assert readback.coverage_status == "uncovered"
    assert readback.heuristic_matches == ["tests/e2e/assign_item.spec.ts"]
    assert "explicit codd covers marker" in readback.suggested_next_action


def test_operational_audit_marks_marker_covered(tmp_path):
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
    tests_dir = tmp_path / "tests" / "e2e"
    tests_dir.mkdir(parents=True)
    (tests_dir / "assign_item.spec.ts").write_text(
        """// codd: covers operation=codd.yaml.operation_flow#assign_item axis=persistence_readback
// codd: dod operation=codd.yaml.operation_flow#assign_item axis=persistence_readback obligation=scenario_state
// codd: dod operation=codd.yaml.operation_flow#assign_item axis=persistence_readback obligation=public_trigger
// codd: dod operation=codd.yaml.operation_flow#assign_item axis=persistence_readback obligation=observable_outcome
// codd: dod operation=codd.yaml.operation_flow#assign_item axis=persistence_readback obligation=durable_readback
test('assign item persists', async () => {});
""",
        encoding="utf-8",
    )

    report = build_operational_e2e_audit(tmp_path)

    readback = next(row for row in report.rows if row.coverage_axis == "persistence_readback")
    assert readback.coverage_status == "covered_by_e2e"
    assert readback.matched_tests == ["tests/e2e/assign_item.spec.ts"]
    assert report.summary["covered_by_e2e"] == 1
    assert report.summary["uncovered"] == 1


def test_operational_audit_requires_dod_markers_after_covers_marker(tmp_path):
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
    tests_dir = tmp_path / "tests" / "e2e"
    tests_dir.mkdir(parents=True)
    (tests_dir / "assign_item.spec.ts").write_text(
        """// codd: covers operation=codd.yaml.operation_flow#assign_item axis=persistence_readback
test('assign item persists', async () => {
  await page.getByRole('button', { name: 'Assign' }).click();
  await expect(page.getByText('assignment persists')).toBeVisible();
});
""",
        encoding="utf-8",
    )

    report = build_operational_e2e_audit(tmp_path)

    readback = next(row for row in report.rows if row.coverage_axis == "persistence_readback")
    assert readback.coverage_status == "needs_dod_evidence"
    assert readback.missing_dod_obligations == [
        "scenario_state",
        "public_trigger",
        "observable_outcome",
        "durable_readback",
    ]
    assert report.summary["needs_dod_evidence"] == 1


def test_operational_audit_does_not_treat_integration_marker_as_e2e(tmp_path):
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        """operation_flow:
  operations:
    - id: process_webhook
      actor: system
      verb: process
      target: external_webhook
      route: /webhooks/provider
      trigger: provider webhook callback
      expected_outcomes: [webhook updates persisted state]
""",
        encoding="utf-8",
    )
    tests_dir = tmp_path / "tests" / "integration"
    tests_dir.mkdir(parents=True)
    (tests_dir / "provider-webhook.test.ts").write_text(
        """// codd: covers operation=codd.yaml.operation_flow#process_webhook axis=happy_path
test('webhook handler persists state with mocked provider', async () => {});
""",
        encoding="utf-8",
    )

    report = build_operational_e2e_audit(tmp_path)

    success = next(row for row in report.rows if row.coverage_axis == "happy_path")
    assert success.coverage_status == "covered_by_lower_test"
    assert success.matched_tests == ["tests/integration/provider-webhook.test.ts"]
    assert report.summary["covered_by_e2e"] == 0
    assert report.summary["covered_by_lower_test"] == 1
    assert report.summary["not_covered_by_e2e"] == 2
    assert report.summary["uncovered"] == 1

    plan = build_agent_workflow_plan(tmp_path, max_scenarios_per_shard=5)
    candidate_names = [
        scenario["name"]
        for shard in plan.shards
        for scenario in shard.scenarios
    ]
    assert "system process_webhook success" in candidate_names


def test_operational_audit_rejects_api_shortcut_for_eventful_trigger(tmp_path):
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        """operation_flow:
  operations:
    - id: save_resume_position
      actor: learner
      verb: update
      target: video_resume_position
      route: /lessons/:lessonId
      trigger: external video player pause event
      measurement_source: video player current time
      expected_outcomes: [position persists]
""",
        encoding="utf-8",
    )
    tests_dir = tmp_path / "tests" / "e2e"
    tests_dir.mkdir(parents=True)
    (tests_dir / "resume_position.spec.ts").write_text(
        """// codd: covers operation=codd.yaml.operation_flow#save_resume_position axis=persistence_readback
test('position persists through API shortcut', async () => {
  await request.put('/api/video/lesson-1/position', { data: { position: 50 } });
  await expect(request.get('/api/video/lesson-1/position')).resolves.toBeTruthy();
});
""",
        encoding="utf-8",
    )

    report = build_operational_e2e_audit(tmp_path)

    readback = next(row for row in report.rows if row.coverage_axis == "persistence_readback")
    assert readback.coverage_status == "needs_trigger_evidence"
    assert any("trigger-source evidence terms" in item for item in readback.required_evidence)
    assert "direct API/storage shortcuts are not enough" in readback.suggested_next_action
    assert report.summary["covered_by_e2e"] == 0
    assert report.summary["needs_trigger_evidence"] == 1
    assert report.summary["not_covered_by_e2e"] == 4
    assert report.summary["uncovered"] == 3


def test_operational_audit_requires_source_term_for_external_stream_trigger(tmp_path):
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        """operation_flow:
  operations:
    - id: complete_media
      actor: learner
      verb: complete
      target: media_lesson
      route: /lessons/:lessonId
      trigger: external media stream watched-duration event reaches completion threshold
      expected_outcomes: [completion persists]
""",
        encoding="utf-8",
    )
    tests_dir = tmp_path / "tests" / "e2e"
    tests_dir.mkdir(parents=True)
    (tests_dir / "complete_media.spec.ts").write_text(
        """// codd: covers operation=codd.yaml.operation_flow#complete_media axis=happy_path
test('completion threshold persists through direct API shortcut', async () => {
  await request.put('/api/media/lesson-1/position', { data: { position: 90 } });
});
""",
        encoding="utf-8",
    )

    report = build_operational_e2e_audit(tmp_path)

    success = next(row for row in report.rows if row.coverage_axis == "happy_path")
    assert success.coverage_status == "needs_trigger_evidence"
    trigger_evidence = next(item for item in success.required_evidence if item.startswith("trigger-source evidence terms"))
    assert "external" in trigger_evidence
    assert "stream" in trigger_evidence
    assert report.summary["covered_by_e2e"] == 0


def test_operational_audit_accepts_event_source_evidence(tmp_path):
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        """operation_flow:
  operations:
    - id: save_resume_position
      actor: learner
      verb: update
      target: video_resume_position
      route: /lessons/:lessonId
      trigger: external video player pause event
      measurement_source: video player current time
      expected_outcomes: [position persists]
""",
        encoding="utf-8",
    )
    tests_dir = tmp_path / "tests" / "e2e"
    tests_dir.mkdir(parents=True)
    (tests_dir / "resume_position.spec.ts").write_text(
        """// codd: covers operation=codd.yaml.operation_flow#save_resume_position axis=persistence_readback
// codd: dod operation=codd.yaml.operation_flow#save_resume_position axis=persistence_readback obligation=scenario_state
// codd: dod operation=codd.yaml.operation_flow#save_resume_position axis=persistence_readback obligation=public_trigger
// codd: dod operation=codd.yaml.operation_flow#save_resume_position axis=persistence_readback obligation=observable_outcome
// codd: dod operation=codd.yaml.operation_flow#save_resume_position axis=persistence_readback obligation=durable_readback
test('position persists from player pause', async ({ page }) => {
  await page.evaluate(() => {
    window.playerjs.Player.pause();
  });
  await expect(page.getByText('position persists')).toBeVisible();
});
""",
        encoding="utf-8",
    )

    report = build_operational_e2e_audit(tmp_path)

    readback = next(row for row in report.rows if row.coverage_axis == "persistence_readback")
    assert readback.coverage_status == "covered_by_e2e"
    assert report.summary["covered_by_e2e"] == 1


def test_operational_audit_rejects_ideal_stub_for_partial_source_signal(tmp_path):
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        """operation_flow:
  operations:
    - id: ingest_external_event
      actor: system
      verb: update
      target: external_state
      route: /events
      trigger: provider callback event
      source_signal: provider callback payload with optional fields
      expected_outcomes: [external state persists]
""",
        encoding="utf-8",
    )
    tests_dir = tmp_path / "tests" / "e2e"
    tests_dir.mkdir(parents=True)
    (tests_dir / "ingest_external_event.spec.ts").write_text(
        """// codd: covers operation=codd.yaml.operation_flow#ingest_external_event axis=partial_signal_contract
// codd: dod operation=codd.yaml.operation_flow#ingest_external_event axis=partial_signal_contract obligation=scenario_state
// codd: dod operation=codd.yaml.operation_flow#ingest_external_event axis=partial_signal_contract obligation=public_trigger
// codd: dod operation=codd.yaml.operation_flow#ingest_external_event axis=partial_signal_contract obligation=observable_outcome
// codd: dod operation=codd.yaml.operation_flow#ingest_external_event axis=partial_signal_contract obligation=partial_source_signal
test('ingests provider callback', async ({ page }) => {
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('provider-callback', {
    detail: { id: 'evt-1', state: 'ready', receivedAt: 123, checksum: 'ok' },
  })));
  await expect(page.getByText('external state persists')).toBeVisible();
});
""",
        encoding="utf-8",
    )

    report = build_operational_e2e_audit(tmp_path)

    partial = next(row for row in report.rows if row.coverage_axis == "partial_signal_contract")
    assert partial.coverage_status == "needs_source_signal_variance"
    assert report.summary["needs_source_signal_variance"] == 1


def test_operational_audit_accepts_explicit_blocker_marker(tmp_path):
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        """operation_flow:
  operations:
    - id: send_provider_email
      actor: system
      verb: send
      target: transactional_email
      route: /notifications
      expected_outcomes: [provider accepts the message]
""",
        encoding="utf-8",
    )
    tests_dir = tmp_path / "tests" / "smoke"
    tests_dir.mkdir(parents=True)
    (tests_dir / "send_provider_email.spec.ts").write_text(
        """// codd: blocked operation=codd.yaml.operation_flow#send_provider_email axis=happy_path reason=environment_or_external_service missing test API key
test('provider accepts the message once credentials exist', async () => {});
""",
        encoding="utf-8",
    )

    report = build_operational_e2e_audit(tmp_path)

    success = next(row for row in report.rows if row.coverage_axis == "happy_path")
    assert success.coverage_status == "blocked"
    assert success.blocker_reason == "environment_or_external_service"
    assert success.blocker_details == "missing test API key"
    assert success.blocker_evidence == ["tests/smoke/send_provider_email.spec.ts"]
    assert report.summary["blocked"] == 1
    assert report.summary["covered_by_e2e"] == 0

    plan = build_agent_workflow_plan(tmp_path, max_scenarios_per_shard=5)
    candidate_names = [
        scenario["name"]
        for shard in plan.shards
        for scenario in shard.scenarios
    ]
    assert "system send_provider_email success" not in candidate_names


def test_cli_e2e_audit_writes_markdown(tmp_path):
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

    result = CliRunner().invoke(main, ["e2e", "audit", "--path", str(tmp_path)])

    assert result.exit_code == 0
    assert "Operational E2E audit: 2 scenario(s)" in result.output
    report_path = tmp_path / "docs" / "e2e" / "operational-audit.md"
    content = report_path.read_text(encoding="utf-8")
    assert "# Operational E2E Audit" in content
    assert "Adapter Boundary" in content
    assert "uncovered" in content


def test_agent_workflow_plan_shards_uncovered_rows_by_operation(tmp_path):
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
    - id: archive_item
      actor: operator
      verb: archive
      target: work_item
      route: /work-items
      expected_outcomes: [item disappears from active list]
""",
        encoding="utf-8",
    )
    tests_dir = tmp_path / "tests" / "e2e"
    tests_dir.mkdir(parents=True)
    (tests_dir / "assign_item.spec.ts").write_text(
        """// codd: covers operation=codd.yaml.operation_flow#assign_item axis=happy_path
// codd: dod operation=codd.yaml.operation_flow#assign_item axis=happy_path obligation=scenario_state
// codd: dod operation=codd.yaml.operation_flow#assign_item axis=happy_path obligation=public_trigger
// codd: dod operation=codd.yaml.operation_flow#assign_item axis=happy_path obligation=observable_outcome
test('assign item starts', async () => {});
""",
        encoding="utf-8",
    )

    plan = build_agent_workflow_plan(tmp_path, max_scenarios_per_shard=1)

    assert plan.version == "agent-workflow-plan/v1"
    assert plan.summary["workflow_candidate_scenarios"] == 4
    assert plan.summary["workflow_shards"] == 4
    assert all(shard.scenario_count == 1 for shard in plan.shards)
    assert all("codd: covers operation=<source_operation>" in shard.recommended_prompt for shard in plan.shards)
    covered_scenarios = [
        scenario["name"]
        for shard in plan.shards
        for scenario in shard.scenarios
        if scenario["status"] == "covered_by_e2e"
    ]
    assert covered_scenarios == []


def test_cli_e2e_workflow_plan_writes_json(tmp_path):
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

    result = CliRunner().invoke(main, ["e2e", "workflow-plan", "--path", str(tmp_path)])

    assert result.exit_code == 0
    assert "Agent workflow E2E plan: 2 candidate scenario(s)" in result.output
    report_path = tmp_path / "docs" / "e2e" / "agent-workflow-plan.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["version"] == "agent-workflow-plan/v1"
    assert payload["summary"]["workflow_shards"] == 1
    assert payload["shards"][0]["source_operations"] == [
        "codd.yaml.operation_flow#submit_request",
    ]


def test_claude_dynamic_workflow_plan_defaults_to_permission_bypass(tmp_path):
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

    plan = build_agent_workflow_plan(
        tmp_path,
        runner_backend="claude-dynamic-workflow",
    )

    invocation = plan.runner_invocation
    assert invocation["backend"] == "claude-dynamic-workflow"
    assert invocation["dangerous_skip_permissions"] is True
    assert "--model claude-opus-4-8" in invocation["command_prefix"]
    assert "--effort max" in invocation["command_prefix"]
    assert "--permission-mode bypassPermissions" in invocation["command_prefix"]
    assert "--dangerously-skip-permissions" in invocation["command_prefix"]


def test_claude_dynamic_workflow_cli_can_disable_permission_bypass(tmp_path):
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

    default_result = CliRunner().invoke(
        main,
        [
            "e2e",
            "workflow-plan",
            "--path",
            str(tmp_path),
            "--runner-backend",
            "claude-dynamic-workflow",
        ],
    )
    assert default_result.exit_code == 0
    payload = json.loads((tmp_path / "docs" / "e2e" / "agent-workflow-plan.json").read_text(encoding="utf-8"))
    assert payload["runner_invocation"]["dangerous_skip_permissions"] is True
    assert "--model claude-opus-4-8" in payload["runner_invocation"]["command_prefix"]
    assert "--effort max" in payload["runner_invocation"]["command_prefix"]
    assert "--permission-mode bypassPermissions" in payload["runner_invocation"]["command_prefix"]
    assert "--dangerously-skip-permissions" in payload["runner_invocation"]["command_prefix"]

    safe_result = CliRunner().invoke(
        main,
        [
            "e2e",
            "workflow-plan",
            "--path",
            str(tmp_path),
            "--runner-backend",
            "claude-dynamic-workflow",
            "--claude-safe-permissions",
        ],
    )
    assert safe_result.exit_code == 0
    payload = json.loads((tmp_path / "docs" / "e2e" / "agent-workflow-plan.json").read_text(encoding="utf-8"))
    assert payload["runner_invocation"]["dangerous_skip_permissions"] is False
    assert "--model claude-opus-4-8" in payload["runner_invocation"]["command_prefix"]
    assert "--effort max" in payload["runner_invocation"]["command_prefix"]
    assert "--dangerously-skip-permissions" not in payload["runner_invocation"]["command_prefix"]
