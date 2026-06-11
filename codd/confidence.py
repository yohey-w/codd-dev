"""Canonical confidence model — ONE vocabulary for evidence confidence.

CoDD historically grew several confidence vocabularies side by side:

* ``bands:`` config (``codd/defaults.yaml``) — numeric thresholds consumed by
  the CEG band classification (``codd.graph.CEG.classify_band``) and the
  propagation engines.
* :mod:`codd.iac_nfr` — categorical ``"high"`` / ``"medium"`` strings on
  :class:`~codd.iac_nfr.NfrCandidate`.
* :mod:`codd.git_evidence` — testimony evidence hard-capped at ``band="amber"``.
* :mod:`codd.restoration_report` — ``green`` / ``amber`` band strings counted
  from restored-document frontmatter.

This module is the single owner of that vocabulary: a canonical numeric scale
(0.0–1.0), the band names, the band-classification thresholds, the categorical
bridges, and the testimony cap. Everything here is pure and dependency-free so
any CoDD module can import it without cycles.

NOT in scope: ``codd dag`` check severities (``red`` / ``amber`` / ``info``).
Severity grades how bad a *check finding* is; confidence grades how well an
*evidence-backed statement* is supported. They merely share the word "amber"
and are deliberately kept separate.

Mapping table (single source of truth)
======================================

===========  ========  =====================================================
vocabulary    value     canonical meaning
===========  ========  =====================================================
band          green     confidence >= 0.90 AND corroborated (see below)
band          amber     confidence >= 0.50 (below green, or uncorroborated)
band          gray      confidence < 0.50 — too weak to act on automatically
category      high      numeric 0.95 — a direct, deterministically-read fact
category      medium    numeric 0.60 — inferred intent (single signal)
category      low       numeric 0.30 — weak/speculative signal
===========  ========  =====================================================

The category numerics are chosen so the categories land in the band the
established semantics intend: ``high`` (0.95) clears the green confidence
threshold (0.90), ``medium`` (0.60) lands in amber [0.50, 0.90), and ``low``
(0.30) lands in gray (< 0.50). ``category_for_numeric`` uses the band
thresholds themselves as the cut points, so the bridge round-trips.

Corroboration (the evidence-count rule)
=======================================

The ``bands:`` config requires ``min_evidence_count`` (default 2) pieces of
evidence for green. That rule exists for *INFERRED* statements: the CEG
accumulates independent inference signals via Noisy-OR, and a single inference
— however confident — must be corroborated before it is treated as
machine-trustworthy (green). Use :func:`band_for` for those.

A *direct fact* is different: a value deterministically read from its one
authoritative declaration (e.g. an explicit replica count in an IaC manifest).
There is nothing to corroborate — the declaration IS the source of truth, and
reading it involves no inference. Demanding a second "piece of evidence" for a
single-source fact is meaningless, so direct facts classify by the confidence
thresholds alone via :func:`band_for_fact` (high + single-source ⇒ green).

Testimony cap
=============

Testimony (a human CLAIM about intent, e.g. a git commit message) is never
fact: :func:`cap_testimony` caps any band at amber, encoding the rule from
:mod:`codd.git_evidence` in one shared place.
"""

from __future__ import annotations

from typing import Any, Mapping

# ---------------------------------------------------------------------------
# Band vocabulary
# ---------------------------------------------------------------------------
BAND_GREEN = "green"
BAND_AMBER = "amber"
BAND_GRAY = "gray"

BANDS: tuple[str, ...] = (BAND_GREEN, BAND_AMBER, BAND_GRAY)

# ---------------------------------------------------------------------------
# Categorical vocabulary (bridged to the numeric scale)
# ---------------------------------------------------------------------------
CATEGORY_HIGH = "high"
CATEGORY_MEDIUM = "medium"
CATEGORY_LOW = "low"

CATEGORIES: tuple[str, ...] = (CATEGORY_HIGH, CATEGORY_MEDIUM, CATEGORY_LOW)

# See the module docstring's mapping table for the rationale of these values.
_CATEGORY_NUMERIC: dict[str, float] = {
    CATEGORY_HIGH: 0.95,
    CATEGORY_MEDIUM: 0.60,
    CATEGORY_LOW: 0.30,
}

# ---------------------------------------------------------------------------
# Band thresholds (the established `bands:` config semantics)
# ---------------------------------------------------------------------------
# These defaults mirror codd/defaults.yaml `bands:` and the historical defaults
# of codd.graph.CEG.classify_band — they are the same numbers on purpose.
DEFAULT_GREEN_MIN_CONFIDENCE = 0.90
DEFAULT_GREEN_MIN_EVIDENCE_COUNT = 2
DEFAULT_AMBER_MIN_CONFIDENCE = 0.50


def thresholds_from_config(
    config: Mapping[str, Any] | None,
) -> tuple[float, int, float]:
    """Read ``(green_min_conf, green_min_evidence, amber_min_conf)`` from a
    resolved config's ``bands:`` section, falling back to the defaults.

    Accepts either a full resolved config (containing a ``bands:`` key) or the
    ``bands:`` mapping itself.
    """

    bands: Mapping[str, Any] = {}
    if isinstance(config, Mapping):
        candidate = config.get("bands")
        bands = candidate if isinstance(candidate, Mapping) else config

    green = bands.get("green") if isinstance(bands.get("green"), Mapping) else {}
    amber = bands.get("amber") if isinstance(bands.get("amber"), Mapping) else {}
    return (
        float(green.get("min_confidence", DEFAULT_GREEN_MIN_CONFIDENCE)),
        int(green.get("min_evidence_count", DEFAULT_GREEN_MIN_EVIDENCE_COUNT)),
        float(amber.get("min_confidence", DEFAULT_AMBER_MIN_CONFIDENCE)),
    )


def classify_band(
    confidence: float,
    evidence_count: int,
    green_threshold: float = DEFAULT_GREEN_MIN_CONFIDENCE,
    green_min_evidence: int = DEFAULT_GREEN_MIN_EVIDENCE_COUNT,
    amber_threshold: float = DEFAULT_AMBER_MIN_CONFIDENCE,
) -> str:
    """The established CEG band classification (low-level, explicit thresholds).

    ``codd.graph.CEG.classify_band`` delegates here; the propagation engines
    pass thresholds read from the ``bands:`` config. Semantics (unchanged):

    * green — ``confidence >= green_threshold`` AND
      ``evidence_count >= green_min_evidence``
    * amber — otherwise, ``confidence >= amber_threshold``
    * gray  — otherwise
    """

    if confidence >= green_threshold and evidence_count >= green_min_evidence:
        return BAND_GREEN
    if confidence >= amber_threshold:
        return BAND_AMBER
    return BAND_GRAY


def band_for(
    confidence: float,
    *,
    evidence_count: int = 1,
    config: Mapping[str, Any] | None = None,
) -> str:
    """Classify a canonical numeric confidence into a band.

    Applies the full ``bands:`` semantics including the evidence-count
    corroboration rule (intended for INFERRED statements — see the module
    docstring). ``config`` may be a resolved config or a ``bands:`` mapping;
    absent values fall back to the shipped defaults.
    """

    green_conf, green_count, amber_conf = thresholds_from_config(config)
    return classify_band(confidence, evidence_count, green_conf, green_count, amber_conf)


def band_for_fact(
    confidence: float,
    *,
    config: Mapping[str, Any] | None = None,
) -> str:
    """Classify a *direct fact* — confidence thresholds only, no count rule.

    For values deterministically read from their one authoritative declaration
    (e.g. an explicit setting in an IaC manifest). The declaration is its own
    corroboration, so the evidence-count rule for inferred statements does not
    apply: a single-source direct fact at high confidence is green.
    """

    green_conf, green_count, amber_conf = thresholds_from_config(config)
    # The single authoritative declaration counts as sufficient corroboration:
    # satisfy the count rule by construction, leaving only the confidence cuts.
    return classify_band(confidence, green_count, green_conf, green_count, amber_conf)


# ---------------------------------------------------------------------------
# Categorical bridges
# ---------------------------------------------------------------------------
def numeric_for_category(category: str, *, default: float | None = None) -> float:
    """Bridge a categorical confidence (``high``/``medium``/``low``) to the
    canonical numeric scale (see the mapping table in the module docstring).

    Unknown categories raise :class:`ValueError` unless ``default`` is given
    (callers bridging legacy/loose data pass a lenient default).
    """

    key = str(category or "").strip().lower()
    if key in _CATEGORY_NUMERIC:
        return _CATEGORY_NUMERIC[key]
    if default is not None:
        return float(default)
    raise ValueError(
        f"unknown confidence category '{category}' (expected one of {CATEGORIES})"
    )


def category_for_numeric(confidence: float) -> str:
    """Bridge a canonical numeric confidence back to a category.

    Cut points are the band thresholds themselves (>= 0.90 high, >= 0.50
    medium, else low) so ``category_for_numeric(numeric_for_category(c)) == c``
    for every category.
    """

    if confidence >= DEFAULT_GREEN_MIN_CONFIDENCE:
        return CATEGORY_HIGH
    if confidence >= DEFAULT_AMBER_MIN_CONFIDENCE:
        return CATEGORY_MEDIUM
    return CATEGORY_LOW


# ---------------------------------------------------------------------------
# Testimony cap
# ---------------------------------------------------------------------------
def cap_testimony(band: str) -> str:
    """Cap a band at amber: testimony (a claim about intent) is never green.

    ``green`` ⇒ ``amber``; ``amber``/``gray`` pass through; anything
    unrecognized is normalized to ``amber`` (never over-state confidence).
    """

    key = str(band or "").strip().lower()
    if key == BAND_GRAY:
        return BAND_GRAY
    return BAND_AMBER
