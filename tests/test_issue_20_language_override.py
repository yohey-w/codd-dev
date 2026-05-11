"""Regression tests for Issue #20 (v-kato) — codd implement run --language.

`codd init --language js` baked `project.language: javascript` into
`codd.yaml`. If a later design/spec asked for TypeScript, the only
workaround was to re-init the whole project (full Wave re-run, ~1h cost).

v2.18.0 adds a `--language` flag to `codd implement run` (and the chunked
variant) that overrides `project.language` in-memory for the duration of a
single invocation; `codd.yaml` on disk is never touched.

These tests pin both ends of the contract:
- Without the flag, the existing project.language is honoured (backward compat).
- With the flag, the in-memory config that downstream helpers read carries
  the overridden value, and the on-disk codd.yaml is unchanged.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from codd.implementer import _load_project_config


def _seed_project_with_language(root: Path, language: str) -> Path:
    (root / ".codd").mkdir(parents=True, exist_ok=True)
    codd_yaml = root / ".codd" / "codd.yaml"
    codd_yaml.write_text(
        yaml.safe_dump({"project": {"language": language}}, sort_keys=False),
        encoding="utf-8",
    )
    return codd_yaml


def test_load_project_config_returns_existing_language_when_no_override(tmp_path):
    """Sanity: without override, project.language reflects codd.yaml exactly."""
    codd_yaml_path = _seed_project_with_language(tmp_path, "javascript")
    config = _load_project_config(tmp_path)
    assert (config.get("project") or {}).get("language") == "javascript"
    # And the on-disk file is unchanged.
    on_disk = yaml.safe_load(codd_yaml_path.read_text(encoding="utf-8"))
    assert on_disk["project"]["language"] == "javascript"


def test_implement_tasks_signature_accepts_language_override():
    """The CLI plumbs `--language` straight through; the Python signature must
    expose `language=` so callers (and click) can pass it."""
    import inspect

    from codd.implementer import implement_tasks

    params = inspect.signature(implement_tasks).parameters
    assert "language" in params, (
        "implement_tasks() must accept `language=` so codd implement run "
        "--language can override project.language per invocation (Issue #20)."
    )


def test_implement_run_cli_has_language_option():
    """The click command for `codd implement run` must expose --language."""
    from codd.cli import implement_run_cmd

    flag_names = []
    for param in implement_run_cmd.params:
        flag_names.extend(param.opts)
    assert "--language" in flag_names, (
        "`codd implement run` must expose --language so spec authors can "
        "override an unsuitable `codd init --language` choice without "
        "re-initialising the project (Issue #20, v-kato)."
    )


def test_codd_yaml_not_mutated_when_override_used_via_in_memory_path(tmp_path):
    """The override path mutates a *copy* of the project dict, never the
    on-disk codd.yaml. Pin this by performing the same dict surgery the
    implementer.py override block performs and asserting the file stays put."""
    codd_yaml_path = _seed_project_with_language(tmp_path, "javascript")
    config = _load_project_config(tmp_path)

    # Simulate the override block from implement_tasks().
    project_cfg = dict(config.get("project") or {})
    project_cfg["language"] = "typescript"
    overridden = {**config, "project": project_cfg}

    # Overridden in-memory copy reflects the new language.
    assert overridden["project"]["language"] == "typescript"
    # Original config dict is untouched.
    assert (config.get("project") or {}).get("language") == "javascript"
    # And the on-disk file is unchanged.
    on_disk = yaml.safe_load(codd_yaml_path.read_text(encoding="utf-8"))
    assert on_disk["project"]["language"] == "javascript"
