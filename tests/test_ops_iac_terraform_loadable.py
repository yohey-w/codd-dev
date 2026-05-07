"""Loadability tests for the ops_iac_terraform lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "ops_iac_terraform"
EXPECTED_KINDS = {
    "terraform_settings_gap",
    "terraform_provider_gap",
    "terraform_resource_gap",
    "terraform_data_source_gap",
    "terraform_variable_output_gap",
    "terraform_state_gap",
    "terraform_module_gap",
    "terraform_workspace_gap",
    "terraform_backend_gap",
    "terraform_sentinel_policy_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_ops_iac_terraform_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "ops_iac_terraform"
    assert "Terraform IaC Coverage Lexicon" in config.prompt_extension_content
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_ops_iac_terraform_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "ops_iac_terraform"
    assert manifest["lexicon_name"] == "ops_iac_terraform"
    assert manifest["source_url"] == (
        "https://developer.hashicorp.com/terraform/language/v1.7.x"
    )
    assert "Terraform Language v1.7.x" in manifest["source_version"]
    assert "Sentinel policy enforcement" in manifest["source_version"]
    assert manifest["observation_dimensions"] == 10
    titles = {item["title"] for item in manifest["references"]}
    assert "Terraform Language Documentation v1.7.x" in titles
    assert "State: Workspaces" in titles
    assert "Define Sentinel policies in HCP Terraform" in titles


def test_ops_iac_terraform_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_ops_iac_terraform_loaded_prompt_includes_base_prompt():
    config = load_lexicon(LEXICON_ROOT)

    assert "Elicitation Prompt L0" in config.prompt_extension_content
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )
