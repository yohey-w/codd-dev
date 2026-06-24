from __future__ import annotations

import json
from pathlib import Path

from codd.dag import DAG, Edge, Node
from codd.dag.checks.user_journey_coherence import UserJourneyCoherenceCheck


def _journey(**overrides) -> dict:
    journey = {
        "name": "login_to_dashboard",
        "criticality": "critical",
        "steps": [{"action": "expect_url", "value": "/dashboard"}],
        "required_capabilities": [],
        "expected_outcome_refs": ["lexicon:e2e_login_journey"],
    }
    journey.update(overrides)
    return journey


def _constraint(**overrides) -> dict:
    constraint = {
        "capability": "tls_termination",
        "required": True,
        "rationale": "Declared journey transport requirement.",
    }
    constraint.update(overrides)
    return constraint


def _dag(
    *,
    journey: dict | None = None,
    constraints: list[dict] | None = None,
    display_fields: list[dict] | None = None,
    presentation_specs: list[dict] | None = None,
    aggregation_policies: list[dict] | None = None,
    expected: bool = True,
    expected_attrs: dict | None = None,
    plan_outputs: list[str] | None = None,
    e2e: bool = True,
    e2e_attrs: dict | None = None,
    runtime_caps: list[str] | None = None,
    runtime_declared: bool = True,
    impl_evidence: list[dict] | None = None,
) -> DAG:
    dag = DAG()
    dag.add_node(
        Node(
            id="docs/design/auth.md",
            kind="design_doc",
            path="docs/design/auth.md",
            attributes={
                "runtime_constraints": constraints or [],
                "user_journeys": [journey or _journey()],
                "display_fields": display_fields or [],
                "presentation_specs": presentation_specs or [],
                "aggregation_policies": aggregation_policies or [],
            },
        )
    )
    dag.add_node(
        Node(
            id="src/auth.ts",
            kind="impl_file",
            path="src/auth.ts",
            attributes={"runtime_evidence": impl_evidence or []},
        )
    )
    dag.add_edge(Edge(from_id="docs/design/auth.md", to_id="src/auth.ts", kind="expects"))

    if expected:
        attrs = {
            "id": "e2e_login_journey",
            "journey": "login_to_dashboard",
            "path": "tests/e2e/login.spec.ts",
            "browser_requirements": [],
        }
        attrs.update(expected_attrs or {})
        dag.add_node(Node(id="lexicon:e2e_login_journey", kind="expected", attributes=attrs))

    if plan_outputs is not None:
        dag.add_node(
            Node(
                id="implementation_plan.md#E2E-LOGIN",
                kind="plan_task",
                path="docs/design/implementation_plan.md",
                attributes={"expected_outputs": plan_outputs},
            )
        )
        for output in plan_outputs:
            if output == "lexicon:e2e_login_journey" and expected:
                dag.add_edge(
                    Edge(
                        from_id="implementation_plan.md#E2E-LOGIN",
                        to_id="lexicon:e2e_login_journey",
                        kind="produces",
                        attributes={"journey": "login_to_dashboard"},
                    )
                )

    if e2e:
        attrs = {
            "kind": "e2e",
            "target": "login",
            "verification_template_ref": "playwright",
            "expected_outcome": {"source": "tests/e2e/login.spec.ts"},
            "in_deploy_flow": True,
        }
        attrs.update(e2e_attrs or {})
        dag.add_node(
            Node(
                id="verification:e2e:tests/e2e/login.spec.ts",
                kind="verification_test",
                path="tests/e2e/login.spec.ts",
                attributes=attrs,
            )
        )

    if runtime_declared:
        dag.add_node(
            Node(
                id="runtime:server_running:server",
                kind="runtime_state",
                attributes={"capabilities_provided": runtime_caps or []},
            )
        )
    elif runtime_caps is not None:
        dag.add_node(
            Node(
                id="runtime:server_running:server",
                kind="runtime_state",
                attributes={},
            )
        )

    return dag


def _run(dag: DAG, tmp_path: Path, settings: dict | None = None):
    return UserJourneyCoherenceCheck().run(dag, tmp_path, settings or {})


def _types(result) -> set[str]:
    return {violation["type"] for violation in result.violations}


def _violation(result, violation_type: str) -> dict:
    return next(violation for violation in result.violations if violation["type"] == violation_type)


def test_user_journeys_undeclared_design_doc_skips_gracefully(tmp_path):
    # No user_journeys and no actors = C7 has no input to verify. It must report a
    # real SKIP (status/skipped), not a clean PASS that a verify summary cannot
    # distinguish from a real verification (false-green).
    dag = DAG()
    dag.add_node(Node(id="docs/design/auth.md", kind="design_doc", attributes={}))

    result = _run(dag, tmp_path)

    assert result.passed is True
    assert result.status == "skip"
    assert result.skipped is True
    assert result.checked_count == 0
    assert "SKIP" in result.message


def test_declared_journey_reports_checked_count(tmp_path):
    # A declared journey is actually verified, so checked_count is non-zero — the
    # verdict is materially distinct from the no-input (skip) case above, whether
    # the journey ultimately passes or fails.
    result = _run(_dag(plan_outputs=["lexicon:e2e_login_journey"], runtime_caps=[]), tmp_path)

    assert result.skipped is False
    assert result.checked_count >= 1


def test_actor_without_any_user_journey_is_amber_warning(tmp_path):
    dag = DAG()
    dag.add_node(Node(id="docs/design/auth.md", kind="design_doc", attributes={"actors": ["Operator"]}))

    result = _run(dag, tmp_path)

    assert result.passed is True
    assert result.severity == "amber"
    assert result.status == "warn"
    assert result.block_deploy is False
    assert result.violation_type == "actors_without_journeys"
    assert result.violations[0]["type"] == "actors_without_journeys"
    assert result.violations[0]["actors"] == ["Operator"]


def test_babok_stakeholder_role_without_any_user_journey_is_amber_warning(tmp_path):
    dag = DAG()
    dag.add_node(
        Node(
            id="finding:stakeholder_roles",
            kind="finding",
            attributes={
                "details": {
                    "dimension": "stakeholder",
                    "roles": [{"name": "Auditor"}],
                }
            },
        )
    )
    dag.add_node(Node(id="docs/design/auth.md", kind="design_doc", attributes={}))

    result = _run(dag, tmp_path)

    assert result.passed is True
    assert result.severity == "amber"
    assert result.violations[0]["actors"] == ["Auditor"]


def test_runtime_constraints_undeclared_does_not_emit_unsatisfied_runtime_capability(tmp_path):
    result = _run(_dag(plan_outputs=["lexicon:e2e_login_journey"], runtime_caps=[]), tmp_path)

    assert "unsatisfied_runtime_capability" not in _types(result)
    assert result.passed is True


def test_capabilities_provided_undeclared_does_not_emit_unsatisfied_runtime_capability(tmp_path):
    result = _run(
        _dag(
            constraints=[_constraint()],
            plan_outputs=["lexicon:e2e_login_journey"],
            runtime_declared=False,
            runtime_caps=None,
        ),
        tmp_path,
    )

    assert "unsatisfied_runtime_capability" not in _types(result)
    assert result.passed is True


def test_unsatisfied_runtime_capability_missing_is_red(tmp_path):
    result = _run(_dag(constraints=[_constraint()], plan_outputs=["lexicon:e2e_login_journey"], runtime_caps=[]), tmp_path)

    assert result.passed is False
    assert _violation(result, "unsatisfied_runtime_capability")["required_capability"] == "tls_termination"


def test_unsatisfied_runtime_capability_present_passes(tmp_path):
    result = _run(
        _dag(
            constraints=[_constraint()],
            plan_outputs=["lexicon:e2e_login_journey"],
            runtime_caps=["tls_termination"],
        ),
        tmp_path,
    )

    assert "unsatisfied_runtime_capability" not in _types(result)
    assert result.passed is True


def test_required_false_runtime_constraint_is_skipped(tmp_path):
    result = _run(
        _dag(
            constraints=[_constraint(required=False)],
            plan_outputs=["lexicon:e2e_login_journey"],
            runtime_caps=[],
        ),
        tmp_path,
    )

    assert "unsatisfied_runtime_capability" not in _types(result)
    assert result.passed is True


def test_capability_requirements_absent_skips_impl_evidence_runtime_mismatch(tmp_path):
    result = _run(
        _dag(
            plan_outputs=["lexicon:e2e_login_journey"],
            runtime_caps=[],
            impl_evidence=[{"capability_kind": "runtime_flag_enabled", "line_ref": "src/auth.ts:2"}],
        ),
        tmp_path,
    )

    assert "impl_evidence_runtime_mismatch" not in _types(result)
    assert result.passed is True


def test_impl_evidence_runtime_mismatch_missing_capability_is_red(tmp_path):
    result = _run(
        _dag(
            plan_outputs=["lexicon:e2e_login_journey"],
            runtime_caps=[],
            impl_evidence=[{"capability_kind": "runtime_flag_enabled", "line_ref": "src/auth.ts:2"}],
        ),
        tmp_path,
        {"coherence": {"capability_requirements": {"runtime_flag_enabled": {"requires_runtime": ["tls_termination"]}}}},
    )

    assert result.passed is False
    assert _violation(result, "impl_evidence_runtime_mismatch")["missing_runtime_capability"] == "tls_termination"


def test_impl_evidence_runtime_mismatch_passes_when_capability_present(tmp_path):
    result = _run(
        _dag(
            plan_outputs=["lexicon:e2e_login_journey"],
            runtime_caps=["tls_termination"],
            impl_evidence=[{"capability_kind": "runtime_flag_enabled", "line_ref": "src/auth.ts:2"}],
        ),
        tmp_path,
        {"coherence": {"capability_requirements": {"runtime_flag_enabled": {"requires_runtime": ["tls_termination"]}}}},
    )

    assert "impl_evidence_runtime_mismatch" not in _types(result)
    assert result.passed is True


def test_missing_journey_lexicon_outputs_suggested_entry(tmp_path):
    result = _run(
        _dag(
            expected=False,
            plan_outputs=["design:login_to_dashboard", "tests/e2e/login.spec.ts"],
            runtime_caps=[],
        ),
        tmp_path,
    )

    violation = _violation(result, "missing_journey_lexicon")
    assert violation["suggested_lexicon_entry"] == {
        "id": "e2e_login_journey",
        "title": "Login to dashboard E2E",
        "scope": "web_app",
        "source": "default_template",
        "journey": "login_to_dashboard",
        "path": "tests/e2e/login_to_dashboard.spec.ts",
    }


def test_existing_journey_lexicon_passes_missing_lexicon_check(tmp_path):
    result = _run(_dag(plan_outputs=["lexicon:e2e_login_journey"], runtime_caps=[]), tmp_path)

    assert "missing_journey_lexicon" not in _types(result)


def test_no_plan_task_for_journey_is_red(tmp_path):
    result = _run(_dag(plan_outputs=None, runtime_caps=[]), tmp_path)

    assert result.passed is False
    assert "no_plan_task_for_journey" in _types(result)


def test_plan_task_for_journey_passes_plan_check(tmp_path):
    result = _run(_dag(plan_outputs=["lexicon:e2e_login_journey"], runtime_caps=[]), tmp_path)

    assert "no_plan_task_for_journey" not in _types(result)


def test_no_e2e_test_for_journey_is_red(tmp_path):
    result = _run(_dag(plan_outputs=["lexicon:e2e_login_journey"], e2e=False, runtime_caps=[]), tmp_path)

    assert result.passed is False
    assert "no_e2e_test_for_journey" in _types(result)


def test_plan_task_to_e2e_verification_source_passes_e2e_check(tmp_path):
    result = _run(
        _dag(plan_outputs=["design:login_to_dashboard", "tests/e2e/login.spec.ts"], runtime_caps=[]),
        tmp_path,
    )

    assert "no_e2e_test_for_journey" not in _types(result)


def test_e2e_not_in_post_deploy_is_red(tmp_path):
    result = _run(
        _dag(
            plan_outputs=["lexicon:e2e_login_journey"],
            e2e_attrs={"in_deploy_flow": False},
            runtime_caps=[],
        ),
        tmp_path,
    )

    assert result.passed is False
    assert "e2e_not_in_post_deploy" in _types(result)


def test_e2e_in_post_deploy_hook_passes(tmp_path):
    (tmp_path / "deploy.yaml").write_text(
        "targets:\n  prod:\n    post_deploy:\n      - npx playwright test tests/e2e/login.spec.ts\n",
        encoding="utf-8",
    )

    result = _run(
        _dag(
            plan_outputs=["lexicon:e2e_login_journey"],
            e2e_attrs={"in_deploy_flow": False},
            runtime_caps=[],
        ),
        tmp_path,
    )

    assert "e2e_not_in_post_deploy" not in _types(result)


def test_browser_expected_not_asserted_is_red(tmp_path):
    result = _run(
        _dag(
            plan_outputs=["lexicon:e2e_login_journey"],
            expected_attrs={"browser_requirements": [{"capability": "state_saved", "value": True}]},
            runtime_caps=[],
        ),
        tmp_path,
    )

    assert result.passed is False
    assert _violation(result, "browser_expected_not_asserted")["required_capability"] == "state_saved"


def test_browser_expected_asserted_by_e2e_attributes_passes(tmp_path):
    result = _run(
        _dag(
            plan_outputs=["lexicon:e2e_login_journey"],
            expected_attrs={"browser_requirements": [{"capability": "state_saved", "value": True}]},
            e2e_attrs={"assertions": ["state_saved"]},
            runtime_caps=[],
        ),
        tmp_path,
    )

    assert "browser_expected_not_asserted" not in _types(result)


def test_presentation_locale_unspecified_is_red_for_display_field_without_spec(tmp_path):
    result = _run(
        _dag(
            plan_outputs=["lexicon:e2e_login_journey"],
            display_fields=[
                {
                    "field_id": "record.published_at",
                    "data_type": "datetime",
                    "lexicon_refs": ["i18n_unicode_cldr#time_zone_handling"],
                    "presentation_required": True,
                }
            ],
        ),
        tmp_path,
    )

    violation = _violation(result, "presentation_locale_unspecified")
    assert result.passed is False
    assert violation["field_id"] == "record.published_at"
    assert violation["missing_attributes"] == ["presentation_spec"]


def test_presentation_locale_violated_is_red_when_evidence_signal_is_unasserted(tmp_path):
    result = _run(
        _dag(
            plan_outputs=["lexicon:e2e_login_journey"],
            display_fields=[
                {
                    "field_id": "record.published_at",
                    "data_type": "datetime",
                    "expected_presentation_signals": ["record_published_at_locale_display"],
                }
            ],
            presentation_specs=[
                {
                    "field_id": "record.published_at",
                    "format": "YYYY-MM-DD HH:mm",
                    "timezone": "Etc/UTC",
                    "locale": "en-US",
                }
            ],
        ),
        tmp_path,
    )

    violation = _violation(result, "presentation_locale_violated")
    assert result.passed is False
    assert violation["missing_evidence_signals"] == ["record_published_at_locale_display"]


def test_presentation_locale_signal_assertion_passes(tmp_path):
    result = _run(
        _dag(
            plan_outputs=["lexicon:e2e_login_journey"],
            display_fields=[
                {
                    "field_id": "record.published_at",
                    "data_type": "datetime",
                    "expected_presentation_signals": ["record_published_at_locale_display"],
                }
            ],
            presentation_specs=[
                {
                    "field_id": "record.published_at",
                    "format": "YYYY-MM-DD HH:mm",
                    "timezone": "Etc/UTC",
                    "locale": "en-US",
                }
            ],
            e2e_attrs={"assertions": ["record_published_at_locale_display"]},
        ),
        tmp_path,
    )

    assert "presentation_locale_unspecified" not in _types(result)
    assert "presentation_locale_violated" not in _types(result)


def test_aggregation_policy_unspecified_is_red_for_many_source_display(tmp_path):
    result = _run(
        _dag(
            plan_outputs=["lexicon:e2e_login_journey"],
            display_fields=[
                {
                    "field_id": "record.summary_value",
                    "cardinality": "0..N",
                    "aggregation_required": True,
                }
            ],
        ),
        tmp_path,
    )

    violation = _violation(result, "aggregation_policy_unspecified")
    assert result.passed is False
    assert violation["field_id"] == "record.summary_value"
    assert violation["cardinality"] == "0..N"


def test_aggregation_policy_violated_is_red_when_evidence_signal_is_unasserted(tmp_path):
    result = _run(
        _dag(
            plan_outputs=["lexicon:e2e_login_journey"],
            display_fields=[
                {
                    "field_id": "record.summary_value",
                    "cardinality": "0..N",
                    "expected_aggregation_signals": ["record_summary_many_source_display"],
                }
            ],
            aggregation_policies=[
                {
                    "field_id": "record.summary_value",
                    "cardinality_when_many": {"policy": "average"},
                    "test_data_variants": {"required_cardinality": ["0", "1", "N"]},
                }
            ],
        ),
        tmp_path,
    )

    violation = _violation(result, "aggregation_policy_violated")
    assert result.passed is False
    assert violation["missing_evidence_signals"] == ["record_summary_many_source_display"]


def test_aggregation_policy_signal_assertion_passes(tmp_path):
    result = _run(
        _dag(
            plan_outputs=["lexicon:e2e_login_journey"],
            display_fields=[
                {
                    "field_id": "record.summary_value",
                    "cardinality": "0..N",
                    "expected_aggregation_signals": ["record_summary_many_source_display"],
                }
            ],
            aggregation_policies=[
                {
                    "field_id": "record.summary_value",
                    "cardinality_when_many": {"policy": "average"},
                    "test_data_variants": {"required_cardinality": ["0", "1", "N"]},
                }
            ],
            e2e_attrs={"assertions": ["record_summary_many_source_display"]},
        ),
        tmp_path,
    )

    assert "aggregation_policy_unspecified" not in _types(result)
    assert "aggregation_policy_violated" not in _types(result)


def test_journey_step_no_assertion_is_amber_and_does_not_fail(tmp_path):
    result = _run(
        _dag(
            journey=_journey(steps=[{"action": "navigate", "target": "/login"}]),
            plan_outputs=["lexicon:e2e_login_journey"],
            runtime_caps=[],
        ),
        tmp_path,
    )

    violation = _violation(result, "journey_step_no_assertion")
    assert result.passed is True
    assert result.severity == "amber"
    assert violation["severity"] == "amber"


def test_journey_step_with_assertion_passes_amber_check(tmp_path):
    result = _run(_dag(plan_outputs=["lexicon:e2e_login_journey"], runtime_caps=[]), tmp_path)

    assert "journey_step_no_assertion" not in _types(result)


def _write_e2e_source(tmp_path: Path, body: str) -> None:
    spec = tmp_path / "tests" / "e2e" / "login.spec.ts"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text(body, encoding="utf-8")


def test_weak_outcome_assertion_skipped_when_explicit_attribute_present(tmp_path):
    _write_e2e_source(tmp_path, "test('persists order', () => { doThing('order_persisted'); });\n")
    result = _run(
        _dag(
            plan_outputs=["lexicon:e2e_login_journey"],
            expected_attrs={"browser_requirements": [{"capability": "order_persisted", "value": True}]},
            e2e_attrs={"assertions": ["order_persisted"]},
            runtime_caps=[],
        ),
        tmp_path,
    )

    assert "weak_outcome_assertion" not in _types(result)


def test_weak_outcome_assertion_amber_for_source_presence_without_assertion(tmp_path):
    _write_e2e_source(
        tmp_path,
        "test('navigates', () => {\n  navigateTo('/order_persisted');\n  logMessage('order_persisted');\n});\n",
    )
    result = _run(
        _dag(
            plan_outputs=["lexicon:e2e_login_journey"],
            expected_attrs={"browser_requirements": [{"capability": "order_persisted", "value": True}]},
            runtime_caps=[],
        ),
        tmp_path,
    )

    violation = _violation(result, "weak_outcome_assertion")
    assert result.passed is True
    assert result.severity == "amber"
    assert violation["severity"] == "amber"
    assert violation["signal"] == "order_persisted"
    assert "browser_expected_not_asserted" not in _types(result)


def test_weak_outcome_assertion_silent_when_no_declared_signal(tmp_path):
    _write_e2e_source(tmp_path, "test('navigates', () => { navigateTo('/dashboard'); });\n")
    result = _run(
        _dag(plan_outputs=["lexicon:e2e_login_journey"], runtime_caps=[]),
        tmp_path,
    )

    assert "weak_outcome_assertion" not in _types(result)


def test_weak_outcome_assertion_skipped_when_source_has_assertion_context(tmp_path):
    _write_e2e_source(
        tmp_path,
        "test('persists order', () => {\n  expect(state).toContain('order_persisted');\n});\n",
    )
    result = _run(
        _dag(
            plan_outputs=["lexicon:e2e_login_journey"],
            expected_attrs={"browser_requirements": [{"capability": "order_persisted", "value": True}]},
            runtime_caps=[],
        ),
        tmp_path,
    )

    assert "weak_outcome_assertion" not in _types(result)


def test_format_report_outputs_journey_report_json(tmp_path):
    result = _run(_dag(constraints=[_constraint()], plan_outputs=["lexicon:e2e_login_journey"], runtime_caps=[]), tmp_path)

    payload = json.loads(UserJourneyCoherenceCheck().format_report(result))

    assert payload["user_journey_coherence_report"][0]["user_journey"] == "login_to_dashboard"
    assert payload["user_journey_coherence_report"][0]["violations"][0]["type"] == "unsatisfied_runtime_capability"


def test_human_review_required_defaults_false(tmp_path):
    result = _run(_dag(constraints=[_constraint()], plan_outputs=["lexicon:e2e_login_journey"], runtime_caps=[]), tmp_path)

    assert _violation(result, "unsatisfied_runtime_capability")["human_review_required"] is False


def test_human_review_required_true_for_budget_level_rationale(tmp_path):
    result = _run(
        _dag(
            constraints=[_constraint(rationale="budget approval needed before changing runtime")],
            plan_outputs=["lexicon:e2e_login_journey"],
            runtime_caps=[],
        ),
        tmp_path,
    )

    assert _violation(result, "unsatisfied_runtime_capability")["human_review_required"] is True


def test_generality_gate_has_no_forbidden_stack_literals():
    source = Path("codd/dag/checks/user_journey_coherence.py").read_text(encoding="utf-8")

    for token in ("NextAuth", "__Secure-", "cookie", "SameSite", "chromium"):
        assert token not in source


def test_tonight_incident_simulation_reports_both_red_violations(tmp_path):
    result = _run(
        _dag(
            constraints=[_constraint(capability="tls_termination")],
            plan_outputs=["lexicon:e2e_login_journey"],
            runtime_caps=[],
            impl_evidence=[
                {
                    "capability_kind": "cookie_security_secure_attribute",
                    "value": True,
                    "line_ref": "src/auth.ts:42",
                }
            ],
        ),
        tmp_path,
        {
            "coherence": {
                "capability_requirements": {
                    "cookie_security_secure_attribute": {"requires_runtime": ["tls_termination"]}
                }
            }
        },
    )

    assert result.passed is False
    assert {"unsatisfied_runtime_capability", "impl_evidence_runtime_mismatch"} <= _types(result)
    assert all(
        _violation(result, violation_type)["severity"] == "red"
        for violation_type in ("unsatisfied_runtime_capability", "impl_evidence_runtime_mismatch")
    )
