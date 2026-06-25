"""Corpus PCUMR test for the Stage-2 (Axis-P) positive corpus.

Connects the 5-fixture positive corpus (``tests/fixtures/stage2_axisp_corpus/``)
to the A3 pure metric :func:`codd.coverage_metrics.compute_pcumr`. The point is
NOT to freeze a particular miss rate; it is to assert that:

* the corpus PCUMR is **measurable** — gold and detected gaps load and match in
  the canonical ``(kind, dimension, subject)`` shape;
* the measurement is **deterministic** — the same corpus yields the same value
  across repeated runs (the detectors are rule-based, the DAG is built from
  on-disk fixtures);
* gold matching is **correct** — each fixture whose gap the detector can express
  is credited as a match. C7 now names the specific journeyless actor even when
  other journeys exist (amber ``actors_without_journeys`` for the remaining
  actors), so the missing_journey fixture is credited as a match too; and
* the corpus is **non-vacuous** — gold is non-empty, so the miss rate is a real
  signal and not the round-16 "empty gold -> false 100%" trap.

These assertions pin the *measurement contract*, so they stay valid as the
detectors evolve. C7 previously could not express the per-actor missing_journey
gap while other journeys existed, so the missing_journey fixture was the lone
miss (corpus 20%). Axis-P closed that hole (C7 amber per-actor detection), so the
fixture now flips from miss to match and the corpus PCUMR is 0% miss — the
*measurement* is still correct, and the dedicated hit assertions below document
the current detection power explicitly.
"""

from __future__ import annotations

import pytest

from codd.coverage_metrics import compute_pcumr

from tests.stage2_axisp_corpus_loader import (
    detect_gaps,
    list_fixtures,
    load_corpus,
    load_gold,
)


def _items(gaps):
    return [gap.as_match_item() for gap in gaps]


def _pcumr_for(fixture_dir):
    gold = load_gold(fixture_dir)
    detected = detect_gaps(fixture_dir)
    return compute_pcumr(_items(gold), _items(detected)), gold, detected


# --- corpus presence / non-vacuity -----------------------------------------


def test_corpus_has_all_five_fixtures():
    names = {path.name for path in list_fixtures()}
    assert names == {
        "missing_journey",
        "missing_producer",
        "negative_space",
        "nfr_variant",
        "acceptance_signal",
    }


def test_corpus_gold_is_non_empty_not_vacuous():
    # round-16 guard: an empty gold makes compute_pcumr vacuously 0% miss, which
    # would be a false signal. The corpus must carry real gold so its PCUMR is
    # meaningful.
    gold, _detected = load_corpus()
    assert len(gold) >= 5  # at least one construction-derived gap per fixture

    result = compute_pcumr(_items(gold), [])
    # With nothing detected, every gold gap is missed: a 100% miss rate, never a
    # vacuous 0%. This proves gold is genuinely populated.
    assert result.total == len(gold)
    assert result.uncovered == len(gold)
    assert result.pct == 100.0


# --- measurement determinism -----------------------------------------------


def test_corpus_pcumr_is_deterministic():
    gold_a, detected_a = load_corpus()
    gold_b, detected_b = load_corpus()

    first = compute_pcumr(_items(gold_a), _items(detected_a))
    second = compute_pcumr(_items(gold_b), _items(detected_b))

    # Same corpus, same inputs, same metric -> identical measurement. The metric
    # is a pure function and the detectors are deterministic, so the value must
    # not drift between runs.
    assert (first.total, first.covered, first.uncovered, first.pct) == (
        second.total,
        second.covered,
        second.uncovered,
        second.pct,
    )


# --- gold-matching correctness (the value of PCUMR) ------------------------


def test_corpus_pcumr_measures_current_detection_power():
    gold, detected = load_corpus()
    result = compute_pcumr(_items(gold), _items(detected))

    # The corpus has exactly one construction-derived gap per fixture (5 total),
    # all of which the deterministic checks now express — including the
    # missing_journey fixture, since C7 names the specific journeyless actor
    # (Admin) as an amber finding even when other journeys exist. So the measured
    # corpus PCUMR is 0/5 = 0% miss. (This was 20% before the C7 amber per-actor
    # detection closed the hole; the metric is unchanged, the detection improved.)
    assert result.metric == "pcumr"
    assert result.total == 5
    assert result.covered == 5
    assert result.uncovered == 0
    assert result.pct == pytest.approx(0.0)


def test_missing_journey_gap_is_now_detected():
    # The missing_journey actor gap is now detected: C7 emits an amber
    # actors_without_journeys finding naming the specific journeyless actor
    # (Admin) even when other journeys are present. The loader expands that
    # finding to a missing_journey/actor/Admin gap, so it matches gold. This is
    # the detection hole that Axis-P closed.
    gold, detected = load_corpus()
    detected_keys = {
        (gap.kind.casefold(), gap.dimension.casefold(), gap.subject.casefold())
        for gap in detected
    }
    missing_journey_gold = [gap for gap in gold if gap.kind == "missing_journey"]
    assert missing_journey_gold, "corpus must include a missing_journey gold gap"
    for gap in missing_journey_gold:
        key = (gap.kind.casefold(), gap.dimension.casefold(), gap.subject.casefold())
        assert key in detected_keys  # now detected (was the lone miss)


@pytest.mark.parametrize(
    "fixture_name",
    ["missing_producer", "negative_space", "nfr_variant", "acceptance_signal", "missing_journey"],
)
def test_detected_fixtures_are_zero_miss(fixture_name):
    # Each of these five fixtures has a gap the deterministic checks express, so
    # its construction-derived gold gap is matched (0% miss). This anchors the
    # corpus so the overall miss rate is NOT a fabricated 100% (the metric
    # genuinely credits real hits). missing_journey joined this set once C7 gained
    # amber per-actor detection (it names the journeyless actor even when other
    # journeys exist).
    fixture_dir = next(path for path in list_fixtures() if path.name == fixture_name)
    result, gold, _detected = _pcumr_for(fixture_dir)
    assert len(gold) == 1
    assert result.total == 1
    assert result.covered == 1
    assert result.pct == 0.0


def test_missing_journey_fixture_is_now_detected():
    # The missing_journey fixture's single construction-derived gold gap is now
    # detected: C7 emits an amber actors_without_journeys finding naming the
    # journeyless actor (Admin) even with other journeys present, so the
    # per-fixture PCUMR is 0% miss. This was 100% (full miss) before Axis-P closed
    # the C7 blind spot.
    fixture_dir = next(path for path in list_fixtures() if path.name == "missing_journey")
    result, gold, _detected = _pcumr_for(fixture_dir)
    assert len(gold) == 1
    assert result.total == 1
    assert result.covered == 1
    assert result.pct == 0.0
