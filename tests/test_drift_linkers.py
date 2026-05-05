from pathlib import Path

import yaml

from codd import drift_linkers


def test_register_linker_adds_to_registry(monkeypatch):
    monkeypatch.setattr(drift_linkers, "_REGISTRY", {})

    @drift_linkers.register_linker("api")
    class ApiLinker:
        pass

    assert drift_linkers.get_registry() == {"api": ApiLinker}


def test_get_registry_returns_copy(monkeypatch):
    monkeypatch.setattr(drift_linkers, "_REGISTRY", {})

    @drift_linkers.register_linker("schema")
    class SchemaLinker:
        pass

    registry = drift_linkers.get_registry()
    registry["other"] = object

    assert drift_linkers.get_registry() == {"schema": SchemaLinker}


def test_run_all_linkers_empty_registry_graceful(monkeypatch, tmp_path):
    monkeypatch.setattr(drift_linkers, "_REGISTRY", {})

    assert drift_linkers.run_all_linkers(
        tmp_path / "expected.yaml",
        tmp_path,
        {"enabled": []},
    ) == []


def test_defaults_yaml_load():
    defaults_dir = Path("codd/drift_linkers/defaults")

    payloads = {
        path.stem: yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for path in sorted(defaults_dir.glob("*.yaml"))
    }

    assert set(payloads) == {"web", "cli", "mobile", "iot"}
    assert payloads["web"]["enabled"] == ["api", "schema", "screen_flow"]
    assert payloads["web"]["design_files"]["api"] == "docs/design/api_design.md"
    assert payloads["cli"]["enabled"] == []
    assert payloads["mobile"]["enabled"] == ["schema"]
    assert payloads["iot"]["enabled"] == []
