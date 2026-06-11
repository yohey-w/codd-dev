"""Tests for the advisory config-key typo guard (codd/config_schema.py)."""

from __future__ import annotations

import textwrap
from pathlib import Path

from click.testing import CliRunner

from codd.cli import main
from codd.config_schema import project_config_key_warnings, validate_config_keys


# --- unknown keys are warned with did-you-mean suggestions --------------------


def test_unknown_top_level_key_warns_with_suggestion():
    warnings = validate_config_keys({"surface_reconcilation": {"enabled": False}})

    assert len(warnings) == 1
    assert "unknown config key 'surface_reconcilation'" in warnings[0]
    assert "did you mean 'surface_reconciliation'?" in warnings[0]


def test_unknown_nested_key_warns_with_dotted_path_and_suggestion():
    warnings = validate_config_keys({"verify": {"e2e_comand": "npx playwright test"}})

    assert len(warnings) == 1
    assert "unknown config key 'verify.e2e_comand'" in warnings[0]
    assert "did you mean 'e2e_command'?" in warnings[0]


def test_unknown_deep_nested_key_warns():
    warnings = validate_config_keys({"surface_reconciliation": {"enabld": True}})

    assert len(warnings) == 1
    assert "unknown config key 'surface_reconciliation.enabld'" in warnings[0]
    assert "did you mean 'enabled'?" in warnings[0]


def test_unknown_key_without_close_match_still_warns():
    warnings = validate_config_keys({"zzz_totally_made_up": 1})

    assert len(warnings) == 1
    assert "unknown config key 'zzz_totally_made_up'" in warnings[0]
    assert "did you mean" not in warnings[0]


# --- open sections: user-defined children are never validated -----------------


def test_ai_commands_custom_names_are_not_warned():
    config = {"ai_commands": {"impl_step_derive": "codex exec", "my_custom_cmd": "x"}}

    assert validate_config_keys(config) == []


def test_artifact_contract_stages_children_are_not_warned():
    config = {
        "artifact_contract": {
            "enabled": True,
            "stages": {"anything_goes": {"required": ["design_doc"]}},
        }
    }

    assert validate_config_keys(config) == []


def test_frontmatter_alias_entries_are_not_warned():
    config = {"extraction": {"frontmatter_alias": {"reqs": "requirements", "x": "y"}}}

    assert validate_config_keys(config) == []


def test_operation_flow_and_capability_patterns_are_open():
    config = {
        "operation_flow": {"operations": [{"id": "create_post", "verb": "create"}]},
        "coherence": {"capability_patterns": {"my_capability": ["src/**"]}},
        "project": {"name": "demo", "frameworks": [], "free_form": True},
        "dag": {"enabled_checks": ["edge_coverage"], "node_extraction": {}},
        "policies": [{"id": "p1", "rule": "no_silent_fallback"}],
    }

    assert validate_config_keys(config) == []


# --- keys read by code but absent from defaults.yaml ---------------------------


def test_code_read_top_level_keys_are_known():
    config = {
        "codex_app_server": {"transport": "stdio", "model": "gpt-5.5"},
        "wave_config": {"waves": []},
        "repair": {"allow_auto": {}},
        "implementer": {"approval_mode_per_step_kind": {}},
        "preflight": {"critical_operations": []},
        "required_artifacts": {"design_doc": {}},
        "lexicon_path": "project_lexicon.yaml",
    }

    assert validate_config_keys(config) == []


def test_code_read_nested_keys_are_known():
    config = {
        "scan": {"common_node_patterns": ["src/**"]},
        "coherence": {"lexicon_path": "lex.yaml", "capability_requirements": {}},
        "runtime": {"global_action_targets": []},
        "requirement_completeness": {"hitl_mode": "cooperative"},
    }

    assert validate_config_keys(config) == []


def test_list_items_are_never_validated():
    config = {
        "runtime": {
            "crud_flow_targets": [{"made_up_target_key": 1, "url": "/x"}],
            "action_outcome_targets": [{"weird": True}],
        },
        "conventions": [{"anything": "goes"}],
    }

    assert validate_config_keys(config) == []


# --- clean config -> no warnings ----------------------------------------------


def test_clean_config_yields_no_warnings():
    config = {
        "version": "0.2.0a1",
        "project": {"frameworks": ["nextjs"]},
        "scan": {"source_dirs": ["src/"], "test_dirs": ["tests/"]},
        "verify": {"test_command": "pytest -q", "e2e_command": "npx playwright test"},
        "surface_reconciliation": {"enabled": False},
        "fix": {"max_attempts": 5, "phenomenon": {"propagate_impl": False}},
    }

    assert validate_config_keys(config) == []


def test_defaults_override_for_tests():
    warnings = validate_config_keys(
        {"custom_sectoin": 1},
        defaults={"custom_section": {}},
    )

    assert len(warnings) == 1
    assert "did you mean 'custom_sectoin'" not in warnings[0]
    assert "did you mean 'custom_section'?" in warnings[0]


# --- doctor integration ---------------------------------------------------------


def _project_with_config(tmp_path: Path, config_text: str) -> Path:
    project = tmp_path / "proj"
    (project / "codd").mkdir(parents=True)
    (project / "codd" / "codd.yaml").write_text(
        textwrap.dedent(config_text), encoding="utf-8"
    )
    return project


def test_doctor_reports_unknown_key_and_stays_advisory(tmp_path: Path) -> None:
    project = _project_with_config(
        tmp_path,
        """
        project:
          frameworks: []
        surface_reconcilation:
          enabled: false
        """,
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "CoDD doctor: WARN" in result.output
    assert "unknown config key 'surface_reconcilation'" in result.output
    assert "did you mean 'surface_reconciliation'?" in result.output


def test_doctor_silent_on_clean_config(tmp_path: Path) -> None:
    project = _project_with_config(
        tmp_path,
        """
        project:
          frameworks: []
        surface_reconciliation:
          enabled: false
        """,
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "unknown config key" not in result.output


def test_project_config_key_warnings_handles_missing_project(tmp_path: Path) -> None:
    assert project_config_key_warnings(tmp_path) == []
