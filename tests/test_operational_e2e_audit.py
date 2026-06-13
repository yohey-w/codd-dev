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


def test_operational_audit_scans_source_tree_when_e2e_lives_under_src(tmp_path):
    # FIX 1 (false-RED, scan-scope): a correctly-marked e2e test that lands
    # under a configured source root (src/tests/e2e/, not the conventional
    # tests/) must be seen. Before the fix the default scan scope was only
    # tests/, so the marker was invisible and the operation reported uncovered.
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        """scan:
  test_dirs: [tests]
  source_dirs: [src]
operation_flow:
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
      expected_outcomes: [archive persists]
""",
        encoding="utf-8",
    )
    # Correct, fully-marked e2e test, but under src/tests/e2e/ (a source root).
    e2e_dir = tmp_path / "src" / "tests" / "e2e"
    e2e_dir.mkdir(parents=True)
    (e2e_dir / "assign_item.spec.ts").write_text(
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

    # The correctly-marked operation under src/tests/e2e is now covered...
    assigned = next(
        row
        for row in report.rows
        if row.source_operation.endswith("#assign_item")
        and row.coverage_axis == "persistence_readback"
    )
    assert assigned.coverage_status == "covered_by_e2e"
    assert assigned.matched_tests == ["src/tests/e2e/assign_item.spec.ts"]
    # ...but a genuinely-unmarked operation still reports uncovered (true-RED preserved).
    archived = next(
        row
        for row in report.rows
        if row.source_operation.endswith("#archive_item")
        and row.coverage_axis == "persistence_readback"
    )
    assert archived.coverage_status == "uncovered"


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


def test_operational_audit_requires_actor_specific_markers_for_multi_actor_axis(tmp_path):
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        """operation_flow:
  operations:
    - id: assign_item
      actor: operator
      observers: [manager, auditor]
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
        """// codd: covers operation=codd.yaml.operation_flow#assign_item axis=cross_actor_reflection
// codd: dod operation=codd.yaml.operation_flow#assign_item axis=cross_actor_reflection obligation=scenario_state
// codd: dod operation=codd.yaml.operation_flow#assign_item axis=cross_actor_reflection obligation=public_trigger
// codd: dod operation=codd.yaml.operation_flow#assign_item axis=cross_actor_reflection obligation=observable_outcome
// codd: dod operation=codd.yaml.operation_flow#assign_item axis=cross_actor_reflection obligation=durable_readback
test('manager sees assignment', async () => {});
""",
        encoding="utf-8",
    )

    report = build_operational_e2e_audit(tmp_path)

    reflected_rows = [row for row in report.rows if row.coverage_axis == "cross_actor_reflection"]
    assert {row.actor for row in reflected_rows} == {"manager", "auditor"}
    assert {row.coverage_status for row in reflected_rows} == {"uncovered"}


def test_operational_audit_accepts_matching_actor_marker_for_multi_actor_axis(tmp_path):
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        """operation_flow:
  operations:
    - id: assign_item
      actor: operator
      observers: [manager, auditor]
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
        """// codd: covers operation=codd.yaml.operation_flow#assign_item axis=cross_actor_reflection actor=manager
// codd: dod operation=codd.yaml.operation_flow#assign_item axis=cross_actor_reflection obligation=scenario_state actor=manager
// codd: dod operation=codd.yaml.operation_flow#assign_item axis=cross_actor_reflection obligation=public_trigger actor=manager
// codd: dod operation=codd.yaml.operation_flow#assign_item axis=cross_actor_reflection obligation=observable_outcome actor=manager
// codd: dod operation=codd.yaml.operation_flow#assign_item axis=cross_actor_reflection obligation=durable_readback actor=manager
test('manager sees assignment', async () => {});
""",
        encoding="utf-8",
    )

    report = build_operational_e2e_audit(tmp_path)

    manager = next(
        row
        for row in report.rows
        if row.coverage_axis == "cross_actor_reflection" and row.actor == "manager"
    )
    auditor = next(
        row
        for row in report.rows
        if row.coverage_axis == "cross_actor_reflection" and row.actor == "auditor"
    )
    assert manager.coverage_status == "covered_by_e2e"
    assert auditor.coverage_status == "uncovered"


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
    # save_resume_position now also yields a cross_route_state_restore scenario (uncovered):
    # "resume" in the operation id triggers the cross-route state contract gate.
    assert report.summary["not_covered_by_e2e"] == 6
    assert report.summary["uncovered"] == 5


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


def test_operational_audit_requires_navigation_reachability_dod_for_parameterized_route(tmp_path):
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        """operation_flow:
  operations:
    - id: edit_record
      actor: operator
      verb: update
      target: record
      route: /records/:recordId/edit
      navigation_from: /records
      expected_outcomes: [record edit form is visible]
""",
        encoding="utf-8",
    )
    tests_dir = tmp_path / "tests" / "e2e"
    tests_dir.mkdir(parents=True)
    (tests_dir / "edit_record.spec.ts").write_text(
        """// codd: covers operation=codd.yaml.operation_flow#edit_record axis=navigation_prerequisite
// codd: dod operation=codd.yaml.operation_flow#edit_record axis=navigation_prerequisite obligation=scenario_state
// codd: dod operation=codd.yaml.operation_flow#edit_record axis=navigation_prerequisite obligation=public_trigger
// codd: dod operation=codd.yaml.operation_flow#edit_record axis=navigation_prerequisite obligation=observable_outcome
test('operator reaches edit form', async ({ page }) => {
  await page.goto('/records');
  await page.getByRole('link', { name: 'Edit record' }).click();
  await expect(page).toHaveURL('/records/rec-1/edit');
});
""",
        encoding="utf-8",
    )

    report = build_operational_e2e_audit(tmp_path)

    navigation = next(row for row in report.rows if row.coverage_axis == "navigation_prerequisite")
    assert navigation.coverage_status == "needs_dod_evidence"
    assert navigation.missing_dod_obligations == ["navigation_reachability"]
    assert any("navigation_reachability" in item for item in navigation.required_evidence)


def test_operational_audit_accepts_navigation_reachability_marker(tmp_path):
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        """operation_flow:
  operations:
    - id: edit_record
      actor: operator
      verb: update
      target: record
      route: /records/:recordId/edit
      navigation_from: /records
      expected_outcomes: [record edit form is visible]
""",
        encoding="utf-8",
    )
    tests_dir = tmp_path / "tests" / "e2e"
    tests_dir.mkdir(parents=True)
    (tests_dir / "edit_record.spec.ts").write_text(
        """// codd: covers operation=codd.yaml.operation_flow#edit_record axis=navigation_prerequisite
// codd: dod operation=codd.yaml.operation_flow#edit_record axis=navigation_prerequisite obligation=scenario_state
// codd: dod operation=codd.yaml.operation_flow#edit_record axis=navigation_prerequisite obligation=public_trigger
// codd: dod operation=codd.yaml.operation_flow#edit_record axis=navigation_prerequisite obligation=observable_outcome
// codd: dod operation=codd.yaml.operation_flow#edit_record axis=navigation_prerequisite obligation=navigation_reachability
test('operator reaches edit form through list navigation', async ({ page }) => {
  await page.goto('/records');
  await page.getByRole('link', { name: 'Edit record' }).click();
  await expect(page).toHaveURL('/records/rec-1/edit');
  await expect(page.getByText('record edit form is visible')).toBeVisible();
});
""",
        encoding="utf-8",
    )

    report = build_operational_e2e_audit(tmp_path)

    navigation = next(row for row in report.rows if row.coverage_axis == "navigation_prerequisite")
    assert navigation.coverage_status == "covered_by_e2e"


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


# --- Check B: orphan covers-marker reverse reconciliation --------------------


def _orphan_project(tmp_path, marker_operation: str, *, extra_codd: str = "") -> None:
    """A project with one declared operation and one spec covers-marker."""
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
"""
        + extra_codd,
        encoding="utf-8",
    )
    tests_dir = tmp_path / "tests" / "e2e"
    tests_dir.mkdir(parents=True)
    (tests_dir / "spec.spec.ts").write_text(
        f"// codd: covers operation={marker_operation} axis=persistence_readback\n"
        "test('x', async () => {});\n",
        encoding="utf-8",
    )


def test_orphan_marker_detected_for_undeclared_operation(tmp_path):
    # The spec claims to cover an operation that exists in NO operation_flow.
    _orphan_project(tmp_path, "ghost_operation")

    report = build_operational_e2e_audit(tmp_path)

    assert len(report.orphan_cover_markers) == 1
    orphan = report.orphan_cover_markers[0]
    assert orphan.operation == "ghost_operation"
    assert orphan.path == "tests/e2e/spec.spec.ts"
    assert report.summary["orphan_cover_markers"] == 1
    assert "orphan_cover_marker" in orphan.message
    assert "ghost_operation" in orphan.message


def test_no_orphan_when_marker_matches_bare_operation_id(tmp_path):
    _orphan_project(tmp_path, "assign_item")

    report = build_operational_e2e_audit(tmp_path)

    assert report.orphan_cover_markers == []
    assert report.summary["orphan_cover_markers"] == 0


def test_no_orphan_when_marker_matches_full_source_key(tmp_path):
    _orphan_project(tmp_path, "codd.yaml.operation_flow#assign_item")

    report = build_operational_e2e_audit(tmp_path)

    assert report.orphan_cover_markers == []


def test_orphan_markers_rendered_in_markdown(tmp_path):
    _orphan_project(tmp_path, "ghost_operation")

    result = CliRunner().invoke(
        main, ["e2e", "audit", "--path", str(tmp_path), "--format", "md"]
    )

    assert result.exit_code == 0
    md = (tmp_path / "docs" / "e2e" / "operational-audit.md").read_text(encoding="utf-8")
    assert "Orphan Covers Markers" in md
    assert "ghost_operation" in md
    assert "Orphan covers markers: 1" in md


def test_doctor_warns_on_orphan_cover_marker(tmp_path):
    _orphan_project(tmp_path, "ghost_operation")

    result = CliRunner().invoke(main, ["doctor", "--path", str(tmp_path)])

    assert result.exit_code == 0
    assert "CoDD doctor: WARN" in result.output
    assert "orphan_cover_marker" in result.output
    assert "ghost_operation" in result.output


def test_doctor_silent_on_declared_cover_marker(tmp_path):
    _orphan_project(tmp_path, "assign_item")

    result = CliRunner().invoke(main, ["doctor", "--path", str(tmp_path)])

    assert result.exit_code == 0
    assert "orphan_cover_marker" not in result.output


def test_doctor_opt_out_suppresses_orphan_warning(tmp_path):
    _orphan_project(
        tmp_path,
        "ghost_operation",
        extra_codd="\norphan_cover_markers:\n  enabled: false\n",
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(tmp_path)])

    assert result.exit_code == 0
    assert "orphan_cover_marker" not in result.output
