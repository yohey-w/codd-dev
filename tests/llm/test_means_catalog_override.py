from __future__ import annotations

from pathlib import Path

import yaml

from codd.dag.extractor import extract_verification_means_catalog
from codd.llm.means_catalog_loader import MeansCatalogLoader


DEFAULT_DOMAINS = {"web_app", "mobile_app", "desktop_app", "cli_tool", "backend_api", "embedded"}


def _write_yaml(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def test_project_lexicon_section_passthrough_has_top_priority(tmp_path: Path):
    means = _write_yaml(tmp_path / "config" / "means.yaml", {"from_config": ["runner_b"]})
    config = _write_yaml(tmp_path / "codd.yaml", {"llm": {"verification_means_catalog_path": str(means)}})

    catalog = MeansCatalogLoader().resolve(
        codd_yaml_path=str(config),
        project_lexicon_catalog={"from_lexicon": ["runner_a"]},
    )

    assert catalog == {"from_lexicon": ["runner_a"]}


def test_project_lexicon_file_has_top_priority_over_config(tmp_path: Path):
    lexicon = _write_yaml(tmp_path / "project_lexicon.yaml", {"verification_means_catalog": {"custom": ["one"]}})
    means = _write_yaml(tmp_path / "means.yaml", {"from_config": ["two"]})
    config = _write_yaml(tmp_path / "codd.yaml", {"llm": {"verification_means_catalog_path": str(means)}})

    catalog = MeansCatalogLoader().resolve(str(lexicon), str(config))

    assert catalog == {"custom": ["one"]}


def test_project_override_completely_replaces_core_default(tmp_path: Path):
    lexicon = _write_yaml(tmp_path / "project_lexicon.yaml", {"verification_means_catalog": {"custom": ["one"]}})

    catalog = MeansCatalogLoader().resolve(str(lexicon), None)

    assert catalog == {"custom": ["one"]}
    assert DEFAULT_DOMAINS.isdisjoint(catalog)


def test_codd_yaml_verification_means_catalog_path_loads_file(tmp_path: Path):
    means = _write_yaml(tmp_path / "config" / "means.yaml", {"from_config": ["runner"]})
    config = _write_yaml(tmp_path / "codd.yaml", {"llm": {"verification_means_catalog_path": "config/means.yaml"}})

    catalog = MeansCatalogLoader().resolve(None, str(config))

    assert catalog == {"from_config": ["runner"]}


def test_legacy_llm_means_catalog_path_still_loads_file(tmp_path: Path):
    means = _write_yaml(tmp_path / "config" / "means.yaml", {"legacy": ["runner"]})
    config = _write_yaml(tmp_path / "codd.yaml", {"llm": {"means_catalog_path": "config/means.yaml"}})

    catalog = MeansCatalogLoader().resolve(None, str(config))

    assert catalog == {"legacy": ["runner"]}


def test_codd_yaml_path_inside_codd_directory_resolves_from_project_root(tmp_path: Path):
    _write_yaml(tmp_path / "config" / "means.yaml", {"project_root_relative": ["runner"]})
    config = _write_yaml(
        tmp_path / ".codd" / "codd.yaml",
        {"llm": {"verification_means_catalog_path": "config/means.yaml"}},
    )

    catalog = MeansCatalogLoader().resolve(None, str(config))

    assert catalog == {"project_root_relative": ["runner"]}


def test_fallback_without_overrides_returns_core_default():
    catalog = MeansCatalogLoader().resolve(None, None)

    assert set(catalog) == DEFAULT_DOMAINS


def test_project_override_accepts_catalog_wrapper(tmp_path: Path):
    lexicon = _write_yaml(
        tmp_path / "project_lexicon.yaml",
        {"verification_means_catalog": {"catalog": {"wrapped": "runner"}}},
    )

    catalog = MeansCatalogLoader().resolve(str(lexicon), None)

    assert catalog == {"wrapped": ["runner"]}


def test_extractor_reads_project_lexicon_catalog_for_passthrough(tmp_path: Path):
    lexicon = _write_yaml(tmp_path / "project_lexicon.yaml", {"verification_means_catalog": {"custom": ["one"]}})

    section = extract_verification_means_catalog(lexicon)
    catalog = MeansCatalogLoader().resolve(project_lexicon_catalog=section)

    assert catalog == {"custom": ["one"]}


def test_extractor_returns_none_when_project_lexicon_has_no_catalog(tmp_path: Path):
    lexicon = _write_yaml(tmp_path / "project_lexicon.yaml", {"node_vocabulary": []})

    assert extract_verification_means_catalog(lexicon) is None
