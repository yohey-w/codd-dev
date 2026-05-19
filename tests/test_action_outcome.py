from __future__ import annotations

from codd.action_outcome import (
    ActionTargetSpec,
    action_target_specs_from_config,
    compare_action_outcome_coverage,
    extract_action_requirements,
)


def test_extracts_generic_mutating_actions_from_operation_flow() -> None:
    flow = {
        "operations": [
            {"id": "record_create", "verb": "add", "target": "record", "actor": "operator"},
            {"id": "record_update", "verb": "edit", "target": "record"},
            {"id": "record_delete", "verb": "remove", "target": "record"},
            {"id": "record_view", "verb": "view", "target": "record"},
        ]
    }

    requirements = extract_action_requirements(flow, source="docs/requirements/record.md")

    assert [(item.operation_id, item.verb, item.target) for item in requirements] == [
        ("record_create", "create", "record"),
        ("record_update", "update", "record"),
        ("record_delete", "delete", "record"),
    ]
    assert requirements[0].actor == "operator"


def test_compare_reports_update_delete_missing_when_targets_are_add_only() -> None:
    flow = {
        "operations": [
            {"id": "record_create", "verb": "add", "target": "record"},
            {"id": "record_update", "verb": "edit", "target": "record"},
            {"id": "record_delete", "verb": "remove", "target": "record"},
        ]
    }
    requirements = extract_action_requirements(flow)

    result = compare_action_outcome_coverage(
        requirements,
        [ActionTargetSpec(target_name="add smoke", action_id="record.create", verb="create", target="record")],
    )

    assert result.covered is False
    assert [gap.missing_verbs for gap in result.gaps] == [("update",), ("delete",)]


def test_compare_accepts_action_outcome_targets_by_verb_and_target() -> None:
    flow = {
        "operations": [
            {"id": "record_update_flow", "verb": "edit", "target": "record"},
            {"id": "record_delete_flow", "verb": "remove", "target": "record"},
        ]
    }
    requirements = extract_action_requirements(flow)
    specs = action_target_specs_from_config(
        {
            "runtime": {
                "action_outcome_targets": [
                    {
                        "name": "record command outcomes",
                        "actions": [
                            {"id": "record.update", "verb": "update", "target": "record", "outcomes": ["persisted"]},
                            {"id": "record.delete", "verb": "delete", "target": "record", "outcomes": ["absence"]},
                        ],
                        "command": "pytest tests/e2e/test_record.py",
                    }
                ]
            }
        }
    )

    result = compare_action_outcome_coverage(requirements, specs)

    assert result.covered is True
    assert [spec.outcomes for spec in specs] == [("persisted",), ("absence",)]


def test_manage_collection_is_ambiguous_until_create_update_delete_are_declared() -> None:
    requirements = extract_action_requirements(
        {"operations": [{"id": "manage_records", "verb": "manage_collection", "target": "record"}]}
    )

    result = compare_action_outcome_coverage(
        requirements,
        [
            ActionTargetSpec(target_name="create only", action_id="record.create", verb="create", target="record"),
        ],
    )

    assert requirements[0].ambiguous is True
    assert result.gaps[0].missing_verbs == ("update", "delete")
