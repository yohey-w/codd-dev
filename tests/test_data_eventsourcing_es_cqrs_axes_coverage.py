"""Coverage-axis tests for the data_eventsourcing_es_cqrs lexicon plug-in."""

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
LEXICON_ROOT = REPO_ROOT / "codd_plugins" / "lexicons" / "data_eventsourcing_es_cqrs"
EXPECTED_AXES = {
    "Event Sourcing",
    "Event",
    "event store",
    "Event Replay",
    "Application State Storage",
    "Complete Rebuild",
    "Temporal Query",
    "CQRS pattern",
}


def _yaml(name: str) -> dict:
    return yaml.safe_load((LEXICON_ROOT / name).read_text(encoding="utf-8"))


def test_data_eventsourcing_es_cqrs_axes_match_manifest_observation_dimensions():
    manifest = _yaml("manifest.yaml")
    axes = _yaml("lexicon.yaml")["coverage_axes"]

    assert len(axes) == manifest["observation_dimensions"]
    assert {axis["axis_type"] for axis in axes} == EXPECTED_AXES


def test_data_eventsourcing_es_cqrs_axes_have_rationale_criticality_and_variants():
    axes = _yaml("lexicon.yaml")["coverage_axes"]

    for axis in axes:
        assert axis["rationale"]
        assert axis["criticality_default"] in {"critical", "high", "medium", "info"}
        assert axis["variants"]
        for variant in axis["variants"]:
            assert variant["id"]
            assert variant["label"]
            assert variant["criticality"] in {"critical", "high", "medium", "info"}


def test_data_eventsourcing_es_cqrs_variant_ids_are_eventsourcing_literals():
    variant_ids = {
        variant["id"]
        for axis in _yaml("lexicon.yaml")["coverage_axes"]
        for variant in axis["variants"]
    }

    for expected in (
        "Event Sourcing",
        "sequence of events",
        "Event",
        "business intent",
        "compensating event",
        "event store",
        "append-only",
        "Event Replay",
        "Application State Storage",
        "snapshot",
        "event log",
        "Complete Rebuild",
        "Temporal Query",
        "CQRS pattern",
        "materialized views",
        "query-optimized projections",
        "eventual consistency",
    ):
        assert expected in variant_ids


def test_data_eventsourcing_es_cqrs_severity_rules_cover_each_axis_gap():
    axes = {axis["axis_type"] for axis in _yaml("lexicon.yaml")["coverage_axes"]}
    rules = _yaml("severity_rules.yaml")["rules"]
    rule_conditions = {rule["when"] for rule in rules}

    for axis in axes:
        assert f"axis={axis} AND coverage=gap" in rule_conditions


def test_data_eventsourcing_es_cqrs_extension_contains_coverage_examples():
    content = (LEXICON_ROOT / "elicit_extend.md").read_text(encoding="utf-8")

    for classification in ("covered", "implicit", "gap"):
        assert f"`{classification}`" in content
    for axis in EXPECTED_AXES:
        assert f"`{axis}`" in content
