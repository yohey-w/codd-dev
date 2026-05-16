"""Tests for bridge integration points."""

from __future__ import annotations

from click.testing import CliRunner
import yaml

from codd.bridge import get_command_handler
from codd.cli import main
from codd.policy import PolicyResult, run_policy
from codd.validator import ValidationResult, validate_project


class _FakeEntryPoint:
    def __init__(self, loaded):
        self._loaded = loaded

    def load(self):
        return self._loaded


def _write_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(
            {
                "project": {"name": "demo", "language": "python"},
                "scan": {"source_dirs": [], "test_dirs": [], "doc_dirs": [], "config_files": [], "exclude": []},
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return project, codd_dir


def test_validate_project_uses_registered_bridge_handler(tmp_path, monkeypatch):
    project, codd_dir = _write_project(tmp_path)
    expected = ValidationResult(documents_checked=42)

    def register(registry):
        def handler(project_root, configured_codd_dir, fallback):
            assert project_root == project
            assert configured_codd_dir == codd_dir
            assert callable(fallback)
            return expected

        registry.register_validator(handler)

    monkeypatch.setattr(
        "codd.bridge.entry_points",
        lambda *, group=None: (_FakeEntryPoint(register),),
    )

    assert validate_project(project, codd_dir) is expected


def test_run_policy_uses_registered_bridge_handler(tmp_path, monkeypatch):
    project, _ = _write_project(tmp_path)
    expected = PolicyResult(files_checked=7, rules_applied=3)

    def register(registry):
        def handler(project_root, *, changed_files, fallback):
            assert project_root == project
            assert changed_files == ["src/demo.py"]
            assert callable(fallback)
            return expected

        registry.register_policy(handler)

    monkeypatch.setattr(
        "codd.bridge.entry_points",
        lambda *, group=None: (_FakeEntryPoint(register),),
    )

    assert run_policy(project, changed_files=["src/demo.py"]) is expected


def test_get_command_handler_uses_plugin_resolver(monkeypatch):
    def sentinel():
        return None

    def register(registry):
        registry.resolve_command_handler = lambda name: sentinel if name == "verify" else None

    monkeypatch.setattr(
        "codd.bridge.entry_points",
        lambda *, group=None: (_FakeEntryPoint(register),),
    )

    assert get_command_handler("verify") is sentinel
    assert get_command_handler("review") is None


def test_removed_legacy_commands_are_unknown():
    runner = CliRunner()

    for args in (["review"], ["audit"], ["risk"]):
        result = runner.invoke(main, args)
        assert result.exit_code != 0
