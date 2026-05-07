"""Loadability tests for the ops_cicd_pipeline lexicon plug-in."""

from pathlib import Path

import yaml

from codd.elicit.lexicon_loader import load_lexicon


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "ops_cicd_pipeline"


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_ops_cicd_pipeline_manifest_loads_as_lexicon_config():
    config = load_lexicon(LEXICON_ROOT)

    assert config.lexicon_name == "ops_cicd_pipeline"
    assert "CI/CD Pipeline GitOps Coverage Lexicon" in config.prompt_extension_content
    assert "extends: codd/elicit/templates/elicit_prompt_L0.md" not in (
        config.prompt_extension_content
    )


def test_ops_cicd_pipeline_manifest_declares_source_metadata():
    manifest = _yaml("manifest.yaml")

    assert manifest["name"] == "ops_cicd_pipeline"
    assert manifest["lexicon_name"] == "ops_cicd_pipeline"
    assert manifest["source_url"] == "https://www.cncf.io/projects/argo/"
    assert "OpenGitOps Principles" in manifest["source_version"]
    titles = {item["title"] for item in manifest["references"]}
    assert "OpenGitOps Principles" in titles
    assert "CNCF Argo project context" in titles


def test_ops_cicd_pipeline_recommended_kinds_load():
    config = load_lexicon(LEXICON_ROOT)

    assert set(config.recommended_kinds) == {
        "declarative_config_gap",
        "version_control_gap",
        "automated_apply_gap",
        "continuous_reconciliation_gap",
        "drift_detection_gap",
        "rollback_gap",
        "pipeline_observability_gap",
    }

