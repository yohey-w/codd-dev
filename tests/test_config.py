"""Tests for merged CoDD configuration loading."""

from pathlib import Path

import yaml

import codd.config as config_module
from codd.propagator import _map_files_to_modules


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


def test_load_project_config_source_dirs_override_defaults_for_nested_framework_layout(
    tmp_path,
    monkeypatch,
):
    defaults_path = tmp_path / "defaults.yaml"
    defaults_path.write_text(
        yaml.safe_dump(
            {
                "scan": {
                    "source_dirs": ["src/"],
                    "doc_dirs": ["docs/"],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "DEFAULTS_PATH", defaults_path)

    project = tmp_path / "project"
    codd_dir = project / "codd"
    codd_dir.mkdir(parents=True)
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(
            {
                "scan": {
                    "source_dirs": ["src/lib/"],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    loaded = config_module.load_project_config(project)

    assert loaded["scan"]["source_dirs"] == ["src/lib/"]
    assert _map_files_to_modules(
        [
            "src/lib/editor/NoteEditor.svelte",
            "src/lib/feed/Feed.svelte",
        ],
        loaded["scan"]["source_dirs"],
    ) == {
        "src/lib/editor/NoteEditor.svelte": "editor",
        "src/lib/feed/Feed.svelte": "feed",
    }


def test_load_project_config_uses_default_source_dirs_when_project_omits_them(
    tmp_path,
    monkeypatch,
):
    defaults_path = tmp_path / "defaults.yaml"
    defaults_path.write_text(
        yaml.safe_dump(
            {
                "scan": {
                    "source_dirs": ["src/"],
                    "doc_dirs": ["docs/"],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "DEFAULTS_PATH", defaults_path)

    project = tmp_path / "project"
    codd_dir = project / "codd"
    codd_dir.mkdir(parents=True)
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(
            {
                "scan": {
                    "doc_dirs": ["docs/design/"],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    loaded = config_module.load_project_config(project)

    assert loaded["scan"]["source_dirs"] == ["src/"]
    assert loaded["scan"]["doc_dirs"] == ["docs/", "docs/design/"]
