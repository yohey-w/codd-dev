from __future__ import annotations

from codd.dag.metadata_access import collect_structured_entries


def test_reads_top_level_attribute() -> None:
    attrs = {"user_journeys": [{"name": "j1"}]}

    entries = collect_structured_entries(attrs, "user_journeys")

    assert entries == [{"name": "j1"}]


def test_reads_frontmatter_codd_nested() -> None:
    # The canonical generator position: the extractor never lifts this into
    # attrs[key], so attrs[key] is empty / absent and the data lives under codd.
    attrs = {
        "user_journeys": [],
        "frontmatter": {"codd": {"user_journeys": [{"name": "j_codd"}]}},
    }

    entries = collect_structured_entries(attrs, "user_journeys")

    assert entries == [{"name": "j_codd"}]


def test_reads_raw_frontmatter_when_not_lifted() -> None:
    # Builder shape that stored only the raw top-level frontmatter without
    # lifting it into attrs[key].
    attrs = {"frontmatter": {"coverage_axes": [{"axis_type": "env"}]}}

    entries = collect_structured_entries(attrs, "coverage_axes")

    assert entries == [{"axis_type": "env"}]


def test_merges_top_level_and_codd_nested() -> None:
    attrs = {
        "coverage_axes": [{"axis_type": "top"}],
        "frontmatter": {"codd": {"coverage_axes": [{"axis_type": "codd"}]}},
    }

    entries = collect_structured_entries(attrs, "coverage_axes")

    assert entries == [{"axis_type": "top"}, {"axis_type": "codd"}]


def test_dedups_lifted_top_level_against_raw_frontmatter() -> None:
    # The extractor lifts a top-level frontmatter key into BOTH attrs[key] and
    # leaves the raw copy at frontmatter[key]; reading both would double-count.
    same = [{"name": "only_once"}]
    attrs = {"user_journeys": same, "frontmatter": {"user_journeys": same}}

    entries = collect_structured_entries(attrs, "user_journeys")

    assert entries == [{"name": "only_once"}]


def test_no_dedup_between_top_level_and_distinct_codd_decl() -> None:
    # A top-level decl PLUS a different frontmatter.codd decl are unioned, not
    # collapsed (matches the resource_flow round-2 behavior).
    attrs = {
        "user_journeys": [{"name": "top"}],
        "frontmatter": {
            "user_journeys": [{"name": "top"}],  # raw duplicate of the lifted one
            "codd": {"user_journeys": [{"name": "nested"}]},
        },
    }

    entries = collect_structured_entries(attrs, "user_journeys")

    assert entries == [{"name": "top"}, {"name": "nested"}]


def test_ignores_non_list_and_non_mapping_items() -> None:
    attrs = {
        "user_journeys": "not-a-list",
        "frontmatter": {"codd": {"user_journeys": [{"name": "ok"}, "scalar", 5]}},
    }

    entries = collect_structured_entries(attrs, "user_journeys")

    assert entries == [{"name": "ok"}]


def test_non_mapping_attrs_returns_empty() -> None:
    assert collect_structured_entries(None, "user_journeys") == []
    assert collect_structured_entries(["x"], "user_journeys") == []
