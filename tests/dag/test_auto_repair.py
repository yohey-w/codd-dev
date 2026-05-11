"""Auto-repair flag tests (cmd_466 #6)."""

from __future__ import annotations

from pathlib import Path

import yaml

from codd.dag.auto_repair import RepairOutcome, apply_auto_repair


def _seed_lexicon(path: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "version": "1.0",
                "project": "test",
                "required_artifacts": [
                    {
                        "id": "e2e_existing",
                        "title": "Existing",
                        "scope": "web_app",
                        "source": "user_override",
                        "journey": "existing",
                        "path": "tests/e2e/existing.spec.ts",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_apply_missing_journey_lexicon_dry_run(tmp_path: Path) -> None:
    _seed_lexicon(tmp_path / "project_lexicon.yaml")
    suggested = {
        "id": "e2e_new",
        "title": "New E2E",
        "scope": "web_app",
        "source": "default_template",
        "journey": "new",
        "path": "tests/e2e/new.spec.ts",
    }
    results = [
        {
            "check_name": "user_journey_coherence",
            "status": "fail",
            "violations": [
                {"type": "missing_journey_lexicon", "suggested_lexicon_entry": suggested}
            ],
        }
    ]

    outcome = apply_auto_repair(tmp_path, results, dry_run=True)

    assert isinstance(outcome, RepairOutcome)
    assert any("e2e_new" in action.description for action in outcome.applied)
    # Dry run must not write.
    payload = yaml.safe_load((tmp_path / "project_lexicon.yaml").read_text())
    assert {entry["id"] for entry in payload["required_artifacts"]} == {"e2e_existing"}


def test_apply_missing_journey_lexicon_writes_when_apply(tmp_path: Path) -> None:
    _seed_lexicon(tmp_path / "project_lexicon.yaml")
    suggested = {
        "id": "e2e_new",
        "title": "New E2E",
        "scope": "web_app",
        "source": "default_template",
        "journey": "new",
        "path": "tests/e2e/new.spec.ts",
    }
    results = [
        {
            "check_name": "user_journey_coherence",
            "violations": [
                {"type": "missing_journey_lexicon", "suggested_lexicon_entry": suggested}
            ],
        }
    ]

    outcome = apply_auto_repair(tmp_path, results, dry_run=False)
    assert any("e2e_new" in action.description for action in outcome.applied)

    payload = yaml.safe_load((tmp_path / "project_lexicon.yaml").read_text())
    ids = {entry["id"] for entry in payload["required_artifacts"]}
    assert ids == {"e2e_existing", "e2e_new"}


def test_existing_lexicon_entry_skipped(tmp_path: Path) -> None:
    _seed_lexicon(tmp_path / "project_lexicon.yaml")
    suggested = {
        "id": "e2e_existing",  # already present
        "title": "Existing",
        "scope": "web_app",
        "source": "default_template",
        "journey": "existing",
        "path": "tests/e2e/existing.spec.ts",
    }
    results = [
        {
            "violations": [
                {"type": "missing_journey_lexicon", "suggested_lexicon_entry": suggested}
            ],
        }
    ]

    outcome = apply_auto_repair(tmp_path, results, dry_run=False)
    assert outcome.applied == []
    assert any("already declared" in action.description for action in outcome.skipped)


def test_non_repairable_violation_recorded_in_skipped(tmp_path: Path) -> None:
    _seed_lexicon(tmp_path / "project_lexicon.yaml")
    results = [
        {
            "violations": [
                {"type": "no_plan_task_for_journey", "user_journey": "x"},
                {"type": "journey_not_executed_under_variant"},
            ],
        }
    ]

    outcome = apply_auto_repair(tmp_path, results, dry_run=True)
    assert outcome.applied == []
    assert {action.description for action in outcome.skipped} >= {
        "no_plan_task_for_journey — no mechanical repair available",
        "journey_not_executed_under_variant — no mechanical repair available",
    }


def test_missing_project_lexicon_yields_no_applied(tmp_path: Path) -> None:
    suggested = {
        "id": "e2e_new",
        "title": "New",
        "scope": "web_app",
        "source": "default_template",
    }
    results = [
        {
            "violations": [
                {"type": "missing_journey_lexicon", "suggested_lexicon_entry": suggested}
            ]
        }
    ]

    outcome = apply_auto_repair(tmp_path, results, dry_run=False)
    assert outcome.applied == []
