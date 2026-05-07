"""Loadability tests for the ops_kubernetes lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "ops_kubernetes"
EXPECTED_KINDS = {
    "kubernetes_workload_gap",
    "kubernetes_service_networking_gap",
    "kubernetes_config_secret_gap",
    "kubernetes_storage_gap",
    "kubernetes_rbac_gap",
    "kubernetes_scheduling_gap",
    "kubernetes_resource_quota_gap",
    "kubernetes_probe_gap",
    "kubernetes_autoscaling_gap",
    "kubernetes_observability_gap",
    "kubernetes_upgrade_gap",
    "kubernetes_namespace_gap",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_ops_kubernetes_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "ops_kubernetes"
    assert "Kubernetes Operations Coverage Lexicon" in (
        config.prompt_extension_content
    )
    assert set(config.recommended_kinds) == EXPECTED_KINDS


def test_ops_kubernetes_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "ops_kubernetes"
    assert manifest["lexicon_name"] == "ops_kubernetes"
    assert manifest["source_url"].endswith("/v1.30/")
    assert "Kubernetes API Reference v1.30.0" in manifest["source_version"]
    assert manifest["observation_dimensions"] == 12
    titles = {item["title"] for item in manifest["references"]}
    assert "Kubernetes API Reference v1.30" in titles
    assert "Kubernetes Concepts" in titles
    assert "Upgrade A Cluster" in titles


def test_ops_kubernetes_required_plugin_files_exist():
    for filename in (
        "manifest.yaml",
        "lexicon.yaml",
        "severity_rules.yaml",
        "coverage_matrix.md",
        "elicit_extend.md",
        "recommended_kinds.yaml",
    ):
        assert (LEXICON_ROOT / filename).is_file()


def test_ops_kubernetes_loaded_prompt_includes_base_prompt():
    config = load_lexicon(LEXICON_ROOT)

    assert "Elicitation Prompt L0" in config.prompt_extension_content
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )
