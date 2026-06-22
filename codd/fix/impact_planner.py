"""Impact planning for ``codd fix [PHENOMENON]`` (Stage 4 target resolution).

The legacy Stage-4 path resolves implementation files by walking *forward*
``expects`` edges from the chosen design node, with a coarse frontmatter
``modules`` glob fallback. In a brownfield codebase whose design docs link only
at MODULE granularity, that resolves 0–1 files and the propagation silently
under-reaches — a multi-surface feature (an entity touched across a create
API, an update API, an end-user render surface, and an admin input surface)
only gets one surface patched, which is a *semantic false green*.

This module replaces "follow existing links" with "DISCOVER the change
surface, then PROVE each candidate by evidence". The principle is UNIFIED
across two phenomenon shapes:

* **field-based** changes (a data field like ``content_body`` is a strong
  discriminator), and
* **field-less** changes (a button styling/gradient change), where the
  discriminator is a specific *anchor* (an identifier/literal/color/path slot)
  rather than a declared data field, and the LLM's abstract obligations
  (``theme.update``, ``ui.display``) are *facet labels*, not code-match terms.

The pipeline::

    phenomenon analysis + design diffs + exact `expects`
      → ExpectedEnvelopes (authoritative target envelopes)
      → AnchorSets (specific discriminator anchors vs generic facet words,
        decided by repo-local document frequency)
      → impact obligations (LLM facets + deterministic baseline +
        expected-target obligations)
      → candidate gathering (DAG-exact, module fallback, anchor-aware code
        search, graph neighbors)
      → drop tests/docs/generated
      → evidence scoring + acceptance (AnchorPolicy; hard sources auto-accept;
        soft evidence needs independent corroboration AND a discriminator)
      → cardinality + too-broad-expected guards
      → obligation coverage (direct, then expected-bridge with a capacity
        guard)
      → status: complete | incomplete | ambiguous

Cardinal rules honored here:

* **anti-false-green** — incomplete or ambiguous resolution is NEVER silently
  downgraded to a partial apply. The planner returns a non-``complete`` status
  with an explicit reason; the caller fail-fasts. Specifically: an exact
  ``expects`` envelope WITHOUT any specific anchor cannot reach ``complete``
  (expected-bridge is gated on ``anchors.specific`` being non-empty); a
  too-broad expected envelope (more exact targets than the cardinality cap)
  forces ``ambiguous``; a concrete-write obligation (api-surface + write-verb)
  is NOT coverable by the abstract expected-bridge (it needs the operation
  literal); and one expected file must not silently bridge-cover many abstract
  obligations (bridge-capacity guard).
* **generality** — NO project, framework or language names; NO synonym
  dictionaries (``theme`` → ``globals.css`` is project/FW-specific and
  forbidden). Only path tokens, content tokens, design-diff tokens, DAG edges,
  import/test graph structure, and repo-local document frequency. The LLM only
  *proposes* facet labels and obligations; deterministic evidence decides the
  allowlist, the coverage, and the status.
* **model-agnostic** — works with an empty LLM decomposition via the
  deterministic baseline obligation derivation plus expected-target
  obligations.
"""

from __future__ import annotations

import re as _re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

from codd.dag.impact import (
    affected_impl_targets,
    find_impl_candidates,
    find_impl_candidates_v2,
    is_dependency_lock,
    is_test_path,
    iter_source_files,
    normalize_terms,
)
from codd.fix.phenomenon_parser import PhenomenonAnalysis

# Evidence sources that are, on their own, sufficient to ADMIT a candidate as a
# write target. These are deterministic graph facts (a design doc declared the
# file via ``expects``, or the lexicon extraction resolved to it), not a fuzzy
# text match. Admission is NOT coverage: a hard-admitted file still has to be
# *covered* by an obligation (directly or via the expected-bridge) before the
# plan can be ``complete`` — that split is what keeps expects from being a
# false-green fallback.
HARD_SOURCES = frozenset({"expects", "expected_extraction"})

# Evidence sources that must NEVER, on their own, satisfy the "independent
# corroboration" requirement:
# * ``content_token`` — a single bare term appears in the file (one common word
#   would otherwise admit half the repo);
# * ``import_neighbor`` — import-graph proximity to another candidate. This is
#   CORROBORATION (it can push a borderline file with its OWN direct evidence
#   over the line), never admission: a file reachable in the import graph from a
#   seed but carrying no direct path/content-pair/anchor signal of its own must
#   not be admitted as a write target. Counting graph proximity as independent
#   let unrelated neighbours (e.g. shared util/template files imported widely)
#   reach the 2-source bar via ``content_pair`` + ``import_neighbor`` and be
#   patched — a false-green. Demoting it keeps the import graph as a tie-breaker,
#   not an admission key (anti-false-green; matches its sub-threshold weight).
_NON_INDEPENDENT_SOURCES = frozenset({"content_token", "import_neighbor"})

# Content-certificate sources. A content-ONLY file (no path signal, no cross-
# category content pair) is normally rejected by the independent-2-sources rule.
# But a *rare, literal, non-target* anchor (or a co-occurring cluster of rare
# anchors) in the file body is the field-less analogue of a field discriminator:
# it singles out the change surface as strongly as a path hit. These sources
# open a SEPARATE acceptance route (``_score_and_accept``) — they do NOT relax
# the independent-sources rule for ordinary content tokens, so the historical
# broad over-match (a common word admitting half the repo) cannot recur: a
# common/high-frequency word never earns a certificate.
_CONTENT_CERTIFYING_SOURCES = frozenset(
    {"content_unique_anchor", "content_anchor_cluster"}
)

# Default soft-acceptance thresholds (overridable by the caller).
_DEFAULT_MIN_SCORE = 0.55
_DEFAULT_MIN_INDEPENDENT_SOURCES = 2
_DEFAULT_MAX_IMPL_CANDIDATES = 12

# Evidence weights. Path equality is the strongest path signal; a cross-category
# content pair (e.g. entity + field co-occurring) is the strongest content
# signal; a lone content token is intentionally tiny.
_WEIGHTS = {
    "expects": 1.0,
    "expected_extraction": 1.0,
    "path_segment": 0.35,
    "path_basename": 0.25,
    "path_substring": 0.15,
    "content_pair": 0.35,
    "content_token": 0.12,
    # A content certificate (rare/literal anchor or rare-anchor cluster) is a
    # strong INDEPENDENT discriminator — weighted just above the soft-acceptance
    # floor so a content-only file with a certificate clears the score gate, but
    # acceptance is still gated by the dedicated certificate route, not by score
    # alone.
    "content_unique_anchor": 0.56,
    "content_anchor_cluster": 0.56,
    "import_neighbor": 0.30,
    "test_map": 0.20,
}

# Generic verb classification for obligation derivation. These are ENGLISH
# verb-shape tokens about *what an operation does*, not domain or framework
# nouns — they encode no project knowledge.
_WRITE_VERBS = frozenset(
    {
        "create",
        "add",
        "insert",
        "new",
        "update",
        "edit",
        "modify",
        "change",
        "patch",
        "put",
        "post",
        "save",
        "persist",
        "store",
        "delete",
        "remove",
        "destroy",
    }
)

# A generic token denoting the API/server surface. ``api`` is an architectural
# token, not a framework name.
_API_SURFACE_TOKENS = frozenset({"api", "server", "endpoint", "route", "backend"})

# Tokens that are NEVER discriminators: ubiquitous facet/verb/surface words. A
# feature's *facet* is described with these ("theme.update", "ui.display"), but
# they appear in too many files to bind an abstract obligation to a real file.
# These are generic English shape-words, not project/framework specifics. They
# are demoted in addition to (not instead of) the repo-local document-frequency
# demotion below, so the rule is robust even in a tiny repo where DF is low.
_UBIQUITOUS_FACET_TOKENS = frozenset(
    {
        "update",
        "display",
        "change",
        "improve",
        "render",
        "show",
        "view",
        "page",
        "screen",
        "ui",
        "theme",
        "style",
        "config",
        "copy",
        "text",
        "label",
        "component",
        "handler",
        "service",
        "manager",
        "data",
        "value",
        "item",
        "list",
        "get",
        "set",
    }
)

# Common English function/stop words. These are language structure, NOT project
# or framework knowledge (and certainly not a synonym dictionary), so they are
# safe under the generality rule. They are demoted from "specific" so a stray
# free-text word ("the", "use", "via") never becomes a discriminator/anchor — a
# critical anti-false-green property: a stopword must not authorize the
# expected-bridge. ``fields`` are forced specific and bypass this filter.
_STOPWORD_TOKENS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "not",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "for",
        "with",
        "from",
        "into",
        "onto",
        "via",
        "per",
        "as",
        "is",
        "are",
        "be",
        "was",
        "were",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "use",
        "uses",
        "used",
        "using",
        "make",
        "makes",
        "made",
        "want",
        "wants",
        "need",
        "needs",
        "should",
        "must",
        "can",
        "will",
        "when",
        "where",
        "which",
        "what",
        "how",
        "all",
        "any",
        "some",
        "new",
        "old",
        "then",
        "than",
        "below",
        "above",
        "under",
        "over",
        "across",
        "etc",
    }
)

# A token is "specific" (a discriminator) when it is long enough to be unlikely
# coincidental AND occurs in few enough files that it actually discriminates.
_MIN_SPECIFIC_TOKEN_LEN = 3
# Repo-local document-frequency ceiling: a token appearing in more than this
# many source files is treated as generic (it cannot single out the change
# surface). Kept small and absolute — generality forbids any project tuning.
_MAX_SPECIFIC_DOCUMENT_FREQUENCY = 6
# Ubiquity cap for FIELD anchors. A declared field is forced specific (it bypasses
# the small cap above and the facet filter), because a real data field is a
# strong discriminator. But the LLM sometimes mislabels a generic word ("body",
# "background", "data") as a field; such a token appears in far more files than a
# focused fix would ever patch and would explode the candidate set. A field whose
# document frequency exceeds this bound is therefore demoted to generic. The
# bound is the cardinality cap (the most files we would ever patch in one fix),
# not a tuned magic number: a discriminator matching many more files than we
# could ever write is, by definition, not discriminating. A genuinely focused
# field (a specific entity attribute) stays well under it.
_MAX_FIELD_DOCUMENT_FREQUENCY = _DEFAULT_MAX_IMPL_CANDIDATES
# Cap how many source files the document-frequency probe scans (cheap, bounded).
_DOC_FREQUENCY_SCAN_LIMIT = 2000

# --- Content-certificate thresholds (field-less discriminator route) ---------
# A content-only file earns a certificate via one of two routes, both keyed on
# CONTENT document-frequency (how many file BODIES contain the anchor), which is
# strictly rarer evidence than the path-or-content frequency used for the
# specific/generic split. Two independent constraints — rarity AND literal shape
# — guard against the historical broad over-match: low frequency alone is NOT
# enough (a stray rare word must not certify), and a bare short word (no
# delimiter/digit, low literal strength) is NOT enough either.
#
# Route A (unique literal): a SINGLE anchor that is value-like (hex/digit-
# bearing, high literal strength) and appears in exactly one file body. This is
# the "lone exact literal" case (a unique color/id/constant).
_MAX_CONTENT_UNIQUE_DF = 1
_MIN_SINGLE_LITERAL_STRENGTH = 4
# Route B (rare cluster): TWO OR MORE rare identifier-shaped anchors co-occur in
# the same file body. Co-occurrence of multiple change-related rare anchors is a
# precise signal even when each is an alphabetic construct name (e.g. a CSS
# ``linear-gradient`` + ``background-image`` pair) rather than a value literal —
# the discriminating construct vocabulary a styling change touches is present in
# the target file BEFORE the new value is written, whereas the new value literal
# is not yet there. Each cluster member must still be identifier-shaped
# (strength >= membership floor) and rare (content_df within the cap); a cluster
# of bare common words never forms.
_MAX_CONTENT_RARE_DF = 2
_MIN_CONTENT_CLUSTER_SIZE = 2
_MIN_CLUSTER_MEMBER_STRENGTH = 2

# A hex-color-shaped literal (with or without a leading ``#``). Generic value
# shape — names no project/framework/language; a 6–8 digit hex id matches too.
_HEXISH_RE = _re.compile(r"#?[0-9a-fA-F]{6,8}")

# Machine-generated code must NEVER be a patch target: it is reproduced from a
# spec/source, so a hand patch is futile (overwritten on regeneration) and only
# pollutes the impact set. Detected by a conventional ``generated`` path segment
# OR a generated-marker header — both are cross-ecosystem conventions, not
# project/framework specifics.
_GENERATED_DIR_SEGMENTS = frozenset({"generated", "__generated__", "_generated"})
_GENERATED_MARKERS = (
    "@generated",
    "do not edit",
    "code generated by",
    "auto-generated",
    "autogenerated",
    "this file is generated",
    "this is a generated file",
)
_GENERATED_SCAN_BYTES = 2000


def _is_generated(rel_path: str, project_root: Path) -> bool:
    """True for machine-generated code that must not be hand-patched."""
    parts = {p.lower() for p in Path(rel_path).parts}
    if parts & _GENERATED_DIR_SEGMENTS:
        return True
    try:
        head = (
            (project_root / rel_path)
            .read_text(encoding="utf-8")[:_GENERATED_SCAN_BYTES]
            .lower()
        )
    except (OSError, UnicodeDecodeError):
        return False
    return any(marker in head for marker in _GENERATED_MARKERS)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImpactEvidence:
    """A single piece of evidence that a file belongs in the impact set."""

    source: str
    detail: str
    weight: float
    # Which analysis/anchor category produced the matched term, when known:
    # "entity" | "field" | "operation" | "surface" | "anchor" | "expected"
    # | "" (unknown / graph).
    category: str = ""


@dataclass
class ImplCandidate:
    """A candidate implementation file plus the evidence gathered for it."""

    path: str
    score: float = 0.0
    evidences: list[ImpactEvidence] = field(default_factory=list)
    accepted: bool = False
    reject_reason: str = ""

    def sources(self) -> set[str]:
        return {e.source for e in self.evidences}

    def categories(self) -> set[str]:
        return {e.category for e in self.evidences if e.category}

    def terms(self) -> set[str]:
        return {e.detail for e in self.evidences}


@dataclass
class ImpactObligation:
    """A change facet the phenomenon implies that the plan must cover.

    ``required_surface`` / ``required_operation`` are abstract *facet* labels
    (the LLM's vocabulary). ``concrete_write`` marks the small subset that name
    an api-like surface AND a write verb — those require the operation literal
    and may NOT be satisfied by the abstract expected-bridge. Abstract facets
    (``theme.update`` / ``ui.display`` / render / config / copy) do NOT require
    the literal and MAY be bridged. ``allow_expected_bridge`` opens the
    expected-bridge coverage route for this obligation.
    """

    id: str
    description: str
    # A candidate covers this obligation when it carries a discriminator AND a
    # match for at least one token in EACH non-empty requirement group below.
    required_surface: list[str] = field(default_factory=list)
    required_operation: list[str] = field(default_factory=list)
    # Concrete evidence anchors (specific discriminator tokens) this obligation
    # is about, when known. Informational for diagnostics; coverage uses the
    # candidate's own discriminator evidence.
    required_anchors: list[str] = field(default_factory=list)
    # Whether the abstract expected-bridge coverage route is allowed.
    allow_expected_bridge: bool = False
    # Whether this is an api-surface write that demands the operation literal
    # and is NOT bridge-coverable.
    concrete_write: bool = False
    # Whether ``required_surface`` is a HARD locator (the obligation is "the
    # feature must REACH surface S", so a candidate must literally match S) vs a
    # soft facet label (an abstract LLM facet whose "surface" need not appear in
    # code). Baseline ``surface.<S>`` obligations set this True so a missing
    # surface stays UNRESOLVED (anti-false-green: a non-S file must not vacuously
    # satisfy "reach S"); LLM facet obligations leave it False so a vague label
    # imposes no literal requirement (the candidate's own anchor discriminator
    # covers it).
    hard_surface: bool = False


@dataclass(frozen=True)
class ExpectedEnvelope:
    """An authoritative target envelope declared by a design node via ``expects``.

    ``exact`` means it came from real ``expects`` edges (not a coarse module
    glob). ``too_broad`` means it names more exact targets than the cardinality
    cap — such an envelope is still usable for candidate *admission* but is
    refused for expected-bridge coverage (anti-false-green: a sprawling
    "expects everything" must go ``ambiguous``, not silently ``complete``).
    """

    design_node_id: str
    paths: frozenset[str]
    exact: bool
    source: Literal["expects"]
    too_broad: bool = False


@dataclass
class AnchorSets:
    """Discriminator vocabulary derived from analysis + phenomenon + design.

    ``specific`` anchors are the generalized form of "field": low-document-
    frequency, sufficiently long identifier/literal tokens (a data field, a
    button name, a color/hex, a gradient name, a config key, an exact expected
    path slot). ``generic`` tokens are searched but never discriminate
    (ubiquitous facet/verb/surface words). ``facet_terms`` are kept only for
    obligation facet labels/diagnostics.

    ``specific_nontarget`` is the subset of ``specific`` that did NOT come from
    an expected target's own path. It is the "concrete anchor" that authorizes
    the expected-bridge: a design declaring ``expects -> {a, b}`` with NO
    concrete signal from the phenomenon/diff/analysis (only the target paths'
    own basenames) must NOT be bridged to ``complete`` — that would let a stale
    ``expects`` edge fake semantic coverage (anti-false-green). The target-slot
    anchors still help *direct* discrimination of the target file itself.
    """

    specific: set[str] = field(default_factory=set)
    specific_nontarget: set[str] = field(default_factory=set)
    generic: set[str] = field(default_factory=set)
    search_terms: set[str] = field(default_factory=set)
    facet_terms: set[str] = field(default_factory=set)
    # Repo-local document frequencies, kept for the coverage/certificate routes.
    # ``df`` is path-OR-content frequency (used for the specific/generic split
    # and for "is this required term observed in the repo at all"); ``content_df``
    # counts file BODIES only (strictly rarer; used for content certificates so a
    # token that is common in PATHS but rare in BODIES is judged on its body
    # rarity). ``path_df`` is kept for symmetry/diagnostics.
    df: dict[str, int] = field(default_factory=dict)
    path_df: dict[str, int] = field(default_factory=dict)
    content_df: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class _AnchorFrequency:
    """Three repo-local document-frequency views over a token set.

    ``any_df`` counts files where the token appears in the path OR the content;
    ``path_df`` only the path; ``content_df`` only the content. The split lets
    the specific/generic classifier use the broad (any) frequency while the
    content-certificate route uses the strictly-rarer content frequency.
    """

    any_df: dict[str, int]
    path_df: dict[str, int]
    content_df: dict[str, int]
    scanned: int


@dataclass
class CoverageContext:
    """Everything the coverage routes need, computed once per plan."""

    anchors: AnchorSets
    expected_by_design: dict[str, ExpectedEnvelope]
    expected_by_path: dict[str, set[str]]  # path -> design_node_ids


@dataclass(frozen=True)
class AnchorPolicy:
    """Acceptance policy: how strict soft candidates must be about anchors."""

    field_terms_present: bool
    specific_terms: frozenset[str]


@dataclass
class ImpactPlan:
    """The resolved impact plan returned by :func:`resolve_impact_plan`."""

    design_node_ids: list[str]
    impl_paths: list[str]
    test_paths: list[str]
    candidates: list[ImplCandidate]
    obligations: list[ImpactObligation]
    covered_obligations: dict[str, list[str]]
    unresolved_obligations: list[str]
    status: Literal["complete", "ambiguous", "incomplete"]
    diagnostics: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Term derivation (typed by analysis category)
# ---------------------------------------------------------------------------


_CAMEL_BOUNDARY_RE = _re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_ALNUM_RE = _re.compile(r"[^A-Za-z0-9]+")
# Identifier-ish token: starts alnum, >= 3 chars, snake/kebab/camel/digits ok.
# Syntax-generic — no framework or language names, no special-casing of .css/.tsx.
_IDENTIFIERISH_RE = _re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,}")


def _split_words_local(term: str) -> list[str]:
    """Split a term into component words (snake/kebab/camel aware)."""
    if not term:
        return []
    spaced = _NON_ALNUM_RE.sub(" ", term)
    spaced = _CAMEL_BOUNDARY_RE.sub(" ", spaced)
    return [w for w in spaced.split() if w]


def _singularize_local(word: str) -> str:
    """Cheap, language-agnostic singularizer (common English endings only)."""
    low = word
    if len(low) > 3 and low.endswith("ies"):
        return low[:-3] + "y"
    if len(low) > 4 and low.endswith("ses"):
        return low[:-2]
    if len(low) > 2 and low.endswith("s") and not low.endswith("ss"):
        return low[:-1]
    return low


@dataclass
class _Category:
    """One analysis category: an ordered list of CANONICAL tokens (one per
    original input term, used for obligation ids) plus the full set of
    normalized VARIANTS (used for path/content matching)."""

    canon: list[str] = field(default_factory=list)
    variants: set[str] = field(default_factory=set)


def _build_category(seeds: Iterable[str]) -> _Category:
    """Canonicalize each seed to a single stable token; expand variants.

    The canonical token is the snake-cased singular form of the seed (a
    camelCase seed becomes snake_case; a plural seed becomes singular).
    Variants are the full :func:`normalize_terms` expansion so a file matches regardless
    of casing/plurality. Non-ASCII seeds (e.g. Japanese subject terms) yield no
    ASCII canonical token and contribute variants only.
    """
    canon: list[str] = []
    seen_canon: set[str] = set()
    variants: set[str] = set()
    for seed in seeds:
        if not isinstance(seed, str):
            continue
        seed = seed.strip()
        if not seed:
            continue
        token_variants = normalize_terms([seed])
        variants |= token_variants
        # Canonical: snake of the singular of the seed; skip if not ASCII-wordy.
        canonical = _canonical_token(seed)
        if canonical and canonical not in seen_canon:
            seen_canon.add(canonical)
            canon.append(canonical)
    return _Category(canon=canon, variants=variants)


def _canonical_token(seed: str) -> str:
    """Stable canonical token for an obligation id (snake, singular, ascii)."""
    words = _split_words_local(seed)
    if not words:
        return ""
    words = [w.lower() for w in words]
    words[-1] = _singularize_local(words[-1])
    token = "_".join(words)
    # Reject tokens with no ascii letters (e.g. pure-numeric or empty).
    if not any(c.isalpha() for c in token):
        return ""
    return token


@dataclass
class _TermSets:
    """Category-tagged terms derived from a phenomenon analysis."""

    entity: _Category
    field: _Category
    operation: _Category
    surface: _Category

    @property
    def all(self) -> set[str]:
        return (
            self.entity.variants
            | self.field.variants
            | self.operation.variants
            | self.surface.variants
        )

    def category_of(self, token: str) -> str:
        # Precedence: entity > field > operation > surface. A token is normally
        # in one bucket; precedence only matters for accidental overlaps.
        if token in self.entity.variants:
            return "entity"
        if token in self.field.variants:
            return "field"
        if token in self.operation.variants:
            return "operation"
        if token in self.surface.variants:
            return "surface"
        return ""

    def variants_for_canon(self, category: _Category, canon: str) -> set[str]:
        """All matching variants for a single canonical token in ``category``."""
        return {v for v in category.variants if _canonical_token(v) == canon}


def _derive_term_sets(
    analysis: PhenomenonAnalysis,
    design_updates: Iterable[Any] = (),
) -> _TermSets:
    """Build category-tagged term sets from the analysis.

    Falls back to ``subject_terms``/``lexicon_hits`` as entity-ish seeds when
    the richer decomposition is absent, so the planner degrades gracefully for
    callers (and LLMs) that supply only the legacy fields.
    """
    entity_seeds = list(analysis.entities)
    field_seeds = list(analysis.fields)
    op_seeds = list(analysis.operations)
    surface_seeds = list(analysis.surfaces)

    if not entity_seeds:
        # Legacy fallback: treat subject terms + lexicon hits as entity-ish
        # discovery seeds so brownfield without LLM decomposition still searches.
        entity_seeds = list(analysis.subject_terms) + list(analysis.lexicon_hits)

    # Design-diff identifiers are deliberately NOT folded into the FIELD category.
    # A generated design paragraph emits many generic identifiers (entity,
    # surface and verb words), and dumping them into `field` dilutes the field
    # discriminator until "field evidence" matches almost any file (observed:
    # 56-candidate over-match on a real brownfield repo even with the field
    # requirement on). The discriminating field signal is the LLM-declared
    # `analysis.fields` only; diff text is NOT discarded, it is preserved as
    # anchors in :func:`_derive_anchor_sets`.

    return _TermSets(
        entity=_build_category(entity_seeds),
        field=_build_category(field_seeds),
        operation=_build_category(op_seeds),
        surface=_build_category(surface_seeds),
    )


def _design_diff_terms(design_updates: Iterable[Any]) -> list[str]:
    """Extract added IDENTIFIER tokens from design-doc diffs (best effort).

    Uses the same ASCII identifier shape as :func:`_extract_identifierish_terms`.
    A bare ``isalnum()`` scan is WRONG here: ``str.isalnum()`` is True for CJK
    (and other non-ASCII) letters, so on a non-English design doc it slurps whole
    prose sentences (Japanese has no inter-word spaces) into single "tokens" that
    then masquerade as specific anchors and explode the candidate set
    (observed: a non-English design diff polluted ``anchors.specific`` with
    sentence fragments and pushed a real repo's candidate set over the
    cardinality cap). Anchors must
    be code-matchable identifiers/literals, not free-text prose, so we extract
    the same ASCII identifier shape the rest of the planner matches against.
    """
    out: list[str] = []
    for update in design_updates or ():
        diff = getattr(update, "diff", "") or ""
        for line in diff.splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            out.extend(_extract_identifierish_terms(line[1:]))
    return out


def _extract_identifierish_terms(text: str) -> list[str]:
    """Pull identifier-ish tokens from free text (syntax-generic).

    Keeps snake/camel/kebab/digit-bearing tokens of length >= 3. No
    framework/language names, no special handling of any file type — a hex
    color, a CSS custom property name, a config key, and a function name all
    fall out of the same regex.
    """
    return [m.group(0) for m in _IDENTIFIERISH_RE.finditer(text or "")]


def _path_tokens(path: str) -> list[str]:
    """Distinctive anchor tokens for an expected target PATH.

    Only the basename identifies the file; parent directory segments (``src``,
    ``components``, ``styles``, ``app`` ...) are shared structure across
    siblings and would make the anchor match every neighbour — a precision leak
    that wrongly pulls in unrelated files in the same directory. So we take the
    basename, its extension-stripped stem, and the stem's component words
    (snake/kebab/camel-split), NOT the directories. This is framework-free: it
    names no segment, it just refuses to let a folder name discriminate.
    """
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if not parts:
        return []
    basename = parts[-1]
    out: list[str] = [basename.lower()]
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    if stem:
        out.append(stem.lower())
        for word in _split_words_local(stem):
            if word:
                out.append(word.lower())
    return out


# ---------------------------------------------------------------------------
# Document frequency (repo-local) + anchor classification
# ---------------------------------------------------------------------------


def _document_frequency(
    project_root: Path, tokens: set[str]
) -> _AnchorFrequency:
    """Count, per token, how many source files contain it — three views.

    Cheap and bounded: scans the same generic source universe as the candidate
    search, capped at :data:`_DOC_FREQUENCY_SCAN_LIMIT` files. Returns path-only,
    content-only, and path-or-content frequencies. Used to DEMOTE ubiquitous
    tokens to generic (``any_df``) and to gate content certificates on body
    rarity (``content_df``). It never promotes anything, so an empty/odd repo
    just yields zeros (everything long-enough stays specific, nothing certifies).
    """
    any_df: dict[str, int] = {t: 0 for t in tokens}
    path_df: dict[str, int] = {t: 0 for t in tokens}
    content_df: dict[str, int] = {t: 0 for t in tokens}
    scanned = 0
    if not tokens:
        return _AnchorFrequency(any_df, path_df, content_df, scanned)
    for path in _iter_text_like_files(project_root):
        if scanned >= _DOC_FREQUENCY_SCAN_LIMIT:
            break
        scanned += 1
        rel = path.relative_to(project_root).as_posix().lower()
        try:
            text = path.read_text(encoding="utf-8").lower()
        except (OSError, UnicodeDecodeError):
            text = ""
        for tok in tokens:
            if not tok:
                continue
            in_path = tok in rel
            in_content = tok in text
            if in_path:
                path_df[tok] += 1
            if in_content:
                content_df[tok] += 1
            if in_path or in_content:
                any_df[tok] += 1
    return _AnchorFrequency(any_df, path_df, content_df, scanned)


def _literal_strength(token: str) -> int:
    """How "value/identifier-like" a token is (syntax-generic, 0..6).

    Higher means more clearly a code value or compound identifier rather than a
    bare prose word: length, digit content, mixed alpha+digit, delimiter chars,
    and an exact hex shape each contribute. Names no project/framework/language —
    a hex color, a kebab CSS construct, a snake config key and a function name
    all score by the same rule; a plain English word scores low.
    """
    t = token.lower()
    score = 0
    if len(t) >= 6:
        score += 1
    if any(c.isdigit() for c in t):
        score += 1
    if any(c.isalpha() for c in t) and any(c.isdigit() for c in t):
        score += 1
    if any(c in t for c in "_-:#@./"):
        score += 1
    if _HEXISH_RE.fullmatch(t):
        score += 2
    if not t.isalpha():
        score += 1
    return score


def _is_specific_anchor(token: str, *, df: dict[str, int]) -> bool:
    """True when a token is a discriminator (specific), not a generic word."""
    if len(token) < _MIN_SPECIFIC_TOKEN_LEN:
        return False
    # A discriminator must be a code-matchable IDENTIFIER (ASCII alnum/_/-), not
    # free-text prose. ``str.isalnum()`` upstream is True for CJK, so a non-
    # English phrase or a space-joined fragment can otherwise slip in as an
    # anchor and authorize the expected-bridge / inflate the candidate set. Code
    # identifiers, CSS custom properties, hex colors and config keys all match
    # this shape; prose (CJK sentences, space-joined English fragments) does not.
    # `fields` are forced specific by the caller and bypass this check.
    if not _IDENTIFIERISH_RE.fullmatch(token):
        return False
    # Check the token AND its cheap singular form, so a pluralized stopword
    # variant ("thes" from "the") is demoted too.
    singular = _singularize_local(token)
    if (
        token in _UBIQUITOUS_FACET_TOKENS
        or token in _STOPWORD_TOKENS
        or singular in _UBIQUITOUS_FACET_TOKENS
        or singular in _STOPWORD_TOKENS
    ):
        return False
    # A token seen in many files cannot single out the change surface.
    if df.get(token, 0) > _MAX_SPECIFIC_DOCUMENT_FREQUENCY:
        return False
    return True


def _derive_anchor_sets(
    *,
    analysis: PhenomenonAnalysis,
    phenomenon_text: str,
    design_updates: Iterable[Any],
    expected: dict[str, ExpectedEnvelope],
    project_root: Path,
) -> AnchorSets:
    """Generalize ``field`` to "specific discriminator anchor".

    Anchors come from analysis.fields (always strong), entity/subject terms,
    phenomenon-text identifiers, design-diff added identifiers, AND exact
    ``expects`` path tokens. Ubiquitous facet/verb/surface words and high-
    document-frequency tokens are demoted to ``generic`` (searched, never
    discriminating). NO synonym dictionaries; only deterministic token shape +
    repo-local document frequency.
    """
    # (token, is_field, from_target) seeds. ``is_field`` forces specific;
    # ``from_target`` marks tokens that come ONLY from an expected target's own
    # path (they are target slots, not concrete change signals).
    seeds: list[tuple[str, bool, bool]] = []

    for t in analysis.fields:
        seeds.append((t, True, False))
    for t in (analysis.entities or analysis.subject_terms):
        seeds.append((t, False, False))
    for t in analysis.lexicon_hits:
        seeds.append((t, False, False))
    for t in _extract_identifierish_terms(phenomenon_text):
        seeds.append((t, False, False))
    for t in _design_diff_terms(design_updates):
        seeds.append((t, False, False))
    for env in expected.values():
        for path in env.paths:
            for t in _path_tokens(path):
                seeds.append((t, False, True))

    # Generic facet vocabulary: operations + surfaces + write verbs + api tokens.
    generic_facets = (
        set(normalize_terms(analysis.operations))
        | set(normalize_terms(analysis.surfaces))
        | _write_verb_variants()
        | _api_surface_variants()
    )

    # Expand every seed into matching variants, then probe document frequency.
    all_variants: set[str] = set()
    expanded: list[tuple[str, bool, bool]] = []  # (variant, is_field, from_target)
    for raw, is_field, from_target in seeds:
        for tok in normalize_terms([raw]):
            expanded.append((tok, is_field, from_target))
            all_variants.add(tok)

    freq = _document_frequency(project_root, all_variants - generic_facets)

    specific: set[str] = set()
    specific_nontarget: set[str] = set()
    generic_terms: set[str] = set()
    for tok, is_field, from_target in expanded:
        if tok in generic_facets:
            generic_terms.add(tok)
            continue
        # A field is forced specific (bypasses the small DF cap + facet filter)
        # UNLESS it is ubiquitous — a field token in more files than we would
        # ever patch is the LLM mislabeling a generic word ("body"/"background")
        # as a field; demote it so it cannot explode the candidate set.
        field_ok = is_field and (
            freq.any_df.get(tok, 0) <= _MAX_FIELD_DOCUMENT_FREQUENCY
        )
        if field_ok or _is_specific_anchor(tok, df=freq.any_df):
            specific.add(tok)
            if not from_target:
                specific_nontarget.add(tok)
        else:
            generic_terms.add(tok)

    # A token cannot be both specific and generic — specific wins.
    generic_terms -= specific

    return AnchorSets(
        specific=specific,
        specific_nontarget=specific_nontarget,
        generic=generic_terms,
        search_terms=specific | generic_terms | generic_facets,
        facet_terms=generic_facets,
        df=freq.any_df,
        path_df=freq.path_df,
        content_df=freq.content_df,
    )


# ---------------------------------------------------------------------------
# Obligation derivation
# ---------------------------------------------------------------------------


def _is_concrete_write(required_surface: set[str], required_operation: set[str]) -> bool:
    """An obligation is a concrete write iff it names an api-like surface AND a
    write verb. ``api.update`` qualifies (strict, literal required);
    ``theme.update`` does NOT (abstract change facet, bridge-coverable)."""
    return bool(
        (required_surface & _api_surface_variants())
        and (required_operation & _write_verb_variants())
    )


def _coerce_llm_obligations(
    analysis: PhenomenonAnalysis,
    terms: _TermSets,
    anchors: AnchorSets,
) -> list[ImpactObligation]:
    """Convert LLM-proposed obligation dicts into typed obligations.

    The LLM's ``terms`` are split into surface/operation *facet* requirements
    using the same category sets. The LLM vocabulary is treated as facet labels
    only; the deterministic ``concrete_write`` test decides whether the
    operation literal is mandatory. Every LLM obligation is allowed to use the
    expected-bridge route (the bridge itself is still gated on exact expects +
    a specific anchor + non-concrete-write inside :func:`_covers_via_expected_bridge`).
    """
    out: list[ImpactObligation] = []
    for raw in analysis.obligations or []:
        if not isinstance(raw, dict):
            continue
        oid = str(raw.get("id", "") or "").strip()
        if not oid:
            continue
        norm = normalize_terms(raw.get("terms") or [])
        req_surface = norm & terms.surface.variants
        req_operation = norm & terms.operation.variants
        out.append(
            ImpactObligation(
                id=oid,
                description=str(raw.get("description", "") or "").strip(),
                required_surface=sorted(req_surface),
                required_operation=sorted(req_operation),
                required_anchors=sorted(norm & anchors.specific),
                allow_expected_bridge=True,
                concrete_write=_is_concrete_write(req_surface, req_operation),
            )
        )
    return out


def _derive_baseline_obligations(
    terms: _TermSets, anchors: AnchorSets
) -> list[ImpactObligation]:
    """Deterministic baseline obligation set, used when the LLM proposes none.

    Generic and surface-anchored (no domain knowledge required):

    * one ``surface.<S>`` obligation per CANONICAL surface token — the feature
      must reach that surface (covered by surface-token + discriminator
      evidence);
    * one ``api.<op>`` obligation per CANONICAL write-operation token when an
      API-like surface is present — writes must be persisted on the server
      (these are ``concrete_write``: the operation literal is required and the
      abstract bridge cannot cover them).

    Ids are built from the canonical (snake/singular) token only, never from
    every cased/plural variant, so the set stays stable. Matching, however, is
    against the full variant set so a file matches in any casing convention.
    """
    obligations: list[ImpactObligation] = []
    api_tokens = sorted(terms.surface.variants & _api_surface_variants())
    has_api_surface = bool(api_tokens)
    write_variants = _write_verb_variants()

    # One obligation per canonical surface token.
    for surface_canon in terms.surface.canon:
        variants = sorted(terms.variants_for_canon(terms.surface, surface_canon))
        if not variants:
            continue
        obligations.append(
            ImpactObligation(
                id=f"surface.{surface_canon}",
                description=f"feature must reach the '{surface_canon}' surface",
                required_surface=variants,
                allow_expected_bridge=True,
                hard_surface=True,
            )
        )

    # Write-operation obligations anchored on the API surface (concrete writes).
    if has_api_surface:
        for op_canon in terms.operation.canon:
            if op_canon not in write_variants:
                continue
            op_variants = sorted(terms.variants_for_canon(terms.operation, op_canon))
            if not op_variants:
                continue
            obligations.append(
                ImpactObligation(
                    id=f"api.{op_canon}",
                    description=f"API persists the '{op_canon}' operation",
                    required_surface=api_tokens,
                    required_operation=op_variants,
                    allow_expected_bridge=False,
                    concrete_write=True,
                )
            )

    return obligations


def _stable_path_id(path: str) -> str:
    """A stable, readable obligation-id fragment for an expected target path."""
    base = Path(path).name or path
    token = _NON_ALNUM_RE.sub("_", base).strip("_").lower()
    return token or "target"


def _derive_expected_target_obligations(
    expected: dict[str, ExpectedEnvelope],
    accepted_by_path: dict[str, ImplCandidate],
    ctx: CoverageContext,
) -> tuple[list[ImpactObligation], list[str]]:
    """Obligations for ANCHOR-ALIGNED exact expected targets only.

    An exact ``expects`` target becomes a hard obligation (so it is not silently
    dropped) ONLY when the target file itself carries a non-``expects``
    discriminator — i.e. the design's prior is corroborated by a concrete change
    signal in that file. An ``expects`` edge to a file with NO such signal is
    treated as a stale/imprecise prior: it is NOT turned into an obligation
    (which would force a false negative / spurious bridge), and the path is
    reported in the returned ``ignored`` list for diagnostics (anti-false-green
    visibility — we surface what the design pointed at but we did not select).
    Too-broad envelopes are skipped (the plan already forces ``ambiguous``).

    Returns ``(obligations, ignored_paths)``.
    """
    out: list[ImpactObligation] = []
    ignored: list[str] = []
    for design_id, env in sorted(expected.items()):
        if env.too_broad or not env.exact:
            continue
        for path in sorted(env.paths):
            cand = accepted_by_path.get(path)
            if cand is not None and _candidate_has_non_expected_discriminator(
                cand, ctx
            ):
                out.append(
                    ImpactObligation(
                        id=f"expected.{_stable_path_id(path)}",
                        description=f"anchor-aligned expected target from "
                        f"{design_id}: {path}",
                        allow_expected_bridge=True,
                        concrete_write=False,
                    )
                )
            else:
                ignored.append(path)
    return out, sorted(set(ignored))


def _api_surface_variants() -> set[str]:
    return normalize_terms(_API_SURFACE_TOKENS)


def _write_verb_variants() -> set[str]:
    return normalize_terms(_WRITE_VERBS)


# ---------------------------------------------------------------------------
# Candidate gathering
# ---------------------------------------------------------------------------


def _add_evidence(
    candidates: dict[str, ImplCandidate],
    path: str,
    evidence: ImpactEvidence,
) -> None:
    norm_path = Path(path).as_posix()
    cand = candidates.get(norm_path)
    if cand is None:
        cand = ImplCandidate(path=norm_path)
        candidates[norm_path] = cand
    # De-duplicate identical (source, detail) evidence.
    for existing in cand.evidences:
        if existing.source == evidence.source and existing.detail == evidence.detail:
            return
    cand.evidences.append(evidence)


def _gather_expected_envelopes(
    candidates: dict[str, ImplCandidate],
    dag: Any,
    design_node_ids: list[str],
    project_root: Path,
    *,
    max_exact_expected: int,
) -> tuple[dict[str, ExpectedEnvelope], dict[str, str]]:
    """Collect exact ``expects`` envelopes and seed hard candidate evidence.

    Returns ``(expected_by_design, dag_sources)``. ``dag_sources`` keeps the
    legacy per-node source diagnostic (``expects`` / ``frontmatter_modules`` /
    ``none``). Exact-``expects`` targets are recorded as hard ``expects``
    evidence (admission), and an :class:`ExpectedEnvelope` is built per design
    node, flagged ``too_broad`` when it names more targets than the cap.
    """
    expected: dict[str, ExpectedEnvelope] = {}
    sources: dict[str, str] = {}
    for node_id in design_node_ids:
        targets = affected_impl_targets(dag, node_id, project_root=project_root)
        sources[node_id] = targets.source
        if targets.source != "expects":
            continue
        paths = frozenset(Path(p).as_posix() for p in targets.impl_paths)
        if not paths:
            continue
        expected[node_id] = ExpectedEnvelope(
            design_node_id=node_id,
            paths=paths,
            exact=True,
            source="expects",
            too_broad=len(paths) > max_exact_expected,
        )
        for path in paths:
            _add_evidence(
                candidates,
                path,
                ImpactEvidence(
                    source="expects",
                    detail=node_id,
                    weight=_WEIGHTS["expects"],
                    category="expected",
                ),
            )
    return expected, sources


def _invert_expected(
    expected: dict[str, ExpectedEnvelope],
) -> dict[str, set[str]]:
    """path -> {design_node_ids that expect it}."""
    out: dict[str, set[str]] = {}
    for design_id, env in expected.items():
        for path in env.paths:
            out.setdefault(path, set()).add(design_id)
    return out


def _gather_module_fallback(
    candidates: dict[str, ImplCandidate],
    dag: Any,
    design_node_ids: list[str],
    project_root: Path,
    terms: _TermSets,
    anchors: AnchorSets,
) -> None:
    """(b) Coarse frontmatter ``modules`` glob fallback (legacy resolver).

    Module-path hits are NOT a hard source: per the spec they must be
    corroborated by phenomenon/diff content evidence before acceptance. So we
    record them as ``path_segment``-class soft evidence only when the matched
    file ALSO carries a feature term (term set OR a specific anchor) — a bare
    module glob can no longer admit a file by itself.
    """
    nodes = getattr(dag, "nodes", {}) or {}
    modules: set[str] = set()
    for node_id in design_node_ids:
        node = nodes.get(node_id)
        if node is None:
            continue
        attributes = getattr(node, "attributes", None) or {}
        frontmatter = attributes.get("frontmatter") or {}
        if isinstance(frontmatter, dict):
            mod = frontmatter.get("modules") or []
            if isinstance(mod, (list, tuple)):
                modules.update(str(m) for m in mod)
            elif mod:
                modules.add(str(mod))

    corroborators = terms.all | anchors.specific
    for module in modules:
        for rel in find_impl_candidates(project_root, module):
            norm = Path(rel).as_posix()
            full = project_root / rel
            try:
                text = full.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                text = ""
            low = text.lower()
            matched = {t for t in corroborators if t in low}
            if not matched:
                continue
            _add_evidence(
                candidates,
                norm,
                ImpactEvidence(
                    source="module_path",
                    detail=module,
                    weight=_WEIGHTS["path_substring"],
                    category="surface",
                ),
            )


def _categorize_term(term: str, terms: _TermSets, anchors: AnchorSets) -> str:
    """Category for a matched term: analysis category, else 'anchor' if specific."""
    category = terms.category_of(term)
    if not category and term in anchors.specific:
        return "anchor"
    return category


def _content_certificate_label(
    content_terms: set[str], anchors: AnchorSets
) -> tuple[str, str] | None:
    """Decide whether a file's body anchors earn a content certificate.

    ``content_terms`` are the normalized terms matched in THIS file's body. Only
    *specific non-target* anchors that actually occur in some body (content_df >
    0) are eligible — a target-slot path token or a never-observed term cannot
    certify. Returns ``(source, detail)`` for the strongest applicable route, or
    ``None``. Two independent guards (rarity via ``content_df`` AND literal shape
    via :func:`_literal_strength`) keep this from re-introducing broad
    over-match: a common word (high ``content_df``) and a bare short word (low
    strength) both fail.
    """
    concrete = {
        t
        for t in content_terms
        if t in anchors.specific_nontarget and anchors.content_df.get(t, 0) > 0
    }
    if not concrete:
        return None

    # Route A: a single value-like literal unique to one file body.
    unique_literals = sorted(
        t
        for t in concrete
        if anchors.content_df.get(t, 0) <= _MAX_CONTENT_UNIQUE_DF
        and _literal_strength(t) >= _MIN_SINGLE_LITERAL_STRENGTH
    )
    if unique_literals:
        return ("content_unique_anchor", "+".join(unique_literals[:3]))

    # Route B: a cluster of rare, identifier-shaped anchors co-occurring here.
    rare = sorted(
        t
        for t in concrete
        if anchors.content_df.get(t, 0) <= _MAX_CONTENT_RARE_DF
        and _literal_strength(t) >= _MIN_CLUSTER_MEMBER_STRENGTH
    )
    if len(rare) >= _MIN_CONTENT_CLUSTER_SIZE:
        return ("content_anchor_cluster", "+".join(rare[:5]))

    return None


def _gather_code_search(
    candidates: dict[str, ImplCandidate],
    project_root: Path,
    terms: _TermSets,
    anchors: AnchorSets,
) -> None:
    """(c) Anchor-aware phenomenon/design code search across path + content.

    Searches the union of the analysis term variants and the anchor search
    terms over a GENERAL text-like source universe (so a stylesheet/config/copy
    file with a specific anchor is discoverable), then types each hit by its
    term's category — promoting a specific-anchor match to the ``anchor``
    category so it can act as a discriminator downstream.
    """
    search_terms = terms.all | anchors.search_terms
    hits = find_impl_candidates_v2(
        project_root, search_terms, suffixes=_text_like_suffixes(project_root)
    )
    for rel, info in hits.items():
        # Path-segment evidence (typed by the matched term's category).
        for p_hit in info.get("path_hits", []):
            _add_evidence(
                candidates,
                rel,
                ImpactEvidence(
                    source=p_hit.where,
                    detail=p_hit.term,
                    weight=_WEIGHTS.get(p_hit.where, 0.1),
                    category=_categorize_term(p_hit.term, terms, anchors),
                ),
            )

        # Content evidence: lone tokens are weak; a cross-category co-occurrence
        # (entity+field, entity+anchor, ...) is a strong INDEPENDENT signal.
        content_terms = [c.term for c in info.get("content_hits", [])]
        content_categories: dict[str, str] = {}
        for t in content_terms:
            cat = _categorize_term(t, terms, anchors)
            if cat:
                content_categories.setdefault(cat, t)
        for term in content_terms:
            _add_evidence(
                candidates,
                rel,
                ImpactEvidence(
                    source="content_token",
                    detail=term,
                    weight=_WEIGHTS["content_token"],
                    category=_categorize_term(term, terms, anchors),
                ),
            )
        # Emit a content_pair when at least two DIFFERENT categories co-occur.
        if len(content_categories) >= 2:
            ordered = sorted(content_categories.items())
            label = "+".join(f"{cat}:{tok}" for cat, tok in ordered[:3])
            _add_evidence(
                candidates,
                rel,
                ImpactEvidence(
                    source="content_pair",
                    detail=label,
                    weight=_WEIGHTS["content_pair"],
                ),
            )

        # Content certificate: a rare/literal anchor (or rare-anchor cluster) in
        # the body self-certifies a content-only file (no path signal needed) —
        # the field-less analogue of a field discriminator. Emitted as an
        # ``anchor``-category evidence so it counts as a discriminator downstream.
        cert = _content_certificate_label(set(content_terms), anchors)
        if cert is not None:
            source, detail = cert
            _add_evidence(
                candidates,
                rel,
                ImpactEvidence(
                    source=source,
                    detail=detail,
                    weight=_WEIGHTS[source],
                    category="anchor",
                ),
            )


def _gather_graph_neighbors(
    candidates: dict[str, ImplCandidate],
    dag: Any,
    project_root: Path,
) -> None:
    """(d) Import-graph neighbors of already-strong candidates.

    A file imported-by (or importing) an accepted-strength candidate is a soft
    corroborating signal — never sufficient alone (weight is below the soft
    threshold), but it can push a borderline neighbor over with other evidence.
    """
    nodes = getattr(dag, "nodes", {}) or {}
    edges = getattr(dag, "edges", []) or []

    # Map rel-path <-> node-id for the candidates we already have.
    path_to_node: dict[str, str] = {}
    for node in nodes.values():
        node_path = str(getattr(node, "path", "") or node.id)
        path_to_node[Path(node_path).as_posix()] = node.id

    # Strong seeds: candidates whose current evidence already crosses a path or
    # content_pair signal (cheap proxy; full scoring happens later).
    seed_paths = {
        c.path
        for c in candidates.values()
        if c.sources() & {"path_segment", "path_basename", "content_pair", "expects"}
    }
    seed_nodes = {path_to_node[p] for p in seed_paths if p in path_to_node}
    if not seed_nodes:
        return

    import_kinds = {"imports", "imported_by", "depends_on"}
    for edge in edges:
        if edge.kind not in import_kinds:
            continue
        for endpoint in (edge.from_id, edge.to_id):
            other = edge.to_id if endpoint == edge.from_id else edge.from_id
            if endpoint not in seed_nodes:
                continue
            node = nodes.get(other)
            if node is None:
                continue
            other_path = Path(str(node.path or node.id)).as_posix()
            if is_test_path(other_path):
                continue
            if not _looks_like_source(other_path):
                continue
            _add_evidence(
                candidates,
                other_path,
                ImpactEvidence(
                    source="import_neighbor",
                    detail=endpoint,
                    weight=_WEIGHTS["import_neighbor"],
                ),
            )


def _looks_like_source(rel_path: str) -> bool:
    return not rel_path.endswith(".md")


# ---------------------------------------------------------------------------
# General text-like source universe (used for search + document frequency)
# ---------------------------------------------------------------------------


def _text_like_suffixes(project_root: Path) -> set[str]:
    """Suffixes for a GENERAL text-like source universe.

    Generality requirement: do NOT hardcode ``.css`` (or any framework
    suffix). Instead, take every file extension actually present in the repo's
    source tree, minus docs and obvious binary/lock kinds. This makes
    stylesheet/config/copy files (``.css``, ``.scss``, ``.toml``, ``.json``,
    ...) discoverable wherever a project happens to use them, without naming any
    of them — they simply fall out of "the text files this repo contains".
    """
    found: set[str] = set()
    scanned = 0
    for path in _iter_repo_files(project_root):
        if scanned >= _DOC_FREQUENCY_SCAN_LIMIT:
            break
        scanned += 1
        suf = path.suffix.lower()
        if not suf:
            continue
        if suf in _NON_TEXT_SUFFIXES or suf == ".md":
            continue
        found.add(suf)
    return found


# Suffixes that are never implementation text (binary, media, locks, archives).
# Generic file-kind exclusions, not project/framework names.
_NON_TEXT_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".svg",
        ".ico",
        ".pdf",
        ".zip",
        ".gz",
        ".tar",
        ".tgz",
        ".bz2",
        ".7z",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".otf",
        ".mp3",
        ".mp4",
        ".mov",
        ".avi",
        ".wav",
        ".so",
        ".dylib",
        ".dll",
        ".bin",
        ".lock",
        ".pyc",
        ".pyo",
        ".class",
        ".o",
        ".a",
        # Build caches / generated artifacts (never hand-edited source).
        ".tsbuildinfo",
        ".map",
    }
)


def _iter_repo_files(project_root: Path) -> Iterable[Path]:
    """Yield repo files, skipping VCS/build/cache dirs (no suffix filter)."""
    from codd.dag.impact import _SKIP_DIR_NAMES  # local import: shared skip set

    root = Path(project_root)
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        # Skip named build/cache/VCS dirs AND any dotdir (.next, .next-e2e,
        # .nuxt, .turbo, .cache ...): build output is never a hand-edited target.
        if any(
            part in _SKIP_DIR_NAMES or (part.startswith(".") and part not in (".", ".."))
            for part in rel_parts[:-1]
        ):
            continue
        yield path


def _iter_text_like_files(project_root: Path) -> Iterable[Path]:
    """Yield non-test, text-like source files for the document-frequency probe."""
    for path in _iter_repo_files(project_root):
        suf = path.suffix.lower()
        if not suf or suf in _NON_TEXT_SUFFIXES or suf == ".md":
            continue
        if is_dependency_lock(path.name):
            continue
        rel = path.relative_to(project_root).as_posix()
        if is_test_path(rel):
            continue
        yield path


# ---------------------------------------------------------------------------
# Scoring & acceptance
# ---------------------------------------------------------------------------

# Discriminator categories: evidence that genuinely singles out the change
# surface (the generalized "field" notion plus entity + specific anchor).
_DISCRIMINATOR_CATEGORIES = frozenset({"field", "entity", "anchor"})


def _score_and_accept(
    candidate: ImplCandidate,
    *,
    min_score: float,
    min_independent_sources: int,
    anchor_policy: AnchorPolicy,
) -> None:
    """Score a candidate and decide acceptance under ``anchor_policy``.

    Hard ``expects``/extraction evidence ADMITS the candidate (it is an
    authoritative target). Admission is not coverage — :func:`_covers_direct` /
    :func:`_covers_via_expected_bridge` still have to bind an obligation to it
    before the plan can be ``complete``. Soft
    candidates need independent corroboration, enough score, AND a discriminator
    (a specific anchor, a field, or — when the phenomenon names no field — any
    field/entity/operation/anchor signal).
    """
    candidate.score = round(sum(e.weight for e in candidate.evidences), 4)
    sources = candidate.sources()

    # Exact expects is target-admission hard evidence (NOT semantic coverage).
    if sources & HARD_SOURCES:
        candidate.accepted = True
        return

    # Content-certificate route (field-less discriminator). A content-only file
    # bearing a rare/literal anchor (or rare-anchor cluster) is accepted WITHOUT
    # the independent-2-sources rule: the certificate is itself a strong specific
    # anchor (it is only issued for low-content-DF, identifier-shaped,
    # non-target anchors), so it discriminates as well as a path hit. This does
    # NOT loosen the rule for ordinary content tokens — only a genuine
    # certificate takes this route, so a common word still cannot self-admit.
    if sources & _CONTENT_CERTIFYING_SOURCES:
        if candidate.score < min_score:
            candidate.accepted = False
            candidate.reject_reason = (
                f"score {candidate.score:.2f} < threshold {min_score:.2f}"
            )
            return
        candidate.accepted = True
        return

    independent = sources - _NON_INDEPENDENT_SOURCES
    if len(independent) < min_independent_sources:
        candidate.accepted = False
        candidate.reject_reason = (
            f"only {len(independent)} independent evidence source(s) "
            f"(need >= {min_independent_sources})"
        )
        return

    if candidate.score < min_score:
        candidate.accepted = False
        candidate.reject_reason = (
            f"score {candidate.score:.2f} < threshold {min_score:.2f}"
        )
        return

    categories = candidate.categories()
    terms = candidate.terms()

    # Precision: when the phenomenon names specific data field(s) OR yields
    # specific anchors, that anchor is the discriminator. Generic surface/
    # operation tokens (api, admin, create, update, get) are ubiquitous in a
    # real codebase and over-match if they alone admit a file; require the
    # named field's evidence OR a specific-anchor term so acceptance narrows to
    # files that actually touch the feature. This is a general inference rule (a
    # feature touching anchor A lives in files referencing A), not a
    # project/framework-specific carve-out.
    if anchor_policy.field_terms_present:
        has_field = "field" in categories
        has_specific = bool(terms & anchor_policy.specific_terms) or (
            "anchor" in categories
        )
        if not (has_field or has_specific):
            candidate.accepted = False
            candidate.reject_reason = (
                "no field/specific-anchor evidence (phenomenon names specific "
                "field(s)/anchor(s); the anchor is the discriminator over "
                "generic surface/operation tokens)"
            )
            return
    else:
        if not (categories & (_DISCRIMINATOR_CATEGORIES | {"operation"})):
            candidate.accepted = False
            candidate.reject_reason = "no discriminator evidence"
            return

    candidate.accepted = True


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def _candidate_has_non_expected_discriminator(
    candidate: ImplCandidate, ctx: CoverageContext
) -> bool:
    """True when the candidate carries a discriminator INDEPENDENT of ``expects``.

    A discriminator is a field/entity-category match, a content certificate, or
    a non-target specific anchor among the candidate's OWN evidence. Hard
    ``expects``/extraction evidence is explicitly ignored: an expects edge is a
    *prior* (it admits the candidate), not proof the candidate is involved in
    THIS change. This is the anti-false-green pivot for the coverage/selection
    routes — a stale ``expects`` edge to a file that shows no concrete change
    signal must not let that file bridge-cover obligations or be selected as a
    write target.
    """
    for e in candidate.evidences:
        if e.source in HARD_SOURCES:
            continue
        if e.source in _CONTENT_CERTIFYING_SOURCES:
            return True
        if e.category in {"field", "entity"}:
            return True
        if e.detail in ctx.anchors.specific_nontarget:
            return True
    return False


def _binding_required_terms(required: Iterable[str], ctx: CoverageContext) -> set[str]:
    """Required facet terms that are ALSO repo-observed concrete anchors.

    An abstract facet label (``shared_ui``, ``theme`` ...) need not appear
    literally in code — requiring it as a hard match is what makes field-less
    obligations un-coverable. So an abstract obligation only HARD-binds the
    required terms that are themselves observed specific non-target anchors
    (present in the repo, ``df > 0``). A concrete write keeps its strict literal
    requirement separately; this relaxation is for abstract facets only.
    """
    return {
        t
        for t in required
        if t in ctx.anchors.specific_nontarget and ctx.anchors.df.get(t, 0) > 0
    }


def _covers_direct(
    candidate: ImplCandidate,
    obligation: ImpactObligation,
    ctx: CoverageContext,
) -> bool:
    """Direct (lexical/anchor) coverage.

    Requires a candidate-owned, non-``expects`` discriminator (field/entity,
    content certificate, or non-target specific anchor). A ``concrete_write``
    obligation keeps its strict literal requirement: a surface-literal match
    (when named) AND the operation literal — an update handler must not silently
    "cover" the create facet (anti-false-green). An ABSTRACT facet
    (display/render/theme/config/copy/shared-ui) leaves no literal code verb, so
    it only HARD-binds those required terms that are repo-observed concrete
    anchors (:func:`_binding_required_terms`); a pure facet label imposes no
    lexical requirement and is covered by the candidate's own discriminator.
    """
    if not _candidate_has_non_expected_discriminator(candidate, ctx):
        return False

    terms = candidate.terms()

    if obligation.concrete_write:
        if obligation.required_surface and not (set(obligation.required_surface) & terms):
            return False
        # The write verb must be present as a literal (anti-false-green).
        return bool(set(obligation.required_operation) & terms)

    if obligation.hard_surface:
        # Surface-reach obligation: the surface token is a HARD locator. A
        # candidate must literally match the surface, else "reach surface S" is
        # not satisfied — a missing surface must stay unresolved (anti-false-
        # green), never vacuously covered by an unrelated discriminator file.
        if obligation.required_surface and not (
            set(obligation.required_surface) & terms
        ):
            return False
    else:
        # Abstract facet: bind only those required surface terms that are
        # repo-observed concrete anchors; a pure label imposes no requirement.
        binding_surface = _binding_required_terms(obligation.required_surface, ctx)
        if binding_surface and not (binding_surface & terms):
            return False

    binding_anchors = _binding_required_terms(obligation.required_anchors, ctx)
    if binding_anchors and not (binding_anchors & terms):
        return False

    return True


def _covers_via_expected_bridge(
    candidate: ImplCandidate,
    obligation: ImpactObligation,
    ctx: CoverageContext,
) -> bool:
    """Expected-bridge coverage (design→impl prior route, candidate-aligned).

    Allowed only when: the candidate is an EXACT (not too-broad) ``expects``
    target of a design node, the obligation opted into the bridge and is NOT a
    concrete write, AND the candidate itself carries a non-``expects``
    discriminator. The candidate-alignment requirement is the anti-false-green
    core: an ``expects`` edge to a file that shows NO concrete change signal of
    its own (a stale/imprecise edge) must NOT bridge-cover an obligation — the
    coverage is then deferred to anchor-discovered targets. An api-write facet is
    never bridge-coverable (it needs the operation literal).
    """
    if not obligation.allow_expected_bridge:
        return False
    if obligation.concrete_write:
        return False
    if "expects" not in candidate.sources():
        return False
    # Candidate must prove its own involvement — not rely on the expects edge.
    if not _candidate_has_non_expected_discriminator(candidate, ctx):
        return False

    design_ids = ctx.expected_by_path.get(candidate.path, set())
    for design_id in design_ids:
        env = ctx.expected_by_design.get(design_id)
        if env is None or not env.exact or env.too_broad:
            continue
        return True
    return False


def _bridge_capacity_ok(
    bridged_only: dict[str, list[str]],
) -> bool:
    """Bridge-capacity guard.

    A single expected file must not silently bridge-cover many abstract
    obligations. ``bridged_only`` maps obligation-id -> covering paths for the
    obligations that have NO direct coverage (bridge-only). If any single path
    is the sole bridge for more than one such obligation, the resolution is too
    weak to trust — force ambiguous.
    """
    usage: dict[str, int] = {}
    for paths in bridged_only.values():
        # An obligation bridged by exactly one file pins that file's budget.
        if len(paths) == 1:
            p = paths[0]
            usage[p] = usage.get(p, 0) + 1
    return all(n <= 1 for n in usage.values())


def _cover_obligations(
    accepted: list[ImplCandidate],
    obligations: list[ImpactObligation],
    ctx: CoverageContext,
) -> tuple[dict[str, list[str]], list[str], dict[str, list[str]]]:
    """Cover obligations: direct first, then expected-bridge.

    Returns ``(covered, unresolved, bridged_only)`` where ``bridged_only`` maps
    obligation-id -> paths for obligations covered ONLY via the bridge (used by
    the capacity guard).
    """
    covered: dict[str, list[str]] = {}
    unresolved: list[str] = []
    bridged_only: dict[str, list[str]] = {}

    for o in obligations:
        direct = [c.path for c in accepted if _covers_direct(c, o, ctx)]
        if direct:
            covered[o.id] = direct
            continue
        bridged = [c.path for c in accepted if _covers_via_expected_bridge(c, o, ctx)]
        if bridged:
            covered[o.id] = bridged
            bridged_only[o.id] = bridged
            continue
        unresolved.append(o.id)

    return covered, unresolved, bridged_only


def _select_impl_paths(
    accepted: list[ImplCandidate],
    covered: dict[str, list[str]],
    ctx: CoverageContext,
) -> list[str]:
    """Choose the actual write targets from accepted candidates.

    Admission (``accepted``) is NOT selection. A file is selected when it either
    COVERS an obligation, or carries its own non-``expects`` discriminator (an
    anchor-discovered target that the abstract LLM facets may not name a specific
    obligation for, yet clearly belongs to the change). Crucially, an
    ``expects``-only admitted file with no concrete signal of its own is NOT
    selected — that is what stops a stale ``expects`` edge from leaking into the
    final patch set (anti-false-green).
    """
    selected: set[str] = set()
    for paths in covered.values():
        selected.update(paths)
    for cand in accepted:
        if _candidate_has_non_expected_discriminator(cand, ctx):
            selected.add(cand.path)
    return sorted(selected)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def resolve_impact_plan(
    *,
    dag: Any,
    project_root: Path,
    design_node_ids: list[str],
    phenomenon_text: str,
    analysis: PhenomenonAnalysis,
    design_updates: list[Any] | None = None,
    config: dict[str, Any] | None = None,
    max_impl_candidates: int = _DEFAULT_MAX_IMPL_CANDIDATES,
    min_score: float = _DEFAULT_MIN_SCORE,
    min_independent_sources: int = _DEFAULT_MIN_INDEPENDENT_SOURCES,
) -> ImpactPlan:
    """Resolve the implementation/test files a phenomenon fix must touch.

    See the module docstring for the pipeline. Returns an :class:`ImpactPlan`
    whose ``status`` is:

    * ``complete``   — every obligation is covered by an accepted candidate;
    * ``incomplete`` — at least one obligation has no covering candidate;
    * ``ambiguous``  — too many candidates were accepted to apply safely, an
      expected envelope is too broad, the bridge capacity guard tripped, or no
      obligations could be derived to verify against.

    Only ``complete`` is safe to apply. ``incomplete``/``ambiguous`` MUST be
    fail-fasted by the caller (anti-false-green): a partial apply that looks
    complete is exactly the failure mode this planner exists to prevent.
    """
    project_root = Path(project_root)
    design_updates = design_updates or []
    diagnostics: list[str] = []

    terms = _derive_term_sets(analysis, design_updates)

    candidates: dict[str, ImplCandidate] = {}

    # ------------------------------------------------------------------
    # 1. Exact expects envelopes (authoritative target admission).
    # ------------------------------------------------------------------
    expected, dag_sources = _gather_expected_envelopes(
        candidates,
        dag,
        design_node_ids,
        project_root,
        max_exact_expected=max_impl_candidates,
    )
    if dag_sources:
        diagnostics.append(
            "dag-exact sources: "
            + ", ".join(f"{nid}={src}" for nid, src in sorted(dag_sources.items()))
        )
    for design_id, env in sorted(expected.items()):
        diagnostics.append(
            f"expected-envelope: {design_id} -> "
            + "{" + ", ".join(sorted(env.paths)) + "}"
            + (" (too_broad)" if env.too_broad else "")
        )

    # ------------------------------------------------------------------
    # 2. Generalized discriminator anchors (field -> specific anchor).
    # ------------------------------------------------------------------
    anchors = _derive_anchor_sets(
        analysis=analysis,
        phenomenon_text=phenomenon_text,
        design_updates=design_updates,
        expected=expected,
        project_root=project_root,
    )
    if anchors.specific:
        diagnostics.append(
            "anchors.specific: " + ", ".join(sorted(anchors.specific))
        )
    else:
        diagnostics.append("anchors.specific: (none)")

    ctx = CoverageContext(
        anchors=anchors,
        expected_by_design=expected,
        expected_by_path=_invert_expected(expected),
    )

    # ------------------------------------------------------------------
    # 3. Semantic obligations: LLM facets if present, else deterministic
    #    baseline. (Expected-target obligations are derived AFTER acceptance —
    #    they depend on whether each expected file is anchor-aligned.)
    # ------------------------------------------------------------------
    semantic_obligations = _coerce_llm_obligations(analysis, terms, anchors)
    if semantic_obligations:
        diagnostics.append(
            f"obligations: {len(semantic_obligations)} from LLM proposal"
        )
    else:
        semantic_obligations = _derive_baseline_obligations(terms, anchors)
        diagnostics.append(
            f"obligations: {len(semantic_obligations)} from deterministic baseline"
        )

    # ------------------------------------------------------------------
    # 4. Soft discovery (anchor-aware): module fallback, code search, graph.
    # ------------------------------------------------------------------
    _gather_module_fallback(
        candidates, dag, design_node_ids, project_root, terms, anchors
    )
    _gather_code_search(candidates, project_root, terms, anchors)
    _gather_graph_neighbors(candidates, dag, project_root)

    # Drop test files (resolved below), design docs, and machine-generated code
    # (never a hand-patch target — regenerated from source).
    for path in list(candidates):
        if (
            is_test_path(path)
            or path.endswith(".md")
            or _is_generated(path, project_root)
        ):
            del candidates[path]

    # ------------------------------------------------------------------
    # 5. Score & accept.
    # ------------------------------------------------------------------
    policy = AnchorPolicy(
        field_terms_present=bool(terms.field.variants) or bool(anchors.specific),
        specific_terms=frozenset(anchors.specific),
    )
    for cand in candidates.values():
        _score_and_accept(
            cand,
            min_score=min_score,
            min_independent_sources=min_independent_sources,
            anchor_policy=policy,
        )

    accepted = sorted(
        (c for c in candidates.values() if c.accepted),
        key=lambda c: (-c.score, c.path),
    )
    accepted_by_path = {c.path: c for c in accepted}
    if accepted:
        diagnostics.append(
            "accepted: "
            + ", ".join(
                f"{c.path}[{','.join(sorted(c.sources()))}]" for c in accepted
            )
        )

    # ------------------------------------------------------------------
    # 5b. Expected-target obligations for ANCHOR-ALIGNED exact expects only.
    #     A stale/imprecise expects edge (target shows no concrete signal) is
    #     not turned into an obligation; it is surfaced as a diagnostic.
    # ------------------------------------------------------------------
    target_obligations, ignored_expected = _derive_expected_target_obligations(
        expected, accepted_by_path, ctx
    )
    if target_obligations:
        diagnostics.append(
            f"expected-target obligations: {len(target_obligations)}"
        )
    if ignored_expected:
        diagnostics.append(
            "ignored expected target(s) without a concrete anchor: "
            + ", ".join(ignored_expected)
        )
    obligations = semantic_obligations + target_obligations

    # ------------------------------------------------------------------
    # 6. Too-broad expected envelope => ambiguous (anti-false-green).
    # ------------------------------------------------------------------
    if any(env.too_broad for env in expected.values()):
        diagnostics.append(
            "expected envelope too broad (> cardinality cap exact targets); "
            "refusing to bridge-cover a sprawling expects set"
        )
        return _ambiguous_plan(
            design_node_ids, accepted, candidates, obligations, diagnostics
        )

    # ------------------------------------------------------------------
    # 7. Obligation coverage (direct, then candidate-aligned expected-bridge).
    # ------------------------------------------------------------------
    covered, unresolved, bridged_only = _cover_obligations(accepted, obligations, ctx)

    # ------------------------------------------------------------------
    # 8. Bridge-capacity guard: one expected file must not silently bridge
    #    many abstract obligations.
    # ------------------------------------------------------------------
    if not _bridge_capacity_ok(bridged_only):
        diagnostics.append(
            "expected-bridge capacity exceeded: a single expected file is the "
            "sole bridge for multiple abstract obligations; refusing (ambiguous)"
        )
        return _ambiguous_plan(
            design_node_ids, accepted, candidates, obligations, diagnostics
        )

    # ------------------------------------------------------------------
    # 9. Selection: admission is not selection. Pick obligation-covering
    #    candidates + anchor-discovered targets; drop expects-only stale edges.
    # ------------------------------------------------------------------
    impl_paths = _select_impl_paths(accepted, covered, ctx)

    # ------------------------------------------------------------------
    # 10. Cardinality guard on the SELECTED write set (refuse a sweeping patch).
    # ------------------------------------------------------------------
    if len(impl_paths) > max_impl_candidates:
        diagnostics.append(
            f"too many implementation targets: {len(impl_paths)} > "
            f"{max_impl_candidates}; refusing to apply a broad AI patch"
        )
        return _ambiguous_plan(
            design_node_ids,
            accepted,
            candidates,
            obligations,
            diagnostics,
            impl_paths=impl_paths,
        )

    # ------------------------------------------------------------------
    # 11. Pure-abstract-facet guard (anti-false-green).
    #     When EVERY obligation is an abstract facet (no concrete write, no
    #     concrete surface-reach) there is no deterministic completeness
    #     contract: an abstract facet is coverable by ANY discriminator-bearing
    #     file, so a generic field/token mismatch (e.g. the LLM mislabeling a
    #     common word as a field) can let unrelated files fake green. Trust such
    #     a plan ONLY when at least one SELECTED target is pinned by a content
    #     certificate (a rare/literal anchor or rare-anchor cluster) — the strong
    #     field-less discriminator. Otherwise refuse (ambiguous): a styling/theme
    #     change with no concretely-pinned target is exactly the case that must
    #     fail safe rather than apply a confident-looking wrong patch.
    # ------------------------------------------------------------------
    if obligations and all(
        not o.concrete_write and not o.hard_surface for o in obligations
    ):
        selected_set = set(impl_paths)
        certified = any(
            (c.sources() & _CONTENT_CERTIFYING_SOURCES)
            for c in accepted
            if c.path in selected_set
        )
        if not certified:
            diagnostics.append(
                "all obligations are abstract facets and no selected target is "
                "pinned by a content certificate (rare/literal anchor); cannot "
                "verify completeness — refusing (anti-false-green)"
            )
            return _ambiguous_plan(
                design_node_ids,
                accepted,
                candidates,
                obligations,
                diagnostics,
                impl_paths=impl_paths,
            )

    # ------------------------------------------------------------------
    # Resolve tests for the selected impls (DAG tested_by + name heuristics).
    # ------------------------------------------------------------------
    test_paths = _resolve_tests(dag, impl_paths, project_root)

    # ------------------------------------------------------------------
    # Status.
    # ------------------------------------------------------------------
    if not obligations:
        # Nothing to verify against — cannot prove completeness, so refuse.
        status: Literal["complete", "ambiguous", "incomplete"] = "ambiguous"
        diagnostics.append(
            "no obligations could be derived from the analysis — cannot verify "
            "completeness; refusing to apply (anti-false-green)"
        )
    elif unresolved:
        status = "incomplete"
        diagnostics.append("unresolved obligation(s): " + ", ".join(unresolved))
    elif not impl_paths:
        status = "incomplete"
        diagnostics.append("no implementation candidates selected")
    else:
        status = "complete"

    return ImpactPlan(
        design_node_ids=list(design_node_ids),
        impl_paths=impl_paths,
        test_paths=test_paths,
        candidates=sorted(candidates.values(), key=lambda c: c.path),
        obligations=obligations,
        covered_obligations=covered,
        unresolved_obligations=unresolved,
        status=status,
        diagnostics=diagnostics,
    )


def _ambiguous_plan(
    design_node_ids: list[str],
    accepted: list[ImplCandidate],
    candidates: dict[str, ImplCandidate],
    obligations: list[ImpactObligation],
    diagnostics: list[str],
    *,
    impl_paths: list[str] | None = None,
    test_paths: list[str] | None = None,
) -> ImpactPlan:
    """Build an ``ambiguous`` plan (refuse to apply)."""
    return ImpactPlan(
        design_node_ids=list(design_node_ids),
        impl_paths=impl_paths if impl_paths is not None else [c.path for c in accepted],
        test_paths=test_paths or [],
        candidates=sorted(candidates.values(), key=lambda c: c.path),
        obligations=obligations,
        covered_obligations={},
        unresolved_obligations=[o.id for o in obligations],
        status="ambiguous",
        diagnostics=diagnostics,
    )


def _resolve_tests(
    dag: Any,
    impl_paths: list[str],
    project_root: Path,
) -> list[str]:
    """Collect test files for the accepted impl files via DAG ``tested_by``."""
    nodes = getattr(dag, "nodes", {}) or {}
    forward: dict[str, list[str]] = {}
    for edge in getattr(dag, "edges", []) or []:
        if edge.kind == "tested_by":
            forward.setdefault(edge.from_id, []).append(edge.to_id)

    # Map rel-path -> node-id.
    path_to_node: dict[str, str] = {}
    for node in nodes.values():
        node_path = Path(str(node.path or node.id)).as_posix()
        path_to_node[node_path] = node.id

    out: list[str] = []
    seen: set[str] = set()
    for impl in impl_paths:
        node_id = path_to_node.get(impl, impl)
        for test_id in forward.get(node_id, []):
            node = nodes.get(test_id)
            test_path = Path(str((node.path if node else None) or test_id)).as_posix()
            if test_path.endswith(".md"):
                continue
            if test_path not in seen:
                seen.add(test_path)
                out.append(test_path)
    return sorted(out)
