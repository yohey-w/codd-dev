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
  is credited as a match, and the one fixture whose gap the detector structurally
  cannot express (missing_journey: C7 names no specific journeyless actor while
  other journeys exist) is credited as a miss; and
* the corpus is **non-vacuous** — gold is non-empty, so the miss rate is a real
  signal and not the round-16 "empty gold -> false 100%" trap.

These assertions pin the *measurement contract*, so they stay valid even if the
detectors later improve (e.g. if C7 gains per-actor missing_journey detection,
the missing_journey fixture would flip from miss to match; the test that the
*measurement* is correct still holds, and the dedicated miss/hit assertions for
each fixture document the current detection power explicitly).
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
    # four of which the deterministic checks express and one of which they do not
    # (missing_journey). So the measured corpus PCUMR is 1/5 = 20% miss — the
    # honest current detection hole, neither 0% (which would hide the hole) nor
    # 100% (which would imply nothing is detected).
    assert result.metric == "pcumr"
    assert result.total == 5
    assert result.covered == 4
    assert result.uncovered == 1
    assert result.pct == pytest.approx(20.0)


def test_missing_journey_gap_is_the_one_miss():
    # The only missed gold gap is the missing_journey actor: C7 only emits
    # actors_without_journeys when NO journey exists, so with some journeys
    # present it never names the specific journeyless actor (Admin). This is the
    # detection hole PCUMR surfaces.
    gold, detected = load_corpus()
    detected_keys = {
        (gap.kind.casefold(), gap.dimension.casefold(), gap.subject.casefold())
        for gap in detected
    }
    missing_journey_gold = [gap for gap in gold if gap.kind == "missing_journey"]
    assert missing_journey_gold, "corpus must include a missing_journey gold gap"
    for gap in missing_journey_gold:
        key = (gap.kind.casefold(), gap.dimension.casefold(), gap.subject.casefold())
        assert key not in detected_keys  # genuinely missed


@pytest.mark.parametrize(
    "fixture_name",
    ["missing_producer", "negative_space", "nfr_variant", "acceptance_signal"],
)
def test_detected_fixtures_are_zero_miss(fixture_name):
    # Each of these four fixtures has a gap the deterministic checks express, so
    # its construction-derived gold gap is matched (0% miss). This anchors the
    # corpus so the overall miss rate is NOT a fabricated 100% (the metric
    # genuinely credits real hits).
    fixture_dir = next(path for path in list_fixtures() if path.name == fixture_name)
    result, gold, _detected = _pcumr_for(fixture_dir)
    assert len(gold) == 1
    assert result.total == 1
    assert result.covered == 1
    assert result.pct == 0.0


def test_missing_journey_fixture_is_full_miss():
    # The missing_journey fixture's single construction-derived gold gap is NOT
    # detected, so its per-fixture PCUMR is 100% miss — the explicit record of
    # the current C7 blind spot.
    fixture_dir = next(path for path in list_fixtures() if path.name == "missing_journey")
    result, gold, _detected = _pcumr_for(fixture_dir)
    assert len(gold) == 1
    assert result.total == 1
    assert result.covered == 0
    assert result.pct == 100.0
