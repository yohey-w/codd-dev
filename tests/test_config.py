"""Tests for merged CoDD configuration loading."""

from pathlib import Path

import yaml

import codd.config as config_module


def test_load_project_config_merges_defaults_and_project_overrides(tmp_path, monkeypatch):
    defaults_path = tmp_path / "defaults.yaml"
    defaults_path.write_text(
        yaml.safe_dump(
            {
                "ai_command": "default-ai --print",
                "coding_principles": "docs/defaults.md",
                "scan": {
                    "doc_dirs": ["docs/requirements/"],
                    "exclude": ["**/node_modules/**"],
                },
                "conventions": [
                    {
                        "targets": ["db:rls_policies"],
                        "reason": "Default tenant isolation rule.",
                    }
                ],
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "DEFAULTS_PATH", defaults_path)

    project = tmp_path / "project"
    project.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(
            {
                "project": {"name": "demo", "language": "typescript"},
                "coding_principles": "docs/custom_principles.md",
                "scan": {
                    "doc_dirs": ["docs/design/"],
                    "exclude": ["**/.next/**"],
                },
                "conventions": [
                    {
                        "targets": ["module:auth"],
                        "reason": "Project auth rule.",
                    }
                ],
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    config = config_module.load_project_config(project)

    assert config["ai_command"] == "default-ai --print"
    assert config["coding_principles"] == "docs/custom_principles.md"
    assert config["scan"]["doc_dirs"] == ["docs/requirements/", "docs/design/"]
    assert config["scan"]["exclude"] == ["**/node_modules/**", "**/.next/**"]
    assert config["conventions"] == [
        {
            "targets": ["db:rls_policies"],
            "reason": "Default tenant isolation rule.",
        },
        {
            "targets": ["module:auth"],
            "reason": "Project auth rule.",
        },
    ]
