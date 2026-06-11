"""Tests for codd.confidence — the canonical confidence model.

Covers: band thresholds (including the evidence-count corroboration rule and
its config override), agreement with the established CEG classifier, the
direct-fact rule (single-source high-confidence facts are green), categorical
bridges and their round-trip, the testimony cap, and the additive adoption by
iac_nfr / git_evidence / restoration_report. All scenarios are synthetic and
project-agnostic.
"""

from __future__ import annotations

import itertools

import pytest

import codd.confidence as confidence
from codd.confidence import (
    BAND_AMBER,
    BAND_GRAY,
    BAND_GREEN,
    BANDS,
    CATEGORIES,
    CATEGORY_HIGH,
    CATEGORY_LOW,
    CATEGORY_MEDIUM,
    DEFAULT_AMBER_MIN_CONFIDENCE,
    DEFAULT_GREEN_MIN_CONFIDENCE,
    DEFAULT_GREEN_MIN_EVIDENCE_COUNT,
    band_for,
    band_for_fact,
    cap_testimony,
    category_for_numeric,
    classify_band,
    numeric_for_category,
    thresholds_from_config,
)


# ---------------------------------------------------------------------------
# Band thresholds (defaults mirror the `bands:` config)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "conf,count,expected",
    [
        (0.95, 2, BAND_GREEN),
        (0.90, 2, BAND_GREEN),  # boundary: green min confidence is inclusive
        (0.95, 1, BAND_AMBER),  # high confidence but uncorroborated → amber
        (0.89, 5, BAND_AMBER),  # corroborated but below green confidence
        (0.50, 1, BAND_AMBER),  # boundary: amber min confidence is inclusive
        (0.60, 1, BAND_AMBER),
        (0.49, 1, BAND_GRAY),
        (0.30, 3, BAND_GRAY),  # evidence count never rescues sub-amber confidence
        (0.0, 0, BAND_GRAY),
    ],
)
def test_band_for_default_thresholds(conf: float, count: int, expected: str):
    assert band_for(conf, evidence_count=count) == expected


def test_band_for_default_evidence_count_is_one():
    # An inferred statement without explicit corroboration info is never green.
    assert band_for(0.99) == BAND_AMBER


def test_band_for_reads_bands_config_full_or_section():
    custom = {"green": {"min_confidence": 0.8, "min_evidence_count": 1}, "amber": {"min_confidence": 0.3}}
    # As the bands: section itself…
    assert band_for(0.85, evidence_count=1, config=custom) == BAND_GREEN
    assert band_for(0.35, evidence_count=1, config=custom) == BAND_AMBER
    assert band_for(0.25, evidence_count=1, config=custom) == BAND_GRAY
    # …and inside a full resolved config.
    assert band_for(0.85, evidence_count=1, config={"bands": custom}) == BAND_GREEN


def test_thresholds_from_config_defaults():
    assert thresholds_from_config(None) == (
        DEFAULT_GREEN_MIN_CONFIDENCE,
        DEFAULT_GREEN_MIN_EVIDENCE_COUNT,
        DEFAULT_AMBER_MIN_CONFIDENCE,
    )
    assert thresholds_from_config({}) == thresholds_from_config(None)


def test_band_vocabulary_is_closed():
    assert BANDS == (BAND_GREEN, BAND_AMBER, BAND_GRAY)


# ---------------------------------------------------------------------------
# Shared semantics with the established CEG classifier
# ---------------------------------------------------------------------------
def test_classify_band_matches_ceg_classifier(tmp_path):
    """codd.graph.CEG.classify_band and codd.confidence.classify_band are the
    SAME semantics (the CEG method delegates); verify over a value grid."""

    from codd.graph import CEG

    ceg = CEG(tmp_path / "scan")
    confidences = [0.0, 0.3, 0.49, 0.5, 0.6, 0.89, 0.9, 0.95, 1.0]
    counts = [0, 1, 2, 3]
    for conf, count in itertools.product(confidences, counts):
        assert ceg.classify_band(conf, count) == classify_band(conf, count)
        # Custom thresholds agree too.
        assert ceg.classify_band(conf, count, 0.8, 1, 0.3) == classify_band(conf, count, 0.8, 1, 0.3)


# ---------------------------------------------------------------------------
# Direct facts (the single-source decision)
# ---------------------------------------------------------------------------
def test_band_for_fact_single_source_high_is_green():
    """A direct fact read from its one authoritative declaration is green at
    high confidence: the evidence-count rule corroborates INFERRED statements,
    and a deterministic read of the declaration itself needs no corroboration."""

    assert band_for_fact(0.95) == BAND_GREEN
    assert band_for_fact(0.90) == BAND_GREEN


def test_band_for_fact_still_respects_confidence_cuts():
    assert band_for_fact(0.60) == BAND_AMBER
    assert band_for_fact(0.30) == BAND_GRAY


def test_band_for_fact_honors_config_thresholds():
    custom = {"green": {"min_confidence": 0.99}, "amber": {"min_confidence": 0.5}}
    assert band_for_fact(0.95, config=custom) == BAND_AMBER


# ---------------------------------------------------------------------------
# Categorical bridges
# ---------------------------------------------------------------------------
def test_category_numeric_values_documented_mapping():
    assert numeric_for_category(CATEGORY_HIGH) == 0.95
    assert numeric_for_category(CATEGORY_MEDIUM) == 0.60
    assert numeric_for_category(CATEGORY_LOW) == 0.30


def test_category_bridge_roundtrips():
    for category in CATEGORIES:
        assert category_for_numeric(numeric_for_category(category)) == category


def test_category_lands_in_intended_band():
    # high clears the green confidence cut; medium is amber; low is gray.
    assert band_for_fact(numeric_for_category(CATEGORY_HIGH)) == BAND_GREEN
    assert band_for(numeric_for_category(CATEGORY_MEDIUM)) == BAND_AMBER
    assert band_for(numeric_for_category(CATEGORY_LOW)) == BAND_GRAY


def test_numeric_for_category_unknown_raises_without_default():
    with pytest.raises(ValueError, match="unknown confidence category"):
        numeric_for_category("certain")


def test_numeric_for_category_unknown_uses_default_when_given():
    assert numeric_for_category("certain", default=0.6) == 0.6


def test_numeric_for_category_is_case_insensitive():
    assert numeric_for_category("HIGH") == numeric_for_category(CATEGORY_HIGH)


# ---------------------------------------------------------------------------
# Testimony cap
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "band,expected",
    [
        (BAND_GREEN, BAND_AMBER),  # never green
        (BAND_AMBER, BAND_AMBER),
        (BAND_GRAY, BAND_GRAY),
        ("unrecognized", BAND_AMBER),  # never over-state
    ],
)
def test_cap_testimony(band: str, expected: str):
    assert cap_testimony(band) == expected


def test_git_evidence_testimony_band_is_the_shared_cap():
    """git_evidence's amber cap is now derived from the shared rule."""

    from codd.git_evidence import TESTIMONY_BAND, GitTestimony

    assert TESTIMONY_BAND == cap_testimony(BAND_GREEN) == BAND_AMBER
    testimony = GitTestimony(locator="src/x.py", commit="abc1234", date="2026-01-01", subject="s")
    assert testimony.band == BAND_AMBER  # default field value unchanged


# ---------------------------------------------------------------------------
# Additive adoption: iac_nfr.NfrCandidate derived properties
# ---------------------------------------------------------------------------
def _candidate(conf: str):
    from codd.iac_nfr import NfrCandidate

    return NfrCandidate(
        category="availability",
        statement="Service runs >=3 replicas",
        source="deploy/app.yaml::Deployment::app",
        confidence=conf,
    )


def test_nfr_candidate_high_single_source_is_green():
    """The deliberate decision: a direct IaC fact (high) is single-source by
    nature — the declaration IS the source of truth — so it is green despite
    the bands-config min_evidence_count=2 rule for inferred statements."""

    from codd.iac_nfr import CONFIDENCE_HIGH

    candidate = _candidate(CONFIDENCE_HIGH)
    assert candidate.numeric_confidence == 0.95
    assert candidate.band == BAND_GREEN


def test_nfr_candidate_medium_inferred_is_amber():
    from codd.iac_nfr import CONFIDENCE_MEDIUM

    candidate = _candidate(CONFIDENCE_MEDIUM)
    assert candidate.numeric_confidence == 0.60
    assert candidate.band == BAND_AMBER


def test_nfr_candidate_unknown_category_degrades_to_medium():
    candidate = _candidate("somehow-unrecognized")
    assert candidate.numeric_confidence == numeric_for_category(CATEGORY_MEDIUM)
    assert candidate.band == BAND_AMBER


def test_nfr_candidate_serialization_unchanged():
    """Backward compatibility: the stored vocabulary stays categorical."""

    from codd.iac_nfr import CONFIDENCE_HIGH

    payload = _candidate(CONFIDENCE_HIGH).to_dict()
    assert payload["confidence"] == "high"
    assert "band" not in payload
    assert "numeric_confidence" not in payload


# ---------------------------------------------------------------------------
# Additive adoption: restoration_report band constants
# ---------------------------------------------------------------------------
def test_restoration_report_band_constants_come_from_confidence():
    from codd import restoration_report

    assert restoration_report.BAND_GREEN is confidence.BAND_GREEN
    assert restoration_report.BAND_AMBER is confidence.BAND_AMBER
    assert restoration_report.BANDS == (BAND_GREEN, BAND_AMBER)
