"""Anti-false-green authenticity gate for ``codd: covers vb=<id>`` markers.

The VB *coverage* audit (:mod:`codd.verifiable_behavior_audit`) reconciles the
declared VB universe against ``codd: covers vb=<id>`` markers — it answers "is a
marker present for every behavior?". A ``covers`` marker, however, is a CLAIM
that a test *proves* the behavior. Once an implement-stage rerun loop is wired to
re-implement on uncovered VBs, the pressure "add a marker" can be satisfied by
LYING: dropping ``codd: covers vb=X`` onto an empty test, a skipped test, or a
``describe`` block with no assertions. That is the false-green this module
exists to block.

This module is the *authenticity* gate. It does NOT decide coverage; it decides
whether each coverage CLAIM is structurally credible. It is run as a HARD gate
alongside the coverage audit (both must pass). The composite check is staged so
each later stage only strengthens, never replaces, the earlier ones:

* **Stage 1 — marker validity.** A ``covers vb=X`` whose id is declared in no VB
  table is an orphan (already surfaced by the coverage audit's
  ``orphan_vb_markers``; re-asserted here so the authenticity gate is a single
  composite verdict). An orphan marker proves nothing — it points at no behavior.
* **Stage 2 — attachment validity.** The marker must be attached to an
  EXECUTABLE test block. A marker on a ``it.skip`` / ``test.todo`` /
  ``describe.skip`` / ``@pytest.mark.skip`` / disabled block — or in a file the
  runner never executes — is not coverage: a skipped test asserts nothing.

  Attachment is *block-ized* (no fixed line lookahead): a contiguous run of
  ``codd:`` marker comments + ordinary comments + blank lines is attached to the
  NEXT executable test block, and ONLY if no non-test statement (``import`` /
  ``const`` / ``for`` / a ``describe`` opener / …) appears before that block.
  This is what lets a stack of 7 markers above one ``it(...)`` all attach to it,
  while a file-top marker separated from the first test by ``import`` lines or a
  fixtures ``const`` does NOT spuriously attach. A marker sitting directly above
  a ``describe`` that contains several ``it``s attaches to the FIRST ``it`` only
  (group-level fan-out would be false coverage; an explicit ``scope=describe``
  syntax is a future, deliberately-unbuilt extension).
* **Stage 3 — assertion EVIDENCE.** The attached test block must contain
  assertion *evidence*. Evidence is an EVIDENCE GRAPH, not a textual ``expect(``
  in the block body, because the standard e2e shape delegates the assertion to a
  helper (``expectSuccessfulRun(result, fixture.stdout)`` whose body runs the
  real ``expect(result.exitCode).toBe(0)``). Evidence is therefore::

      a direct primitive assertion in the test body that is NOT constant-only
      OR
      a call to a RESOLVED helper whose body contains a primitive assertion/fail
      AND references the helper's argument(s) (an "argument anchor")

  A DIRECT primitive assertion is credited only when it references at least one
  non-ignored name (a SUT call, exception, state, local, or output). A
  CONSTANT-only direct assertion (``assert True`` / ``assert 1 == 1`` /
  ``expect(true).toBe(true)``) references nothing but assertion APIs, literals,
  keywords and obvious builtins — it proves nothing, so it is NOT evidence (the
  direct-side analogue of the helper-side argument anchor that already rejects a
  constant-only helper). A SELF-COMPARISON tautology (``assert x == x`` /
  ``assert f(a) == f(a)`` / ``assertEqual(sorted(v), sorted(v))``) is also NOT
  evidence: normalized-operand equivalence (same AST dump) on a vacuously-true
  operator (``==``/``is``/``<=``/``>=`` and the equality-family xUnit asserts)
  proves nothing, so it is rejected (reason ``tautology_direct``). The dual
  vacuously-FALSE forms (``!=``/``<``/``>``/``is not``) are deliberately NOT
  flagged — they would FAIL the test (not a false-green) and ``x != x`` is the
  legitimate NaN idiom (anti-false-red).

  It is NOT, however, a full dataflow analysis. Three documented RESIDUALS
  remain (all bounded by the coverage↔authenticity pincer, the intent-worded
  contract, and verify actually executing the test):
    - a local-constant alias (``x = True; assert x``) — closing it risks
      false-RED on legitimate callback / mutation tests (``called = False; …;
      assert called``);
    - a SUT-DISCONNECTED literal assertion (``x = 4; assert x == 4`` — a real
      name, a non-tautological compare, but the value never came from running
      the SUT) — a test-body call-origin dataflow analysis is future work;
    - a SEMANTIC tautology whose two operands DIFFER textually but are equal by
      meaning (``assert x == 4`` next to ``x = 2 + 2``) — left to a future
      mutation-probe stage (Stage 5), never to a brittle pattern hunt.

  The argument anchor is what keeps this anti-false-green: a no-op helper
  (``function expectSuccess() { expect(true).toBe(true); }``) does not reference
  its arguments, so marker-spam delegating to it still FAILS. The helper NAME
  (``expect``/``assert``/``should``/``verify``/``ensure``/``require``/``check``…)
  is used ONLY to decide WHICH call to resolve — never to hard-pass; a
  ``checkConfig()`` whose body has no primitive assertion fails. Resolution is a
  single hop through the same-repo import that binds the helper symbol (the same
  import-resolution discipline as the native implement-oracle), and is delegated
  to the per-profile adapter so the gate logic stays language-agnostic.

Stages 4 (behavior-anchor) and 5 (mutation probe) from the design are
deliberately NOT implemented here — proving that a natural-language VB row's
*meaning* is exercised by a test, from prose alone, over-constrains and produces
false-RED. They are left as explicit extension hooks (see ``AuthenticityHook``)
so a profile that ships machine-readable anchors / a mutation harness can opt in
later without reworking this gate.

Marker STACKING (one test file carrying many ``codd: covers vb=`` markers) is
another deliberate residual: it is REPORTED for audit visibility
(``verifiable_behavior_audit.summarize_marker_distribution``) but NOT capped —
a per-file marker ceiling would false-RED a legitimate table-driven /
parametrized test that covers several related VBs in one file (anti-false-red).

GENERALITY (see ``feedback_codd_generality_preservation``): every language-
specific operation — locating the test block a marker sits in, detecting skip,
detecting a PRIMITIVE assertion, extracting an assertion-like helper call, and
RESOLVING that call to its helper body — is delegated to a per-profile
:class:`~codd.project_types.TestBlockProfile` adapter resolved from the active
:class:`~codd.project_types.LayoutProfile`. The gate logic here is language-
agnostic. When NO adapter is available for a stack (an unknown language, or a
file the adapter cannot parse), the gate GRACEFULLY DEGRADES: stage 1 still
applies (it is language-agnostic), stages 2-3 are SKIPPED for that file with a
warning rather than failing — an un-parseable stack must never produce a
false-RED. When an adapter IS available but a *helper call cannot be resolved*
(the parser exists, the helper just does not resolve to a credible body), that
is a hard FAIL in greenfield strict: an unresolved assertion helper is not
evidence (the design's "unresolved helper = fail" rule).
"""

from __future__ import annotations

import ast
import builtins
import re
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol

from codd.operational_e2e_audit import (
    _COVER_MARKER_RE,
    _iter_test_files,
    _load_optional_config,
    _rel_path,
    _resolve_vb_scan_dirs,
)
from codd.path_safety import resolve_project_path
from codd.verifiable_behavior_audit import (
    VBAuditReport,
    _VB_TOKEN_RE,
    _normalize_vb_id,
    load_verifiable_behaviors,
)


# ---------------------------------------------------------------------------
# Per-profile adapter contract (the ONLY language-specific surface)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TestBlock:
    """One executable test block located in a test file.

    ``start_line``/``end_line`` are 1-based inclusive line bounds of the block's
    body (the region a marker placed *inside* the block falls within, and the
    region searched for assertions). ``is_executable`` is False when the block is
    skipped/todo/disabled (a marker attached to it is not coverage).
    ``has_assertion`` is True when the body contains a DIRECT PRIMITIVE assertion
    (a language built-in like ``expect(...)`` / ``assert`` — NOT a bare call to a
    helper; helper delegation is resolved separately via
    :meth:`TestBlockProfile.resolve_assertion_evidence`). ``body_text`` is the
    block's body source (used by the gate to ask the adapter to resolve a
    delegated-assertion helper). ``label`` is a short human name (the test title)
    for diagnostics.
    """

    start_line: int
    end_line: int
    is_executable: bool
    has_assertion: bool
    label: str = ""
    body_text: str = ""


#: Verdict for the assertion-EVIDENCE stage of ONE attached executable block.
#: ``ok`` is True iff the block carries credible assertion evidence. ``reason``
#: is a machine token the gate maps to a violation kind / message:
#:   ``direct``              — a direct primitive assertion in the body (pass)
#:   ``constant_direct``     — a direct primitive assertion that is CONSTANT-only
#:                             (``assert True`` / ``expect(true).toBe(true)``) —
#:                             references no non-ignored name, so proves nothing
#:   ``helper_resolved``     — a resolved helper with primitive + arg anchor (pass)
#:   ``library_terminal``    — a DECLARED library fluent-terminal call credited off
#:                             ``assertion_hints.library_assertion_terminals`` data
#:                             (e.g. ArchUnit's ``rule.check(classes)``) — pass
#:   ``no_assertion``        — neither a primitive nor any assertion-like call
#:   ``unresolved_helper``   — an assertion-like call not resolvable 1-hop
#:   ``helper_no_primitive`` — resolved helper body has no primitive assertion/fail
#:   ``constant_helper``     — resolved helper asserts only constants (no anchor)
#:   ``unaccepted_confidence`` — evidence resolved ``ok=True`` but its
#:                             ``confidence`` is not in this profile's
#:                             ``authenticity_policy.accepted_assertion_confidence``
@dataclass(frozen=True)
class AssertionEvidence:
    ok: bool
    reason: str
    detail: str = ""
    #: How directly this evidence was observed. ``"certain"`` (the default, and
    #: the ONLY value any resolver produced before this field existed) is a
    #: primitive assertion seen directly or reached through the language-free
    #: helper-hop engine (:func:`_resolve_evidence`). ``"declared"`` is evidence
    #: credited off DECLARED, profile-supplied metadata rather than a resolved
    #: body (today: a library fluent-terminal call — see ``library_terminal``
    #: above). The gate (:func:`build_authenticity_report`) only accepts a
    #: profile's declared ``authenticity_policy.accepted_assertion_confidence``
    #: list; a profile that never mentions the key implicitly accepts only
    #: ``["certain"]`` — unchanged behavior for every profile that does not
    #: explicitly opt a wider tier in.
    confidence: str = "certain"


class TestBlockProfile(Protocol):
    """Language-specific test-structure adapter (resolved from LayoutProfile).

    An implementation parses ONE test file's text into the executable test blocks
    it contains, with skip/PRIMITIVE-assertion facts already resolved per block,
    and can RESOLVE a block's delegated-assertion helper one hop through the
    same-repo import graph. Returning an empty block list means "no recognizable
    test blocks" (the gate then degrades for that file). Implementations must be
    PURE and best-effort: a parse they cannot do should return ``[]`` (degrade),
    never raise.
    """

    def handles_file(self, rel_path: str) -> bool:
        """Whether this adapter recognizes ``rel_path`` as one of its test files."""

    def parse_test_blocks(self, text: str) -> list[TestBlock]:
        """Parse a test file's text into executable-test-block records."""

    def resolve_assertion_evidence(
        self, block: TestBlock, *, importer_text: str, importer_rel: str, project_root: Path
    ) -> AssertionEvidence:
        """Decide whether ``block`` carries assertion evidence (direct or via helper).

        Called by the gate ONLY for an attached, executable block that has no
        DIRECT primitive assertion (``block.has_assertion`` is False). The adapter
        extracts assertion-like helper calls from ``block.body_text``, resolves
        each one hop through ``importer_text``'s imports to its helper body, and
        returns an :class:`AssertionEvidence` verdict. ``importer_rel`` /
        ``project_root`` locate sibling helper modules for resolution.
        """


# ---------------------------------------------------------------------------
# Gate result model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthenticityViolation:
    """One inauthentic coverage CLAIM.

    ``kind`` is one of ``orphan`` (stage 1), ``skipped`` / ``unattached`` (stage
    2), ``no_assertion`` (stage 3 — covers a body with no primitive AND no
    credible helper evidence, including an unresolved / constant-only / no-op
    helper). ``vb_id`` is the claimed behavior; ``path`` and ``line`` locate the
    marker.
    """

    kind: str
    vb_id: str
    path: str
    line: int
    message: str


@dataclass
class AuthenticityReport:
    """Composite authenticity verdict over all ``covers vb=`` markers."""

    version: str
    violations: list[AuthenticityViolation] = field(default_factory=list)
    #: files the gate could not parse (stages 2-3 skipped → degraded to stage 1)
    degraded_paths: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.violations


AUTHENTICITY_CONTRACT_VERSION = "vb-marker-authenticity/v1"

#: Stage-4/5 extension hook: ``hook(marker, block, behaviors) -> str | None``.
#: Return a violation message to FAIL, or ``None`` to pass. A profile that ships
#: machine-readable behavior anchors / a mutation harness registers one here.
AuthenticityHook = Callable[..., "str | None"]


# ---------------------------------------------------------------------------
# Marker scanning (line-accurate — coverage audit only needs file granularity)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CoverMarker:
    path: str
    vb_id: str
    line: int  # 1-based line the marker sits on


def _scan_cover_markers_with_lines(text: str, rel_path: str) -> list[_CoverMarker]:
    """Locate every ``codd: covers vb=<id>`` marker with its 1-based line.

    Mirrors the coverage audit's multi-token recovery (``covers vb=22 vb=23``)
    but additionally records WHERE each marker is so attachment/assertion can be
    evaluated against the enclosing test block. Operation-subject markers (no
    ``vb=``) are owned by the operational-e2e audit and skipped here.
    """

    markers: list[_CoverMarker] = []
    for match in _COVER_MARKER_RE.finditer(text):
        first_vb = match.group("vb")
        if first_vb is None:
            continue
        line = text.count("\n", 0, match.start()) + 1
        raw_details = match.group("details") or ""
        extra_ids = [token.group("vb").strip() for token in _VB_TOKEN_RE.finditer(raw_details)]
        for vb_id in [first_vb.strip(), *extra_ids]:
            markers.append(_CoverMarker(path=rel_path, vb_id=vb_id, line=line))
    return markers


#: Provenance header CoDD stamps onto every file it generates. The implementer
#: writes ``@generated-by: codd implement``, the generator ``@generated-by: codd
#: generate``, the propagator ``@generated-by: codd propagate`` — all sharing this
#: language-agnostic substring under the file's own comment prefix (``//`` / ``#``;
#: see ``codd.implementer.COMMENT_PREFIX_BY_SUFFIX`` and ``_build_traceability_
#: comment``). The same substring is what ``implementer._root_artifact_overwrite_
#: blocked`` uses to recognize a CoDD-owned file.
_HARNESS_PROVENANCE_MARKER = "@generated-by: codd"


def _is_harness_generated(text: str) -> bool:
    """Whether ``text`` is a file CoDD ITSELF generated (deterministic; no LLM).

    True iff the file carries CoDD's generation-provenance header
    (``@generated-by: codd …``). This is the HARNESS-OWNED vs USER/CUSTOM
    discriminator for the strict-observability gate: a harness-owned file with no
    parseable test block is a genuine false-green CoDD produced (hard-RED), while
    a user-authored file in a recognized extension that our block-parser merely
    cannot extract must degrade rather than false-RED. Matching the bare
    ``@generated-by: codd`` substring keeps this language-agnostic — the marker is
    emitted under each language's own comment prefix but the substring is constant.
    """

    return _HARNESS_PROVENANCE_MARKER in text


def _attached_block(marker_line: int, text: str, blocks: list[TestBlock]) -> TestBlock | None:
    """The executable test block a marker on ``marker_line`` attaches to.

    A marker is either INSIDE a leaf test block's body, or written ABOVE the test
    it annotates (the conventional leading comment). The algorithm:

    1. Find the SMALLEST block whose body contains the marker line. If it is a
       LEAF (``it``/``test``/``def test_*`` — not a ``describe`` group), the
       marker is that leaf's (a marker inside a nested ``it`` is the ``it``'s, not
       the enclosing ``describe``'s).
    2. Otherwise the marker is a LEADING marker (above a test, possibly nested
       inside a ``describe`` body but above the first ``it``). Find the nearest
       test block opening AT/AFTER the marker (within the containing group, if
       any) and attach ONLY when every line strictly between the marker and that
       opener is a HEADER line (comment / marker / decorator / blank). A real
       statement in between (``import`` / ``const`` / ``for`` / a fixtures decl)
       means the marker is a banner / belongs to nothing → unattached. When the
       chosen opener is itself a ``describe`` group, attach to its FIRST nested
       test (group-level fan-out would be false coverage).

    Returning ``None`` means "unattached" (stage-2 failure).
    """

    lines = text.splitlines()

    # 1. Smallest block CONTAINING the marker line.
    containing: TestBlock | None = None
    for block in blocks:
        if block.start_line <= marker_line <= block.end_line:
            if containing is None or (block.end_line - block.start_line) < (
                containing.end_line - containing.start_line
            ):
                containing = block
    if containing is not None and not _is_group_block(containing, blocks):
        return containing  # marker inside a leaf test block.

    # 2. Leading marker. Nearest test block opening AT/AFTER the marker. If the
    # marker is inside a group body, restrict to that group's descendants so a
    # marker in describe-A does not jump to a sibling describe-B's first test.
    scope = containing
    candidates = [b for b in blocks if b.start_line > marker_line]
    if scope is not None:
        candidates = [b for b in candidates if b.end_line <= scope.end_line]
    if not candidates:
        return None
    opener = min(candidates, key=lambda b: b.start_line)
    for ln in range(marker_line + 1, opener.start_line):  # 1-based exclusive range
        stripped = lines[ln - 1].strip() if ln - 1 < len(lines) else ""
        if not stripped:
            continue
        if _is_comment_or_marker_line(stripped):
            continue
        # A non-header statement separates the marker from the next test block.
        return None
    if _is_group_block(opener, blocks):
        # describe group → attach to its FIRST nested test block.
        return _first_child_block(opener, blocks)
    return opener


_COMMENT_PREFIXES = ("//", "#", "/*", "*", "*/")


def _is_comment_or_marker_line(stripped: str) -> bool:
    """A line that belongs to a test's LEADING header block during attachment.

    The attachment walk treats a contiguous run of these lines above a test as
    the test's header, while the FIRST real statement (not one of these)
    terminates the walk. Header lines are: a ``codd:`` marker, a pure comment
    (any language), or a DECORATOR / annotation line (``@pytest.mark.skip`` /
    ``@pytest.mark.parametrize`` / a TS method decorator) — a decorator binds to
    the ``def`` that follows it, so it must not break the marker→test link (the
    skip/non-skip of that def is already resolved by the per-profile parser).
    """

    if "codd:" in stripped:
        return True
    if stripped.startswith("@"):
        return True
    return stripped.startswith(_COMMENT_PREFIXES)


def _block_opening_at(line: int, blocks: list[TestBlock]) -> TestBlock | None:
    """The block whose opening (``start_line``) is exactly ``line``, if any."""

    for block in blocks:
        if block.start_line == line:
            return block
    return None


def _is_group_block(block: TestBlock, blocks: list[TestBlock]) -> bool:
    """Whether ``block`` strictly encloses at least one other (it is a group)."""

    for other in blocks:
        if other is block:
            continue
        if block.start_line <= other.start_line and other.end_line <= block.end_line:
            if not (block.start_line == other.start_line and block.end_line == other.end_line):
                return True
    return False


def _first_child_block(group: TestBlock, blocks: list[TestBlock]) -> TestBlock | None:
    """The first (lowest start_line) block strictly nested inside ``group``."""

    children = [
        other
        for other in blocks
        if other is not group
        and group.start_line <= other.start_line
        and other.end_line <= group.end_line
        and not (other.start_line == group.start_line and other.end_line == group.end_line)
    ]
    if not children:
        return None
    return min(children, key=lambda b: b.start_line)


# ---------------------------------------------------------------------------
# The composite authenticity gate
# ---------------------------------------------------------------------------


def _helper_evidence(
    adapter: TestBlockProfile,
    block: TestBlock,
    *,
    importer_text: str,
    importer_rel: str,
    project_root: Path,
) -> AssertionEvidence:
    """Resolve a block's DELEGATED-assertion (helper) evidence, fail-closed.

    Thin wrapper over the per-profile :meth:`TestBlockProfile.resolve_assertion_
    evidence`: a resolver that raises is treated as NO evidence (toward
    false-RED, never false-GREEN) rather than crashing the gate.
    """

    try:
        return adapter.resolve_assertion_evidence(
            block,
            importer_text=importer_text,
            importer_rel=importer_rel,
            project_root=project_root,
        )
    except Exception:  # noqa: BLE001 — resolver that throws ⇒ no evidence.
        return AssertionEvidence(ok=False, reason="no_assertion", detail="resolver error")


def _direct_evidence(
    adapter: TestBlockProfile,
    block: TestBlock,
    *,
    importer_text: str = "",
    importer_rel: str = "",
    project_root: Path | None = None,
    config: dict[str, Any] | None = None,
    profile: Any = None,
) -> AssertionEvidence:
    """Resolve a block's DIRECT (in-body) primitive-assertion evidence.

    The built-in Python/TypeScript adapters expose
    ``resolve_direct_assertion_evidence`` which decides whether the direct
    primitive assertion is constant-only (``constant_direct`` ⇒ not ok),
    library-only (``library_only_direct`` ⇒ not ok), or references a real name
    (``direct`` ⇒ ok). The test-file context (``importer_text`` / ``project_root``
    / ``profile``) is forwarded for origin classification. An EXTERNAL adapter that
    predates the context kwargs (accepts only ``(block)``) is called the old way
    (TypeError fallback); one that predates the hook entirely keeps the pre-existing
    behavior (a raw primitive is credited as ``direct``) — only the built-in stacks
    are tightened here.
    """

    resolver = getattr(adapter, "resolve_direct_assertion_evidence", None)
    if not callable(resolver):
        return AssertionEvidence(ok=True, reason="direct")
    try:
        return resolver(
            block,
            importer_text=importer_text,
            importer_rel=importer_rel,
            project_root=project_root,
            config=config,
            profile=profile,
        )
    except TypeError:
        # Older adapter signature: resolve_direct_assertion_evidence(block) only.
        try:
            return resolver(block)
        except Exception:  # noqa: BLE001 — a resolver that throws ⇒ keep credit (back-compat).
            return AssertionEvidence(ok=True, reason="direct")
    except Exception:  # noqa: BLE001 — a resolver that throws ⇒ keep credit (back-compat).
        return AssertionEvidence(ok=True, reason="direct")


#: The confidence tier every resolver produced before the ``confidence`` field
#: existed, and what every profile that never declares
#: ``authenticity_policy.accepted_assertion_confidence`` implicitly accepts.
_DEFAULT_ACCEPTED_CONFIDENCE: frozenset[str] = frozenset({"certain"})


def _accepted_assertion_confidence(profile: Any) -> frozenset[str]:
    """Resolve ``authenticity_policy.accepted_assertion_confidence`` for ``profile``.

    ``profile`` is the active :class:`~codd.project_types.LayoutProfile` passed to
    :func:`build_authenticity_report` (or any object exposing a ``language``
    attribute — e.g. a test stub). This resolves the KERNEL
    :class:`~codd.languages.profile.LanguageProfile` for that language via
    :mod:`codd.languages.registry` and reads ``tests.authenticity_policy.
    accepted_assertion_confidence`` from its YAML. That import is a ONE-WAY edge
    (``codd.languages`` never imports this module) so it carries no cycle risk,
    unlike ``codd.project_types`` which this module must never import (it imports
    *this* module lazily inside ``LayoutProfile.test_block_profile()``).

    Absent ``profile``, an unresolvable/unknown ``language``, a profile with no
    ``tests`` block, or the key simply not being declared ⇒ the STRICT default
    ``{"certain"}`` — today's behavior, byte-for-byte unchanged for every profile
    that does not explicitly opt a wider confidence tier in. Best-effort: never
    raises (a malformed value degrades to the strict default, never a crash and
    never a silent widen).
    """

    language = getattr(profile, "language", None) if profile is not None else None
    if not language or not str(language).strip():
        return _DEFAULT_ACCEPTED_CONFIDENCE
    try:
        from codd.languages.registry import default_registry

        lang_profile = default_registry.resolve(str(language))
    except Exception:  # noqa: BLE001 — resolution is best-effort; never raise.
        return _DEFAULT_ACCEPTED_CONFIDENCE
    tests = getattr(lang_profile, "tests", None)
    policy = getattr(tests, "authenticity_policy", None) if tests is not None else None
    if not isinstance(policy, Mapping):
        return _DEFAULT_ACCEPTED_CONFIDENCE
    raw = policy.get("accepted_assertion_confidence")
    if not raw:
        return _DEFAULT_ACCEPTED_CONFIDENCE
    try:
        values = frozenset(str(v) for v in raw)
    except TypeError:
        return _DEFAULT_ACCEPTED_CONFIDENCE
    return values or _DEFAULT_ACCEPTED_CONFIDENCE


def build_authenticity_report(
    project_root: Path | str,
    *,
    config: dict[str, Any] | None = None,
    profile: Any = None,
    test_dirs: Iterable[Path | str] | None = None,
    hooks: Iterable[AuthenticityHook] | None = None,
    strict_observability: bool = False,
) -> AuthenticityReport:
    """Evaluate every ``covers vb=`` marker for structural authenticity.

    ``profile`` is the active :class:`~codd.project_types.LayoutProfile` (or any
    object exposing ``test_block_profile()``); when it is ``None`` or yields no
    adapter, stages 2-3 degrade to a warning and only stage 1 (orphan) applies.

    ``strict_observability`` (contract authenticity.observable_in_supported_stack.v1):
    when True, a marker-bearing file the adapter RECOGNIZES (``handles_file``) but
    from which it parses ZERO executable test blocks is an
    ``unobservable_test_structure`` VIOLATION rather than a silent degrade — BUT
    ONLY when that file is HARNESS-OWNED (CoDD generated it; it carries a
    ``@generated-by: codd …`` provenance header). A harness-owned file with no
    parseable test is a genuine false-green CoDD produced. A USER/CUSTOM file in a
    recognized extension WITHOUT a provenance header DEGRADES instead: our block-
    parser's incompleteness (a Mocha variant, a decorated / framework-wrapped test
    style) must not hard-RED a user's valid-but-unparsed-by-us test. An UNSUPPORTED
    file (no adapter / not handled) also degrades in either mode (never a
    false-RED). The greenfield autopilot passes True; the default is False for
    back-compat with non-autopilot callers.
    """

    project_root = Path(project_root).resolve()
    if config is None:
        config = _load_optional_config(project_root)
    if test_dirs is None:
        test_dirs = _resolve_vb_scan_dirs(project_root, config)

    behaviors = load_verifiable_behaviors(project_root, config=config)
    declared_ids = {_normalize_vb_id(b.vb_id) for b in behaviors}

    adapter: TestBlockProfile | None = None
    if profile is not None:
        getter = getattr(profile, "test_block_profile", None)
        if callable(getter):
            try:
                adapter = getter()
            except Exception:  # noqa: BLE001 — adapter resolution is best-effort.
                adapter = None
    accepted_confidence = _accepted_assertion_confidence(profile)

    hook_list = list(hooks or ())
    violations: list[AuthenticityViolation] = []
    degraded: list[str] = []

    for path in _iter_test_files(project_root, test_dirs=test_dirs):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = _rel_path(path, project_root)
        markers = _scan_cover_markers_with_lines(text, rel)
        if not markers:
            continue

        # ── Stage 1: marker validity (language-agnostic — always applies) ──
        live_markers: list[_CoverMarker] = []
        for marker in markers:
            if _normalize_vb_id(marker.vb_id) not in declared_ids:
                violations.append(
                    AuthenticityViolation(
                        kind="orphan",
                        vb_id=marker.vb_id,
                        path=rel,
                        line=marker.line,
                        message=(
                            f"{rel}:{marker.line} `codd: covers vb={marker.vb_id}` references a VB "
                            "id declared in no VB table — the marker proves no declared behavior. "
                            "Fix the id or declare the behavior in the canonical VB doc."
                        ),
                    )
                )
            else:
                live_markers.append(marker)
        if not live_markers:
            continue

        # ── Stages 2-3 need the per-profile structural parse ──
        blocks: list[TestBlock] = []
        adapter_handles = adapter is not None and adapter.handles_file(rel)
        if adapter_handles:
            try:
                blocks = adapter.parse_test_blocks(text)
            except Exception:  # noqa: BLE001 — a parser that throws degrades, never fails.
                blocks = []
        if not blocks:
            if adapter_handles and strict_observability and _is_harness_generated(text):
                # OBSERVABILITY (contract authenticity.observable_in_supported_stack.v1),
                # NARROWED to HARNESS-OWNED files. The adapter RECOGNIZES this file
                # type but extracted ZERO executable test blocks though live coverage
                # markers are present. We HARD-FAIL this ONLY when CoDD ITSELF
                # generated the file (it carries a ``@generated-by: codd …``
                # provenance header): CoDD produced a marker-bearing test with no
                # parseable assertion, which is a genuine false-green CoDD owns and
                # must not ship. A USER/CUSTOM file in a recognized extension (NO
                # provenance header) falls through to the degrade path below: our
                # block-parser's incompleteness — a Mocha variant, a decorated /
                # framework-wrapped test style we cannot extract — must NOT hard-RED a
                # user's valid-but-unparsed-by-us test (that would be a false-RED).
                # Stage 1 (orphan) already ran for every file regardless.
                for marker in live_markers:
                    violations.append(
                        AuthenticityViolation(
                            kind="unobservable_test_structure",
                            vb_id=marker.vb_id,
                            path=rel,
                            line=marker.line,
                            message=(
                                f"{rel}:{marker.line} `codd: covers vb={marker.vb_id}` sits in a "
                                "CoDD-generated test file (`@generated-by: codd …`) from which the "
                                "structural parser extracted NO executable test block — the coverage "
                                "claim is unobservable (a generated marker with no parseable test "
                                "proves nothing). Generate a real test case the runner executes, or "
                                "use `codd: blocked vb=… reason=…` if it genuinely cannot run yet."
                            ),
                        )
                    )
                continue
            # Graceful degradation: an UNSUPPORTED stack/file (no adapter, or the
            # adapter does not handle this file), strict observability off, OR a
            # USER/CUSTOM recognized-extension file (no CoDD provenance header) our
            # parser could not extract a block from — our parser's incompleteness must
            # not hard-fail a user's valid test. Stage 1 already ran; skip stages 2-3
            # with a warning (never false-RED).
            degraded.append(rel)
            continue

        for marker in live_markers:
            block = _attached_block(marker.line, text, blocks)
            if block is None:
                # Stage 2 (attachment): marker is not inside/above any test block
                # (e.g. a file-top banner, a helper, dead region, or separated
                # from the next test by an import/const/non-test statement).
                violations.append(
                    AuthenticityViolation(
                        kind="unattached",
                        vb_id=marker.vb_id,
                        path=rel,
                        line=marker.line,
                        message=(
                            f"{rel}:{marker.line} `codd: covers vb={marker.vb_id}` is not attached to "
                            "an executable test block (no test case at/below the marker, or a "
                            "non-test statement separates it from the next test). A coverage claim "
                            "must sit on the test that proves the behavior."
                        ),
                    )
                )
                continue
            if not block.is_executable:
                # Stage 2 (attachment): skipped / todo / disabled block.
                label = f" ({block.label})" if block.label else ""
                violations.append(
                    AuthenticityViolation(
                        kind="skipped",
                        vb_id=marker.vb_id,
                        path=rel,
                        line=marker.line,
                        message=(
                            f"{rel}:{marker.line} `codd: covers vb={marker.vb_id}` is attached to a "
                            f"SKIPPED/TODO/disabled test{label} — a skipped test proves nothing. "
                            "Enable the test (or use `codd: blocked vb=… reason=…` if it genuinely "
                            "cannot run yet)."
                        ),
                    )
                )
                continue

            # ── Stage 3 (assertion EVIDENCE): direct primitive OR resolved helper ──
            # A DIRECT primitive assertion must not be CONSTANT-only. ``assert
            # True`` / ``expect(true).toBe(true)`` is a primitive (so
            # ``has_assertion`` is True) yet proves nothing — the direct-side
            # analogue of the helper-side argument anchor. We resolve direct
            # evidence through ``_direct_evidence`` (per-profile, AST for Python /
            # lexical for TS); if it is constant-only we DO NOT immediately fail —
            # the same block may ALSO call a credible helper, so we fall back to
            # the helper resolver and only RED when BOTH paths fail.
            if block.has_assertion:
                evidence = _direct_evidence(
                    adapter,
                    block,
                    importer_text=text,
                    importer_rel=rel,
                    project_root=project_root,
                    config=config,
                    profile=profile,
                )
                if not evidence.ok:
                    helper_evidence = _helper_evidence(
                        adapter,
                        block,
                        importer_text=text,
                        importer_rel=rel,
                        project_root=project_root,
                    )
                    if helper_evidence.ok:
                        evidence = helper_evidence
            else:
                evidence = _helper_evidence(
                    adapter,
                    block,
                    importer_text=text,
                    importer_rel=rel,
                    project_root=project_root,
                )
            # Confidence gate: evidence that resolved ``ok=True`` still only
            # counts if its confidence tier is one this profile's
            # ``authenticity_policy.accepted_assertion_confidence`` accepts.
            # Every existing resolver only ever produces ``"certain"``, and
            # every profile without the key accepts only ``{"certain"}``, so
            # this is a no-op for every stack that has not opted a wider tier
            # in (see :func:`_accepted_assertion_confidence`).
            if evidence.ok and evidence.confidence not in accepted_confidence:
                evidence = AssertionEvidence(
                    ok=False,
                    reason="unaccepted_confidence",
                    detail=(
                        f"confidence={evidence.confidence!r} not accepted by this profile "
                        f"(accepted={sorted(accepted_confidence)!r})"
                    ),
                )
            if not evidence.ok:
                label = f" ({block.label})" if block.label else ""
                violations.append(
                    AuthenticityViolation(
                        kind="no_assertion",
                        vb_id=marker.vb_id,
                        path=rel,
                        line=marker.line,
                        message=_no_assertion_message(rel, marker, label, evidence),
                    )
                )
                continue
            # ── Stages 4-5 extension hooks (opt-in; none ship by default) ──
            for hook in hook_list:
                try:
                    detail = hook(marker=marker, block=block, behaviors=behaviors)
                except Exception:  # noqa: BLE001 — a hook that throws is skipped, not fatal.
                    detail = None
                if detail:
                    violations.append(
                        AuthenticityViolation(
                            kind="hook",
                            vb_id=marker.vb_id,
                            path=rel,
                            line=marker.line,
                            message=f"{rel}:{marker.line} {detail}",
                        )
                    )

    return AuthenticityReport(
        version=AUTHENTICITY_CONTRACT_VERSION,
        violations=violations,
        degraded_paths=sorted(set(degraded)),
    )


def _no_assertion_message(
    rel: str, marker: _CoverMarker, label: str, evidence: AssertionEvidence
) -> str:
    """A reason-specific message for a stage-3 assertion-evidence failure.

    All variants are the same ``no_assertion`` violation KIND (a body that does
    not credibly prove the behavior) but the prose pinpoints WHY so the rerun
    feedback is actionable — and never suggests "add a marker".
    """

    head = f"{rel}:{marker.line} `codd: covers vb={marker.vb_id}` is attached to a test{label}"
    if evidence.reason == "constant_direct":
        why = (
            " whose direct primitive assertion is CONSTANT-only "
            "(`assert True`, `assert 1 == 1`, or `expect(true).toBe(true)`) — "
            "a constant assertion proves nothing. Assert against an observed result, "
            "exception, state, or output that would FAIL if the behavior were broken."
        )
    elif evidence.reason == "tautology_direct":
        why = (
            " whose direct primitive assertion compares a value to ITSELF "
            "(`assert x == x`, `assert f(a) == f(a)`, `assertEqual(sorted(v), sorted(v))`) — "
            "a self-comparison is a tautology that holds regardless of the system under test "
            "and proves nothing. Compute the expected value INDEPENDENTLY of the value under "
            "test and assert the two are equal."
        )
    elif evidence.reason == "library_only_direct":
        detail = f" ({evidence.detail})" if evidence.detail else ""
        why = (
            " whose direct primitive assertion observes ONLY library/builtin behavior"
            f"{detail} (`math.sqrt`, `os.path`, a third-party package, `sorted`, etc.) "
            "and never references a first-party result, local observation, fixture, or "
            "unknown runtime value — a library property test does not prove the VB. Call "
            "the SUT and assert against ITS result, state, or raised error."
        )
    elif evidence.reason == "unresolved_helper":
        why = (
            " whose only assertion is delegated to a helper that could not be resolved "
            f"({evidence.detail or 'no resolvable helper body'}). An unresolved assertion "
            "helper is not evidence — import the helper from a same-repo module whose body "
            "runs a real assertion, or assert directly in the test."
        )
    elif evidence.reason == "helper_no_primitive":
        why = (
            f" that delegates to a helper ({evidence.detail or 'helper'}) whose body contains "
            "NO assertion — calling a helper that checks nothing does not prove the behavior. "
            "Make the helper assert (or assert directly in the test)."
        )
    elif evidence.reason == "constant_helper":
        why = (
            f" that delegates to a helper ({evidence.detail or 'helper'}) which asserts only "
            "CONSTANTS (it never references its arguments, e.g. `expect(true).toBe(true)`) — a "
            "constant assertion proves nothing. Assert against the call's actual result."
        )
    elif evidence.reason == "unaccepted_confidence":
        why = (
            f" whose evidence ({evidence.detail or 'a lower-confidence signal'}) this profile's "
            "`authenticity_policy.accepted_assertion_confidence` does not accept. Strengthen the "
            "assertion so it resolves at an accepted confidence tier, or widen the profile's "
            "accepted list if this class of evidence is genuinely trusted."
        )
    else:  # no_assertion
        why = (
            " with NO assertion — running code without checking an outcome does not prove the "
            "behavior. Add an assertion (directly, or via a helper whose body asserts the result) "
            "that would FAIL if the behavior were broken."
        )
    return head + why


def format_authenticity_feedback(report: AuthenticityReport) -> str:
    """Render authenticity violations as SUT-facing rerun feedback.

    The feedback is deliberately about *strengthening tests*, never about
    *adding markers* — adding a marker to satisfy the gate is precisely the
    false-green this gate blocks (see ``format_gap_feedback`` for the same
    discipline on the coverage side).
    """

    lines = [
        "Some `codd: covers vb=<id>` markers are not authentic coverage claims.",
        "A `covers` marker asserts that a test PROVES the behavior. Do not add or keep "
        "`codd: covers vb=X` on a test unless that test exercises the public behavior X "
        "describes and contains an assertion that would FAIL if X were broken (directly, or "
        "via a helper whose body runs a real assertion against the call's result). Fix each "
        "below by making the test real (enable it, add the missing assertion, resolve the "
        "assertion helper, or move the marker to the test that actually proves the behavior) "
        "— never by silencing the gate.",
        "",
        "Inauthentic coverage claims:",
    ]
    for violation in report.violations:
        lines.append(f"- {violation.message}")
    return "\n".join(lines)


def render_authenticity_markdown(report: AuthenticityReport) -> str:
    """Render the authenticity report as Markdown (for ``codd test audit`` etc.)."""

    lines = [
        "# Verifiable-Behavior Marker Authenticity",
        "",
        f"- Contract: {report.version}",
        f"- Inauthentic coverage claims: {len(report.violations)}",
        f"- Files not structurally parseable (stage 1 only): {len(report.degraded_paths)}",
    ]
    if report.violations:
        lines.extend(
            [
                "",
                "## Inauthentic Coverage Claims",
                "| Test | Line | VB | Kind |",
                "| --- | --- | --- | --- |",
            ]
        )
        for violation in report.violations:
            lines.append(
                f"| {violation.path} | {violation.line} | {violation.vb_id} | {violation.kind} |"
            )
    if report.degraded_paths:
        lines.extend(
            [
                "",
                "## Stage-1-only (un-parseable) files",
                "Attachment/assertion checks were skipped for these (graceful degradation):",
                "",
            ]
        )
        for path in report.degraded_paths:
            lines.append(f"- {path}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# DIRECT-assertion evidence (the direct-side analogue of the helper argument
# anchor). A primitive assertion IN THE TEST BODY is credited only when it
# references at least one NON-IGNORED name — a SUT call, exception, Enum, state,
# local, or output. A CONSTANT-only direct assertion (``assert True`` /
# ``expect(true).toBe(true)``) references nothing but assertion APIs, literals,
# keywords and obvious builtins, so it proves nothing and is NOT evidence.
#
# GENERALITY GUARDRAILS (do NOT widen the ignored sets):
#   * Only assertion APIs, literals/keywords, and obvious builtins are ignored.
#     SUT call names, exception classes, Enum names and local variables are NEVER
#     ignored — so ``assert validate()`` (validate), ``expect(conv()).toBe(1)``
#     (conv) and ``assert exc.value.code == ErrorCode.X`` (exc) stay GREEN. This
#     is what prevents a false-RED on a legitimate NO-FIXTURE test.
#   * No local-constant / dataflow analysis. ``x = True; assert x`` stays GREEN —
#     closing it would risk false-RED on callback / mutation tests
#     (``called = False; …; assert called``). It is a documented residual.
# ---------------------------------------------------------------------------


#: Python names that are NOT credit-worthy references inside a direct primitive
#: assertion: the assertion APIs themselves plus literals / keywords / obvious
#: builtins. NOTHING here is a SUT symbol — a function/Enum/exception/local name
#: is never ignored, so a real observation (``assert validate()`` /
#: ``assert exc.value.code == ErrorCode.X``) is always credited.
_PY_DIRECT_IGNORED_NAMES = frozenset(
    {
        "assert",
        "pytest",
        "self",
        "cls",
        "np",
        "True",
        "False",
        "None",
        "Ellipsis",
        "bool",
        "int",
        "float",
        "str",
        "bytes",
        "list",
        "tuple",
        "dict",
        "set",
        "frozenset",
        "len",
        "sum",
        "min",
        "max",
        "all",
        "any",
        "isinstance",
        "issubclass",
        "type",
        "object",
        "Exception",
        "BaseException",
        "AssertionError",
        # the ``raise`` keyword: in the lexical (syntax-error) fallback path, a
        # ``raise AssertionError("constant")`` must not look like it carries an
        # observation name — without this it would mis-credit as a non-constant direct.
        "raise",
        "ValueError",
        "TypeError",
        "RuntimeError",
    }
)


#: TypeScript/JavaScript counterpart of :data:`_PY_DIRECT_IGNORED_NAMES`: the
#: vitest/jest/chai assertion APIs, the matcher names, plus literals / keywords /
#: obvious global builtins. A SUT call name / class is never ignored, so
#: ``expect(conv()).toBe(1)`` (``conv`` remains) stays GREEN while
#: ``expect(true).toBe(true)`` (only ignored names) becomes RED.
#:
#: NOTE (TS inline-block leak — why the test-DECLARATION globals are here too):
#: unlike a Python ``def test_x():`` whose ``body_text`` starts AFTER the def
#: line, a TS leaf block's ``body_text`` for an INLINE form
#: (``it('x', () => { expect(true).toBe(true); });``) is the WHOLE ``it(...)``
#: line — so the lexical window harvests the block-declaration callee (``it`` /
#: ``test`` / ``describe``) and the arrow keyword (``async``) as "references".
#: These are vitest/jest reserved test-framework globals on the same footing as
#: ``expect`` / ``vi`` / the matchers, so they are ignored too — otherwise an
#: inline constant-only ``it('x', () => expect(true).toBe(true))`` would leak
#: ``it`` and be mis-credited ``direct``. A real observation
#: (``expect(conv()).toBe(1)``) still retains its SUT name (``conv``), so adding
#: these never false-REDs a genuine test; and were a SUT ever literally named
#: ``it``/``test``/``describe`` the only effect is a fail-CLOSED RED on that one
#: assertion (safe direction), never a false-GREEN.
_TS_DIRECT_IGNORED_NAMES = frozenset(
    {
        "expect",
        "vi",
        "assert",
        "throw",
        "new",
        "typeof",
        "instanceof",
        "async",
        "await",
        # vitest/jest block-declaration + lifecycle globals (leak from an inline
        # block's ``body_text``; see the NOTE above).
        "it",
        "test",
        "describe",
        "xit",
        "xtest",
        "xdescribe",
        "fit",
        "fdescribe",
        "beforeEach",
        "afterEach",
        "beforeAll",
        "afterAll",
        "true",
        "false",
        "null",
        "undefined",
        "NaN",
        "Infinity",
        "toBe",
        "toEqual",
        "toStrictEqual",
        "toThrow",
        "toContain",
        "toMatch",
        "toHaveLength",
        "Boolean",
        "Number",
        "String",
        "Object",
        "Array",
        "Map",
        "Set",
        "Date",
        "RegExp",
        "Math",
        "JSON",
        "Promise",
        "Error",
        "TypeError",
        "RangeError",
        "SyntaxError",
    }
)


def _py_callee_name(node: ast.AST) -> str:
    """Dotted callee name of a call target (``pytest.raises`` / ``self.assertX``)."""

    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _py_callee_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _is_py_assertion_error_raise(node: ast.AST) -> bool:
    """Whether ``node`` is ``raise AssertionError`` / ``raise AssertionError(...)``.

    ``assert cond, msg`` IS ``if not cond: raise AssertionError(msg)``, so an explicit
    ``raise AssertionError(...)`` is a primitive assertion (the same family as the
    already-recognized ``pytest.fail()``). Restricted to ``AssertionError`` ON PURPOSE:
    a bare ``raise``/``raise err``/``raise ValueError(...)`` is exception re-raise or
    error propagation, NOT an assertion — crediting those would widen the false-GREEN
    surface. Aliased / ``builtins.AssertionError`` forms are intentionally out of scope
    (alias tracking exceeds this false-RED fix).
    """

    if not isinstance(node, ast.Raise) or node.exc is None:
        return False
    exc = node.exc
    if isinstance(exc, ast.Call):
        exc = exc.func
    return isinstance(exc, ast.Name) and exc.id == "AssertionError"


#: Callees that, used as a ``with`` CONTEXT MANAGER, ARE a primitive assertion. An
#: EXACT whitelist on purpose: at the ``with`` position the broad ``self.assert*``
#: prefix is unsafe (``with self.assertEqual(x, y):`` is runtime-broken — assertEqual
#: returns no context manager — yet would wrongly credit). These are the official
#: context-manager assertions (pytest.raises/warns + unittest assertRaises/Warns/Logs).
_WITH_ASSERT_CONTEXT_CALLEES = frozenset(
    {
        "pytest.raises",
        "pytest.warns",
        "self.assertRaises",
        "self.assertRaisesRegex",
        "self.assertRaisesRegexp",  # legacy py2 alias; harmless if unused
        "self.assertWarns",
        "self.assertWarnsRegex",
        "self.assertLogs",
        "self.assertNoLogs",
    }
)


def _is_py_primitive_assert_node(node: ast.AST) -> bool:
    """Whether ``node`` is a PRIMITIVE python assertion (mirrors ``_PY_PRIMITIVE_ASSERT_RE``).

    An ``assert`` statement, ``raise AssertionError(...)``, a ``with`` context-manager
    assertion (``pytest.raises``/``warns`` or unittest ``self.assertRaises``/``Warns``/
    ``Logs`` — see :data:`_WITH_ASSERT_CONTEXT_CALLEES`), or an expression statement
    calling ``pytest.fail`` / ``self.fail`` / ``self.assert*`` / ``np.testing.assert*``.
    A bare call to a NAMED helper is NOT primitive (it is resolved via the evidence
    graph), so it is not matched here.
    """

    if isinstance(node, ast.Assert):
        return True
    if _is_py_assertion_error_raise(node):
        return True
    if isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            expr = item.context_expr
            if isinstance(expr, ast.Call) and _py_callee_name(expr.func) in _WITH_ASSERT_CONTEXT_CALLEES:
                return True
        return False
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        callee = _py_callee_name(node.value.func)
        return (
            callee == "pytest.fail"
            or callee == "self.fail"
            or callee.startswith("self.assert")
            or callee.startswith("np.testing.assert")
        )
    return False


def _python_body_has_primitive_assertion(body_text: str) -> bool:
    """AST-first ``has_assertion`` for a python test body.

    Prefer the AST (``_is_py_primitive_assert_node``) so a primitive token sitting in
    a STRING or COMMENT (e.g. ``x = "raise AssertionError(y)"``) does not spuriously
    set ``has_assertion`` — which would otherwise reach the fail-open direct-evidence
    path and become a false-GREEN. Falls back to the regex only when the body is not
    parseable in isolation (mirrors the line-scanner fallback elsewhere)."""

    try:
        tree = ast.parse(textwrap.dedent(body_text))
    except (SyntaxError, ValueError):
        return bool(_PY_PRIMITIVE_ASSERT_RE.search(body_text))
    return any(_is_py_primitive_assert_node(node) for node in ast.walk(tree))


#: Python standard-library top-level module names (PEP-official frozenset; includes
#: platform-specific and disabled modules). Used to POSITIVELY classify a stdlib
#: import as a library reference. Absence here never forces a verdict (fail-open).
_PY_STDLIB_NAMES = frozenset(getattr(sys, "stdlib_module_names", frozenset()))
#: Python runtime builtins (``sorted``, ``abs``, ``round``, …). Used to classify a
#: builtin-only assertion as a library reference. NOTE: builtins are classified
#: here, NOT added to ``_PY_DIRECT_IGNORED_NAMES`` — a first-party import that
#: SHADOWS a builtin (``from app.sorting import sorted``) must still credit.
_PY_BUILTIN_NAMES = frozenset(dir(builtins))


@dataclass(frozen=True)
class _PyOriginContext:
    """Per-test-block context for classifying assertion reference names by ORIGIN.

    The contract is ``direct.library_only_reference.v1`` (GPT-5.5 Pro design,
    dogfood/gpt_authenticity_sut_ref_design.md): a direct primitive assertion is
    not credible VB evidence when EVERY non-framework reference is POSITIVELY a
    library/runtime/builtin reference and none is first-party / local / unknown.
    ``unknown`` always credits — "not provably first-party" must NEVER be treated
    as third-party (that would false-RED src-layout / editable / path-alias code).
    """

    imports: dict[str, tuple[str, bool]]  # bound-name -> (dotted module, is_relative)
    local_names: frozenset[str]  # body assignment targets + this block's params
    package_name: str | None
    source_roots: tuple[Path, ...]
    third_party: frozenset[str]  # normalized manifest dependency top-names


def _python_file_import_table(text: str) -> dict[str, tuple[str, bool]]:
    """Map each imported BOUND NAME to ``(dotted module, is_relative)``.

    ``import a.b`` binds ``a``; ``import a.b as x`` binds ``x``; ``from a.b import c``
    binds ``c``; ``from .m import c`` is relative (within the SUT package). Star
    imports bind no specific name (their names stay UNKNOWN ⇒ credit). Parse
    failure ⇒ empty table (fail-open). Walks the whole tree so function-level
    imports are seen too.
    """

    table: dict[str, tuple[str, bool]] = {}
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return table
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                table[bound] = (alias.name, False)
        elif isinstance(node, ast.ImportFrom):
            is_relative = (node.level or 0) > 0
            module = node.module or ""
            for alias in node.names:
                if alias.name == "*":
                    continue
                bound = alias.asname or alias.name
                table[bound] = (module, is_relative)
    return table


def _python_function_param_names(text: str, func_name: str) -> set[str]:
    """Parameter names of the top-level ``def func_name`` (incl. fixtures) — or {}."""

    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set()
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            a = node.args
            names: set[str] = set()
            for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs):
                names.add(arg.arg)
            if a.vararg:
                names.add(a.vararg.arg)
            if a.kwarg:
                names.add(a.kwarg.arg)
            return names
    return set()


def _target_names(target: ast.AST) -> set[str]:
    """Bound names in an assignment-target node (Name / Tuple / List / Starred)."""

    names: set[str] = set()
    if isinstance(target, ast.Name):
        names.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            names |= _target_names(elt)
    elif isinstance(target, ast.Starred):
        names |= _target_names(target.value)
    return names


def _python_body_assigned_names(body_text: str) -> set[str]:
    """Names BOUND inside a test body: assignments, with-as, for-targets, walrus,
    comprehension targets. These are LOCAL observations (a local may hold a SUT
    result; by policy we do NOT taint-track, so a local always credits)."""

    names: set[str] = set()
    try:
        tree = ast.parse(textwrap.dedent(body_text))
    except (SyntaxError, ValueError):
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                names |= _target_names(t)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            names |= _target_names(node.target)
        elif isinstance(node, ast.NamedExpr):
            names |= _target_names(node.target)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            names |= _target_names(node.target)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    names |= _target_names(item.optional_vars)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            for gen in node.generators:
                names |= _target_names(gen.target)
    return names


#: Poetry MANIFEST-FORMAT reserved keys that appear under
#: ``[tool.poetry.dependencies]`` but are NOT third-party packages. Poetry stores
#: the interpreter version constraint as a pseudo-dependency named ``python``
#: (``python = "^3.11"``); it is the interpreter pin, not a distribution to
#: import. This is a poetry FILE-FORMAT fact (the analogue of registry/profile
#: DATA), NOT a target-language gate dispatch — the surrounding parser is already
#: unconditionally Python-specific. Excluding it keeps the third-party set exact;
#: membership here is byte-identical to the former inline ``k.lower() != "python"``
#: filter (Contract Kernel Cut Condition A — vb_marker_authenticity.py).
_POETRY_RESERVED_NON_DEPENDENCY_KEYS = frozenset({"python"})


def _python_manifest_top_dependencies(project_root: Path | None) -> frozenset[str]:
    """Declared third-party dependency names from ``pyproject.toml`` (PEP 621 +
    poetry), normalized for top-module matching. Empty when absent/unparseable —
    an unconfirmable third-party import then stays UNKNOWN (credit), per design."""

    if project_root is None:
        return frozenset()
    path = project_root / "pyproject.toml"
    if not path.exists():
        return frozenset()
    try:
        try:  # tomllib is stdlib from 3.11; tomli is its <3.11 backport (a declared dep)
            import tomllib  # type: ignore[import-not-found]
        except ModuleNotFoundError:  # Python < 3.11
            import tomli as tomllib  # type: ignore[import-not-found, no-redef]
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — no manifest signal ⇒ fail-open (empty set).
        return frozenset()
    reqs: list[str] = []
    project = data.get("project", {})
    if isinstance(project, dict):
        deps = project.get("dependencies", [])
        if isinstance(deps, list):
            reqs.extend(d for d in deps if isinstance(d, str))
        optional = project.get("optional-dependencies", {})
        if isinstance(optional, dict):
            for group in optional.values():
                if isinstance(group, list):
                    reqs.extend(d for d in group if isinstance(d, str))
    poetry = data.get("tool", {}).get("poetry", {}) if isinstance(data.get("tool"), dict) else {}
    if isinstance(poetry, dict):
        pdeps = poetry.get("dependencies", {})
        if isinstance(pdeps, dict):
            reqs.extend(
                k
                for k in pdeps
                if isinstance(k, str) and k.lower() not in _POETRY_RESERVED_NON_DEPENDENCY_KEYS
            )
    names: set[str] = set()
    for req in reqs:
        m = re.match(r"\s*([A-Za-z0-9._-]+)", req)
        if not m:
            continue
        dist = m.group(1).lower()
        names.add(dist)
        names.add(dist.replace("-", "_"))
    return frozenset(names)


def _module_resolves_first_party(module: str, top: str, source_roots: tuple[Path, ...]) -> bool:
    """True when ``module`` (or its top package) resolves to a source file under a
    configured source root — covers flat AND src/<package> layouts."""

    rel = module.replace(".", "/") if module else ""
    for root in source_roots:
        if rel and ((root / f"{rel}.py").exists() or (root / rel / "__init__.py").exists()):
            return True
        if top and (
            (root / f"{top}.py").exists()
            or (root / top / "__init__.py").exists()
            or (root / top).is_dir()
        ):
            return True
    return False


def _classify_py_name(name: str, ctx: _PyOriginContext) -> str:
    """Classify one assertion reference name as ``"library"`` or ``"credit"``.

    Order (GPT design): local/param shadowing ▸ first-party import ▸ stdlib ▸
    confirmed third-party ▸ builtin ▸ UNKNOWN. Only stdlib / confirmed-third-party
    / builtin are ``"library"``; everything else (incl. UNKNOWN) credits."""

    if name in ctx.local_names:
        return "credit"  # LOCAL_OR_PARAM
    entry = ctx.imports.get(name)
    if entry is not None:
        module, is_relative = entry
        if is_relative:
            return "credit"  # relative import = within the SUT package
        top = module.split(".")[0] if module else ""
        if not top:
            return "credit"
        if ctx.package_name and top == ctx.package_name:
            return "credit"  # FIRST_PARTY by package name
        if _module_resolves_first_party(module, top, ctx.source_roots):
            return "credit"  # FIRST_PARTY by source-file existence
        if top in _PY_STDLIB_NAMES:
            return "library"  # stdlib
        if top.lower() in ctx.third_party:
            return "library"  # confirmed external dependency
        return "credit"  # imported but origin unconfirmable ⇒ UNKNOWN ⇒ fail-open
    if name in _PY_BUILTIN_NAMES:
        return "library"  # builtin (only when NOT shadowed by a first-party import/local)
    return "credit"  # param / fixture / free name / star-imported ⇒ UNKNOWN ⇒ fail-open


def _build_py_origin_context(
    block: TestBlock,
    *,
    importer_text: str,
    project_root: Path | None,
    profile: Any,
) -> _PyOriginContext:
    """Assemble the per-block origin-classification context from the test file."""

    package_name = getattr(profile, "package_name", None) if profile is not None else None
    source_roots: list[Path] = []
    if project_root is not None:
        source_root = getattr(profile, "source_root", None) if profile is not None else None
        if source_root:
            source_roots.append(project_root / source_root)
        package_root = getattr(profile, "package_root", None) if profile is not None else None
        if package_root:
            source_roots.append((project_root / package_root).parent)
        source_roots.append(project_root)
    local_names = _python_body_assigned_names(block.body_text)
    if importer_text and block.label:
        local_names |= _python_function_param_names(importer_text, block.label)
    return _PyOriginContext(
        imports=_python_file_import_table(importer_text) if importer_text else {},
        local_names=frozenset(local_names),
        package_name=package_name,
        source_roots=tuple(dict.fromkeys(source_roots)),
        third_party=_python_manifest_top_dependencies(project_root),
    )


#: xUnit-style two-argument EQUALITY asserts — ``assertEqual(x, x)`` etc. are a
#: tautology (vacuously true) when both args are structurally identical, exactly
#: like ``assert x == x``. Only equality/``>=``/``<=``-family methods are listed:
#: they PASS vacuously on identical args (a false-green). ``assertNotEqual`` /
#: ``assertGreater`` / ``assertIsNot`` on identical args would FAIL the test, so
#: they are not a false-green and are deliberately excluded (and ``x != x`` is a
#: legitimate NaN idiom — see :func:`_py_assert_node_is_tautology`).
_PY_TWO_ARG_EQ_ASSERT_METHODS = frozenset(
    {
        "assertEqual",
        "assertEquals",
        "assertIs",
        "assertListEqual",
        "assertDictEqual",
        "assertSetEqual",
        "assertTupleEqual",
        "assertSequenceEqual",
        "assertMultiLineEqual",
        "assertCountEqual",
        "assertAlmostEqual",
        "assertGreaterEqual",
        "assertLessEqual",
    }
)


def _py_assert_node_is_tautology(node: ast.AST) -> bool:
    """True when a primitive-assert node compares a value to a STRUCTURALLY IDENTICAL value.

    Normalized-operand equivalence: two operands are equivalent when their AST
    dumps match (position-independent), so ``assert x == x``, ``assert f(a) ==
    f(a)``, and ``assertEqual(sorted(v), sorted(v))`` are all detected — a self-
    comparison is a tautology that holds regardless of the system under test and
    proves nothing. This is a STRENGTHENING of the existing constant-assertion
    classification (``assert x == x`` currently credits because ``x`` is a real
    name), not a loosening of any accept criterion.

    Restricted to operators/methods that pass VACUOUSLY-TRUE on identical
    operands (``==``, ``is``, ``<=``, ``>=`` and the equality-family xUnit
    asserts). A vacuously-FALSE self-comparison (``!=``, ``is not``, ``<``,
    ``>``) would FAIL the test — it is not a false-green — and ``x != x`` is the
    legitimate NaN idiom, so those are intentionally NOT flagged (anti-false-red).
    """

    for sub in ast.walk(node):
        if isinstance(sub, ast.Compare):
            operands = [sub.left, *sub.comparators]
            for index, op in enumerate(sub.ops):
                if isinstance(op, (ast.Eq, ast.Is, ast.LtE, ast.GtE)) and ast.dump(
                    operands[index]
                ) == ast.dump(operands[index + 1]):
                    return True
        elif isinstance(sub, ast.Call):
            name = _py_callee_name(sub.func)
            if name and name.split(".")[-1] in _PY_TWO_ARG_EQ_ASSERT_METHODS:
                positional = [arg for arg in sub.args if not isinstance(arg, ast.Starred)]
                if len(positional) >= 2 and ast.dump(positional[0]) == ast.dump(positional[1]):
                    return True
    return False


def _python_direct_assertion_evidence(
    body_text: str, *, origin_ctx: _PyOriginContext | None = None
) -> AssertionEvidence:
    """AST verdict: does ``body_text`` carry a NON-constant direct primitive assertion?

    For each primitive-assert node, collect every ``ast.Name`` id inside it and
    subtract :data:`_PY_DIRECT_IGNORED_NAMES`. If ANY name remains the assertion
    references a real observation (a SUT call, exception, Enum, local, or state)
    ⇒ ``direct`` (ok). If every assertion is constant-only ⇒ ``constant_direct``
    (not ok). Falls back to the lexical scanner on a syntax error so an
    un-parseable body never false-REDs here (it degrades to the line-based path,
    which itself fails OPEN per window).

    When an ``origin_ctx`` is supplied (the gate path), each remaining name is
    classified by ORIGIN (contract ``direct.library_only_reference.v1``): an
    assertion whose references are ALL positively library/builtin (``math.sqrt``,
    ``os.path``, a confirmed third-party dep, ``sorted``) and include no
    first-party / local / unknown reference proves a LIBRARY, not the SUT ⇒
    ``library_only_direct`` (not ok). Without a context, classification is skipped
    (any non-ignored name credits, exactly as before) — "cannot classify" never
    false-REDs.
    """

    try:
        tree = ast.parse(textwrap.dedent(body_text))
    except (SyntaxError, ValueError):
        return _lexical_direct_assertion_evidence(
            body_text,
            primitive_re=_PY_PRIMITIVE_ASSERT_RE,
            ignored_names=_PY_DIRECT_IGNORED_NAMES,
        )

    saw_primitive = False
    saw_library_only = False
    saw_tautology = False
    library_detail = ""
    for node in ast.walk(tree):
        if not _is_py_primitive_assert_node(node):
            continue
        saw_primitive = True
        if _py_assert_node_is_tautology(node):
            # A self-comparison (`assert x == x`) proves nothing about the SUT —
            # treat as NO observation even though it references a real name. If
            # the same test ALSO carries a genuine assertion, that node still
            # credits below (this only withholds credit from the tautology).
            saw_tautology = True
            continue
        ids = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} - _PY_DIRECT_IGNORED_NAMES
        if not ids:
            continue  # constant-only assertion (no observed name)
        if origin_ctx is None:
            # No classification context (legacy / direct unit call): any non-ignored
            # name credits, exactly as before.
            return AssertionEvidence(ok=True, reason="direct")
        if any(_classify_py_name(name, origin_ctx) != "library" for name in ids):
            # A first-party / local / unknown reference ⇒ a real observation.
            return AssertionEvidence(ok=True, reason="direct")
        # Every reference is POSITIVELY a library/builtin ⇒ a library-only proof.
        saw_library_only = True
        if not library_detail:
            library_detail = ", ".join(sorted(ids))

    if not saw_primitive:
        # The raw regex (``has_assertion``) matched but the AST recognized no
        # primitive node (e.g. ``assert`` inside a string, or an assertion shape
        # the AST walker does not classify). Fail OPEN — keep the pre-existing
        # ``direct`` credit rather than inventing a false-RED.
        return AssertionEvidence(ok=True, reason="direct")
    if saw_library_only:
        return AssertionEvidence(ok=False, reason="library_only_direct", detail=library_detail)
    if saw_tautology:
        return AssertionEvidence(ok=False, reason="tautology_direct")
    return AssertionEvidence(ok=False, reason="constant_direct")


def _primitive_assertion_windows(body: str, primitive_re: re.Pattern[str]) -> list[str]:
    """Each primitive-assertion line PLUS its more-indented continuation block.

    Mirrors the window walk in :func:`_body_references_params`: an assertion's
    "window" is the matching line plus every following line indented MORE than it
    (its block) — so a context-manager assertion (``with pytest.raises(...):\\n
    fn()``) carries the ``fn()`` call into its window, while a trailing
    same-indent statement after a constant assertion is not folded in.
    """

    lines = body.splitlines()
    windows: list[str] = []
    total = len(lines)
    for idx, line in enumerate(lines):
        if not primitive_re.search(line):
            continue
        base_indent = len(line[: len(line) - len(line.lstrip())].expandtabs())
        window = [line]
        scan = idx + 1
        while scan < total:
            nxt = lines[scan]
            if not nxt.strip():
                scan += 1
                continue
            nxt_indent = len(nxt[: len(nxt) - len(nxt.lstrip())].expandtabs())
            if nxt_indent <= base_indent:
                break
            window.append(nxt)
            scan += 1
        windows.append("\n".join(window))
    return windows


def _lexical_direct_assertion_evidence(
    body_text: str,
    *,
    primitive_re: re.Pattern[str],
    ignored_names: frozenset[str],
) -> AssertionEvidence:
    """Lexical verdict (TS, and Python-on-syntax-error): a non-constant direct assertion.

    For each primitive-assertion window, harvest GENUINE references via
    :func:`_rhs_reference_idents` (which already drops identifiers inside strings,
    comments, object keys, and member-property positions) and subtract
    ``ignored_names``. If any window references a non-ignored name ⇒ ``direct``
    (ok). A window whose references cannot be confidently scanned (``None`` —
    unterminated string/comment) is SKIPPED so it can never manufacture a
    false-RED. Only when EVERY window is constant-only ⇒ ``constant_direct``.
    """

    saw_primitive = False
    for window in _primitive_assertion_windows(body_text, primitive_re):
        saw_primitive = True
        refs = _rhs_reference_idents(window)
        if refs is None:
            return AssertionEvidence(ok=True, reason="direct")  # cannot scan ⇒ keep credit
        if refs - ignored_names:
            return AssertionEvidence(ok=True, reason="direct")
    if not saw_primitive:
        return AssertionEvidence(ok=True, reason="direct")  # no window ⇒ keep credit
    return AssertionEvidence(ok=False, reason="constant_direct")


#: npm "local" dependency protocols — a dep declared with these is a LOCAL package
#: (symlinked workspace / on-disk), NOT an external library, so it must NOT be
#: classified LIBRARY (monorepo false-RED prevention).
_TS_LOCAL_PROTOCOL_PREFIXES = ("workspace:", "file:", "link:", "portal:")
_TS_IMPORT_FROM_RE = re.compile(
    r"""^[ \t]*import\s+(?:type\s+)?(?P<clause>[^;'"]+?)\s+from\s+['"](?P<spec>[^'"]+)['"]""",
    re.MULTILINE,
)
_TS_REQUIRE_RE = re.compile(
    r"""^[ \t]*(?:const|let|var)\s+(?P<bind>[^=;]+?)\s*=\s*require\(\s*['"](?P<spec>[^'"]+)['"]\s*\)""",
    re.MULTILINE,
)
_TS_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")


@dataclass(frozen=True)
class _TsOriginContext:
    """Per-block origin context for the TS/JS library-only classifier (contract
    direct.library_only_reference.v1, cross-language). Mirrors :class:`_PyOriginContext`
    but lexical. ``external_deps`` is the set of package names POSITIVELY confirmed as
    external (in package.json deps/devDeps, EXCLUDING local-protocol + workspace-local
    packages) — the ONLY names that can be LIBRARY. Everything else credits (fail-open)."""

    imports: dict[str, str]  # bound name -> module specifier
    local_names: frozenset[str]  # assertion-body local bindings + params
    external_deps: frozenset[str]  # POSITIVELY-external package names


def _ts_package_name(spec: str) -> str:
    """npm package name of a bare specifier: ``@scope/pkg/sub`` -> ``@scope/pkg``;
    ``lodash/fp`` -> ``lodash``. Relative / absolute specifiers return ``""``."""

    s = spec.strip()
    if not s or s[0] in "./":
        return ""
    parts = [p for p in s.split("/") if p]
    if s.startswith("@"):
        return "/".join(parts[:2]) if len(parts) >= 2 else ""
    return parts[0] if parts else ""


def _ts_bind_clause(clause: str, spec: str, table: dict[str, str]) -> None:
    """Record the bound names of an ``import <clause> from '<spec>'`` clause."""

    named = ""
    if "{" in clause and "}" in clause:
        named = clause[clause.index("{") + 1 : clause.rindex("}")]
        clause = clause[: clause.index("{")]
    for piece in named.split(","):
        piece = piece.strip()
        if not piece:
            continue
        # ``a`` | ``a as b`` | ``type a`` | ``type a as b`` -> bound name is the alias
        toks = [t for t in piece.replace(" as ", " ").split() if t and t != "type"]
        if toks:
            m = _TS_IDENT_RE.fullmatch(toks[-1])
            if m:
                table[toks[-1]] = spec
    # default + namespace (``X`` / ``* as ns`` / ``X, * as ns``)
    for part in clause.split(","):
        part = part.strip()
        if not part:
            continue
        if part.startswith("*"):
            after = part[1:].strip()
            if after.startswith("as "):
                ns = after[3:].strip()
                if _TS_IDENT_RE.fullmatch(ns):
                    table[ns] = spec
        elif _TS_IDENT_RE.fullmatch(part):
            table[part] = spec


def _ts_import_table(text: str) -> dict[str, str]:
    """Map each imported bound name to its module specifier (lexical, top-of-line
    import/require only). Strings/comments mid-line do not match (line-anchored)."""

    table: dict[str, str] = {}
    for m in _TS_IMPORT_FROM_RE.finditer(text):
        _ts_bind_clause(m.group("clause").strip(), m.group("spec"), table)
    for m in _TS_REQUIRE_RE.finditer(text):
        bind = m.group("bind").strip().strip("{}[]")
        for piece in bind.split(","):
            name = piece.split(":")[0].strip()
            if _TS_IDENT_RE.fullmatch(name):
                table[name] = m.group("spec")
    return table


def _ts_local_bindings(body_text: str) -> set[str]:
    """Lexically harvest local binding names in a test body: ``const/let/var`` (incl.
    object/array destructuring), ``for (const x of …)``, ``catch (e)``. Approximate —
    anything missed falls through to UNKNOWN credit (no false-RED)."""

    names: set[str] = set()
    decl_re = re.compile(r"\b(?:const|let|var)\s+(?P<bind>[^=;\n]+?)\s*(?:=|of\b|in\b)")
    for m in decl_re.finditer(body_text):
        for ident in _TS_IDENT_RE.findall(m.group("bind")):
            names.add(ident)
    for m in re.finditer(r"\bcatch\s*\(\s*([A-Za-z_$][\w$]*)", body_text):
        names.add(m.group(1))
    return names


def _ts_manifest_external_deps(project_root: Path | None, profile: Any) -> frozenset[str]:
    """Package names POSITIVELY external: in package.json deps/devDeps, EXCLUDING
    local-protocol (workspace:/file:/link:) deps and workspace-local package names
    (from ``profile.workspace_manifest_globs``). Absent/unparseable ⇒ empty (fail-open)."""

    if project_root is None:
        return frozenset()
    import json

    def _read_json(path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    pkg = _read_json(project_root / "package.json")
    if not pkg:
        return frozenset()

    # Local workspace package NAMES (declared in matched workspace manifests) are
    # first-party, never library — even when consumed by a normal version range.
    local_names: set[str] = set()
    globs = getattr(profile, "workspace_manifest_globs", ()) or () if profile is not None else ()
    for pattern in globs:
        for manifest in project_root.glob(pattern):
            name = _read_json(manifest).get("name")
            if isinstance(name, str) and name.strip():
                local_names.add(name.strip())

    external: set[str] = set()
    for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        deps = pkg.get(key)
        if not isinstance(deps, dict):
            continue
        for name, version in deps.items():
            if not isinstance(name, str) or not name.strip():
                continue
            if isinstance(version, str) and version.strip().startswith(_TS_LOCAL_PROTOCOL_PREFIXES):
                continue  # local protocol -> not external
            if name in local_names:
                continue  # workspace-local package -> not external
            external.add(name)
    return frozenset(external)


def _build_ts_origin_context(
    block: TestBlock, *, importer_text: str, project_root: Path | None, profile: Any
) -> _TsOriginContext:
    return _TsOriginContext(
        imports=_ts_import_table(importer_text) if importer_text else {},
        local_names=frozenset(_ts_local_bindings(block.body_text)),
        external_deps=_ts_manifest_external_deps(project_root, profile),
    )


def _classify_ts_name(name: str, ctx: _TsOriginContext) -> str:
    """``"library"`` or ``"credit"``. Order (GPT design): local/shadow ▸ relative
    (first-party) ▸ confirmed-external dep ▸ UNKNOWN. A specifier not POSITIVELY
    external (alias / not-in-manifest / workspace) credits (fail-open)."""

    if name in ctx.local_names:
        return "credit"  # local binding / shadow — checked BEFORE imports
    spec = ctx.imports.get(name)
    if spec is None:
        return "credit"  # not an import we tracked -> UNKNOWN
    if spec.strip()[:1] == "." :
        return "credit"  # relative import = first-party
    pkg = _ts_package_name(spec)
    if pkg and pkg in ctx.external_deps:
        return "library"  # POSITIVELY-confirmed external dependency
    return "credit"  # bare-but-unconfirmed (alias / workspace / not-in-manifest) -> fail-open


def _typescript_direct_assertion_evidence(
    body_text: str, *, origin_ctx: _TsOriginContext | None = None
) -> AssertionEvidence:
    """TS/JS direct-assertion verdict — lexical, reusing the shared window scanner.

    Without an ``origin_ctx`` (legacy / no gate context): any non-ignored reference
    credits. With a context (the gate path): a window whose references are ALL
    positively LIBRARY (a confirmed-external dependency) ⇒ ``library_only_direct``;
    a first-party / local / unknown reference credits (contract
    direct.library_only_reference.v1). ``Math``/``JSON``/builtin globals stay handled
    by the ignored set (constant_direct), so this only adds the imported-library case.
    """

    if origin_ctx is None:
        return _lexical_direct_assertion_evidence(
            body_text,
            primitive_re=_TS_PRIMITIVE_ASSERT_RE,
            ignored_names=_TS_DIRECT_IGNORED_NAMES,
        )

    saw_primitive = False
    saw_library_only = False
    library_detail = ""
    for window in _primitive_assertion_windows(body_text, _TS_PRIMITIVE_ASSERT_RE):
        saw_primitive = True
        refs = _rhs_reference_idents(window)
        if refs is None:
            return AssertionEvidence(ok=True, reason="direct")  # cannot scan ⇒ credit
        ids = refs - _TS_DIRECT_IGNORED_NAMES
        if not ids:
            continue  # constant-only window
        if any(_classify_ts_name(name, origin_ctx) != "library" for name in ids):
            return AssertionEvidence(ok=True, reason="direct")
        saw_library_only = True
        if not library_detail:
            library_detail = ", ".join(sorted(ids))

    if not saw_primitive:
        return AssertionEvidence(ok=True, reason="direct")
    if saw_library_only:
        return AssertionEvidence(ok=False, reason="library_only_direct", detail=library_detail)
    return AssertionEvidence(ok=False, reason="constant_direct")


# ---------------------------------------------------------------------------
# Shared helper-resolution machinery (language-agnostic skeleton; the per-
# profile adapters supply the regexes/extensions for their stack).
# ---------------------------------------------------------------------------


#: Names that mark a call as an ASSERTION-LIKE helper worth resolving. The set is
#: used ONLY to choose WHICH calls to resolve (never to pass on name alone). It is
#: a prefix match on the call's leading identifier segment so ``expectSuccessfulRun``,
#: ``assert_rejected``, ``requireSuccess``, ``verifyState``, ``ensureOk``,
#: ``shouldMatch``, ``checkInvariant`` are all candidates.
_ASSERTION_HELPER_NAME_RE = re.compile(
    r"^(?:expect|assert|should|verify|ensure|require|check)", re.IGNORECASE
)


def _looks_like_assertion_helper(name: str) -> bool:
    # Strip a leading underscore so a private helper (``_assert_error``) is a
    # candidate. Candidacy only chooses WHICH calls to resolve; the helper still
    # must resolve to a def whose body has a primitive assertion anchored on a
    # parameter, so this never credits a non-asserting helper.
    return bool(_ASSERTION_HELPER_NAME_RE.match((name or "").lstrip("_")))


def _shared_marker_lines(text: str) -> set[int]:
    """1-based line numbers occupied by a ``codd:`` marker comment (any subject).

    Used so a marker line is never mistaken for a block boundary or counted as a
    test body line.
    """

    out: set[int] = set()
    for idx, line in enumerate(text.splitlines(), start=1):
        if "codd:" in line:
            out.add(idx)
    return out


# ---------------------------------------------------------------------------
# Built-in per-profile adapters (python, typescript/javascript). A new stack
# adds its adapter here and wires it in LayoutProfile.test_block_profile().
# ---------------------------------------------------------------------------


_PY_TEST_DEF_RE = re.compile(r"^(?P<indent>[ \t]*)def\s+(?P<name>test_[A-Za-z0-9_]*)\s*\(")
_PY_METHOD_TEST_RE = re.compile(r"^(?P<indent>[ \t]*)def\s+(?P<name>test_[A-Za-z0-9_]*)\s*\(self")
# A skip applied via decorator on the lines directly above the def.
_PY_SKIP_DECORATOR_RE = re.compile(
    r"@(?:pytest\.mark\.skip|pytest\.mark\.skipif|unittest\.skip|skip)\b", re.IGNORECASE
)
#: PRIMITIVE python assertion: a language built-in that FAILS on its own — an
#: ``assert`` statement, ``pytest.raises``/``pytest.fail``, a ``unittest``
#: ``self.assert*`` / ``self.fail`` call, or ``np.testing.assert*``. A bare call
#: to a named helper (``assert_that(...)``, ``verify(...)``) is NOT primitive — it
#: is resolved via the evidence graph so a no-op helper cannot pass on its name.
#: unittest assertion can be called on a TestCase reference that is NOT ``self`` — a
#: shared assert helper takes the TestCase as a parameter and calls ``testcase.assertX``.
#: A NARROW receiver allowlist (not any ``X.assertY``) keeps a namespace helper
#: (``helpers.assert_token_values(...)``) from being mis-read as a primitive.
_PY_TESTCASE_RECEIVER_PATTERN = r"(?:self|cls|testcase|test_case|tc|case)"
#: A ``<testcase-receiver>.assert<CamelCase>(`` call — the unittest assert-method form.
#: CamelCase (``assert[A-Z]``) on purpose: a snake helper name (``assert_token_values``)
#: is NOT a unittest assert method, so it stays helper-resolution-only.
_PY_TESTCASE_ASSERT_CALL_RE = re.compile(
    rf"\b(?P<recv>{_PY_TESTCASE_RECEIVER_PATTERN})\.assert[A-Z][A-Za-z0-9_]*\s*\("
)
_PY_PRIMITIVE_ASSERT_RE = re.compile(
    r"(^|[^A-Za-z0-9_])(assert\b|pytest\.(?:raises|warns)\b|pytest\.fail\s*\(|"
    rf"{_PY_TESTCASE_RECEIVER_PATTERN}\.assert[A-Z][A-Za-z0-9_]*\s*\(|self\.fail\s*\(|np\.testing\.assert)"
    # ``raise AssertionError`` (the explicit form of ``assert``) — LINE-ANCHORED so a
    # ``raise AssertionError`` inside a string/comment does not match (the AST-first
    # ``_python_body_has_primitive_assertion`` is the primary guard; this is fallback).
    r"|^[ \t]*raise\s+AssertionError\b",
    re.MULTILINE,
)


def _testcase_assert_receivers_in(text: str) -> set[str]:
    """Receiver names used as ``<recv>.assert<CamelCase>(`` in ``text`` (e.g. ``testcase``).

    These are the assertion API ``器`` (the TestCase), NOT observed values — so they must
    be EXCLUDED from the argument-anchor (otherwise ``testcase.assertEqual(1, 1)`` would
    anchor via ``testcase`` and a constant-only helper would falsely pass). A no-op for
    TS bodies (no ``self|tc|...\\.assertX`` patterns), so it is safe in the shared anchor."""

    return {m.group("recv") for m in _PY_TESTCASE_ASSERT_CALL_RE.finditer(text)}
#: A python ``def helper(...):`` header (captures name + the parameter list so the
#: argument-anchor check can confirm the helper references its own arguments).
_PY_DEF_RE = re.compile(
    r"^(?P<indent>[ \t]*)def\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\((?P<params>[^)]*)\)"
)


def _python_decorator_start_lines(text: str) -> dict[int, int]:
    """Map a ``test_*`` def's 1-based line → its FIRST decorator's 1-based line.

    Uses Python's AST so a marker placed immediately above a MULTI-LINE decorator
    (e.g. a wrapped ``@pytest.mark.parametrize(...)``) still attaches to the test:
    the block's ``start_line`` becomes the decorator block's first line, so the
    marker sits directly above the block. An arbitrary non-decorator statement
    between the marker and the decorator still breaks attachment (the marker would
    no longer be adjacent to ``start_line``). Falls back to ``{}`` on a parse error
    so callers keep the def-line behavior.
    """

    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return {}
    starts: dict[int, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not str(node.name).startswith("test_"):
            continue
        decorators = getattr(node, "decorator_list", ()) or ()
        if decorators:
            starts[int(node.lineno)] = min(int(d.lineno) for d in decorators)
    return starts


def _python_test_function_nodes(
    text: str,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Top-level ``test_*`` functions (module- or class-level) via AST.

    AST is signature-layout-agnostic, so a multi-line ``def test_x(\n  a,\n) -> None:``
    yields the CORRECT body extent — unlike the legacy line-scanner, which read the
    closing ``)`` line (at the def's own indent) as the body dedent and collapsed
    the body to the parameter-annotation lines (the ``no_assertion`` FALSE-RED this
    fixes). A ``test_*`` nested INSIDE another function is excluded (not a
    pytest-collected unit). Returns [] on syntax error (caller falls back to the
    line-scanner).
    """

    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    nodes: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not str(node.name).startswith("test_"):
            continue
        if not isinstance(parents.get(node), (ast.Module, ast.ClassDef)):
            continue
        nodes.append(node)
    return sorted(nodes, key=lambda n: int(n.lineno))


@dataclass(frozen=True)
class PythonTestBlockProfile:
    """pytest / unittest structural adapter.

    A test block is a ``def test_*`` function; its body runs until the next line
    at an indent <= the def's indent (standard Python block scoping). Skip is a
    ``@pytest.mark.skip``/``skipif`` / ``unittest.skip`` decorator on the lines
    above the def, or a ``pytest.skip(...)`` call in the body. A PRIMITIVE
    assertion is an ``assert`` statement, ``pytest.raises``/``pytest.fail``, a
    ``unittest`` ``self.assert*`` / ``self.fail`` call, or ``np.testing.assert``.
    A bare call to a named assertion helper is resolved one hop via
    :meth:`resolve_assertion_evidence`.
    """

    def handles_file(self, rel_path: str) -> bool:
        return rel_path.endswith(".py")

    def parse_test_blocks(self, text: str) -> list[TestBlock]:
        # AST is the authoritative body-extent source. The legacy line-scanner
        # (kept as a fallback for syntactically-invalid files) mis-reads a
        # MULTI-LINE def signature: the closing ``) -> None:`` sits at the def's
        # own indent, so it was taken as the body dedent and the body collapsed to
        # the parameter-annotation lines (a ``no_assertion`` FALSE-RED on a test
        # that genuinely asserts). AST yields the true ``body[0]..end_lineno``
        # extent regardless of signature layout, return annotation, decorators, or
        # nested defs. has_assertion stays the raw primitive check (unchanged).
        try:
            ast.parse(text)
        except (SyntaxError, ValueError):
            return self._parse_test_blocks_linescan(text)
        lines = text.splitlines()
        blocks: list[TestBlock] = []
        for node in _python_test_function_nodes(text):
            def_line = int(node.lineno)
            start_line = min(
                [int(d.lineno) for d in node.decorator_list], default=def_line
            )
            body_start = int(node.body[0].lineno) if node.body else def_line
            body_end = int(getattr(node, "end_lineno", body_start) or body_start)
            body_text = "\n".join(lines[body_start - 1 : body_end])
            skipped = bool(re.search(r"\bpytest\.skip\s*\(", body_text)) or bool(
                re.search(r"\bself\.skipTest\s*\(", body_text)
            )
            decorator_text = "\n".join(lines[start_line - 1 : def_line - 1])
            if _PY_SKIP_DECORATOR_RE.search(decorator_text):
                skipped = True
            has_assertion = _python_body_has_primitive_assertion(body_text)
            blocks.append(
                TestBlock(
                    start_line=start_line,
                    end_line=body_end,
                    is_executable=not skipped,
                    has_assertion=has_assertion,
                    label=node.name,
                    body_text=body_text,
                )
            )
        return blocks

    def _parse_test_blocks_linescan(self, text: str) -> list[TestBlock]:
        lines = text.splitlines()
        marker_lines = _shared_marker_lines(text)
        decorator_starts = _python_decorator_start_lines(text)
        blocks: list[TestBlock] = []
        index = 0
        total = len(lines)
        while index < total:
            match = _PY_TEST_DEF_RE.match(lines[index])
            if not match:
                index += 1
                continue
            def_line = index + 1  # 1-based
            # Attach point: the FIRST decorator line (so a marker above a
            # multi-line decorator still attaches to this test); the def line when
            # undecorated / unparseable.
            start_line = decorator_starts.get(def_line, def_line)
            indent = len(match.group("indent").expandtabs())
            name = match.group("name")
            # Body: from the line after the def until dedent to <= indent. A
            # ``codd:`` marker line at the def's own indent is NOT treated as the
            # dedent boundary while scanning (so a leading marker for the NEXT
            # test does not prematurely close this body) — but it IS trimmed from
            # the body EXTENT afterwards, so this block does not "contain" the
            # next test's leading marker (which must attach to the next test).
            body_start = def_line + 1
            body_end = def_line
            scan = index + 1
            while scan < total:
                raw = lines[scan]
                stripped = raw.strip()
                if stripped and (scan + 1) not in marker_lines:
                    cur_indent = len(raw[: len(raw) - len(raw.lstrip())].expandtabs())
                    if cur_indent <= indent:
                        break
                body_end = scan + 1
                scan += 1
            # Trim trailing blank / comment / marker lines from the body extent so
            # ``end_line`` reflects the last real CODE line of this test.
            while body_end > def_line:
                tail = lines[body_end - 1].strip()
                if tail and not _is_comment_or_marker_line(tail):
                    break
                body_end -= 1
            body_text = "\n".join(lines[def_line:body_end])  # def's body region
            # Skip via the FULL decorator block above the def (start_line..def),
            # which captures a multi-line ``@pytest.mark.skipif(...)`` the old
            # line-by-line upward scan could miss; plus an in-body skip call.
            skipped = bool(re.search(r"\bpytest\.skip\s*\(", body_text)) or bool(
                re.search(r"\bself\.skipTest\s*\(", body_text)
            )
            decorator_text = "\n".join(lines[start_line - 1 : index])
            if _PY_SKIP_DECORATOR_RE.search(decorator_text):
                skipped = True
            has_assertion = _python_body_has_primitive_assertion(body_text)
            blocks.append(
                TestBlock(
                    start_line=start_line,
                    end_line=max(body_end, def_line),
                    is_executable=not skipped,
                    has_assertion=has_assertion,
                    label=name,
                    body_text=body_text,
                )
            )
            index = scan
        return blocks

    def resolve_assertion_evidence(
        self, block: TestBlock, *, importer_text: str, importer_rel: str, project_root: Path
    ) -> AssertionEvidence:
        return _resolve_python_evidence(
            block, importer_text=importer_text, importer_rel=importer_rel, project_root=project_root
        )

    def resolve_direct_assertion_evidence(
        self,
        block: TestBlock,
        *,
        importer_text: str = "",
        importer_rel: str = "",
        project_root: Path | None = None,
        config: dict[str, Any] | None = None,
        profile: Any = None,
    ) -> AssertionEvidence:
        """Whether the block's DIRECT primitive assertion references a real name.

        AST-based: ``constant_direct`` when the assertion is constant-only, and —
        when the gate supplies the test-file context (``importer_text`` + ``profile``
        / ``project_root``) — ``library_only_direct`` when every reference is a
        positively-classified library/builtin (contract
        ``direct.library_only_reference.v1``). The context kwargs are OPTIONAL so a
        bare ``resolve_direct_assertion_evidence(block)`` (older callers / unit
        tests) still works and simply skips origin classification (fail-open).
        Called by the gate only when ``block.has_assertion`` is True.
        """

        origin_ctx = (
            _build_py_origin_context(
                block, importer_text=importer_text, project_root=project_root, profile=profile
            )
            if (importer_text or project_root is not None or profile is not None)
            else None
        )
        return _python_direct_assertion_evidence(block.body_text, origin_ctx=origin_ctx)


# vitest/jest: it(...) / test(...) blocks; describe(...) groups. Skip via
# ``.skip`` / ``.todo`` / ``xit`` / ``xtest`` / ``it.skipIf`` and a
# ``describe.skip`` wrapping. PRIMITIVE assertion = ``expect(`` / vitest
# ``assert``/``assert.*`` / ``vi.expect`` / a throw inside a fail helper.
_TS_TEST_OPEN_RE = re.compile(
    r"^(?P<indent>[ \t]*)"
    r"(?P<x>x)?"
    r"(?P<fn>it|test|describe)"
    r"(?P<mods>(?:\.(?:skip|only|todo|concurrent|sequential|each|skipIf|runIf|failing))*)"
    r"\s*(?:\.each\s*\([^)]*\)\s*)?\(",
)
#: PRIMITIVE TS/JS assertion: a vitest/jest/chai BUILT-IN that fails on its own —
#: ``expect(...)`` (incl. ``vi.expect``), chai ``assert``/``assert.*``, a Node
#: ``assert(...)`` call, or a ``throw`` (the body of a throw-style fail helper).
#: A bare call to a named helper (``expectSuccessfulRun(...)``) is NOT primitive —
#: it is resolved one hop so a no-op helper cannot pass on its name.
_TS_PRIMITIVE_ASSERT_RE = re.compile(
    r"(^|[^A-Za-z0-9_$.])"
    r"(?:(?:vi\.)?expect\s*\(|assert\s*\(|assert\.[A-Za-z]+\s*\(|throw\b)"
)
#: A TS/JS function/method DEFINITION NAME anchor. The parameter list (which may
#: span several lines) and body are parsed by paren/brace matching from the
#: ``(`` that follows — so multi-line signatures (a wrapped
#: ``export function f(\n  a,\n  b\n): void {``) are handled, which the e2e
#: ``expectRejectedRun(...)`` helper relies on. Covers ``function f(``,
#: ``const f = (`` (arrow), ``const f = function (`` and method ``f(`` forms.
_TS_FUNC_NAME_RES = (
    re.compile(r"\bfunction\s+(?P<name>[A-Za-z_$][\w$]*)\s*\("),
    re.compile(r"\b(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\("),
    re.compile(
        r"\b(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?function\s*\("
    ),
)


@dataclass(frozen=True)
class TypeScriptTestBlockProfile:
    """vitest / jest structural adapter (TS + JS + JSX/TSX).

    Brace-matched: each ``it``/``test``/``describe`` block runs from its opening
    line to the matching close brace. Skip is ``.skip``/``.todo`` (or ``xit`` /
    ``xtest`` / ``xdescribe``) on the block, OR a skip on an enclosing
    ``describe``. A PRIMITIVE assertion = ``expect(`` (vitest/jest, incl.
    ``vi.expect``), chai/Node ``assert`` / ``assert.*``, or a ``throw``. A bare
    call to a named assertion helper is resolved one hop via
    :meth:`resolve_assertion_evidence`. Only LEAF ``it``/``test`` blocks (and a
    bare ``test`` with no nested cases) are returned as coverage targets — a
    ``describe`` is a grouping container whose skip/non-skip is inherited by its
    children.
    """

    def handles_file(self, rel_path: str) -> bool:
        return rel_path.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"))

    def parse_test_blocks(self, text: str) -> list[TestBlock]:
        lines = text.splitlines()
        marker_lines = _shared_marker_lines(text)
        # First, find every it/test/describe opener with its brace-matched span.
        spans: list[dict[str, Any]] = []
        for idx, line in enumerate(lines):
            if (idx + 1) in marker_lines:
                continue
            match = _TS_TEST_OPEN_RE.match(line)
            if not match:
                continue
            fn = match.group("fn")
            mods = match.group("mods") or ""
            skipped_self = bool(match.group("x")) or ".skip" in mods or ".todo" in mods
            start_line = idx + 1
            end_line = self._matching_close(lines, idx)
            spans.append(
                {
                    "fn": fn,
                    "skipped_self": skipped_self,
                    "start": start_line,
                    "end": end_line,
                }
            )
        if not spans:
            return []

        def _is_skipped(span: dict[str, Any]) -> bool:
            if span["skipped_self"]:
                return True
            # Inherit skip from any enclosing describe span.
            for other in spans:
                if other is span:
                    continue
                if (
                    other["fn"] == "describe"
                    and other["skipped_self"]
                    and other["start"] <= span["start"]
                    and span["end"] <= other["end"]
                ):
                    return True
            return False

        # Emit ALL it/test/describe spans as blocks. Leaf it/test blocks are the
        # coverage TARGETS; a describe (group) block is kept too — not as a target
        # but as an attachment WAYPOINT, so a marker sitting on (or just above) a
        # ``describe`` resolves to that group's first nested test (the gate's
        # ``_attached_block`` redirects a group to its first child; a group is
        # never itself a credible coverage target). This is what lets a marker
        # written above a ``describe`` attach to its first ``it``.
        blocks: list[TestBlock] = []
        for span in spans:
            body = "\n".join(lines[span["start"] - 1 : span["end"]])
            blocks.append(
                TestBlock(
                    start_line=span["start"],
                    end_line=span["end"],
                    is_executable=not _is_skipped(span),
                    has_assertion=bool(_TS_PRIMITIVE_ASSERT_RE.search(body)),
                    label=span["fn"],
                    body_text=body,
                )
            )
        return blocks

    def resolve_assertion_evidence(
        self, block: TestBlock, *, importer_text: str, importer_rel: str, project_root: Path
    ) -> AssertionEvidence:
        return _resolve_typescript_evidence(
            block, importer_text=importer_text, importer_rel=importer_rel, project_root=project_root
        )

    def resolve_direct_assertion_evidence(
        self,
        block: TestBlock,
        *,
        importer_text: str = "",
        importer_rel: str = "",
        project_root: Path | None = None,
        config: dict[str, Any] | None = None,
        profile: Any = None,
    ) -> AssertionEvidence:
        """Whether the block's DIRECT primitive assertion references a real name.

        Lexical: ``constant_direct`` when constant-only, and — when the gate supplies
        the test-file context — ``library_only_direct`` when every reference is a
        positively-confirmed external dependency (contract
        direct.library_only_reference.v1). Context kwargs are OPTIONAL so a bare
        ``resolve_direct_assertion_evidence(block)`` still works (skips classification,
        fail-open). Called by the gate only when ``block.has_assertion`` is True.
        """

        origin_ctx = (
            _build_ts_origin_context(
                block, importer_text=importer_text, project_root=project_root, profile=profile
            )
            if (importer_text or project_root is not None or profile is not None)
            else None
        )
        return _typescript_direct_assertion_evidence(block.body_text, origin_ctx=origin_ctx)

    @staticmethod
    def _matching_close(lines: list[str], open_index: int) -> int:
        """1-based line of the brace that closes the block opened at ``open_index``.

        A simple brace counter that ignores braces inside line/block string
        literals and ``//`` comments well enough for generated test code. Falls
        back to end-of-file when unbalanced (best-effort, never raises).
        """

        depth = 0
        started = False
        total = len(lines)
        idx = open_index
        while idx < total:
            depth += _net_braces(lines[idx])
            if "{" in lines[idx]:
                started = True
            if started and depth <= 0:
                return idx + 1
            idx += 1
        return total


def _net_braces(line: str) -> int:
    """``{`` minus ``}`` outside strings/line-comments (coarse but stable)."""

    depth = 0
    in_str: str | None = None
    prev = ""
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if in_str is not None:
            if ch == in_str and prev != "\\":
                in_str = None
        elif ch in ("'", '"', "`"):
            in_str = ch
        elif ch == "/" and i + 1 < n and line[i + 1] == "/":
            break  # rest of line is a comment
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        prev = ch
        i += 1
    return depth


# ---------------------------------------------------------------------------
# Go (``go test`` / ``testing`` + optional testify) structural adapter.
#
# Mirrors the Python/TS adapters EXACTLY (same ``TestBlockProfile`` interface,
# same ``has_assertion`` / ``resolve_direct_assertion_evidence`` /
# ``resolve_assertion_evidence`` contract). The design contract is
# ``dogfood/gpt_language_generality_design.md`` §1.7 (test_semantics adapter):
#
#   * test blocks: ``func TestXxx(t *testing.T) { ... }`` (brace-matched bodies),
#     plus ``t.Run("name", func(t *testing.T){...})`` SUBTESTS as nested blocks
#     (the same group→leaf nesting the TS adapter emits for ``describe``→``it``).
#   * skip: ``t.Skip()`` / ``t.Skipf(...)`` / ``t.SkipNow()`` on the testing
#     receiver (the ``*testing.T`` parameter), mirroring TS ``.skip`` / py
#     ``pytest.skip``.
#   * PRIMITIVE assertion (``has_assertion``): a stdlib testing FAILURE call on
#     the receiver (``t.Error/Errorf/Fatal/Fatalf/Fail/FailNow``) OR a testify
#     ``assert.X(...)`` / ``require.X(...)`` call. A bare call to a NAMED helper
#     (``checkServer(t, got)``) is NOT primitive — it is resolved one hop via
#     :meth:`resolve_assertion_evidence`, exactly like the Python/TS helper graph.
#
# The assertion-AUTHENTICITY rule (the anti-false-green core), mirroring the
# Python/TS ``constant_direct`` analogue:
#   (a) a FAILURE call is REAL only when it is REACHED VIA A CONDITION that
#       references a NON-constant (a variable / SUT-call result / expected var) —
#       ``if got != want { t.Fatalf(...) }`` is real (``got``/``want``);
#       ``if 1 != 1 { t.Fatal() }`` (constant condition) and an UNCONDITIONAL
#       ``t.Fatal("todo")`` with no SUT/expected reference are NOT real.
#   (b) a testify call is REAL only when its VALUE args (everything after the
#       leading ``t`` testing-receiver arg, minus a trailing string/format msg)
#       reference a NON-constant. ``assert.Equal(t, 1, 1)`` is NOT real.
# When origin is UNKNOWN/uncertain we CREDIT (the same false-RED防波堤 the
# Python/TS adapters use): only a provably constant-only / no-assertion body is
# rejected.
# ---------------------------------------------------------------------------


#: ``func TestXxx(t *testing.T) {`` — a top-level Go test function. The receiver
#: parameter NAME is captured (``t`` by convention, but any identifier is legal —
#: ``func TestX(tt *testing.T)``) so skip / failure-call / subtest detection
#: anchors on the ACTUAL receiver, never a hardcoded ``t``. ``*testing.T`` is
#: required (a ``TestMain(m *testing.M)`` or a benchmark ``*testing.B`` is not a
#: ``go test`` unit and is intentionally not matched).
_GO_TEST_FUNC_RE = re.compile(
    r"^(?P<indent>[ \t]*)func\s+(?P<name>Test[A-Za-z0-9_]*)\s*\(\s*"
    r"(?P<recv>[A-Za-z_][A-Za-z0-9_]*)\s+\*testing\.T\s*\)\s*\{",
    re.MULTILINE,
)
#: ``<recv>.Run("name", func(<sub> *testing.T) {`` — a subtest. The subtest's OWN
#: receiver name is captured (it may shadow / rename the parent's, e.g.
#: ``t.Run("x", func(t *testing.T){...})`` or ``func(st *testing.T)``) so the
#: subtest body's skip / failure calls anchor on ITS receiver.
_GO_SUBTEST_RE = re.compile(
    r"(?P<recv>[A-Za-z_][A-Za-z0-9_]*)\.Run\s*\([^,]*,\s*func\s*\(\s*"
    r"(?P<sub>[A-Za-z_][A-Za-z0-9_]*)\s+\*testing\.T\s*\)\s*\{",
)
#: Stdlib ``testing`` FAILURE methods (a call that, on its own, can fail the test).
_GO_FAIL_METHODS = ("Error", "Errorf", "Fatal", "Fatalf", "Fail", "FailNow")
#: Stdlib ``testing`` SKIP methods.
_GO_SKIP_METHODS = ("Skip", "Skipf", "SkipNow")
#: testify packages whose ``assert.X`` / ``require.X`` calls are primitive
#: assertions. The package ALIAS (the import's bound name) is resolved per-file so
#: ``import tassert "github.com/stretchr/testify/assert"`` (``tassert.Equal``) is
#: still recognized; these defaults cover the conventional unaliased imports.
_GO_TESTIFY_RECEIVERS = ("assert", "require")


def _go_receiver_call_re(receiver: str, methods: Iterable[str]) -> re.Pattern[str]:
    """``<receiver>.<Method>(`` for any ``Method`` in ``methods`` (word-bounded)."""

    alt = "|".join(re.escape(m) for m in methods)
    return re.compile(rf"(?<![A-Za-z0-9_.]){re.escape(receiver)}\.(?:{alt})\b\s*\(")


def _go_strip_comments(text: str) -> str:
    """``text`` with ``//`` line and ``/* */`` block comments blanked to spaces.

    Newlines are PRESERVED (line/offset math is unaffected) and only comment bytes
    become spaces — string literals and code are untouched. This is the STRUCTURAL
    SKELETON the assertion/skip scanners run over so a fake assertion written in a
    COMMENT (``// if got != want { t.Fatalf(...) }``) is never mistaken for a real
    one (the false-GREEN this closes). String literals are intentionally left
    intact so testify message-arg trimming still sees real strings; identifiers
    inside string literals are independently dropped by :func:`_go_reference_idents`
    at the reference-extraction step, so a call-shaped string is still safe.
    """

    out: list[str] = []
    i = 0
    n = len(text)
    in_str: str | None = None
    prev = ""
    while i < n:
        ch = text[i]
        if in_str is not None:
            out.append(ch)
            if ch == in_str and prev != "\\":
                in_str = None
            prev = ch
            i += 1
            continue
        if ch in ("'", '"', "`"):
            in_str = ch
            out.append(ch)
            prev = ch
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
            prev = ""
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            # Preserve newlines inside the block comment for line math.
            out.append("".join(c if c == "\n" else " " for c in text[i:end]))
            i = end
            prev = ""
            continue
        out.append(ch)
        prev = ch
        i += 1
    return "".join(out)


def _go_testify_aliases(text: str) -> dict[str, str]:
    """Map each bound testify package alias → its package leaf (``assert``/``require``).

    Handles ``import "…/testify/assert"`` (binds ``assert``), an explicit alias
    ``import tassert "…/testify/assert"`` (binds ``tassert``), and grouped
    ``import ( … )`` blocks. A package not under ``testify/{assert,require}`` is
    ignored. Absent/none ⇒ the conventional unaliased ``assert``/``require`` names
    are still recognized by the caller (these only ADD aliases, never remove).
    """

    aliases: dict[str, str] = {}
    # Single or grouped import lines: optional alias then a quoted path.
    import_line = re.compile(
        r'^[ \t]*(?:import[ \t]+)?(?:(?P<alias>[A-Za-z_][A-Za-z0-9_]*)[ \t]+)?'
        r'"(?P<path>[^"]*testify/(?P<leaf>assert|require))"',
        re.MULTILINE,
    )
    for m in import_line.finditer(text):
        leaf = m.group("leaf")
        alias = m.group("alias") or leaf
        aliases[alias] = leaf
    return aliases


def _go_testify_call_res(text: str) -> list[tuple[str, re.Pattern[str]]]:
    """``(alias, regex)`` for every testify package call form active in ``text``.

    The regex matches ``<alias>.<Func>(`` for an UPPER-CamelCase exported function
    (``Equal`` / ``NoError`` / ``True`` / ``Contains`` / …) — the testify public
    surface — without enumerating every function name (so ``assert.JSONEq`` etc.
    are covered). Constant-only-ness is decided later from the ARG text, not the
    function name, so a permissive function match never false-GREENs.
    """

    aliases = dict.fromkeys((*_GO_TESTIFY_RECEIVERS,))  # defaults always recognized
    aliases.update(_go_testify_aliases(text))
    out: list[tuple[str, re.Pattern[str]]] = []
    for alias in aliases:
        out.append(
            (
                alias,
                re.compile(
                    rf"(?<![A-Za-z0-9_.]){re.escape(alias)}\.[A-Z][A-Za-z0-9_]*\s*\("
                ),
            )
        )
    return out


def _go_body_has_primitive_assertion(
    body_text: str, receivers: Iterable[str], *, alias_source: str | None = None
) -> bool:
    """Whether ``body_text`` contains a Go PRIMITIVE assertion (lexical).

    A primitive is a ``<recv>.<FailMethod>(`` failure call on ANY of the in-scope
    testing receivers, OR a testify ``<alias>.<Func>(`` call. A bare named-helper
    call is NOT primitive (resolved via the evidence graph). Receiver-anchored so a
    SUT method that happens to be named ``Fatal`` (``server.Fatal()``) is not
    mistaken for ``t.Fatal`` — only the ``*testing.T`` receiver(s) count.

    ``alias_source`` is the text the testify import ALIASES are resolved from — it
    must be the WHOLE FILE, because an ``import tassert "…/testify/assert"`` lives
    OUTSIDE the function body. When ``None`` (a body whose file is unknown) it
    degrades to ``body_text`` and the conventional unaliased ``assert``/``require``
    names are still recognized (an aliased call in that degraded case is then seen
    by the helper-resolution path instead — never a silent false-GREEN).

    Comments are stripped before scanning so a ``t.Fatalf`` written in a COMMENT is
    not counted as an assertion (false-GREEN guard).
    """

    skeleton = _go_strip_comments(body_text)
    for recv in receivers:
        if _go_receiver_call_re(recv, _GO_FAIL_METHODS).search(skeleton):
            return True
    for _alias, pattern in _go_testify_call_res(alias_source if alias_source is not None else body_text):
        if pattern.search(skeleton):
            return True
    return False


def _go_body_is_skipped(body_text: str, receivers: Iterable[str]) -> bool:
    """Whether ``body_text`` unconditionally skips via ``<recv>.Skip``/``Skipf``/``SkipNow``.

    Comments stripped first so a ``t.Skip()`` mentioned in a COMMENT does not mark a
    real test skipped (which would be a false-RED — the opposite hazard)."""

    skeleton = _go_strip_comments(body_text)
    for recv in receivers:
        if _go_receiver_call_re(recv, _GO_SKIP_METHODS).search(skeleton):
            return True
    return False


#: Go reserved words / literals / obvious builtins that are NOT credit-worthy
#: references inside an assertion condition or testify value arg — the Go analogue
#: of :data:`_PY_DIRECT_IGNORED_NAMES` / :data:`_TS_DIRECT_IGNORED_NAMES`. A SUT
#: call name, a local variable, an expected var, an Enum/const NAME a test declares
#: are NEVER here, so ``if got != want`` (got/want) / ``assert.Equal(t, want, got)``
#: stay GREEN while ``if true {`` / ``assert.Equal(t, 1, 1)`` (only ignored tokens)
#: become RED. ``t`` (the testing receiver) is ignored: it is the assertion API
#: ``器``, not an observed value (``assert.Equal(t, 1, 1)`` must not anchor via ``t``).
_GO_IGNORED_NAMES = frozenset(
    {
        "t",
        "true",
        "false",
        "nil",
        "iota",
        "len",
        "cap",
        "make",
        "new",
        "append",
        "copy",
        "delete",
        "string",
        "int",
        "int8",
        "int16",
        "int32",
        "int64",
        "uint",
        "uint8",
        "uint16",
        "uint32",
        "uint64",
        "byte",
        "rune",
        "float32",
        "float64",
        "bool",
        "error",
        "complex64",
        "complex128",
        "if",
        "else",
        "for",
        "range",
        "return",
        "func",
        "var",
        "const",
        "switch",
        "case",
        "default",
        "select",
        "go",
        "defer",
        "map",
        "struct",
        "interface",
        "chan",
        "type",
        "package",
        "import",
    }
)
#: A Go identifier token (the condition / arg scanner harvests these).
_GO_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: A Go STRING or rune literal (double-quoted, raw-backtick, or single-quoted
#: rune) — its inner identifiers are not references. Backslash escapes honoured for
#: the double/single forms; backtick raw strings have no escapes.
_GO_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"|`[^`]*`' + r"|'(?:\\.|[^'\\])*'")


def _go_reference_idents(expr: str) -> set[str]:
    """Genuine identifier REFERENCES in a Go expression (strings/comments stripped).

    Mirrors :func:`_rhs_reference_idents`: identifiers inside string/rune literals
    and ``//`` / ``/* */`` comments are EXCLUDED, and a SELECTOR field after ``.``
    (``got.Code`` → ``got`` references, ``Code`` does not) is dropped so a constant
    compared to a struct field still anchors on the base var. Best-effort and
    fail-OPEN: anything ambiguous stays a reference (toward credit, never a
    false-RED). Returns the harvested non-property identifier set.
    """

    # Strip block + line comments first (coarse; safe — only removes text).
    no_block = re.sub(r"/\*.*?\*/", " ", expr, flags=re.DOTALL)
    no_comments = re.sub(r"//[^\n]*", " ", no_block)
    # Blank out string/rune literals so their inner words are not harvested.
    blanked = _GO_STRING_RE.sub(" ", no_comments)
    refs: set[str] = set()
    for m in _GO_IDENT_RE.finditer(blanked):
        start = m.start()
        # Drop a selector field: an identifier immediately preceded by ``.``.
        k = start - 1
        while k >= 0 and blanked[k] in " \t":
            k -= 1
        if k >= 0 and blanked[k] == ".":
            continue
        refs.add(m.group(0))
    return refs


def _go_condition_is_nonconstant(condition: str) -> bool:
    """Whether a failure-guard CONDITION references a NON-constant observation.

    ``got != want`` / ``err != nil`` / ``len(out) == 0`` reference ``got``/``want``/
    ``err``/``out`` (non-ignored) ⇒ real. ``1 != 1`` / ``true`` reference only
    literals/keywords ⇒ constant. (``nil`` is ignored, so ``err != nil`` is REAL
    purely on ``err``; ``len`` is ignored, so ``len(out) == 0`` is REAL on ``out``.)
    """

    return bool(_go_reference_idents(condition) - _GO_IGNORED_NAMES)


def _go_testify_value_args(args: str) -> str:
    """The VALUE-arg slice of a testify call's argument text (drops the ``t`` lead).

    testify's signature is ``assert.Equal(t, expected, actual, msgAndArgs...)``.
    The FIRST arg is the ``*testing.T`` (the assertion API handle, not an
    observation), and a TRAILING string-literal message (``"msg"`` / a ``"%s"``
    format + its args) is human text, not the asserted value. We drop the leading
    ``t`` arg and any trailing args once a string-literal message arg is seen, then
    return the remaining value-arg text for the non-constant check. Best-effort: if
    splitting is ambiguous we keep MORE args (toward credit / fail-open).
    """

    parts = _go_split_args(args)
    if not parts:
        return ""
    # Drop the leading testing-handle arg (conventionally ``t``); keep it only if
    # there is exactly one arg (degenerate / non-standard call → fail-open).
    if len(parts) >= 2:
        parts = parts[1:]
    # Drop a trailing message: once an arg is a bare string/format literal, it and
    # everything after it are msgAndArgs. Scan from the end.
    while parts:
        last = parts[-1].strip()
        if _GO_STRING_RE.fullmatch(last):
            parts = parts[:-1]
        else:
            break
    return " , ".join(parts)


def _go_split_args(args: str) -> list[str]:
    """Top-level comma split of a Go call's argument text (string/bracket aware)."""

    out: list[str] = []
    depth = 0
    in_str: str | None = None
    prev = ""
    current = ""
    for ch in args:
        if in_str is not None:
            current += ch
            if ch == in_str and prev != "\\":
                in_str = None
            prev = ch
            continue
        if ch in ("'", '"', "`"):
            in_str = ch
            current += ch
        elif ch in "([{":
            depth += 1
            current += ch
        elif ch in ")]}":
            depth = max(0, depth - 1)
            current += ch
        elif ch == "," and depth == 0:
            out.append(current)
            current = ""
        else:
            current += ch
        prev = ch
    if current.strip():
        out.append(current)
    return [p for p in out if p.strip()]


def _go_direct_assertion_evidence(
    body_text: str, receivers: Iterable[str], *, alias_source: str | None = None
) -> AssertionEvidence:
    """Verdict: does ``body_text`` carry a NON-constant Go primitive assertion?

    For each primitive assertion in the body decide REAL vs CONSTANT-only:

    * a FAILURE call (``<recv>.Fatal``/``Errorf``/…) is REAL when it is GUARDED by
      a condition that references a non-constant (``if got != want { t.Fatalf }``).
      An UNCONDITIONAL failure call (no enclosing ``if`` whose body holds it) is
      treated as constant-only here UNLESS the call's OWN args reference a
      non-constant (``t.Fatalf("got %v", got)`` carries ``got``) — a deliberate
      fail-open so a legitimate post-computation ``t.Fatalf("...", got)`` credits.
    * a testify call (``assert.Equal(t, want, got)``) is REAL when its VALUE args
      (after the ``t`` handle, minus a trailing message) reference a non-constant.

    ``alias_source`` (the WHOLE FILE) resolves testify import aliases for the (b)
    scan; ``None`` degrades to ``body_text`` (unaliased ``assert``/``require`` still
    recognized). If ANY primitive in the body is REAL ⇒ ``direct`` (ok). If at least
    one primitive was seen and ALL are constant-only ⇒ ``constant_direct`` (not ok).
    If the regex ``has_assertion`` matched but this scanner recognizes no primitive
    (an assertion shape it cannot classify) ⇒ fail OPEN (``direct``), never a
    false-RED.
    """

    receivers = tuple(receivers)
    saw_primitive = False
    # Scan a comment-stripped SKELETON so an assertion / condition written in a
    # COMMENT (``// if got != want { t.Fatalf(...) }``) is never read as real code
    # (the false-GREEN this closes). Offsets are preserved (comments → spaces), so
    # ``_balanced_args`` / ``_go_enclosing_if_condition`` positions still align.
    skeleton = _go_strip_comments(body_text)

    # ── (a) stdlib failure calls, with their enclosing ``if`` guard condition ──
    for recv in receivers:
        fail_re = _go_receiver_call_re(recv, _GO_FAIL_METHODS)
        for m in fail_re.finditer(skeleton):
            saw_primitive = True
            # The call's OWN argument text (carries ``got`` in ``t.Fatalf("%v", got)``).
            call_args = _balanced_args(skeleton, m.end() - 1)
            if _go_reference_idents(call_args) - _GO_IGNORED_NAMES:
                return AssertionEvidence(ok=True, reason="direct")
            # Otherwise look for the GUARD: the nearest enclosing ``if <cond> {``
            # whose brace-matched body contains this call.
            cond = _go_enclosing_if_condition(skeleton, m.start())
            if cond is not None and _go_condition_is_nonconstant(cond):
                return AssertionEvidence(ok=True, reason="direct")

    # ── (b) testify calls, judged on their VALUE args ──
    for _alias, pattern in _go_testify_call_res(alias_source if alias_source is not None else body_text):
        for m in pattern.finditer(skeleton):
            saw_primitive = True
            args = _balanced_args(skeleton, m.end() - 1)
            value_args = _go_testify_value_args(args)
            if _go_reference_idents(value_args) - _GO_IGNORED_NAMES:
                return AssertionEvidence(ok=True, reason="direct")

    if not saw_primitive:
        # ``has_assertion`` matched but no primitive classified here → fail OPEN.
        return AssertionEvidence(ok=True, reason="direct")
    return AssertionEvidence(ok=False, reason="constant_direct")


def _go_enclosing_if_condition(body_text: str, call_pos: int) -> str | None:
    """Condition of the nearest ``if <cond> {`` whose body encloses ``call_pos``.

    Scans every ``if ... {`` opener before ``call_pos``; for each, brace-matches
    its block from the ``{`` and keeps the INNERMOST one whose ``[open, close]``
    span contains the call. Returns the condition text (between ``if`` and the body
    ``{``), or ``None`` when the call is not inside any ``if`` (unconditional). The
    condition may itself contain ``if``-free parens/calls (``if errors.Is(err, X)``)
    — those are returned verbatim for the reference scan. A ``for <cond> {`` /
    ``switch`` guard is intentionally NOT treated as an assertion guard (only an
    ``if`` is the idiomatic failure guard); this stays anti-false-green because the
    fallback is to look at the call's own args (already done by the caller).
    """

    best: str | None = None
    best_open = -1
    for m in re.finditer(r"(?<![A-Za-z0-9_])if\b", body_text):
        brace = _go_find_block_brace(body_text, m.end())
        if brace < 0:
            continue
        close = _go_match_brace(body_text, brace)
        if close < 0:
            continue
        if brace < call_pos < close and brace > best_open:
            # Innermost (latest-opening) enclosing ``if`` wins.
            best_open = brace
            best = body_text[m.end():brace]
    return best.strip() if best is not None else None


def _go_find_block_brace(text: str, start: int) -> int:
    """Index of the ``{`` that opens an ``if`` body starting the scan at ``start``.

    Skips over balanced ``()`` / ``[]`` (a condition's own parens/index exprs) and
    string/rune literals so the FIRST top-level ``{`` is the block opener, not a
    composite-literal brace inside the condition (``if x == T{} {`` is rare in a
    guard; this still returns the block ``{`` by requiring top-level depth). Returns
    -1 if none found before a statement terminator.
    """

    depth = 0
    in_str: str | None = None
    prev = ""
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if in_str is not None:
            if ch == in_str and prev != "\\":
                in_str = None
            prev = ch
            i += 1
            continue
        if ch in ("'", '"', "`"):
            in_str = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        elif ch == "{" and depth == 0:
            return i
        elif ch == "\n" and depth == 0 and text[max(0, i - 1)] in ";":
            return -1
        prev = ch
        i += 1
    return -1


def _go_match_brace(text: str, open_idx: int) -> int:
    """Index of the ``}`` matching the ``{`` at ``open_idx`` (string-aware), or -1."""

    depth = 0
    in_str: str | None = None
    prev = ""
    i = open_idx
    n = len(text)
    while i < n:
        ch = text[i]
        if in_str is not None:
            if ch == in_str and prev != "\\":
                in_str = None
            prev = ch
            i += 1
            continue
        if ch in ("'", '"', "`"):
            in_str = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        prev = ch
        i += 1
    return -1


def _go_blank_spans(inner: str, inner_offset: int, spans: list[tuple[int, int]]) -> str:
    """``inner`` with each absolute ``spans`` region blanked (newlines preserved).

    ``inner`` is a slice of the file that begins at absolute index ``inner_offset``;
    ``spans`` are absolute ``(start, end)`` char ranges (the subtests). Each char in
    a span is replaced by a space EXCEPT newlines (kept so any line-based scan over
    the result still maps to the right lines). Used to compute a GROUP function's OWN
    skip/assertion facts without seeing its children's content.
    """

    if not spans:
        return inner
    chars = list(inner)
    n = len(inner)
    for start, end in spans:
        lo = max(0, start - inner_offset)
        hi = min(n, end - inner_offset)
        for i in range(lo, hi):
            if chars[i] != "\n":
                chars[i] = " "
    return "".join(chars)


@dataclass(frozen=True)
class GoTestBlockProfile:
    """Go (``testing`` + optional testify) structural adapter.

    A test block is a ``func TestXxx(t *testing.T) { ... }`` function (its body is
    brace-matched). ``t.Run("name", func(t *testing.T){...})`` SUBTESTS are emitted
    as NESTED blocks — exactly the group→leaf shape the TS adapter uses for
    ``describe``→``it`` (the gate's ``_attached_block`` redirects a marker above a
    group to its first nested test, and a flat ``TestXxx`` with no subtest is a leaf
    coverage target itself). Skip is ``t.Skip()`` / ``t.Skipf(...)`` / ``t.SkipNow()``
    on the receiver. A PRIMITIVE assertion is a stdlib failure call
    (``t.Error/Errorf/Fatal/Fatalf/Fail/FailNow``) or a testify ``assert.X`` /
    ``require.X`` call; a bare named-helper call is resolved one hop via
    :meth:`resolve_assertion_evidence`.
    """

    def handles_file(self, rel_path: str) -> bool:
        return rel_path.endswith("_test.go")

    def parse_test_blocks(self, text: str) -> list[TestBlock]:
        def _line_of(pos: int) -> int:
            return text.count("\n", 0, pos) + 1

        blocks: list[TestBlock] = []
        for fm in _GO_TEST_FUNC_RE.finditer(text):
            brace = text.index("{", fm.end() - 1)
            close = _go_match_brace(text, brace)
            if close < 0:
                close = len(text) - 1
            fn_recv = fm.group("recv")
            start_line = _line_of(fm.start())
            # Body extent = the opening-brace line to the close brace; body_text for
            # assertion scanning is the inside of the braces. ``text`` (the whole
            # file) is the testify-alias source for both function and subtest scans.
            end_line = _line_of(close)
            fn_body_inner = text[brace + 1 : close]

            # Subtests: each ``<recv>.Run("name", func(<sub> *testing.T){...})``
            # inside this function becomes a nested leaf block. Its receiver is the
            # subtest's OWN ``*testing.T`` parameter. We also record each subtest's
            # absolute char span so the FUNCTION-level skip/assertion scan can blank
            # it out (a group's facts are its OWN, not inherited from a child — the
            # same discipline as TS ``describe`` skip = its own ``.skip`` modifier,
            # never a child's).
            subtests: list[TestBlock] = []
            sub_spans: list[tuple[int, int]] = []
            for sm in _GO_SUBTEST_RE.finditer(text, brace + 1, close):
                sbrace = text.index("{", sm.end() - 1)
                sclose = _go_match_brace(text, sbrace)
                if sclose < 0 or sclose > close:
                    continue
                sub_recv = sm.group("sub")
                sub_inner = text[sbrace + 1 : sclose]
                sub_spans.append((sm.start(), sclose + 1))
                subtests.append(
                    TestBlock(
                        start_line=_line_of(sm.start()),
                        end_line=_line_of(sclose),
                        is_executable=not _go_body_is_skipped(sub_inner, (sub_recv,)),
                        has_assertion=_go_body_has_primitive_assertion(
                            sub_inner, (sub_recv,), alias_source=text
                        ),
                        label=f"{fm.group('name')}/subtest",
                        body_text=sub_inner,
                    )
                )

            # Function-OUTER body: the function's inner text with subtest regions
            # blanked (newlines preserved so line math is unaffected). This is what
            # the function-level skip / has_assertion are computed over, so a group
            # is not falsely marked skipped/asserting because a CHILD skips/asserts.
            fn_outer = _go_blank_spans(fn_body_inner, brace + 1, sub_spans)

            # The function block itself. A function that ONLY groups subtests is a
            # group (the gate redirects a marker above it to the first subtest); a
            # flat function is a leaf coverage target. ``has_assertion`` / skip
            # reflect the function's OWN body (outside any subtest), which for a flat
            # test IS the assertion site. Receivers: the function's own receiver
            # (subtests carry their own).
            blocks.append(
                TestBlock(
                    start_line=start_line,
                    end_line=end_line,
                    is_executable=not _go_body_is_skipped(fn_outer, (fn_recv,)),
                    has_assertion=_go_body_has_primitive_assertion(
                        fn_outer, (fn_recv,), alias_source=text
                    ),
                    label=fm.group("name"),
                    body_text=fn_outer,
                )
            )
            blocks.extend(subtests)
        # Keep document order (start_line) so attachment's smallest-containing and
        # nearest-after scans behave like the TS/PY adapters.
        return sorted(blocks, key=lambda b: (b.start_line, -b.end_line))

    def resolve_assertion_evidence(
        self, block: TestBlock, *, importer_text: str, importer_rel: str, project_root: Path
    ) -> AssertionEvidence:
        return _resolve_go_evidence(
            block, importer_text=importer_text, importer_rel=importer_rel, project_root=project_root
        )

    def resolve_direct_assertion_evidence(
        self,
        block: TestBlock,
        *,
        importer_text: str = "",
        importer_rel: str = "",
        project_root: Path | None = None,
        config: dict[str, Any] | None = None,
        profile: Any = None,
    ) -> AssertionEvidence:
        """Whether the block's DIRECT Go primitive assertion references a real name.

        ``constant_direct`` when every primitive is constant-only (an unconditional
        ``t.Fatal()`` / ``if 1 != 1`` guard / ``assert.Equal(t, 1, 1)``); ``direct``
        when a failure call is guarded by a non-constant condition (or carries a
        non-constant in its own args) or a testify call's value args reference a
        non-constant. The body's testing receivers are recovered from ``body_text``
        so the check works without re-parsing the file; ``importer_text`` (the whole
        file) resolves testify import aliases. Called by the gate only when
        ``block.has_assertion`` is True.
        """

        receivers = _go_block_receivers(block)
        return _go_direct_assertion_evidence(
            block.body_text, receivers, alias_source=importer_text or block.body_text
        )


def _go_block_receivers(block: TestBlock) -> tuple[str, ...]:
    """Testing-receiver names in scope for a Go block's body.

    A flat ``TestXxx`` body is scanned with the conventional ``t`` plus any name
    that appears as ``<name>.<FailMethod>(`` / ``<name>.Skip(`` in the body (so a
    renamed receiver ``func TestX(tt *testing.T)`` is honored even though the
    profile only stores the body text). Always includes ``t`` (the overwhelming
    convention) so a body using ``t`` is covered even when no fail/skip call is
    present yet. Failing to recover a receiver only ever makes a primitive look
    ABSENT (fail-open toward credit at the direct stage), never a false-GREEN.
    """

    recvs: set[str] = {"t"}
    for m in re.finditer(
        r"(?<![A-Za-z0-9_.])(?P<r>[A-Za-z_][A-Za-z0-9_]*)\.(?:"
        + "|".join((*_GO_FAIL_METHODS, *_GO_SKIP_METHODS))
        + r")\b\s*\(",
        block.body_text,
    ):
        recvs.add(m.group("r"))
    return tuple(recvs)


def _resolve_go_evidence(
    block: TestBlock, *, importer_text: str, importer_rel: str, project_root: Path
) -> AssertionEvidence:
    """Go DELEGATED-assertion (helper) resolution — mirrors the PY/TS engine.

    A Go test that delegates its check to a same-package / imported helper
    (``checkServer(t, got)`` whose body runs ``if ... { t.Fatalf(...) }``) is
    resolved one hop through the shared :func:`_resolve_evidence` engine. The
    helper-body primitive detector is the Go failure-call / testify regex (a body
    with a real ``t.Fatalf`` + an argument anchor passes; a no-op / constant helper
    fails). Import resolution uses the Go module/same-directory rules in
    :func:`_go_resolve_module`.
    """

    return _resolve_evidence(
        block,
        importer_text=importer_text,
        importer_rel=importer_rel,
        project_root=project_root,
        primitive_re=_GO_HELPER_PRIMITIVE_RE,
        imported_lookup=_go_imported_module,
        module_resolver=_go_resolve_module,
        def_finder=_go_find_function_def,
        reexport_edges=None,
    )


#: Helper-body primitive detector for Go (used by the shared evidence engine's
#: ``primitive_re``). A failure call on the CONVENTIONAL ``t`` receiver or a
#: testify ``assert.``/``require.`` call. (Helper bodies overwhelmingly take the
#: testing handle as ``t``; a renamed handle in a helper degrades to unresolved,
#: which is fail-closed — never a false pass.)
_GO_HELPER_PRIMITIVE_RE = re.compile(
    r"(?<![A-Za-z0-9_.])t\.(?:" + "|".join(_GO_FAIL_METHODS) + r")\b\s*\("
    r"|(?<![A-Za-z0-9_.])(?:assert|require)\.[A-Z][A-Za-z0-9_]*\s*\("
)


#: A Go helper ``func name(params) {`` / ``func name(params) ret {`` definition —
#: captures the name + the (possibly multi-line) parameter list (read by paren
#: matching). A METHOD (``func (r R) name(...)``) is intentionally not matched: a
#: test calls a bare ``helper(t, x)``, not a method, in the conventional shape.
_GO_FUNC_DEF_RE = re.compile(r"(?<![A-Za-z0-9_])func\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _go_imported_module(importer_text: str, symbol: str, full_callee: str = "") -> str | None:
    """Binding 'module' for a Go helper symbol — always ``None`` (same-file search).

    Go has NO per-symbol import statement: a same-package helper is callable with
    no import at all (one package per directory), and a cross-package helper is a
    ``pkg.Helper`` SELECTOR (which the assertion-helper extractor reduces to its
    leaf ``Helper``, losing the package qualifier). Returning ``None`` makes the
    shared evidence engine keep ``module_text = importer_text`` and search the
    SAME FILE for the helper's ``func`` def — which is where the conventional
    generated Go test helper lives (a ``func check(t *testing.T, got int){...}``
    above/below the test in the same ``*_test.go``). A helper that lives in a
    SEPARATE same-package file is not followed (the def-finder won't find it in the
    importer text ⇒ ``unresolved_helper`` ⇒ fail-CLOSED in strict, never a false
    pass) — a deliberate, documented residual matching the PY/TS "1-hop, same-repo"
    discipline (a future ``go-module`` import-resolver adapter can widen this).
    ``full_callee`` (E1) is accepted for signature parity with the shared engine's
    ``imported_lookup`` slot but unused — Go's lookup never needed the qualifier.
    """

    return None


def _go_resolve_module(importer_rel: str, spec: str, project_root: Path) -> Path | None:
    """No-op module resolver for Go (same-file helper search only).

    Never called with a real ``spec`` because :func:`_go_imported_module` always
    returns ``None`` (the engine then searches the importer text in-place and does
    not invoke a module resolver). Present only to satisfy the shared engine's
    adapter signature; returns ``None`` defensively.
    """

    return None


def _go_find_function_def(module_text: str, name: str) -> "tuple[str, list[str]] | None":
    """Find Go ``func name(params...) {``'s body + parameter names in ``module_text``.

    Returns ``(body_text, param_names)`` or ``None``. The body is brace-matched
    from the function-opening ``{`` after the (possibly multi-line) parameter list
    and an optional return signature, then COMMENT-STRIPPED (so a ``t.Fatalf`` in a
    helper COMMENT is not read as a real assertion by the shared engine's
    ``primitive_re`` / argument-anchor scan — the helper-side analogue of the
    direct-side comment false-GREEN guard). Parameter names are extracted from the
    param list, KEEPING the ``*testing.T`` handle's name (the anchor check separately
    requires a NON-``t`` param reference, so a helper that asserts only ``t``-handle
    constants stays constant-only). Mirrors :func:`_py_find_function_def` /
    :func:`_ts_find_function_def`.
    """

    skeleton = _go_strip_comments(module_text)
    for m in _GO_FUNC_DEF_RE.finditer(skeleton):
        if m.group("name") != name:
            continue
        params, after = _read_paren_group(skeleton, m.end() - 1)
        if params is None:
            continue
        brace = skeleton.find("{", after)
        if brace < 0:
            continue
        # Guard: the ``{`` must be the body, not a composite literal in the return
        # type. Accept the first ``{`` at/after ``after`` whose match is balanced —
        # for the conventional ``func f(...) {`` and ``func f(...) error {`` this is
        # correct; an exotic return type with a brace degrades (fail-closed). The
        # body is from the comment-stripped skeleton, so embedded comments are gone.
        body = _read_brace_group(skeleton, brace)
        return body, _go_split_param_names(params)
    return None


def _go_split_param_names(params: str) -> list[str]:
    """Parameter NAMES from a Go parameter list (``got int, want int`` → [got, want]).

    Go groups consecutive same-type params (``a, b int``) and writes ``name type``.
    We take the leading identifier of each comma-group as a name when the group has
    two+ tokens (``got int``); a single-token group is a name in a grouped list
    (``a, b int`` → ``a`` then ``b int``). The ``*testing.T`` handle name (commonly
    ``t``) is KEPT (the anchor check separately requires a non-``t`` reference).
    Best-effort: anything ambiguous is included (fail-open at the param level; the
    primitive + anchor check downstream is the real gate).
    """

    names: list[str] = []
    for raw in _go_split_args(params):
        tokens = raw.split()
        if not tokens:
            continue
        # ``name type`` → name is tokens[0]; ``name`` (grouped) → the token itself.
        candidate = tokens[0].lstrip("*")
        m = re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", candidate)
        if m:
            names.append(candidate)
    return names


# ---------------------------------------------------------------------------
# C# (xUnit / NUnit / MSTest) structural adapter.
#
# A test block is a test METHOD annotated with a recognized test attribute
# (``[Fact]`` / ``[Theory]`` xUnit, ``[Test]`` / ``[TestCase]`` NUnit,
# ``[TestMethod]`` MSTest). C# has no flat-function tests (everything lives in a
# class) and no Go-style subtests, so — unlike :class:`GoTestBlockProfile` — there
# is NO group→leaf fan-out: each annotated method is a single leaf coverage
# target. Skip is an attribute fact (xUnit ``[Fact(Skip="…")]`` / ``[Theory(Skip=
# …)]``, NUnit ``[Ignore("…")]``, MSTest ``[Ignore]``) — NOT a body call (C# has no
# in-body skip idiom like Go's ``t.Skip()``), so it is read off the attribute block
# ABOVE the signature. A PRIMITIVE assertion is an ``Assert.<Method>(`` call on the
# framework's static ``Assert`` API (xUnit ``Assert.Equal``/``True``/``Throws``/…,
# NUnit ``Assert.That``/``AreEqual``/…, MSTest ``Assert.AreEqual``/``IsTrue``/…); a
# bare named-helper call is resolved one hop via the shared evidence engine.
#
# REUSE: ``_go_strip_comments`` (C# uses ``//`` line + ``/* */`` block comments,
# identical to Go), ``_go_match_brace`` (string-aware brace matcher), ``_balanced_
# args`` (call-arg extraction) and ``_go_reference_idents`` (identifier-reference
# harvest, ``.``-selector aware, string/comment stripped) — C#'s lexical surface
# for these is the same as Go's, so re-using them keeps ONE audited implementation
# of each rather than a parallel C# copy that could drift.
# ---------------------------------------------------------------------------


#: C# reserved words / literals / obvious builtins that are NOT credit-worthy
#: references inside an ``Assert.X(...)`` argument list — the C# analogue of
#: :data:`_GO_IGNORED_NAMES`. The ``Assert`` token itself is ignored (it is the
#: assertion API 器, not an observed value, so ``Assert.Equal(1, 1)`` must not
#: anchor via ``Assert``), as are the literals ``true``/``false``/``null`` and the
#: common assertion method names (so ``Assert.Equal``'s ``Equal`` selector, already
#: dropped by :func:`_go_reference_idents`, is doubly safe). A SUT call name
#: (``Add``), a local variable, an expected/actual var, an enum/const NAME a test
#: declares are NEVER here, so ``Assert.Equal(5, Add(2, 3))`` (``Add``) stays GREEN
#: while ``Assert.Equal(1, 1)`` / ``Assert.True(true)`` (only ignored tokens)
#: become RED (``constant_direct``).
_CSHARP_IGNORED_NAMES = frozenset(
    {
        # The assertion API handle + framework receiver tokens.
        "Assert",
        "Xunit",
        "NUnit",
        "Is",  # NUnit constraint root: ``Assert.That(x, Is.EqualTo(1))`` → ``Is``.
        # Boolean / null literals.
        "true",
        "false",
        "null",
        # Common assertion method names (selector fields are already dropped by
        # _go_reference_idents, but harvesting belt-and-suspenders for any call
        # form that surfaces the bare method name).
        "Equal",
        "NotEqual",
        "True",
        "False",
        "Null",
        "NotNull",
        "Same",
        "NotSame",
        "Throws",
        "ThrowsAsync",
        "Contains",
        "DoesNotContain",
        "Empty",
        "NotEmpty",
        "That",
        "AreEqual",
        "AreNotEqual",
        "AreSame",
        "IsTrue",
        "IsFalse",
        "IsNull",
        "IsNotNull",
        "IsInstanceOf",
        # C# keywords / type names that may appear in an inline arg expression
        # (``Assert.Equal(typeof(int), x.GetType())``) and prove nothing on their
        # own — a SUT call/local in the SAME arg still anchors.
        "var",
        "new",
        "typeof",
        "nameof",
        "default",
        "void",
        "int",
        "uint",
        "long",
        "ulong",
        "short",
        "ushort",
        "byte",
        "sbyte",
        "bool",
        "char",
        "string",
        "object",
        "float",
        "double",
        "decimal",
        "return",
        "if",
        "else",
        "for",
        "foreach",
        "while",
        "switch",
        "case",
        "this",
        "base",
        "async",
        "await",
        "public",
        "private",
        "protected",
        "internal",
        "static",
    }
)

#: A recognized C# test-method ATTRIBUTE name (without the leading ``[``). xUnit
#: ``Fact``/``Theory``, NUnit ``Test``/``TestCase``, MSTest ``TestMethod``. The
#: attribute may carry args (``Fact(Skip="…")``) and may be one of several stacked
#: attributes (``[Trait(...)] [Fact]``); the parser tolerates other attributes in
#: the block above the signature and only requires that AT LEAST ONE of these is
#: present (otherwise the method is an ordinary, non-test method and is skipped).
_CSHARP_TEST_ATTRIBUTES = (
    "Fact",
    "Theory",
    "Test",
    "TestCase",
    "TestMethod",
)
#: ``[<Attr>`` opener for any recognized test attribute (word-bounded after ``[``).
#: Matches ``[Fact]``, ``[Fact(Skip="x")]``, ``[Theory(...)]``, ``[Test]`` etc. We
#: anchor on the ``[`` + attribute name; the attribute's optional ``(...)`` args
#: and the ``]`` are scanned separately when we need the Skip fact.
_CSHARP_TEST_ATTR_RE = re.compile(
    r"\[\s*(?P<attr>" + "|".join(_CSHARP_TEST_ATTRIBUTES) + r")\b"
)
#: A C# method SIGNATURE opener: optional modifiers, optional return type, the
#: method NAME, a parameter ``(...)`` list, then the body ``{``. Permissive on
#: modifiers/return type (``public``/``private``/``static``/``async``/``Task``/
#: ``void``/``Task<T>`` …) because the test attribute above is what authorizes the
#: block; this regex only needs to LOCATE the name + body brace. ``[^;{(\n]*?`` for
#: the leading modifiers/return type stays non-greedy and forbids ``;`` (so a field
#: / abstract-method-without-body declaration is not matched), ``(`` / ``{`` (so
#: the name is the token immediately before the param list) and ``\n`` (so the
#: head + name stay on the signature line — C# signatures conventionally do).
_CSHARP_METHOD_SIG_RE = re.compile(
    r"(?P<indent>[ \t]*)(?P<head>[A-Za-z_][^;{(\n]*?\b)"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\([^;{]*?\)\s*(?:where[^{;]*?)?\{",
    re.DOTALL,
)
#: An ``[Ignore`` attribute (NUnit / MSTest) anywhere in a method's attribute block
#: ⇒ the method is skipped (``[Ignore]`` or ``[Ignore("reason")]``).
_CSHARP_IGNORE_ATTR_RE = re.compile(r"\[\s*Ignore\b")
#: ``Skip=`` inside an xUnit ``[Fact(...)]`` / ``[Theory(...)]`` attribute's arg
#: list ⇒ the test is skipped (``[Fact(Skip="wip")]``). Matched against the
#: attribute's argument text only (extracted via brace/paren scan) so a ``Skip=``
#: appearing in an unrelated string elsewhere never marks a test skipped.
_CSHARP_SKIP_ARG_RE = re.compile(r"\bSkip\s*=")
#: A C# ``Assert.<Method>(`` primitive-assertion call: the static ``Assert`` API
#: followed by a Capitalized method name and ``(``. Keyed on ``Assert.`` + an
#: UpperCamel method (covers xUnit ``Assert.Equal``/``True``/``Throws``, NUnit
#: ``Assert.That``/``AreEqual``, MSTest ``Assert.IsTrue``/``IsNotNull`` …) without
#: enumerating every method name. Constant-only-ness is decided later from the ARG
#: text, never the method name, so this permissive match never false-GREENs.
#: ``(?<![A-Za-z0-9_.])`` so a member access ``foo.Assert.X`` (an unrelated SUT
#: type that happens to expose an ``Assert`` member) does not masquerade as the
#: framework's static ``Assert``.
#: Optional generic type-argument list between the method name and ``(`` —
#: ``Assert.IsType<ParseError>(x)``, ``Assert.Throws<T>(() => …)``, nested up to
#: depth 3 (``Assert.IsType<Dictionary<string, List<int>>>(x)``). ``;{}`` are
#: excluded so a malformed ``<`` can never swallow past a statement boundary.
#: Without this, generic-only asserts were reported as "test with NO assertion"
#: (false-RED observed on the csharp exprcalc greenfield dogfood, 2026-07-11).
_CSHARP_GENERIC_ARGS = (
    r"<[^<>;{}]*(?:<[^<>;{}]*(?:<[^<>;{}]*>[^<>;{}]*)*>[^<>;{}]*)*>"
)
_CSHARP_ASSERT_CALL_RE = re.compile(
    r"(?<![A-Za-z0-9_.])Assert\.[A-Z][A-Za-z0-9_]*\s*(?:"
    + _CSHARP_GENERIC_ARGS
    + r")?\s*\("
)


def _csharp_attr_args(text: str, attr_open_idx: int) -> str:
    """The argument text of a C# attribute whose ``[`` is at ``attr_open_idx``.

    For ``[Fact(Skip="wip")]`` this returns ``Skip="wip"`` (the text inside the
    attribute's ``(...)``). When the attribute has NO ``(`` before its closing
    ``]`` (a bare ``[Fact]``), returns ``""``. Best-effort and string-aware (a
    ``)`` inside a string arg does not close the group); used only to look for a
    ``Skip=`` argument, so a fail-open empty result simply means "not skipped",
    which for the bare ``[Fact]`` is correct.
    """

    n = len(text)
    # Find the attribute's own ``]`` (so we never read into a SIBLING attribute's
    # parens) and its ``(`` if any, scanning string-aware.
    i = attr_open_idx
    in_str: str | None = None
    prev = ""
    paren_at = -1
    while i < n:
        ch = text[i]
        if in_str is not None:
            if ch == in_str and prev != "\\":
                in_str = None
            prev = ch
            i += 1
            continue
        if ch in ("'", '"'):
            in_str = ch
        elif ch == "(" and paren_at < 0:
            paren_at = i
        elif ch == "]" and paren_at < 0:
            return ""  # bare attribute, no args before the closer.
        elif ch == "(" and paren_at >= 0:
            break
        prev = ch
        i += 1
    if paren_at < 0:
        return ""
    return _balanced_args(text, paren_at)


@dataclass(frozen=True)
class CSharpTestBlockProfile:
    """C# (xUnit / NUnit / MSTest) structural adapter.

    A test block is a test METHOD annotated with a recognized test attribute
    (``[Fact]`` / ``[Theory]`` xUnit, ``[Test]`` / ``[TestCase]`` NUnit,
    ``[TestMethod]`` MSTest) inside a test class. Its body is brace-matched with
    :func:`_go_match_brace` (string-aware). Unlike :class:`GoTestBlockProfile`
    there are NO subtests / no group→leaf fan-out: every annotated method is a
    single LEAF coverage target (C# tests live in classes, and a ``[Theory]``'s
    data rows are not separately-markable blocks). The block's ``start_line`` is
    the line of the method's FIRST recognized test attribute (``[Fact]``), so a
    ``codd: covers`` marker written on the line immediately ABOVE the attribute
    attaches as a LEADING marker with an empty between-range (the same shape as a
    marker above a Go ``func TestX``).

    SKIP is an ATTRIBUTE fact, not a body call (C# has no ``t.Skip()`` idiom): an
    xUnit ``[Fact(Skip="…")]`` / ``[Theory(Skip=…)]`` (a ``Skip=`` argument inside
    the test attribute), or a NUnit/MSTest ``[Ignore]`` / ``[Ignore("…")]``
    attribute on the method. A PRIMITIVE assertion is an ``Assert.<Method>(`` call
    on the framework's static ``Assert`` API; a bare named-helper call
    (``VerifyResult(actual)``) is NOT primitive and is resolved one hop via
    :meth:`resolve_assertion_evidence`.

    PURE and best-effort: a parse it cannot do returns ``[]`` (the gate then
    degrades for that file), never raises.
    """

    def handles_file(self, rel_path: str) -> bool:
        """Whether ``rel_path`` is a C# test file.

        Permissive (the gate degrades on a file it cannot parse, so a false
        POSITIVE here is harmless; a false NEGATIVE would silently skip a real C#
        test). True iff the file is a ``.cs`` file AND it is either under a
        ``tests`` / ``.Tests`` path segment (the conventional C# test-project
        location) OR its basename matches the conventional ``*Test.cs`` /
        ``*Tests.cs`` naming. The conformance fixture is ``tests/XTests.cs``, which
        satisfies BOTH the path and the basename predicate.
        """

        if not rel_path.endswith(".cs"):
            return False
        lowered = rel_path.replace("\\", "/").lower()
        if "/tests/" in f"/{lowered}" or ".tests/" in lowered or lowered.startswith("tests/"):
            return True
        base = lowered.rsplit("/", 1)[-1]
        return base.endswith("test.cs") or base.endswith("tests.cs")

    def parse_test_blocks(self, text: str) -> list[TestBlock]:
        """Parse ``text`` into one leaf :class:`TestBlock` per annotated test method.

        For each method signature, look BACKWARD over the contiguous attribute /
        comment / blank block immediately above it for a recognized test attribute
        (``[Fact]`` / ``[Theory]`` / ``[Test]`` / ``[TestCase]`` / ``[TestMethod]``);
        a method with no such attribute is an ordinary method and is skipped. The
        block's ``start_line`` is the line of that FIRST test attribute so a marker
        placed directly above the attribute attaches cleanly. Skip / assertion
        facts are computed off a comment-stripped skeleton (so a ``Skip=`` or
        ``Assert.X`` written in a COMMENT is never read as real). Returns ``[]`` on
        anything it cannot parse (degrade, never raise).
        """

        try:
            return self._parse(text)
        except Exception:  # noqa: BLE001 — best-effort parser ⇒ degrade, never raise.
            return []

    def _parse(self, text: str) -> list[TestBlock]:
        def _line_of(pos: int) -> int:
            return text.count("\n", 0, pos) + 1

        # Comment-stripped skeleton: attribute / signature / Skip / assertion
        # scanning all run over THIS so a test attribute, a ``Skip=`` arg, or an
        # ``Assert.X`` written inside a COMMENT is never mistaken for real code
        # (the false-GREEN this closes). Offsets/newlines are preserved by
        # ``_go_strip_comments`` (comment bytes → spaces), so positions taken from
        # the skeleton map back onto ``text`` line-for-line.
        skeleton = _go_strip_comments(text)
        lines = skeleton.splitlines()

        blocks: list[TestBlock] = []
        for sig in _CSHARP_METHOD_SIG_RE.finditer(skeleton):
            brace = skeleton.index("{", sig.end() - 1)
            close = _go_match_brace(skeleton, brace)
            if close < 0:
                close = len(skeleton) - 1
            body_inner = skeleton[brace + 1 : close]

            # ── Walk the contiguous attribute/comment/blank block ABOVE the
            # signature line, collecting it and finding the FIRST recognized test
            # attribute. The walk stops at the first line that is NOT an attribute
            # (``[...]``), a comment, or blank — i.e. the previous statement /
            # method / class-opener — so attributes belonging to an EARLIER member
            # are never attributed to this method.
            sig_line = _line_of(sig.start())
            attr_lines: list[str] = []
            attr_block_start_line = sig_line  # default if no attribute precedes.
            first_test_attr_line: int | None = None
            ln = sig_line - 1
            while ln >= 1:
                raw = lines[ln - 1] if ln - 1 < len(lines) else ""
                stripped = raw.strip()
                if not stripped:
                    ln -= 1
                    continue
                is_attr = stripped.startswith("[")
                is_comment = stripped.startswith(_COMMENT_PREFIXES)
                if not is_attr and not is_comment:
                    break  # a real statement / brace — top of the attribute block.
                attr_block_start_line = ln
                if is_attr:
                    attr_lines.append(stripped)
                ln -= 1

            # The method must carry at least one recognized TEST attribute to be a
            # test block; otherwise it is an ordinary method (helper / setup) and is
            # skipped entirely (NOT emitted as a block).
            attr_text = "\n".join(reversed(attr_lines))
            if not _CSHARP_TEST_ATTR_RE.search(attr_text):
                continue

            # The block's ``start_line`` is the line of the FIRST recognized test
            # attribute (so a marker directly above ``[Fact]`` attaches with an
            # empty between-range). Find it within the attribute block.
            for cand in range(attr_block_start_line, sig_line):
                cand_raw = lines[cand - 1] if cand - 1 < len(lines) else ""
                if _CSHARP_TEST_ATTR_RE.search(cand_raw):
                    first_test_attr_line = cand
                    break
            start_line = first_test_attr_line if first_test_attr_line is not None else sig_line
            end_line = _line_of(close)

            blocks.append(
                TestBlock(
                    start_line=start_line,
                    end_line=end_line,
                    is_executable=not self._is_skipped(attr_text),
                    has_assertion=self._has_primitive_assertion(body_inner),
                    label=sig.group("name"),
                    body_text=body_inner,
                )
            )
        # Document order (start_line) so the gate's smallest-containing / nearest-
        # after attachment scans behave like the Go/TS/PY adapters.
        return sorted(blocks, key=lambda b: (b.start_line, -b.end_line))

    @staticmethod
    def _is_skipped(attr_text: str) -> bool:
        """Whether a method's attribute block marks it SKIPPED.

        xUnit: a ``Skip=`` argument inside the ``[Fact(...)]`` / ``[Theory(...)]``
        test attribute (``[Fact(Skip="wip")]``). NUnit / MSTest: an ``[Ignore]`` /
        ``[Ignore("…")]`` attribute on the method. ``attr_text`` is already
        comment-stripped (the caller built it from the skeleton), so a ``Skip=`` /
        ``[Ignore]`` written in a COMMENT does not mark a real test skipped (which
        would be a false-RED — the opposite hazard).
        """

        if _CSHARP_IGNORE_ATTR_RE.search(attr_text):
            return True
        # A ``Skip=`` argument INSIDE an xUnit ``[Fact(...)]`` / ``[Theory(...)]``.
        for m in _CSHARP_TEST_ATTR_RE.finditer(attr_text):
            if m.group("attr") not in ("Fact", "Theory"):
                continue
            args = _csharp_attr_args(attr_text, m.start())
            if _CSHARP_SKIP_ARG_RE.search(args):
                return True
        return False

    @staticmethod
    def _has_primitive_assertion(body_text: str) -> bool:
        """Whether ``body_text`` contains a C# PRIMITIVE ``Assert.X(`` call.

        A primitive is a static-``Assert`` API call (``Assert.Equal(`` /
        ``Assert.True(`` / ``Assert.That(`` / ``Assert.AreEqual(`` / …). A bare
        named-helper call (``VerifyResult(x)``) is NOT primitive (resolved via the
        evidence graph). ``body_text`` is already a comment-stripped skeleton (the
        parser passes the skeleton body), so an ``Assert.X`` written in a COMMENT is
        not counted (false-GREEN guard).
        """

        return bool(_CSHARP_ASSERT_CALL_RE.search(body_text))

    def resolve_assertion_evidence(
        self, block: TestBlock, *, importer_text: str, importer_rel: str, project_root: Path
    ) -> AssertionEvidence:
        """C# DELEGATED-assertion (helper) evidence — fail-CLOSED minimal resolver.

        Called by the gate ONLY for an attached, executable block with NO direct
        primitive assertion (``block.has_assertion`` is False). C# helper
        resolution (following a ``VerifyResult(...)`` call to its method body one
        hop through ``using`` imports) is NOT implemented here — the conformance
        fixtures, like the Go ones, do not delegate. We therefore fail CLOSED
        without ever spuriously crediting:

        * if the body contains an assertion-LIKE bare call (a helper named
          ``Assert*``/``Verify*``/``Check*``/``Expect*``/… per
          :func:`_looks_like_assertion_helper`) that we cannot resolve →
          ``unresolved_helper`` (an unresolved assertion helper is not evidence);
        * otherwise (no primitive, no assertion-like call — e.g. the
          ``fake_no_assertion`` body that only calls ``Add(2, 3)``) →
          ``no_assertion``.

        NEVER returns ``ok=True`` (no resolution path is implemented), so this can
        only ever REJECT, never false-GREEN. A future ``dotnet-using`` import
        resolver can widen this to real 1-hop helper following.
        """

        body = _go_strip_comments(block.body_text)
        if _extract_helper_calls(body):
            return AssertionEvidence(
                ok=False,
                reason="unresolved_helper",
                detail="csharp helper resolution not implemented",
            )
        return AssertionEvidence(ok=False, reason="no_assertion")

    def resolve_direct_assertion_evidence(
        self,
        block: TestBlock,
        *,
        importer_text: str = "",
        importer_rel: str = "",
        project_root: Path | None = None,
        config: dict[str, Any] | None = None,
        profile: Any = None,
    ) -> AssertionEvidence:
        """Whether the block's DIRECT C# ``Assert.X`` assertion references a real name.

        Modeled on :func:`_go_direct_assertion_evidence`. For each ``Assert.X(`` in
        the comment-stripped body, extract its argument text via
        :func:`_balanced_args` and decide REAL vs CONSTANT-only: the call is REAL
        when its arguments reference a NON-constant identifier
        (``_go_reference_idents(args) - _CSHARP_IGNORED_NAMES`` is non-empty) — a
        SUT call, local, expected/actual var, exception, or output.
        ``Assert.Equal(5, Add(2, 3))`` references ``Add`` ⇒ ``direct`` (ok).
        ``Assert.True(true)`` / ``Assert.Equal(1, 1)`` reference only literals / the
        ignored ``Assert`` token ⇒ ``constant_direct`` (not ok), the direct-side
        analogue of the helper-side argument anchor.

        If ANY ``Assert.X`` is REAL ⇒ ``direct``. If at least one was seen and ALL
        are constant-only ⇒ ``constant_direct``. If the body's ``has_assertion`` was
        True but this scanner classifies no primitive (an assertion shape it cannot
        read) ⇒ fail OPEN (``direct``), never a false-RED. Called by the gate only
        when ``block.has_assertion`` is True.
        """

        skeleton = _go_strip_comments(block.body_text)
        saw_primitive = False
        for m in _CSHARP_ASSERT_CALL_RE.finditer(skeleton):
            saw_primitive = True
            args = _balanced_args(skeleton, m.end() - 1)
            if _go_reference_idents(args) - _CSHARP_IGNORED_NAMES:
                return AssertionEvidence(ok=True, reason="direct")
        if not saw_primitive:
            # ``has_assertion`` matched but no primitive classified here → fail OPEN
            # (toward credit, never a false-RED) — the same防波堤 the Go/PY/TS
            # direct resolvers use.
            return AssertionEvidence(ok=True, reason="direct")
        return AssertionEvidence(ok=False, reason="constant_direct")


# ---------------------------------------------------------------------------
# Per-profile 1-hop helper resolution (the assertion EVIDENCE graph).
#
# Both implementations follow the SAME shape (mirroring the native implement-
# oracle's import resolution), differing only in their language regexes:
#   1. Extract assertion-LIKE helper calls from the test body (name-pattern).
#   2. For each, find the symbol's binding import in the importing file and
#      resolve the specifier to a sibling module file (1 hop, same repo).
#   3. In that module, find the helper's definition body and check it carries a
#      PRIMITIVE assertion/fail AND references one of the helper's parameters
#      (the argument anchor that defeats no-op / constant-only helpers).
# A helper that itself delegates to a deeper helper is followed ONE more hop
# (so expectSuccessfulRun → expectExitCode is honored), bounded to avoid cycles.
#
# BARREL re-export following (step 2b). The binding module is frequently a
# *barrel* — an index module that carries NO definition of its own and only
# RE-EXPORTS sibling modules (TS ``export * from "./assertions"`` /
# ``export { expectOk as ok } from "./asserts"``; Python ``__init__.py`` with
# ``from .asserts import expect_ok``). The conventional e2e shape imports helpers
# from such a barrel (``import { expectSuccessResult } from "./helpers"`` →
# ``helpers/index.ts`` → ``export * from "./assertions"`` → the real
# ``expectSuccessResult`` body). When ``def_finder`` finds no def in the binding
# module, the resolver FOLLOWS the module's re-exports to the file that actually
# defines the symbol (bounded by ``_MAX_REEXPORT_HOPS`` + a cycle guard). This is
# purely an "ability to reach the real body"; the body still has to carry a
# PRIMITIVE assertion + argument anchor — a barrel re-exporting a no-op/constant
# helper still FAILS, and an unfollowable / depth-exhausted re-export is still an
# unresolved helper (greenfield strict ⇒ fail). The follower is per-profile (TS
# ``export */named/alias``; Python ``__init__`` ``from .x import y``) so the gate
# stays language-agnostic; an unknown stack supplies no follower and degrades.
# ---------------------------------------------------------------------------


_MAX_HELPER_HOPS = 2
#: Bound on how many re-export edges (barrel → barrel → … → defining module) the
#: resolver will chase for ONE symbol. Generous enough for nested index barrels
#: (``helpers/index → assertions/index → cli-assertions``) yet bounded to keep a
#: pathological re-export web finite; combined with a per-symbol ``seen`` set of
#: visited module files for cycle safety.
_MAX_REEXPORT_HOPS = 4

#: A bare or member call ``name(`` / ``a.b.name(`` — captures the dotted callee.
_CALL_RE = re.compile(r"(?P<callee>[A-Za-z_$][\w$.]*)\s*\(")


def _extract_helper_calls(body_text: str) -> list[tuple[str, str, str]]:
    """Assertion-like ``name(args)`` calls in a test body → (callee, name, args).

    Returns calls whose callee leading identifier matches the assertion-helper
    name set: ``callee`` is the FULL matched text (``rule.check`` /
    ``HarnessAssertions.assertSuccess`` / a bare ``checkFoo``), ``name`` is its
    final segment (``check`` / ``assertSuccess`` / ``checkFoo``), and ``args`` is
    the raw argument text (used for the argument-anchor check). Only
    assertion-ish LEAF names are considered — this is the candidate-selection
    step, never a pass. The full ``callee`` (added for a receiver-aware lookup —
    e.g. Java's static-import binding and the library fluent-terminal check, both
    of which need the qualifier a bare leaf name discards) is additive: every
    existing caller that only used the (name, args) pair keeps working by
    unpacking the first element as ``_``.
    """

    out: list[tuple[str, str, str]] = []
    for match in _CALL_RE.finditer(body_text):
        callee = match.group("callee")
        segment = callee.split(".")[-1]
        if not _looks_like_assertion_helper(segment):
            continue
        args = _balanced_args(body_text, match.end("callee"))
        out.append((callee, segment, args))
    return out


def _balanced_args(text: str, open_paren_search_from: int) -> str:
    """The argument text between the call's matched parens (best-effort).

    ``open_paren_search_from`` is the index just past the callee; we find the next
    ``(`` and return everything up to its matching ``)``. String/paren nesting is
    tracked coarsely. Returns ``""`` if unbalanced.
    """

    i = text.find("(", open_paren_search_from)
    if i < 0:
        return ""
    depth = 0
    in_str: str | None = None
    prev = ""
    start = i + 1
    j = i
    n = len(text)
    while j < n:
        ch = text[j]
        if in_str is not None:
            if ch == in_str and prev != "\\":
                in_str = None
        elif ch in ("'", '"', "`"):
            in_str = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start:j]
        prev = ch
        j += 1
    return ""


def _arg_identifiers(args: str) -> set[str]:
    """Identifiers appearing in a call's argument text (for the anchor check).

    e.g. ``result, fixture.stdout`` → {result, fixture, stdout}. Used to confirm
    the helper body references something derived from the CALL's arguments, so a
    constant-only helper does not slip through on a coincidental name.
    """

    return set(re.findall(r"[A-Za-z_$][\w$]*", args or ""))


# ── TypeScript / JavaScript resolution ──────────────────────────────────────

#: Named import binding the helper symbol: ``import { a, b as c } from "./x"``.
_TS_IMPORT_NAMED_RE = re.compile(
    r"""\bimport\b[^;{]*\{(?P<names>[^}]*)\}\s*from\s*['"](?P<spec>[^'"]+)['"]""",
    re.MULTILINE,
)
_TS_SOURCE_EXTS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")


def _ts_imported_specifier(importer_text: str, symbol: str, full_callee: str = "") -> str | None:
    """The module specifier that imports ``symbol`` into the importing file.

    ``full_callee`` (E1) is accepted for signature parity with the shared
    engine's ``imported_lookup`` slot but unused — TS/JS named-import binding
    only ever needs the bare local symbol.
    """

    for match in _TS_IMPORT_NAMED_RE.finditer(importer_text):
        for raw in match.group("names").split(","):
            name = raw.strip()
            if not name:
                continue
            # ``orig as alias`` — the LOCAL binding is the alias (after ``as``).
            local = name.split(" as ")[-1].strip()
            if local == symbol:
                return match.group("spec")
    return None


def _ts_resolve_specifier(importer_rel: str, spec: str, project_root: Path) -> Path | None:
    """Resolve a relative TS specifier to a real sibling file (NodeNext-aware)."""

    if not spec.startswith("."):
        return None  # bare/package specifier — outside the 1-hop same-repo graph.
    base = (project_root / importer_rel).resolve().parent
    raw = (base / spec).resolve()
    for candidate in _ts_candidates(raw):
        if candidate.is_file():
            # JAIL: a test's import specifier is attacker-shaped data — a relative
            # specifier (``../../../etc/passwd``) can resolve OUTSIDE the project
            # root, and the caller would then read the off-root file as a helper
            # body (a path-escape false-green into the VB authenticity gate). Confine
            # the resolved candidate to the root; an escape yields None (helper
            # unresolved ⇒ fail-CLOSED). In-root resolution is unchanged.
            if resolve_project_path(project_root, candidate) is None:
                return None
            return candidate
    return None


def _ts_candidates(raw: Path) -> list[Path]:
    candidates: list[Path] = []
    if raw.suffix:
        candidates.append(raw)
        stem = raw.with_suffix("")
        for ext in _TS_SOURCE_EXTS:
            candidates.append(stem.with_suffix(ext))
    else:
        for ext in _TS_SOURCE_EXTS:
            candidates.append(raw.with_suffix(ext))
    for ext in _TS_SOURCE_EXTS:
        candidates.append(raw / f"index{ext}")
    return candidates


def _ts_find_function_def(module_text: str, name: str) -> "tuple[str, list[str]] | None":
    """Find ``name``'s definition body + its parameter names in ``module_text``.

    Returns ``(body_text, param_names)`` or ``None``. Anchored on the function
    NAME, then the parameter list is read by paren-matching from the following
    ``(`` (so a MULTI-LINE signature works), and the body is brace-matched from
    the function-body ``{`` AFTER any return-type annotation (so a brace-bearing
    return type like ``Promise<{ error: string }>`` / ``: { ok: boolean }`` is not
    mistaken for the body — see :func:`_ts_body_brace_index`). Arrow /
    function-expression / ``function`` declaration forms are all handled.
    """

    for pattern in _TS_FUNC_NAME_RES:
        for m in pattern.finditer(module_text):
            if m.group("name") != name:
                continue
            # The name-anchor match ends at the def's opening ``(``; paren-match
            # the (possibly multi-line) params, then locate the BODY ``{`` past
            # any return-type annotation, then brace-match the body.
            params, after = _read_paren_group(module_text, m.end() - 1)
            if params is None:
                continue
            brace = _ts_body_brace_index(module_text, after)
            if brace < 0:
                continue
            body = _read_brace_group(module_text, brace)
            return body, _split_params(params)
    return None


def _ts_body_brace_index(text: str, after_params: int) -> int:
    """Index of the FUNCTION-BODY ``{`` that follows a TS signature's params.

    ``after_params`` is the index just past the params' closing ``)``. Between it
    and the body there may be a RETURN-TYPE ANNOTATION (``): T {``) and/or an arrow
    (``) => {``). The naive "first ``{`` after the params" is WRONG when the return
    type itself contains braces — ``): Promise<{ error: string }> {`` (the noteapi
    false-RED), ``): { ok: boolean } {`` (object-type literal), or
    ``): Promise<Array<{ id: number }>> {`` — there the first ``{`` is part of the
    TYPE, not the body. This skips the annotation and returns the body ``{``.

    The decision uses the ``:`` (return-type marker) as the disambiguator:

    * The first significant token after the params is ``{`` → the BODY (no return
      type at all, e.g. ``function g(a) {``).
    * It is ``=>`` → an arrow with NO return type; the body is the next top-level
      ``{`` (an EXPRESSION-bodied arrow has no block ``{`` → return ``-1`` so the
      caller degrades rather than mis-extract).
    * It is ``:`` → a return-type annotation. Consume the type (skipping balanced
      ``<> () [] {}`` groups so a generic / tuple / object-type literal inside the
      type is never seen at top level), and stop at the top-level ``{`` (body) or
      ``=>`` (typed arrow → body is its next ``{``). An object-type literal that is
      the WHOLE return type (``: { ok: boolean } {``) is the first balanced
      ``{...}`` right after ``:`` — it is skipped, and the body is the ``{`` after.

    Returns ``-1`` when no body brace is found (unbalanced / expression arrow).

    KNOWN LIMITATION (fails CLOSED, never false-green): a return type that is
    itself a FUNCTION TYPE (``): (x) => { y: 1 } {``) is not disambiguated — the
    inner ``=>`` is read as the function's own arrow. This degrades to a wrong
    body span ⇒ ``helper_no_primitive`` ⇒ the marker FAILS (a conservative
    false-RED, not a false pass). Such a return annotation on an assertion helper
    is vanishingly rare; the gate stays anti-false-green either way.
    """

    n = len(text)
    i = _ts_skip_trivia(text, after_params)
    if i >= n:
        return -1
    # No return-type annotation: an immediate body brace, or a `=>` arrow.
    if text[i] == "{":
        return i
    if text.startswith("=>", i):
        j = _ts_skip_trivia(text, i + 2)
        return j if j < n and text[j] == "{" else -1
    if text[i] != ":":
        # Unexpected token between params and body — be permissive: take the next
        # top-level brace past it (covers exotic but brace-free prefixes).
        return _ts_scan_type_to_body(text, i)
    # `:` return-type annotation. Skip it and consume the type up to the body.
    return _ts_scan_type_to_body(text, i + 1)


def _ts_skip_trivia(text: str, i: int) -> int:
    """Advance ``i`` past whitespace and ``//`` / ``/* */`` comments."""

    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "/":
            nl = text.find("\n", i)
            i = n if nl < 0 else nl + 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            break
    return i


#: Type-level binary operators that JOIN type atoms (``A | B``, ``A & B``). After
#: one of these the parser expects ANOTHER atom, so a following object-type literal
#: ``{...}`` is still part of the return type, not the body.
_TS_TYPE_JOIN_CHARS = frozenset("|&")


def _ts_scan_type_to_body(text: str, i: int) -> int:
    """From inside a return-type annotation, return the body ``{`` index (or -1).

    Models the return type as a sequence of type ATOMS joined by ``|`` / ``&``
    operators, ending at the body ``{`` or a typed-arrow ``=>``. ``expecting_atom``
    starts True (the type begins right after ``:`` or a join operator); a top-level
    ``{`` while ``expecting_atom`` is an OBJECT-TYPE LITERAL atom (``: { ok } {`` /
    ``: A | { err } {``) and is skipped as a balanced group, whereas a top-level
    ``{`` while NOT expecting an atom (a complete type already consumed, no join
    operator following) is the BODY. Generics ``<…>``, tuples ``[…]`` and paren
    types ``(…)`` are skipped as balanced groups (their inner braces never surface).
    """

    n = len(text)
    angle = paren = bracket = 0
    expecting_atom = True
    in_str: str | None = None
    prev = ""
    while i < n:
        ch = text[i]
        if in_str is not None:
            if ch == in_str and prev != "\\":
                in_str = None
            prev = ch
            i += 1
            continue
        if ch in ("'", '"', "`"):
            in_str = ch
            expecting_atom = False
            prev = ch
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            nl = text.find("\n", i)
            i = n if nl < 0 else nl
            prev = ""
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            prev = ""
            continue
        top = angle == 0 and paren == 0 and bracket == 0
        if ch == "<":
            angle += 1
            expecting_atom = False
        elif ch == ">":
            if angle > 0:
                angle -= 1
        elif ch == "(":
            paren += 1
            expecting_atom = False
        elif ch == "[":
            bracket += 1
            expecting_atom = False
        elif ch in (")", "]"):
            if ch == ")" and paren > 0:
                paren -= 1
            elif ch == "]" and bracket > 0:
                bracket -= 1
        elif top and text.startswith("=>", i):
            # Typed arrow: the body is the next top-level `{`.
            j = _ts_skip_trivia(text, i + 2)
            return j if j < n and text[j] == "{" else -1
        elif top and ch == "{":
            if not expecting_atom:
                return i  # a complete type precedes this `{` → it is the body.
            # Object-type literal atom (`: { ok } {` / `A | { err } {`) — skip its
            # balanced group; an atom is now consumed (no longer expecting one).
            group = _read_brace_group(text, i)
            i += len(group)
            expecting_atom = False
            prev = "}"
            continue
        elif top and ch in _TS_TYPE_JOIN_CHARS:
            expecting_atom = True  # `|`/`&` → another atom follows.
        elif top and not ch.isspace():
            expecting_atom = False  # identifier / `.` / `:` … consumes/continues an atom
        prev = ch
        i += 1
    return -1


def _read_paren_group(text: str, open_idx: int) -> "tuple[str | None, int]":
    """Inner text of the paren group whose ``(`` is at/just after ``open_idx``.

    Returns ``(inner, index_after_close)`` or ``(None, open_idx)`` if unbalanced.
    Tolerates nesting and string literals (so a typed param like
    ``reasons: readonly string[]`` or a default ``= "x"`` is preserved).
    """

    i = text.find("(", open_idx)
    if i < 0:
        return None, open_idx
    depth = 0
    in_str: str | None = None
    prev = ""
    start = i + 1
    j = i
    n = len(text)
    while j < n:
        ch = text[j]
        if in_str is not None:
            if ch == in_str and prev != "\\":
                in_str = None
        elif ch in ("'", '"', "`"):
            in_str = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start:j], j + 1
        prev = ch
        j += 1
    return None, open_idx


def _read_brace_group(text: str, open_brace_idx: int) -> str:
    """The full ``{ ... }`` block starting at ``open_brace_idx`` (best-effort)."""

    depth = 0
    in_str: str | None = None
    prev = ""
    j = open_brace_idx
    n = len(text)
    while j < n:
        ch = text[j]
        if in_str is not None:
            if ch == in_str and prev != "\\":
                in_str = None
        elif ch in ("'", '"', "`"):
            in_str = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_brace_idx : j + 1]
        prev = ch
        j += 1
    return text[open_brace_idx:]


# ── TypeScript / JavaScript barrel re-export following ───────────────────────
#
# A barrel module re-exports siblings instead of defining the symbol. Two forms:
#   ``export * from "./X"``                 (star — symbol unchanged, try X)
#   ``export { Y } from "./X"``             (named — local name Y, original Y)
#   ``export { Y as Z } from "./X"``        (named+alias — local Z, original Y)
# A ``export {}`` with NO ``from`` clause is a LOCAL re-export of in-file symbols,
# not a re-export edge, and is ignored here (the def, if any, is found in-file by
# ``_ts_find_function_def`` already).

#: ``export * from "./x"`` and ``export * as ns from "./x"`` (a namespace star
#: binds a NAMESPACE object, not the bare symbol, so it is NOT a transparent
#: re-export of ``symbol`` — only the plain ``export * from`` is followed).
_TS_EXPORT_STAR_RE = re.compile(
    r"""^[ \t]*export\s+\*\s+from\s*['"](?P<spec>[^'"]+)['"]""", re.MULTILINE
)
#: ``export { a, b as c } from "./x"`` (the ``from`` clause makes it a re-export).
_TS_EXPORT_NAMED_FROM_RE = re.compile(
    r"""^[ \t]*export\s+(?:type\s+)?\{(?P<names>[^}]*)\}\s*from\s*['"](?P<spec>[^'"]+)['"]""",
    re.MULTILINE,
)


def _ts_reexport_edges(module_text: str, symbol: str) -> list[tuple[str, str]]:
    """Re-export edges from a TS barrel that can carry ``symbol`` onward.

    Returns ``(spec, original_name)`` pairs: ``spec`` is the relative module to
    follow, ``original_name`` is the name to look for THERE (an alias flips the
    name back). ``export * from "./x"`` forwards ``symbol`` unchanged. A named
    re-export forwards ONLY when its LOCAL name (after ``as``) equals ``symbol``,
    and the original (before ``as``) is what the target module defines. Order:
    explicit named re-exports first (a precise alias should win over a star), then
    ``export *`` fan-out. Only relative (same-repo) specs are returned.
    """

    edges: list[tuple[str, str]] = []
    for match in _TS_EXPORT_NAMED_FROM_RE.finditer(module_text):
        spec = match.group("spec")
        if not spec.startswith("."):
            continue
        for raw in match.group("names").split(","):
            name = raw.strip()
            if not name:
                continue
            parts = [p.strip() for p in name.split(" as ")]
            original = parts[0]
            local = parts[-1]
            if local == symbol:
                edges.append((spec, original))
    for match in _TS_EXPORT_STAR_RE.finditer(module_text):
        spec = match.group("spec")
        if spec.startswith("."):
            edges.append((spec, symbol))  # star forwards the name unchanged
    return edges


# ── Python resolution ───────────────────────────────────────────────────────

#: ``from pkg.mod import a, b as c`` / ``from .mod import x`` — one top-level binding.
@dataclass(frozen=True)
class _PyFromImport:
    mod: str
    names: tuple[tuple[str, str], ...]  # (original, local)


def _iter_py_from_imports(module_text: str) -> list[_PyFromImport]:
    """TOP-LEVEL Python ``from X import ...`` bindings, parsed via ``ast.ImportFrom``.

    Using Python's own parser (not a regex) makes parenthesized MULTI-LINE imports,
    backslash continuations, aliases, inline comments, and ``*`` all parse by
    SYNTAX. The previous regex (``names=[^\\n#]+``) truncated the load-bearing
    ``from tests.e2e.helpers import (\\n  a,\\n  b,\\n)`` package-barrel form to
    ``(`` — so a marker's assert-helper went unresolved and was wrongly reported
    ``no_assertion`` (a false-RED that blocked Python greenfield;
    ``PC-assertion-helper-package-barrel-falsered``). TOP-LEVEL only: a
    function-local import is NOT a module binding (a broader ``ast.walk`` could
    misattribute a local import and admit a false-green). A file that does not
    parse degrades to ``[]`` (the compile layer owns SyntaxErrors; never raise).
    """

    try:
        tree = ast.parse(module_text)
    except (SyntaxError, ValueError):
        return []
    out: list[_PyFromImport] = []
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        mod = "." * int(node.level or 0) + (node.module or "")
        if not mod:
            continue
        out.append(
            _PyFromImport(
                mod=mod,
                names=tuple(
                    (alias.name, alias.asname or alias.name) for alias in node.names
                ),
            )
        )
    return out


def _py_imported_module(importer_text: str, symbol: str, full_callee: str = "") -> str | None:
    """The module path a ``from <mod> import <symbol>`` binds ``symbol`` from.

    ``full_callee`` (E1) is accepted for signature parity with the shared
    engine's ``imported_lookup`` slot but unused — Python's ``from`` binding
    only ever needs the bare local symbol.
    """

    for item in _iter_py_from_imports(importer_text):
        for original, local in item.names:
            if local == symbol:
                return item.mod
            # ``from helpers import *`` may bind the symbol; the downstream
            # resolver still requires a real def/re-export + assertion evidence,
            # so admitting the star here never passes on its own.
            if original == "*":
                return item.mod
    return None


def _py_reexport_edges(module_text: str, symbol: str) -> list[tuple[str, str]]:
    """Re-export edges from a Python ``__init__.py`` barrel carrying ``symbol``.

    Mirrors :func:`_ts_reexport_edges` for the Python package-barrel convention.
    Returns ``(mod, original_name)`` pairs: ``mod`` is the relative module to
    follow (resolved with the SAME ``_py_resolve_module`` rules, with the barrel's
    own path as the importer), ``original_name`` is the name to look for there.
      ``from .x import y``        → forwards ``y`` unchanged when ``y == symbol``
      ``from .x import y as z``   → forwards ``y`` when the LOCAL ``z == symbol``
      ``from .x import *``        → forwards ``symbol`` unchanged (star)
    Explicit-relative imports are followed, and ABSOLUTE re-exports are admitted as
    CANDIDATE edges too (a first-party barrel commonly re-exports via
    ``from tests.helpers import x``). same-repo-ness is enforced downstream by
    ``_py_resolve_module``: it returns a file only for a project module, so a
    stdlib / third-party absolute re-export stays unresolved and cannot pass.
    """

    edges: list[tuple[str, str]] = []
    for item in _iter_py_from_imports(module_text):
        mod = item.mod
        for original, local in item.names:
            if original == "*":
                edges.append((mod, symbol))  # star re-export forwards the name
                continue
            if local == symbol:
                edges.append((mod, original))
    return edges


def _py_resolve_module(importer_rel: str, mod: str, project_root: Path) -> Path | None:
    """Resolve a python ``from <mod> import`` module to a sibling file (1 hop).

    Handles an explicit-relative ``from .mod`` / ``from ..pkg.mod`` against the
    importer's directory, and an absolute dotted ``a.b.c`` against the project
    root. Returns the module file (or its ``__init__.py``) if it exists.
    """

    importer_dir = (project_root / importer_rel).resolve().parent
    if mod.startswith("."):
        dots = len(mod) - len(mod.lstrip("."))
        rest = mod[dots:]
        base = importer_dir
        for _ in range(dots - 1):
            base = base.parent
        rel_parts = rest.split(".") if rest else []
    else:
        base = project_root
        rel_parts = mod.split(".")
    target = base
    for part in rel_parts:
        target = target / part
    for candidate in (target.with_suffix(".py"), target / "__init__.py"):
        if candidate.is_file():
            # JAIL: a test's ``from ....mod import`` specifier is attacker-shaped data
            # — a deep relative module (``....evil``) can resolve OUTSIDE the project
            # root, and the caller would then read the off-root module as a helper
            # body (a path-escape false-green into the VB authenticity gate). Confine
            # the resolved candidate to the root; an escape yields None (helper
            # unresolved ⇒ fail-CLOSED). In-root resolution is unchanged.
            if resolve_project_path(project_root, candidate) is None:
                return None
            return candidate
    return None


def _py_find_function_def(module_text: str, name: str) -> "tuple[str, list[str]] | None":
    """Find python ``def name(...)``'s body + parameter names in ``module_text``."""

    lines = module_text.splitlines()
    total = len(lines)
    for idx, line in enumerate(lines):
        m = _PY_DEF_RE.match(line)
        if not m or m.group("name") != name:
            continue
        if m.group("indent"):
            # Only a TOP-LEVEL (module-bound) def is a helper a test can import or
            # call. A nested/indented def (inside a function or class body) is not
            # an importable binding; crediting its assertion would be a false-green
            # for a helper the test cannot actually reach. Fail closed.
            continue
        params = _split_params(m.group("params"))
        indent = len(m.group("indent").expandtabs())
        end = idx + 1
        scan = idx + 1
        while scan < total:
            raw = lines[scan]
            stripped = raw.strip()
            if stripped:
                cur = len(raw[: len(raw) - len(raw.lstrip())].expandtabs())
                if cur <= indent:
                    break
            end = scan + 1
            scan += 1
        body = "\n".join(lines[idx:end])
        return body, params
    return None


# ── shared parameter handling ────────────────────────────────────────────────


def _split_params(params: str) -> list[str]:
    """Parameter NAMES from a (possibly typed/defaulted) parameter list.

    ``result: CliRunResult, expected = ""`` → [result, expected];
    ``self, result, stdout`` → [result, stdout] (``self``/``cls`` dropped).
    """

    out: list[str] = []
    depth = 0
    current = ""
    for ch in params:
        if ch in "[({<":
            depth += 1
            current += ch
        elif ch in "])}>":
            depth = max(0, depth - 1)
            current += ch
        elif ch == "," and depth == 0:
            out.append(current)
            current = ""
        else:
            current += ch
    if current.strip():
        out.append(current)
    names: list[str] = []
    for raw in out:
        token = raw.strip()
        if not token or token in ("self", "cls"):
            continue
        token = token.lstrip("*")  # *args / **kwargs / ...rest
        # strip default / type annotation
        token = re.split(r"[:=]", token, maxsplit=1)[0].strip()
        token = token.strip("{}[] ")  # destructured param surface
        name = re.match(r"^[A-Za-z_$][\w$]*", token)
        if name:
            names.append(name.group(0))
    return names


# ── language-agnostic evidence orchestrators (one per profile) ──────────────


def _resolve_evidence(
    block: TestBlock,
    *,
    importer_text: str,
    importer_rel: str,
    project_root: Path,
    primitive_re: re.Pattern[str],
    imported_lookup: Callable[[str, str, str], str | None],
    module_resolver: Callable[[str, str, Path], Path | None],
    def_finder: Callable[[str, str], "tuple[str, list[str]] | None"],
    reexport_edges: Callable[[str, str], list[tuple[str, str]]] | None = None,
    fallback_module_candidates: Callable[[str, str, Path, str], list[Path]] | None = None,
) -> AssertionEvidence:
    """Shared 1-hop helper-resolution engine (language plug-ins supply the rest).

    ``primitive_re`` detects a primitive assertion/fail in a helper body;
    ``imported_lookup(importer_text, symbol, full_callee)`` returns the binding
    specifier/module (``full_callee`` is the whole matched callee text — e.g.
    ``HarnessAssertions.assertSuccess`` — for a receiver-aware lookup; a profile
    that only needs the bare symbol ignores it);
    ``module_resolver(importer_rel, spec, root)`` resolves it to a file;
    ``def_finder(module_text, name)`` returns ``(body, params)`` for the helper;
    ``reexport_edges(module_text, symbol)`` (optional) returns the barrel
    re-export edges that can carry ``symbol`` onward, so a helper imported from a
    barrel index that only RE-EXPORTS its real definition is still reachable. A
    profile that supplies no follower simply never crosses a barrel (degrades to
    the 2.31.0 direct/simple-import behavior). ``fallback_module_candidates``
    (optional, E2) is consulted ONLY when same-file + import-bound + barrel
    resolution all miss — see :func:`_resolve_one_helper` for the exact seam.
    """

    calls = _extract_helper_calls(block.body_text)
    if not calls:
        return AssertionEvidence(ok=False, reason="no_assertion")

    saw_unresolved = False
    saw_no_primitive = False
    saw_constant = False
    last_helper = ""
    for full_callee, name, _args in calls:
        last_helper = name
        verdict = _resolve_one_helper(
            name=name,
            full_callee=full_callee,
            importer_text=importer_text,
            importer_rel=importer_rel,
            project_root=project_root,
            primitive_re=primitive_re,
            imported_lookup=imported_lookup,
            module_resolver=module_resolver,
            def_finder=def_finder,
            reexport_edges=reexport_edges,
            fallback_module_candidates=fallback_module_candidates,
            hops=_MAX_HELPER_HOPS,
            seen=frozenset(),
        )
        if verdict.ok:
            return verdict
        if verdict.reason == "unresolved_helper":
            saw_unresolved = True
        elif verdict.reason == "helper_no_primitive":
            saw_no_primitive = True
        elif verdict.reason == "constant_helper":
            saw_constant = True
    # No call produced evidence — report the most informative reason.
    if saw_no_primitive:
        return AssertionEvidence(ok=False, reason="helper_no_primitive", detail=last_helper)
    if saw_constant:
        return AssertionEvidence(ok=False, reason="constant_helper", detail=last_helper)
    if saw_unresolved:
        return AssertionEvidence(ok=False, reason="unresolved_helper", detail=last_helper)
    return AssertionEvidence(ok=False, reason="no_assertion")


def _follow_reexports_for_def(
    *,
    name: str,
    module_text: str,
    module_rel: str,
    project_root: Path,
    module_resolver: Callable[[str, str, Path], Path | None],
    def_finder: Callable[[str, str], "tuple[str, list[str]] | None"],
    reexport_edges: Callable[[str, str], list[tuple[str, str]]],
    depth: int,
    visited: frozenset[str],
) -> "tuple[tuple[str, list[str]], str, str] | None":
    """Follow a barrel's re-export edges to the module that DEFINES ``name``.

    ``module_text`` / ``module_rel`` are the current (barrel) module. Each
    re-export edge ``(spec, original_name)`` from :func:`reexport_edges` is
    resolved with the SAME ``module_resolver`` used for imports (the barrel acts
    as the importer for its own relative re-exports), the target module is read,
    and ``def_finder`` is tried there for ``original_name``. A target that is
    itself a barrel is recursed into (``original_name`` carried forward, depth
    decremented). Returns ``((body, params), defining_text, defining_rel)`` for
    the FIRST edge that reaches a real def, or ``None`` if no edge within
    ``depth`` hops resolves. Bounded by ``depth`` and a ``visited`` set of module
    rel-paths (cycle guard: ``a → b → a`` barrels terminate).

    Pure reach: the returned body is still subject to the caller's primitive +
    argument-anchor checks, so a barrel that re-exports a no-op/constant helper
    yields that no-op body (which then FAILS) — never a pass.
    """

    if depth <= 0 or module_rel in visited:
        return None
    visited = visited | {module_rel}

    for spec, original in reexport_edges(module_text, name):
        target_file = module_resolver(module_rel, spec, project_root)
        if target_file is None:
            continue
        try:
            target_text = target_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            target_rel = target_file.resolve().relative_to(project_root).as_posix()
        except ValueError:
            target_rel = spec
        if target_rel in visited:
            continue
        found = def_finder(target_text, original)
        if found is not None:
            return found, target_text, target_rel
        # Target is itself a barrel — recurse, carrying the (possibly aliased)
        # original name onward and decrementing the re-export budget.
        deeper = _follow_reexports_for_def(
            name=original,
            module_text=target_text,
            module_rel=target_rel,
            project_root=project_root,
            module_resolver=module_resolver,
            def_finder=def_finder,
            reexport_edges=reexport_edges,
            depth=depth - 1,
            visited=visited,
        )
        if deeper is not None:
            return deeper
    return None


def _resolve_one_helper(
    *,
    name: str,
    full_callee: str = "",
    importer_text: str,
    importer_rel: str,
    project_root: Path,
    primitive_re: re.Pattern[str],
    imported_lookup: Callable[[str, str, str], str | None],
    module_resolver: Callable[[str, str, Path], Path | None],
    def_finder: Callable[[str, str], "tuple[str, list[str]] | None"],
    reexport_edges: Callable[[str, str], list[tuple[str, str]]] | None = None,
    fallback_module_candidates: Callable[[str, str, Path, str], list[Path]] | None = None,
    hops: int,
    seen: frozenset[str],
) -> AssertionEvidence:
    """Resolve ONE assertion-like helper call to a credible body (≤``hops`` hops).

    A helper passes when its body has a primitive assertion/fail that references
    one of its parameters (the argument anchor). A helper whose body merely
    delegates to a DEEPER assertion helper (forwarding its own params, checked via
    ``inner_anchor & param_set`` below) is followed one more hop, so a chain like
    ``expectSuccessfulRun → expectExitCode`` resolves.

    When the binding module is a BARREL (it carries no def of ``name`` and only
    re-exports siblings), the def is located by following the module's re-export
    edges via ``reexport_edges`` (bounded by ``_MAX_REEXPORT_HOPS`` + a visited-
    file cycle guard). The defining module's text/rel then replace the barrel's so
    the SAME primitive + argument-anchor checks (and any deeper helper hop) run
    against the real body — barrel following adds reach, never a pass.

    ``full_callee`` (E1 — the whole matched callee text, e.g.
    ``HarnessAssertions.assertSuccess``, vs. ``name``'s bare leaf
    ``assertSuccess``) is threaded to ``imported_lookup`` for a receiver-aware
    binding lookup (e.g. a static-import table keyed on the leaf, or a future
    profile that also wants the qualifier). ``fallback_module_candidates``
    (E2, optional) is consulted ONLY as the LAST resort, after same-file +
    import-bound + barrel resolution ALL miss (``found`` is still ``None``): it
    returns candidate files to search for ``name``'s definition (e.g. Java's
    sibling-by-class-name guess, tried in the importer's own directory first
    and one subdirectory level down when that finds nothing, for a qualified
    call with no static import). It supplies PLACES TO LOOK ONLY — every candidate is
    re-jailed via :func:`resolve_project_path` before its content is read, and
    the SAME primitive + argument-anchor checks below still judge whatever is
    found there, so a bad guess can degrade to unresolved but never false-GREEN.
    """

    if hops <= 0 or name in seen:
        return AssertionEvidence(ok=False, reason="unresolved_helper", detail=name)
    seen = seen | {name}

    spec = imported_lookup(importer_text, name, full_callee)
    module_text = importer_text  # same-file helper is allowed (no import needed)
    resolved_rel = importer_rel
    if spec is not None:
        module_file = module_resolver(importer_rel, spec, project_root)
        if module_file is None:
            return AssertionEvidence(ok=False, reason="unresolved_helper", detail=name)
        try:
            module_text = module_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return AssertionEvidence(ok=False, reason="unresolved_helper", detail=name)
        try:
            resolved_rel = module_file.resolve().relative_to(project_root).as_posix()
        except ValueError:
            resolved_rel = importer_rel

    found = def_finder(module_text, name)
    if found is None and reexport_edges is not None and spec is not None:
        # Binding module has no def of ``name`` — it may be a BARREL that only
        # re-exports the real definition. Follow re-export edges to the defining
        # module, then continue with the SAME credibility checks below.
        followed = _follow_reexports_for_def(
            name=name,
            module_text=module_text,
            module_rel=resolved_rel,
            project_root=project_root,
            module_resolver=module_resolver,
            def_finder=def_finder,
            reexport_edges=reexport_edges,
            depth=_MAX_REEXPORT_HOPS,
            visited=frozenset(),
        )
        if followed is not None:
            found, module_text, resolved_rel = followed
    if found is None and fallback_module_candidates is not None:
        # E2 LAST RESORT: same-file, import-bound, AND barrel resolution all
        # missed. Ask the plug-in for a bounded list of "places to look" (e.g.
        # a sibling file, in the importer's directory or one level below,
        # whose name matches the call's receiver class); every candidate is
        # re-jailed here regardless of what the plug-in returned — a
        # bad/out-of-tree guess can never itself become a path escape (defense
        # in depth on top of the plug-in's own jailing).
        root = Path(project_root).resolve()
        for candidate in fallback_module_candidates(
            importer_text, importer_rel, project_root, full_callee
        )[:16]:
            candidate_resolved = resolve_project_path(project_root, candidate)
            if candidate_resolved is None:
                continue
            try:
                candidate_text = candidate_resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            candidate_found = def_finder(candidate_text, name)
            if candidate_found is None:
                continue
            try:
                candidate_rel = candidate_resolved.relative_to(root).as_posix()
            except ValueError:
                candidate_rel = importer_rel
            found = candidate_found
            module_text = candidate_text
            resolved_rel = candidate_rel
            break
    if found is None:
        # Not defined in the binding module, no same-file def, no resolvable
        # re-export chain, and no fallback candidate reaches a def → unresolved
        # (greenfield strict ⇒ fail).
        return AssertionEvidence(ok=False, reason="unresolved_helper", detail=name)
    body, params = found
    param_set = set(params)

    # Primitive assertion/fail in the helper body?
    if primitive_re.search(body):
        # Argument anchor (anti-no-op): a PRIMITIVE assertion must reference one of
        # the helper's own parameters OR a local DERIVED from a parameter (1-2
        # hops), e.g. ``const body = await readJson(response); expect(body.error)``
        # — ``body`` flows from the ``response`` param, so the assertion IS anchored
        # to the call's data. A helper that asserts only a constant
        # (``expect(true).toBe(true)`` / ``assert True``) references neither, so
        # marker-spam delegating to it FAILS — the helper NAME is never trusted.
        anchor_set = _params_and_derived_locals(body, param_set)
        if _body_references_params(body, anchor_set, primitive_re):
            return AssertionEvidence(ok=True, reason="helper_resolved", detail=name)
        return AssertionEvidence(ok=False, reason="constant_helper", detail=name)

    # No primitive here — does the body delegate to a DEEPER assertion helper,
    # forwarding its own params? Follow one more hop.
    for inner_full_callee, inner_name, inner_args in _extract_helper_calls(body):
        inner_anchor = _arg_identifiers(inner_args)
        if not (inner_anchor & param_set):
            continue  # the inner call must carry THIS helper's data forward
        deeper = _resolve_one_helper(
            name=inner_name,
            full_callee=inner_full_callee,
            importer_text=module_text,
            importer_rel=resolved_rel,
            project_root=project_root,
            primitive_re=primitive_re,
            imported_lookup=imported_lookup,
            module_resolver=module_resolver,
            def_finder=def_finder,
            reexport_edges=reexport_edges,
            fallback_module_candidates=fallback_module_candidates,
            hops=hops - 1,
            seen=seen,
        )
        if deeper.ok:
            return AssertionEvidence(ok=True, reason="helper_resolved", detail=name)
    return AssertionEvidence(ok=False, reason="helper_no_primitive", detail=name)


#: An ASSIGNMENT binding a local from an expression: ``const body = …`` /
#: ``let x = …`` / ``var y = …`` (TS/JS) and a bare ``name = …`` (Python). Captures
#: the bound NAME(s) (lhs) and the RHS expression so a local derived from a
#: parameter can be added to the anchor set. ``==`` / ``=>`` / ``<=`` / ``>=`` /
#: ``!=`` are excluded (comparisons / arrows, not bindings). Single-name binds are
#: matched anywhere a line allows; DESTRUCTURING (``const { a, b } = …`` /
#: ``const [x] = …``) requires a leading ``const``/``let``/``var`` keyword and may
#: not span a newline — this is what stops the lhs ``{…}`` alternative from
#: swallowing the FUNCTION BODY's own opening brace.
_ASSIGN_RE = re.compile(
    r"^[ \t]*(?:"
    r"(?:const|let|var)\s+(?P<destructure>\{[^}\n]*\}|\[[^\]\n]*\])"
    r"|(?:(?:const|let|var)\s+)?(?P<name>[A-Za-z_$][\w$]*)"
    r")\s*=(?![=>])\s*(?P<rhs>.+)$",
    re.MULTILINE,
)
#: Derivation hops: ``a = f(param); b = g(a); expect(b…)`` resolves with 2 passes.
_MAX_DERIVE_PASSES = 2

#: An identifier token (TS/JS ``$``-bearing names and python names alike).
_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")


def _rhs_reference_idents(rhs: str) -> set[str] | None:
    """Genuine variable REFERENCES on an assignment RHS — fails CLOSED.

    The naive ``re.findall(IDENT, rhs)`` (pre-2.48) harvested every identifier
    on the RHS, INCLUDING those inside string literals, comments, and object
    KEYS — a false-GREEN: ``const body = "response"`` (string), ``const x =
    { response: 1 }`` (key), or ``const body = compute(); // response`` (comment)
    all wrongly marked the lhs param-derived, so a constant-only helper that
    merely *contains* that binding looked anchored to the call's data.

    This is a small DETERMINISTIC char scanner (no LLM, no new deps). It returns
    only names that are genuine references:

    * identifiers inside ``'…'`` / ``"…"`` / ``\\`…\\``` strings (backslash escapes
      honoured) and inside ``// line`` / ``/* block */`` comments are EXCLUDED;
    * an object-literal KEY (``{ key: … }`` — an identifier immediately followed
      by ``:`` while inside ``{…}``) is NOT a reference (its VALUE still is);
    * a PROPERTY name after a ``.`` member access (``foo.response``) is NOT an
      independent reference (the base ``foo`` still is).

    Returns ``None`` when the RHS cannot be confidently scanned (an unterminated
    string or block comment) — the caller then declines to add the lhs to the
    derived set (FAIL-CLOSED ⇒ toward false-RED, never false-GREEN).
    """

    refs: set[str] = set()
    i = 0
    n = len(rhs)
    # Brace-depth of OBJECT LITERALS in this RHS expression: in expression
    # position ``{`` opens an object literal, so an ``ident :`` at depth > 0 is a
    # key. Tracking depth keeps a top-level ternary ``cond ? a : b`` (depth 0)
    # from having its branches mis-read as keys (which would only ever fail
    # closed, but needlessly).
    brace_depth = 0
    while i < n:
        ch = rhs[i]
        # ── string literals: skip to the matching unescaped quote ──
        if ch == "'" or ch == '"' or ch == "`":
            quote = ch
            i += 1
            closed = False
            while i < n:
                c = rhs[i]
                if c == "\\":  # escape — skip the next char
                    i += 2
                    continue
                if c == quote:
                    i += 1
                    closed = True
                    break
                i += 1
            if not closed:
                return None  # unterminated string → cannot scan → fail closed
            continue
        # ── comments: ``// …`` to EOL, ``/* … */`` block ──
        if ch == "/" and i + 1 < n and rhs[i + 1] == "/":
            while i < n and rhs[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and rhs[i + 1] == "*":
            end = rhs.find("*/", i + 2)
            if end == -1:
                return None  # unterminated block comment → fail closed
            i = end + 2
            continue
        # ── object-literal brace tracking ──
        if ch == "{":
            brace_depth += 1
            i += 1
            continue
        if ch == "}":
            if brace_depth > 0:
                brace_depth -= 1
            i += 1
            continue
        # ── identifier ──
        if ch.isalpha() or ch == "_" or ch == "$":
            m = _IDENT_RE.match(rhs, i)
            assert m is not None  # ch starts a valid identifier
            name = m.group(0)
            start, j = m.start(), m.end()
            # PROPERTY after a ``.`` member access? walk back over whitespace.
            k = start - 1
            while k >= 0 and rhs[k] in " \t":
                k -= 1
            is_property = k >= 0 and rhs[k] == "."
            # OBJECT-LITERAL KEY? immediately followed by ``:`` while inside a
            # ``{…}`` (and not ``::``, which is not a key separator).
            p = j
            while p < n and rhs[p] in " \t":
                p += 1
            is_key = (
                brace_depth > 0
                and p < n
                and rhs[p] == ":"
                and not (p + 1 < n and rhs[p + 1] == ":")
            )
            if not is_property and not is_key:
                refs.add(name)
            i = j
            continue
        i += 1
    return refs


def _params_and_derived_locals(body: str, param_set: set[str]) -> set[str]:
    """``param_set`` plus locals DERIVED from a parameter within ``body`` (≤2 hops).

    The argument anchor must credit ``const body = await readJson(response);
    expect(body.error)…`` — the assertion references ``body``, a local that FLOWS
    from the ``response`` parameter, so it is genuinely anchored to the call's data
    (root-cause-#2: the 2.31.0 anchor only saw a DIRECT param reference and wrongly
    rejected param-derived locals). A binding ``X = <rhs>`` adds ``X`` to the set
    iff its RHS references something already in the set (a param or an
    already-derived local). Two passes follow a short ``param → a → b`` chain.
    Constants never enter the set (``const k = 5`` references no param), so a
    constant-only helper stays unanchored ⇒ FAIL. Destructured bindings
    (``const { error } = body``) add each bound name when the RHS is derived.

    The RHS is scanned by :func:`_rhs_reference_idents`, which counts only
    genuine variable references: identifiers inside STRING LITERALS, COMMENTS, and
    object KEYS do NOT flow a param (``const body = "response"`` does not derive
    ``body`` from a ``response`` param). When an RHS cannot be confidently scanned
    (unterminated string/comment) the lhs is NOT added — FAIL-CLOSED, never
    crediting fake coverage.
    """

    if not param_set:
        return set()
    # A TestCase passed as a parameter (``def helper(testcase, ...): testcase.assertEqual(...)``)
    # is the assertion API ``器``, NOT an observed value — exclude it from the anchor so a
    # constant-only helper (``testcase.assertEqual(1, 1)``) stays unanchored ⇒ FAIL. No-op
    # for TS / non-unittest bodies (no ``<recv>.assertX`` calls). Cross-language: the
    # detector only matches the unittest assert-method form, so other stacks are untouched.
    param_set = param_set - _testcase_assert_receivers_in(body)
    if not param_set:
        return set()
    derived = set(param_set)
    for _ in range(_MAX_DERIVE_PASSES):
        grew = False
        for match in _ASSIGN_RE.finditer(body):
            rhs_idents = _rhs_reference_idents(match.group("rhs"))
            if rhs_idents is None:
                continue  # RHS not confidently scannable → fail closed
            if not (rhs_idents & derived):
                continue
            lhs = match.group("destructure") or match.group("name") or ""
            for lhs_name in _IDENT_RE.findall(lhs):
                if lhs_name not in derived:
                    derived.add(lhs_name)
                    grew = True
        if not grew:
            break
    return derived


def _body_references_params(
    body: str, param_set: set[str], primitive_re: re.Pattern[str]
) -> bool:
    """A primitive assertion in ``body`` references one of ``param_set``.

    The "argument anchor": a primitive assertion must mention a parameter (or a
    value derived from it — ``param_set`` is pre-expanded with param-derived locals
    by :func:`_params_and_derived_locals` at the call site), so a no-op helper that
    asserts only a CONSTANT (``expect(true).toBe(true)`` / ``assert True``) and
    merely *names* a param elsewhere is NOT anchored. The check is per
    primitive-assertion STATEMENT, where a statement is the assertion line PLUS any
    following lines indented MORE than it (its block) — so a context-manager
    assertion (``with pytest.raises(...):\\n    fn()``) is anchored by the ``fn()``
    call in its block, while a trailing same-indent ``log(result)`` after a
    constant ``expect(true)`` is not.
    """

    if not param_set:
        return False
    lines = body.splitlines()
    total = len(lines)
    for idx, line in enumerate(lines):
        if not primitive_re.search(line):
            continue
        base_indent = len(line[: len(line) - len(line.lstrip())].expandtabs())
        window = [line]
        scan = idx + 1
        while scan < total:
            nxt = lines[scan]
            if not nxt.strip():
                scan += 1
                continue
            nxt_indent = len(nxt[: len(nxt) - len(nxt.lstrip())].expandtabs())
            if nxt_indent <= base_indent:
                break  # back to the assertion's own level — block ended.
            window.append(nxt)
            scan += 1
        idents: set[str] = set()
        for w in window:
            idents |= set(re.findall(r"[A-Za-z_$][\w$]*", w))
        if idents & param_set:
            return True
    return False


def _resolve_typescript_evidence(
    block: TestBlock, *, importer_text: str, importer_rel: str, project_root: Path
) -> AssertionEvidence:
    def _mod_resolver(rel: str, spec: str, root: Path) -> Path | None:
        return _ts_resolve_specifier(rel, spec, root)

    return _resolve_evidence(
        block,
        importer_text=importer_text,
        importer_rel=importer_rel,
        project_root=project_root,
        primitive_re=_TS_PRIMITIVE_ASSERT_RE,
        imported_lookup=_ts_imported_specifier,
        module_resolver=_mod_resolver,
        def_finder=_ts_find_function_def,
        reexport_edges=_ts_reexport_edges,
    )


def _resolve_python_evidence(
    block: TestBlock, *, importer_text: str, importer_rel: str, project_root: Path
) -> AssertionEvidence:
    def _mod_resolver(rel: str, mod: str, root: Path) -> Path | None:
        return _py_resolve_module(rel, mod, root)

    return _resolve_evidence(
        block,
        importer_text=importer_text,
        importer_rel=importer_rel,
        project_root=project_root,
        primitive_re=_PY_PRIMITIVE_ASSERT_RE,
        imported_lookup=_py_imported_module,
        module_resolver=_mod_resolver,
        def_finder=_py_find_function_def,
        reexport_edges=_py_reexport_edges,
    )


# ---------------------------------------------------------------------------
# Java (JUnit 4/5 + Hamcrest/AssertJ) structural adapter
#
# Modeled cell-for-cell on :class:`GoTestBlockProfile`. A test block is a JUnit
# test METHOD — a ``[modifiers] void name(...) [throws ...] { ... }`` whose
# leading annotation run contains ``@Test`` (JUnit5 ``org.junit.jupiter.api.Test``
# or JUnit4 ``org.junit.Test``). The body is brace-matched with the SAME
# string-aware :func:`_go_match_brace` the Go adapter uses (braces/strings are
# language-agnostic), and comments are stripped with :func:`_go_strip_comments`
# (Java uses ``//`` + ``/* */`` — identical to Go, verified to preserve string
# literals + offsets), so a fake assertion written in a COMMENT never counts
# (the false-GREEN guard). Skip is the ``@Disabled`` (JUnit5) / ``@Ignore``
# (JUnit4) annotation on the method, OR a body that unconditionally aborts via
# ``Assumptions.abort(...)`` / ``Assumptions.assumeTrue(false)`` — a skipped test
# proves nothing. A PRIMITIVE assertion is a JUnit/Hamcrest/AssertJ assertion
# call (``assertEquals(`` / ``assertTrue(`` / ``assertThat(`` / ``fail(`` / …);
# a bare named-helper call (``checkResult(x)``) is NOT primitive — it is resolved
# one hop via :meth:`resolve_assertion_evidence`. Constant-only-ness is decided
# from the ARGUMENT text (``_go_reference_idents`` minus an ignored set), NOT the
# method name, exactly like the Go testify path, so a permissive primitive match
# never false-GREENs (``assertEquals(1, 1)`` is constant-only; ``assertEquals(5,
# add(2, 3))`` references ``add`` ⇒ real). The reused helpers (``_go_match_brace``,
# ``_go_strip_comments``, ``_balanced_args``, ``_go_reference_idents``) are all
# language-agnostic (brace matching / comment stripping / identifier extraction),
# so no Java-specific re-implementation is needed for them.
# ---------------------------------------------------------------------------


#: Java JUnit/Hamcrest/AssertJ PRIMITIVE assertion call names. A DIRECT primitive
#: is one of these tokens followed by ``(`` (word-boundary anchored so a SUT method
#: ``myAssertEquals(`` or ``doFail(`` is not mistaken for ``assertEquals``/``fail``).
#: ``assertThat(`` covers both Hamcrest (``assertThat(x, is(y))``) AND AssertJ
#: (``assertThat(x).isEqualTo(y)``) — the fluent tail is part of the same statement,
#: so the single ``assertThat(`` token suffices. Constant-only-ness is judged from
#: the args downstream, so a permissive name list never false-GREENs.
_JAVA_PRIMITIVE_ASSERT_NAMES = (
    "assertEquals",
    "assertNotEquals",
    "assertTrue",
    "assertFalse",
    "assertNull",
    "assertNotNull",
    "assertSame",
    "assertNotSame",
    "assertArrayEquals",
    "assertThrows",
    "assertThrowsExactly",
    "assertDoesNotThrow",
    "assertTimeout",
    "assertIterableEquals",
    "assertLinesMatch",
    "assertAll",
    "assertThat",
    "fail",
)

#: One regex matching ANY Java primitive assertion CALL (name + ``(``). The
#: ``(?<![A-Za-z0-9_.])`` guard means the name must not be the tail of a longer
#: identifier or a member-select (``obj.fail(`` / ``myassertTrue(`` do not match),
#: mirroring the Go receiver-anchoring discipline. Built once at import.
_JAVA_PRIMITIVE_ASSERT_RE = re.compile(
    r"(?<![A-Za-z0-9_.])(?:" + "|".join(_JAVA_PRIMITIVE_ASSERT_NAMES) + r")\s*\("
)

#: Helper-body primitive detector for Java (used by the shared evidence engine's
#: ``primitive_re`` when resolving a delegated-assertion helper). SAME surface as
#: the direct primitive regex — a helper whose body runs a real ``assertEquals(``
#: + an argument anchor passes; a no-op / constant helper fails. (Java's
#: assertion calls are STATIC imports, not receiver methods, so unlike Go there is
#: no receiver to rename — the name set is stable across direct and helper sites.)
_JAVA_HELPER_PRIMITIVE_RE = _JAVA_PRIMITIVE_ASSERT_RE

#: A JUnit ``@Test`` annotation (JUnit5 ``org.junit.jupiter.api.Test`` or JUnit4
#: ``org.junit.Test``; possibly fully-qualified ``@org.junit.jupiter.api.Test``).
#: A trailing ``(`` would be a different annotation (``@TestFactory`` is its own
#: word; ``@TestInstance(...)`` is excluded by requiring NOT-an-identifier-char
#: after ``Test``). Word-boundary so ``@TestFactory`` / ``@TestTemplate`` do not
#: match (those are not plain ``@Test`` methods).
_JAVA_TEST_ANNOTATION_RE = re.compile(
    r"@(?:[A-Za-z_][A-Za-z0-9_]*\.)*Test(?![A-Za-z0-9_])"
)

#: Skip annotations: JUnit5 ``@Disabled`` / JUnit4 ``@Ignore`` (optionally
#: fully-qualified, optionally with a ``("reason")`` arg). A method carrying one is
#: not executable — a skipped test asserts nothing (stage-2 attachment failure).
_JAVA_SKIP_ANNOTATION_RE = re.compile(
    r"@(?:[A-Za-z_][A-Za-z0-9_]*\.)*(?:Disabled|Ignore)(?![A-Za-z0-9_])"
)

#: Body-level unconditional skip: ``Assumptions.abort(...)`` / a constant
#: ``assumeTrue(false)`` / ``assumeFalse(true)`` aborts the test at runtime, so a
#: marker on it is not coverage. Best-effort (the PRIMARY skip signal is the
#: ``@Disabled``/``@Ignore`` annotation); these only ADD detection, never remove.
_JAVA_BODY_SKIP_RE = re.compile(
    r"(?<![A-Za-z0-9_.])(?:Assumptions\s*\.\s*)?abort\s*\("
    r"|(?<![A-Za-z0-9_.])(?:Assumptions\s*\.\s*)?assumeTrue\s*\(\s*false\s*\)"
    r"|(?<![A-Za-z0-9_.])(?:Assumptions\s*\.\s*)?assumeFalse\s*\(\s*true\s*\)"
)

#: A Java test-method SIGNATURE: optional modifiers, ``void``, a method name, a
#: parameter list, an optional ``throws`` clause, then the body-opening ``{``. We
#: do NOT require ``public`` (JUnit5 tolerates package-private test methods). The
#: return type is constrained to ``void`` — JUnit test methods return void (a
#: non-void ``@TestFactory`` / parameterized provider is intentionally out of
#: scope; those are a deliberately-unbuilt extension, mirroring Go's flat-func
#: focus). ``name`` is captured for the block label; the match ends just before
#: the body ``{`` so brace-matching starts there.
_JAVA_TEST_METHOD_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:(?:public|protected|private|static|final|synchronized|"
    r"abstract|default)\s+)*void\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\([^;{}]*\)"
    r"(?:\s*throws\s+[A-Za-z0-9_.,\s<>]+?)?\s*\{",
    re.DOTALL,
)

#: Java reserved words / literals / obvious builtins + the assertion METHOD names
#: that are NOT credit-worthy references inside an assertion's argument list — the
#: Java analogue of :data:`_GO_IGNORED_NAMES`. A SUT call name (``add``), a local
#: variable (``got``/``want``), an expected constant a test declares are NEVER
#: here, so ``assertEquals(5, add(2, 3))`` (references ``add``) stays GREEN while
#: ``assertEquals(1, 1)`` / ``assertTrue(true)`` (only ignored tokens + literals)
#: become RED. The assertion method names themselves are ignored so a primitive's
#: OWN callee name never anchors it (``assertTrue(true)`` must not credit via
#: ``assertTrue``); Hamcrest/AssertJ matcher/fluent words (``is``/``equalTo``/
#: ``isEqualTo``/``containsExactly``/…) are NOT listed — a matcher applied to a
#: real value still anchors on that value, and a matcher applied to a literal is
#: still constant (the matcher words are dropped as selector fields after ``.`` by
#: :func:`_go_reference_idents`, or are themselves constant-only callees).
_JAVA_IGNORED_NAMES = frozenset(
    {
        # literals / keywords
        "true",
        "false",
        "null",
        "this",
        "super",
        "new",
        "void",
        "int",
        "long",
        "short",
        "byte",
        "char",
        "boolean",
        "float",
        "double",
        "String",
        "Integer",
        "Long",
        "Short",
        "Byte",
        "Character",
        "Boolean",
        "Float",
        "Double",
        "Object",
        "Class",
        "if",
        "else",
        "for",
        "while",
        "do",
        "return",
        "throw",
        "throws",
        "try",
        "catch",
        "finally",
        "switch",
        "case",
        "default",
        "break",
        "continue",
        "instanceof",
        "var",
        "final",
        "static",
        "public",
        "private",
        "protected",
        "class",
        "interface",
        "enum",
        "extends",
        "implements",
        "import",
        "package",
        # assertion API method names (the callee is the 器, not an observation)
        *_JAVA_PRIMITIVE_ASSERT_NAMES,
        # the conventional JUnit assertion entry classes (a fully-qualified call
        # ``Assertions.assertEquals(1, 1)`` must not anchor on ``Assertions``).
        "Assertions",
        "Assert",
        "Assumptions",
        "Assume",
        "MatcherAssert",
    }
)


def _java_method_blocks(text: str) -> "list[tuple[int, int, str, str, bool]]":
    """Locate every JUnit ``@Test`` method in ``text``.

    Returns a list of ``(start_line, end_line, name, body_inner, is_executable)``
    tuples (1-based inclusive line bounds). A method qualifies when its LEADING
    ANNOTATION RUN — the contiguous run of ``@Annotation`` / comment / blank lines
    immediately above its signature — contains ``@Test`` (the annotation may sit
    several lines above the signature with ``@DisplayName(...)`` / other
    annotations in between, all tolerated). ``start_line`` is the line of the FIRST
    annotation in that run (so a coverage marker placed immediately ABOVE the
    ``@Test`` annotation attaches to this block via the gate's ``_attached_block``,
    whose walk treats ``@`` / comment / blank lines as header lines). The body is
    brace-matched with the string-aware :func:`_go_match_brace`. ``is_executable``
    is False when a ``@Disabled``/``@Ignore`` annotation is in the run OR the body
    unconditionally aborts. PURE + best-effort: a signature whose body cannot be
    brace-matched is skipped (never raises).
    """

    def _line_of(pos: int) -> int:
        return text.count("\n", 0, pos) + 1

    # Comment-stripped skeleton for STRUCTURE detection (so an ``@Test`` or a
    # ``void foo() {`` written inside a COMMENT/string is not read as a real
    # method — the false-GREEN guard). Offsets are preserved (comments → spaces),
    # so positions map back onto the original ``text`` for line math + brace match.
    skeleton = _go_strip_comments(text)

    out: list[tuple[int, int, str, str, bool]] = []
    for m in _JAVA_TEST_METHOD_RE.finditer(skeleton):
        brace = m.end() - 1  # the signature regex ends ON the body-opening ``{``.
        close = _go_match_brace(text, brace)
        if close < 0:
            continue  # unbalanced body ⇒ skip (degrade), never raise.
        name = m.group("name")
        if name in {"main"}:
            continue  # not a test method.

        # ── Leading annotation run: walk UP from the signature over @ / comment /
        # blank lines, collecting the annotation text, until a real statement /
        # another method / a brace. The run must contain ``@Test`` to qualify.
        sig_line = _line_of(m.start())
        lines = text.splitlines()
        run_lines: list[str] = []
        first_line = sig_line
        ln = sig_line - 1  # 1-based line ABOVE the signature line
        while ln >= 1:
            raw = lines[ln - 1] if (ln - 1) < len(lines) else ""
            stripped = raw.strip()
            if not stripped:
                ln -= 1
                first_line = ln + 1  # blank lines are part of the header run
                continue
            if stripped.startswith("@") or stripped.startswith(
                ("//", "/*", "*", "*/")
            ):
                run_lines.append(stripped)
                first_line = ln
                ln -= 1
                continue
            break  # a real statement / brace terminates the annotation run.
        annotation_blob = "\n".join(reversed(run_lines))
        # The signature line itself may carry inline annotations before ``void``
        # (``@Test void foo()`` on one line) — include the pre-signature slice.
        pre_sig = skeleton[m.start():m.start() + (m.start("name") - m.start())]
        annotation_scope = annotation_blob + "\n" + pre_sig

        if not _JAVA_TEST_ANNOTATION_RE.search(annotation_scope):
            continue  # not a ``@Test`` method.

        body_inner = text[brace + 1 : close]
        skip_skeleton = _go_strip_comments(body_inner)
        is_executable = not (
            _JAVA_SKIP_ANNOTATION_RE.search(annotation_scope)
            or _JAVA_BODY_SKIP_RE.search(skip_skeleton)
        )
        out.append(
            (
                first_line,
                _line_of(close),
                name,
                body_inner,
                is_executable,
            )
        )
    return out


def _java_language_profile() -> Any:
    """Resolve the bundled ``java.yaml`` :class:`~codd.languages.profile.LanguageProfile`.

    Self-contained: :mod:`codd.languages.registry` (and its ``loader``/``profile``
    dependencies) never import this module, so this one-way import carries no
    cycle risk — unlike ``codd.project_types``, which imports THIS module lazily
    and therefore must never be imported back from here. ``"java"`` is hardcoded
    because this helper is itself Java-specific, exactly like
    :data:`_JAVA_PRIMITIVE_ASSERT_NAMES`. Returns ``None`` on any resolution
    failure (missing/broken YAML, package unavailable) so every caller degrades
    to its hardcoded fallback rather than raising — this adapter stays
    best-effort, never raising, like the rest of this module.
    """

    try:
        from codd.languages.registry import default_registry

        return default_registry.resolve("java")
    except Exception:  # noqa: BLE001 — resolution is best-effort; never raise.
        return None


def _java_assertion_hints() -> Mapping[str, Any]:
    """The active java profile's ``tests.assertion_hints`` mapping (or ``{}``)."""

    profile = _java_language_profile()
    tests = getattr(profile, "tests", None) if profile is not None else None
    hints = getattr(tests, "assertion_hints", None) if tests is not None else None
    return hints if isinstance(hints, Mapping) else {}


def _java_entry_classes() -> frozenset[str]:
    """Declared qualified-call ENTRY CLASSES across every ``assertion_hints`` library.

    Generic over the ``assertion_hints`` SHAPE — a mapping of library name to
    ``{import_path, assertion_methods, ..., entry_classes: [...]}``, plus the
    sibling ``library_assertion_terminals`` LIST (skipped here because it is a
    list, not a mapping — never by checking its key NAME). No library name
    (``junit_jupiter``, ...) is ever hardcoded; only the STRUCTURE (a mapping
    value's own ``entry_classes`` list) is read. A qualified call
    ``<EntryClass>.<assertMethod>(`` is recognized as a Java primitive assertion
    iff ``<EntryClass>`` is one of these declared names (see
    :func:`_java_qualified_primitive_re`) — this is what makes
    ``Assertions.assertEquals(...)`` (bound by a PLAIN, non-static import)
    resolve as a DIRECT primitive instead of an unresolved helper call.
    """

    classes: set[str] = set()
    for value in _java_assertion_hints().values():
        if not isinstance(value, Mapping):
            continue
        raw = value.get("entry_classes")
        if not raw:
            continue
        classes.update(str(c) for c in raw if str(c).strip())
    return frozenset(classes)


def _java_qualified_primitive_re(entry_classes: frozenset[str]) -> re.Pattern[str] | None:
    """A ``<EntryClass>.<assertMethod>(`` matcher built from DECLARED ``entry_classes``.

    ``None`` when no entry classes are declared (nothing to match — the profile
    opted out, or could not be resolved). Built fresh from the DATA each call
    (cheap: a handful of names; ``re.compile`` itself caches identical pattern
    strings) rather than a module-level constant, since ``entry_classes`` is
    profile/YAML-driven, not a Python literal.
    """

    if not entry_classes:
        return None
    classes_alt = "|".join(re.escape(c) for c in sorted(entry_classes))
    names_alt = "|".join(_JAVA_PRIMITIVE_ASSERT_NAMES)
    return re.compile(rf"(?<![A-Za-z0-9_.])(?:{classes_alt})\.(?:{names_alt})\s*\(")


def _java_body_has_primitive_assertion(body_text: str) -> bool:
    """Whether ``body_text`` contains a Java PRIMITIVE assertion (lexical).

    A primitive is a JUnit/Hamcrest/AssertJ assertion call (``assertEquals(`` /
    ``assertThat(`` / ``fail(`` / …) OR a QUALIFIED call on a declared
    ``entry_classes`` name (``Assertions.assertEquals(...)`` bound by a plain,
    non-static import — see :func:`_java_qualified_primitive_re`). A bare
    named-helper call (``checkResult(x)``) is NOT primitive — that is resolved
    via the evidence graph. Comments are stripped first (``_go_strip_comments``)
    so an assertion written in a COMMENT is not counted (false-GREEN guard).
    """

    skeleton = _go_strip_comments(body_text)
    if _JAVA_PRIMITIVE_ASSERT_RE.search(skeleton):
        return True
    qualified_re = _java_qualified_primitive_re(_java_entry_classes())
    return bool(qualified_re and qualified_re.search(skeleton))


def _java_direct_assertion_evidence(body_text: str) -> AssertionEvidence:
    """Verdict: does ``body_text`` carry a NON-constant Java primitive assertion?

    Mirrors :func:`_go_direct_assertion_evidence` (the testify ``value args``
    branch): for each primitive assertion call — UNQUALIFIED
    (``_JAVA_PRIMITIVE_ASSERT_RE``) OR QUALIFIED on a declared ``entry_classes``
    name (``_java_qualified_primitive_re``; e.g. ``Assertions.assertEquals(...)``
    with only a plain, non-static ``import org.junit.jupiter.api.Assertions;``)
    — in the (comment-stripped) body, extract its argument text via
    :func:`_balanced_args` and decide REAL vs CONSTANT-only by whether the args
    reference a NON-ignored identifier (``_go_reference_idents(args) -
    _JAVA_IGNORED_NAMES`` — ``Assertions``/``Assert``/… are already ignored names,
    so a qualifier never self-anchors). ``assertEquals(5, add(2, 3))`` /
    ``Assertions.assertEquals(5, add(2, 3))`` references ``add`` ⇒ REAL ⇒
    ``direct`` (ok). ``assertEquals(1, 1)`` / ``assertTrue(true)`` reference only
    literals/ignored names ⇒ CONSTANT-only. If ANY primitive is REAL ⇒ ``direct``
    (ok). If at least one primitive was seen and ALL are constant-only ⇒
    ``constant_direct`` (not ok). If the regex matched but this scanner
    classifies no primitive ⇒ fail OPEN (``direct``), never a false-RED —
    exactly the Go contract. Called by the gate only when ``block.has_assertion``
    is True.
    """

    # Comment-stripped SKELETON so an assertion / arg written in a COMMENT is not
    # read as real code. Offsets preserved, so ``_balanced_args`` positions align.
    skeleton = _go_strip_comments(body_text)
    saw_primitive = False
    for pattern in (
        _JAVA_PRIMITIVE_ASSERT_RE,
        _java_qualified_primitive_re(_java_entry_classes()),
    ):
        if pattern is None:
            continue
        for m in pattern.finditer(skeleton):
            saw_primitive = True
            args = _balanced_args(skeleton, m.end() - 1)
            if _go_reference_idents(args) - _JAVA_IGNORED_NAMES:
                return AssertionEvidence(ok=True, reason="direct")
    if not saw_primitive:
        # ``has_assertion`` matched but no primitive classified here → fail OPEN.
        return AssertionEvidence(ok=True, reason="direct")
    return AssertionEvidence(ok=False, reason="constant_direct")


@dataclass(frozen=True)
class JavaTestBlockProfile:
    """Java (JUnit 4/5 + Hamcrest/AssertJ) structural adapter.

    A test block is a JUnit test METHOD — a ``[modifiers] void name(...) { ... }``
    whose leading annotation run contains ``@Test`` (JUnit5
    ``org.junit.jupiter.api.Test`` or JUnit4 ``org.junit.Test``). The body is
    brace-matched with the string-aware :func:`_go_match_brace`. Skip is a
    ``@Disabled`` (JUnit5) / ``@Ignore`` (JUnit4) annotation on the method, or a
    body that unconditionally aborts via ``Assumptions.abort(...)`` /
    ``assumeTrue(false)``. A PRIMITIVE assertion is a JUnit/Hamcrest/AssertJ
    assertion call (``assertEquals(`` / ``assertThat(`` / ``fail(`` / …) — either
    UNQUALIFIED (bound by a static import) or QUALIFIED on a declared
    ``entry_classes`` name (``Assertions.assertEquals(...)``); a bare named-helper
    call is resolved one hop through the shared engine
    (:func:`_resolve_java_evidence`) via :meth:`resolve_assertion_evidence`, with
    a data-driven fallback for a DECLARED library fluent terminal (e.g. ArchUnit's
    ``.check(classes)`` — see ``assertion_hints.library_assertion_terminals``).

    Java methods are NOT nested the way Go subtests / TS ``describe``→``it`` are
    (a ``@Test`` method cannot contain another ``@Test`` method), so EVERY block
    this adapter emits is a LEAF coverage target — there is no group→leaf fan-out
    to model (the one structural simplification vs the Go template). PURE +
    best-effort: a parse that cannot be done returns ``[]`` (the gate then
    degrades for that file), never raises.
    """

    def handles_file(self, rel_path: str) -> bool:
        """Whether ``rel_path`` is a Java test file.

        True for a ``.java`` file that is EITHER under a Maven test source root
        (``src/test/java``) OR whose basename signals a test class
        (``*Test.java`` / ``*Tests.java`` / ``*IT.java``). The basename rule (not
        just the dir) is what makes the conformance fixture ``tests/XTest.java``
        — written OUTSIDE ``src/test/java`` by the harness — recognized. A
        non-test ``Main.java`` under no test root returns False (the gate then
        degrades rather than parsing a production file for coverage markers).
        """

        if not rel_path.endswith(".java"):
            return False
        norm = rel_path.replace("\\", "/")
        if "src/test/java/" in norm or norm.startswith("src/test/java/"):
            return True
        base = norm.rsplit("/", 1)[-1]
        stem = base[: -len(".java")]
        return stem.endswith("Test") or stem.endswith("Tests") or stem.endswith("IT")

    def parse_test_blocks(self, text: str) -> list[TestBlock]:
        """Parse ``text`` into one :class:`TestBlock` per JUnit ``@Test`` method."""

        blocks: list[TestBlock] = []
        for start_line, end_line, name, body_inner, is_executable in _java_method_blocks(
            text
        ):
            blocks.append(
                TestBlock(
                    start_line=start_line,
                    end_line=end_line,
                    is_executable=is_executable,
                    has_assertion=_java_body_has_primitive_assertion(body_inner),
                    label=name,
                    body_text=body_inner,
                )
            )
        # Document order (start_line), like the Go/TS/PY adapters, so the gate's
        # smallest-containing / nearest-after attachment scans behave identically.
        return sorted(blocks, key=lambda b: (b.start_line, -b.end_line))

    def resolve_direct_assertion_evidence(
        self,
        block: TestBlock,
        *,
        importer_text: str = "",
        importer_rel: str = "",
        project_root: Path | None = None,
        config: dict[str, Any] | None = None,
        profile: Any = None,
    ) -> AssertionEvidence:
        """Whether the block's DIRECT Java primitive assertion references a real name.

        ``constant_direct`` when every primitive is constant-only
        (``assertTrue(true)`` / ``assertEquals(1, 1)``); ``direct`` when an
        assertion's args reference a non-constant (``assertEquals(5, add(2, 3))``
        references ``add``). Delegates to :func:`_java_direct_assertion_evidence`.
        Called by the gate only when ``block.has_assertion`` is True.
        """

        return _java_direct_assertion_evidence(block.body_text)

    def resolve_assertion_evidence(
        self, block: TestBlock, *, importer_text: str, importer_rel: str, project_root: Path
    ) -> AssertionEvidence:
        """Resolve a block's DELEGATED-assertion (helper) evidence.

        Called by the gate ONLY for an attached, executable block with NO direct
        primitive assertion (``block.has_assertion`` is False). Delegates to
        :func:`_resolve_java_evidence` — the shared, language-free 1-hop helper-
        resolution engine (:func:`_resolve_evidence`) plugged with THIN Java
        adapters (static-import lookup, module resolution that tries the
        project's OWN declared ``scan.test_dirs``/``source_dirs`` roots first
        (and up to two subdirectory levels beneath them) and the conventional
        Maven ``src/test/java``/``src/main/java`` roots as a fallback, a
        same-package qualified-call fallback that searches the importer's own
        directory and then one subdirectory level down when no import at all
        names the helper), with a data-driven fallback to a
        DECLARED library fluent-terminal credit
        (``assertion_hints.library_assertion_terminals``) when the helper-hop
        engine still cannot resolve the call.
        """

        return _resolve_java_evidence(
            block, importer_text=importer_text, importer_rel=importer_rel, project_root=project_root
        )


# ---------------------------------------------------------------------------
# Java helper-resolution plug-ins (E1/E2-extended shared engine, THIN adapters).
#
# Java gets NO new engine — it reuses :func:`_resolve_evidence` /
# :func:`_resolve_one_helper` exactly like Go/TS/Python, supplying only its own
# regexes/lexical scanners, following the SAME naming convention as Go's
# ``_go_imported_module`` / ``_go_resolve_module`` / ``_go_find_function_def``
# (this section's template/precedent). Brace-matching and comment-stripping are
# REUSED verbatim from Go's utilities (:func:`_go_match_brace`,
# :func:`_go_strip_comments`, :func:`_balanced_args`, :func:`_go_reference_idents`)
# rather than duplicated — the same discipline the C# adapter's own docstrings
# already argue for.
# ---------------------------------------------------------------------------

#: ``import static a.b.C.sym;`` — binds the bare LEAF ``sym`` to the class ``a.b.C``.
_JAVA_IMPORT_STATIC_NAMED_RE = re.compile(
    r"^\s*import\s+static\s+(?P<fqcn>[\w.]+)\.(?P<sym>[A-Za-z_][A-Za-z0-9_]*)\s*;",
    re.MULTILINE,
)
#: ``import static a.b.C.*;`` — binds EVERY unqualified call name to ``a.b.C``.
_JAVA_IMPORT_STATIC_STAR_RE = re.compile(
    r"^\s*import\s+static\s+(?P<fqcn>[\w.]+)\.\*\s*;",
    re.MULTILINE,
)


def _java_imported_lookup(importer_text: str, symbol: str, full_callee: str = "") -> str | None:
    """Java per-symbol import binding for the shared evidence engine (E1-aware).

    Only a STATIC import binds a bare symbol to a class the engine can then
    resolve to a file via :func:`_java_resolve_module`:

    * ``import static a.b.C.sym;`` binds ``sym`` (exact match) to ``a.b.C``.
    * ``import static a.b.C.*;`` binds EVERY unqualified name to ``a.b.C`` —
      best-effort: the FIRST star import is tried (mirrors every other
      ``imported_lookup`` here returning a single spec; several star imports
      each naming a different class is a documented, acceptable residual).

    A PLAIN ``import a.b.C;`` (non-static) binds NOTHING for a static-call
    lookup — Java requires the ``C.sym`` receiver shape for that, which this
    per-symbol table cannot express (it is keyed on the bare leaf regardless of
    ``full_callee``). That case — and the common same-package case where
    ``C.sym`` needs NO import at all — falls through to same-file search and
    then :func:`_java_fallback_candidates` (E2). ``full_callee`` is accepted
    (E1) but unused here for that reason.
    """

    for m in _JAVA_IMPORT_STATIC_NAMED_RE.finditer(importer_text):
        if m.group("sym") == symbol:
            return m.group("fqcn")
    for m in _JAVA_IMPORT_STATIC_STAR_RE.finditer(importer_text):
        return m.group("fqcn")  # best-effort: first star-import candidate.
    return None


#: Conventional Maven roots, used ONLY when the java profile itself cannot be
#: resolved (defensive fallback — :func:`_java_test_and_source_roots` prefers
#: the profile-driven ``layout.test_sets`` / ``layout.source_sets`` roots).
_JAVA_DEFAULT_TEST_ROOTS: tuple[str, ...] = ("src/test/java",)
_JAVA_DEFAULT_SOURCE_ROOTS: tuple[str, ...] = ("src/main/java",)


def _java_test_and_source_roots() -> tuple[str, ...]:
    """TEST source roots, then MAIN source roots, for FQCN → file resolution.

    PROFILE-DRIVEN: read from the resolved java :class:`LanguageProfile`'s
    ``layout.test_sets`` / ``layout.source_sets`` (never a hardcoded literal) —
    for the bundled ``java.yaml`` this is ``("src/test/java", "src/main/java")``,
    but a project-specific override to the profile would be honored too. Falls
    back to the conventional Maven paths only if the profile cannot be resolved
    at all (best-effort, matching this module's degrade-never-raise discipline).
    Test roots are tried FIRST — a same-package TEST helper is far more common
    than a helper living under ``main``.
    """

    profile = _java_language_profile()
    layout = getattr(profile, "layout", None) if profile is not None else None
    test_roots = tuple(
        str(ts.root) for ts in (getattr(layout, "test_sets", None) or ()) if getattr(ts, "root", None)
    ) or _JAVA_DEFAULT_TEST_ROOTS
    source_roots = tuple(
        str(ss.root) for ss in (getattr(layout, "source_sets", None) or ()) if getattr(ss, "root", None)
    ) or _JAVA_DEFAULT_SOURCE_ROOTS
    return test_roots + source_roots


#: Bound on how many directory levels beneath a project-declared scan root
#: _java_resolve_module will descend while looking for the point where a
#: FQCN's package path actually begins (the project's REAL Maven/Gradle-style
#: "source root"). Diagnosed by direct inspection of a live dogfood project
#: (/tmp/codd_greenfield_java_v2_ExprCalc): its codd.yaml declares
#: `scan.test_dirs: [tests/]`, but the E2E suite's actual Java sources begin
#: TWO levels further in (tests/e2e/java/com/...), not directly under
#: `tests/` as a bare declared-root+FQCN-suffix join (depth 0) requires.
#: Exactly this many levels, never unbounded: the same finite-hop discipline
#: as _JAVA_MAX_FALLBACK_CANDIDATES / _MAX_HELPER_HOPS elsewhere in this
#: module — a declared root nested deeper than this stays a deliberate,
#: documented unresolved residual, not silently accepted.
_JAVA_MAX_DECLARED_ROOT_DEPTH = 2


def _java_declared_root_search_dirs(root: Path, project_root: Path, max_depth: int) -> list[Path]:
    """``root`` itself, then its subdirectories, then THEIR subdirectories, ...
    up to ``max_depth`` levels down — the candidate "source root" directories
    :func:`_java_resolve_module` joins its FQCN-derived relative path onto.

    Ordered shallowest-first (``root`` itself, then EVERY depth-1 directory,
    then EVERY depth-2 directory, ...) so the caller's first hit is always the
    LEAST-inferred match — a direct depth-0 join (the pre-existing behavior)
    is always tried before any inferred deeper guess, mirroring the same
    "stronger signal first" precedence :func:`_java_fallback_candidates` (E2)
    already uses for its own same-directory-vs-subdirectory scan. No
    directory NAME is ever assumed (no "look for a directory literally called
    `java`" heuristic) — EVERY immediate subdirectory at each level is a
    candidate, exactly like E2's own unrestricted one-level glob.

    EVERY discovered child directory is individually re-resolved via
    :func:`resolve_project_path` against ``project_root`` (never against its
    own parent) at the moment it is discovered — BEFORE it is ever added to
    the returned list — so an in-root symlinked subdirectory whose target
    escapes the project is never followed, and never even reaches the caller
    as a later join base. This mirrors :func:`_java_fallback_candidates`'s own
    discipline exactly (it re-resolves each ``sub`` against ``project_root``
    before passing it on, rather than trusting a raw ``iterdir()`` result) —
    re-jailing a directory against its OWN unresolved self would be
    circular and provide no protection at all, since the caller
    (:func:`_java_resolve_module`) later uses each returned directory
    AS THE TRUST BASE for its own final ``resolve_project_path(directory,
    rel)`` join-check; only an ALREADY-verified-against-``project_root``
    directory is safe to hand back for that. A missing/unreadable directory
    at any level simply contributes no deeper candidates (``OSError``
    degrades to "nothing further here", never a crash); the walk then stops
    widening (there is nothing left to descend into) but keeps every
    shallower level already collected.
    """

    ordered: list[Path] = [root]
    level = [root]
    for _ in range(max_depth):
        next_level: list[Path] = []
        for directory in level:
            try:
                children = sorted(p for p in directory.iterdir() if p.is_dir())
            except OSError:
                continue  # missing/unreadable directory -- no candidates from here.
            for child in children:
                resolved_child = resolve_project_path(project_root, child)
                if resolved_child is None:
                    continue  # symlinked subdirectory escaping the project -- never follow.
                next_level.append(resolved_child)
        if not next_level:
            break
        ordered.extend(next_level)
        level = next_level
    return ordered


def _java_resolve_module(importer_rel: str, spec: str, project_root: Path) -> Path | None:
    """Java FQCN → file: the project's OWN declared scan roots first (searched
    a small bounded number of directory levels down), then the conventional
    Maven TEST/SOURCE roots as a fallback.

    ``spec`` is a fully-qualified class name (e.g. ``com.example.util.
    HarnessAssertions``) bound by a STATIC import (:func:`_java_imported_lookup`).
    Two root sources, tried in this explicit, mutually exclusive order (the
    SAME "stronger signal first, weaker fallback only when that finds nothing"
    precedence :func:`_java_fallback_candidates` (E2) already uses for its own
    same-directory-vs-subdirectory scan):

    1. The project's OWN declared ``scan.test_dirs`` / ``scan.source_dirs``
       (``codd.yaml``), resolved via :func:`_resolve_vb_scan_dirs` — the SAME
       config-access pattern :func:`_java_directory_in_scan_roots` /
       :func:`_java_fallback_candidates` already use (test dirs first, source
       dirs second — ``_resolve_vb_scan_dirs`` itself merges them in that
       order). These are the ground truth for THIS project: a project whose
       test/source roots are not the Maven convention (e.g. a declared
       ``tests/`` instead of ``src/test/java``) is resolved correctly without
       teaching the bundled ``java.yaml`` profile about every possible layout.
       A declared root does not even have to BE the source root directly:
       :func:`_java_declared_root_search_dirs` also tries up to
       :data:`_JAVA_MAX_DECLARED_ROOT_DEPTH` levels of subdirectories beneath
       it (shallowest first, so a direct depth-0 hit is always preferred over
       an inferred deeper one), closing the gap a project whose declared root
       merely CONTAINS the real source root (e.g. a declared ``tests/`` whose
       actual Java sources begin at ``tests/e2e/java/``) would otherwise miss.
    2. :func:`_java_test_and_source_roots` — the profile-driven Maven
       convention (``src/test/java``, ``src/main/java`` for the bundled
       ``java.yaml``) — consulted ONLY when NONE of the project's own declared
       roots (at ANY searched depth) resolves ``spec``. Some Java projects
       genuinely use the Maven layout (or declare no ``scan.test_dirs``/
       ``source_dirs`` override at all), so this fallback is KEPT, never
       replaced, and is NOT itself depth-widened (the Maven convention already
       names the exact source root; that was not part of the diagnosed gap).

    Every candidate is re-jailed via :func:`resolve_project_path` — the
    declared root itself, every subdirectory level beneath it (an operator-
    declared ``codd.yaml`` value is not trusted as already in-tree, and
    neither is any directory reached by walking it), and the final joined
    file — so neither a misconfigured/malicious declared scan root NOR a
    maliciously-dotted ``spec`` (``../../etc/passwd``-shaped after
    substitution; not actually reachable since a literal ``.`` → ``/``
    substitution can never itself produce ``..``, but re-jailed regardless as
    defense in depth, exactly E2's own discipline) can resolve outside
    ``project_root``. Mirrors :func:`_go_resolve_module` /
    :func:`_py_resolve_module` / :func:`_ts_resolve_specifier`'s "``None`` on
    any miss" contract.
    """

    rel = spec.strip().strip(".").replace(".", "/") + ".java"

    # 1. The project's OWN declared roots — ground truth for THIS project.
    config = _load_optional_config(project_root)
    for raw_root in _resolve_vb_scan_dirs(project_root, config) or [str(project_root)]:
        declared_root = resolve_project_path(project_root, raw_root)
        if declared_root is None:
            continue  # out-of-tree declared root.
        if declared_root.is_file():
            # A file-shaped declared root has no package tree beneath IT, but
            # its directory does — same "fall back to the parent" handling
            # :func:`_java_directory_in_scan_roots` already uses for this.
            declared_root = declared_root.parent
        for directory in _java_declared_root_search_dirs(
            declared_root, project_root, _JAVA_MAX_DECLARED_ROOT_DEPTH
        ):
            candidate = resolve_project_path(directory, rel)
            if candidate is not None and candidate.is_file():
                return candidate

    # 2. Maven-conventional fallback — consulted ONLY because step 1 found
    # nothing (never merged with it: a declared-root hit is always preferred).
    for root in _java_test_and_source_roots():
        candidate = resolve_project_path(project_root, f"{root.strip('/')}/{rel}")
        if candidate is not None and candidate.is_file():
            return candidate
    return None


#: A Java method DEFINITION: optional modifiers, a return-type token (identifier,
#: optionally generic/array), the method name, a parameter list (no ``;``/``{``/
#: ``}`` inside — never a statement spanning a call), an optional ``throws``
#: clause, then the body-opening ``{``. Generalizes :data:`_JAVA_TEST_METHOD_RE`
#: (which is pinned to ``void`` + ``@Test``) to ANY return type and ANY name — a
#: helper like ``HarnessAssertions.assertSuccess`` is exactly this shape. A bare
#: CALL (``assertSuccess(result, "14.0");``) never matches: it has no
#: return-type-shaped token before it, and it ends in ``;`` rather than ``{``.
#: The return type accepts a QUALIFIED / nested name (``Expr.Literal``,
#: ``Map.Entry<K, V>``) — dot-separated identifier segments — not just a single
#: identifier: a same-file helper returning a nested type was otherwise
#: invisible, resolving as ``unresolved_helper`` and false-redding real
#: delegated assertions (java3 exprcalc dogfood, 2026-07-11).
_JAVA_METHOD_DEF_RE = re.compile(
    r"(?<![A-Za-z0-9_.])(?:(?:public|protected|private|static|final|synchronized|"
    r"abstract|default|native|strictfp)\s+)*"
    r"[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)*(?:<[^;{}]*?>)?(?:\[\])*\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\((?P<params>[^;{}]*)\)"
    r"(?:\s*throws\s+[A-Za-z0-9_.,\s<>]+?)?\s*\{",
    re.DOTALL,
)


def _java_split_param_names(params: str) -> list[str]:
    """Parameter NAMES from a Java parameter list (``Result result, String expected``
    → ``[result, expected]``).

    Java writes ``[annotations] [final] Type name`` per parameter (never Go's
    grouped ``a, b int`` shape), so — unlike :func:`_go_split_param_names` — each
    comma-group's LAST whitespace-separated token is the name; this also handles
    varargs (``String... args`` → ``args``) and arrays (``String[] names`` →
    ``names``) since the bare name is still the final token either way. Best
    effort: an unparsable group is simply skipped (fail-open at the param level;
    the primitive + argument-anchor check downstream is the real gate).
    """

    names: list[str] = []
    for raw in _go_split_args(params):
        tokens = [t for t in raw.replace(",", " ").split() if t and t != "final" and not t.startswith("@")]
        if not tokens:
            continue
        candidate = tokens[-1].lstrip("*")
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", candidate):
            names.append(candidate)
    return names


def _java_find_method_def(module_text: str, name: str) -> "tuple[str, list[str]] | None":
    """Find a Java method definition named ``name`` in ``module_text``.

    Mirrors :func:`_go_find_function_def` / :func:`_py_find_function_def`:
    COMMENT-STRIPPED first (:func:`_go_strip_comments`) so a definition-shaped
    comment (or a commented-out assertion inside a real definition's body) is
    never read as real, brace-matched via the string-aware :func:`_go_match_brace`
    (run against the SAME skeleton, so offsets stay aligned), and the body
    returned is ALSO from that comment-stripped skeleton — exactly Go's
    contract — so the shared engine's ``primitive_re`` / argument-anchor scan
    never credits a commented-out assertion. Returns ``(body, param_names)`` for
    the FIRST matching definition (Java overloading can share a name; this
    module's existing helper-resolution never attempts full overload
    resolution), or ``None`` when nothing matches.
    """

    skeleton = _go_strip_comments(module_text)
    for m in _JAVA_METHOD_DEF_RE.finditer(skeleton):
        if m.group("name") != name:
            continue
        brace = m.end() - 1
        close = _go_match_brace(skeleton, brace)
        if close < 0:
            continue  # unbalanced body ⇒ skip (degrade), never raise.
        body = skeleton[brace + 1 : close]
        return body, _java_split_param_names(m.group("params"))
    return None


#: Hard bound on E2 fallback candidates (design cap) — a same-directory glob is
#: already narrow, but this keeps a pathological huge directory finite. Shared
#: across BOTH scan levels :func:`_java_fallback_candidates` now tries (the
#: importer's own directory, and — only when that finds nothing — one level of
#: subdirectories): the COMBINED total returned never exceeds this, regardless
#: of how many levels contributed candidates.
_JAVA_MAX_FALLBACK_CANDIDATES = 16


def _java_directory_in_scan_roots(
    directory: Path, project_root: Path, config: dict[str, Any] | None
) -> bool:
    """Whether ``directory`` (already resolved, in-root) sits under a declared
    project scan root.

    Reuses :func:`_resolve_vb_scan_dirs` — the SAME ``scan.test_dirs`` /
    ``scan.source_dirs`` (``codd.yaml``) resolution the marker scan itself
    already uses to find test files — so the E2 fallback search is confined to
    the project's OWN declared source/test trees FROM CONFIG, never a hardcoded
    path, while staying permissive-by-default (an unconfigured project's
    resolution is the whole tree) exactly like the rest of this gate's file
    discovery. ``directory`` is trusted to already be inside ``project_root``
    (the caller establishes that); this only narrows further.

    Called ONLY for the importer's own directory (see
    :func:`_java_fallback_candidates`) — a subdirectory of an in-scan-root
    directory is ALWAYS itself in-scan-root too (``Path.relative_to`` accepts
    any descendant depth, not just an immediate child), so the one-level-down
    widening does not need, and does not pay for, a second call here.
    """

    roots = _resolve_vb_scan_dirs(project_root, config) or [str(project_root)]
    for raw_root in roots:
        resolved_root = resolve_project_path(project_root, raw_root)
        if resolved_root is None:
            continue
        if resolved_root.is_file():
            resolved_root = resolved_root.parent
        try:
            directory.relative_to(resolved_root)
            return True
        except ValueError:
            continue
    return False


def _java_sibling_matches(
    directory: Path, importer_path: Path, receiver: str, limit: int
) -> list[Path]:
    """``.java`` files directly inside ``directory`` matching the E2 receiver rule.

    Shared by BOTH scan levels in :func:`_java_fallback_candidates` (the
    importer's own directory, and — when that finds nothing — each of its
    immediate subdirectories): a QUALIFIED call (``receiver`` non-empty) only
    matches a file whose STEM equals it; an UNQUALIFIED call (``receiver`` is
    ``""``) matches every ``.java`` file in ``directory``, since there is no
    class-name clue to narrow the guess. Never proposes ``importer_path``
    itself. Returns at most ``limit`` matches, in sorted (deterministic) order;
    a non-positive ``limit`` short-circuits to ``[]`` without touching the
    filesystem.
    """

    if limit <= 0:
        return []
    try:
        siblings = sorted(p for p in directory.iterdir() if p.is_file() and p.suffix == ".java")
    except OSError:
        return []

    matches: list[Path] = []
    for sib in siblings:
        try:
            if sib.resolve() == importer_path:
                continue  # never propose the importer's own file as a candidate.
        except OSError:
            continue
        if receiver and sib.stem != receiver:
            continue
        matches.append(sib)
        if len(matches) >= limit:
            break
    return matches


def _java_fallback_candidates(
    importer_text: str, importer_rel: str, project_root: Path, full_callee: str
) -> list[Path]:
    """E2 fallback-candidate plug for Java (see :func:`_resolve_one_helper`).

    Consulted by the shared engine ONLY as the LAST resort, after same-file +
    import-bound + barrel resolution all miss. Covers the conventional Java
    shapes neither a same-file search nor a STATIC import can:

    * a same-PACKAGE helper class (``HarnessAssertions.assertSuccess(...)``
      where ``HarnessAssertions.java`` sits in the SAME DIRECTORY as the
      importing test) — Maven's layout makes "same directory" and "same
      package" identical, so a same-package helper needs NO import statement
      at all; and
    * the SAME shape ONE SUBDIRECTORY LEVEL DOWN (``…/support/
      HarnessAssertions.java``, ``…/helpers/…``, ``…/util/…``,
      ``…/fixtures/…`` — an idiomatic, framework-agnostic test-helper layout
      convention, not a fixed name list this function special-cases: it globs
      EVERY immediate subdirectory of the importer's own directory, never a
      hardcoded set of directory names). This shape is bound by a PLAIN,
      non-static import (``import foo.support.HarnessAssertions;``), which
      :func:`_java_imported_lookup` deliberately does not resolve (Java's
      per-symbol lookup table is static-import-only) — this directory glob is
      what closes that gap structurally, without parsing the plain import.

    For a QUALIFIED call (``full_callee`` carries a receiver, e.g.
    ``HarnessAssertions.assertSuccess``) the targeted candidate is the file
    whose STEM equals the receiver's simple name. For an UNQUALIFIED call (no
    receiver) every ``.java`` file at the scanned level is a candidate (still
    bounded below), since there is no class-name clue to narrow the guess.
    Matching itself is :func:`_java_sibling_matches`, shared by both levels.

    Scan-level precedence (an explicit, bounded choice): the SAME-DIRECTORY
    scan runs first; the one-subdirectory-level scan runs ONLY when that finds
    NOTHING. The two are mutually exclusive, never additive, so the combined
    result stays bounded by the SAME :data:`_JAVA_MAX_FALLBACK_CANDIDATES` cap
    that already governed the same-directory-only scan — never
    same-directory-near-cap PLUS a second cap's worth from subdirectories —
    and a same-package hit (the stronger, more conventional signal) is never
    diluted by a weaker one-level-down guess. Exactly ONE level down, never
    deeper: a subdirectory's own subdirectories are not walked, so a helper
    two levels down stays a deliberate, documented unresolved residual — the
    same finite-hop discipline as ``_MAX_HELPER_HOPS`` elsewhere in this
    module, not unbounded recursion.

    This plug returns PLACES TO LOOK ONLY — it is never itself evidence; the
    existing primitive + argument-anchor + hop-bound logic in
    :func:`_resolve_one_helper` still judges whatever file content is found
    there, and every returned path is independently re-jailed by the CALLER via
    :func:`resolve_project_path` before its content is ever read. Hard-bounded
    to :data:`_JAVA_MAX_FALLBACK_CANDIDATES` and confined to the project's
    declared scan roots (:func:`_java_directory_in_scan_roots`) — each
    candidate SUBDIRECTORY is also independently re-resolved via
    :func:`resolve_project_path` before its contents are ever listed, so an
    in-root symlinked subdirectory whose target escapes the project is never
    followed.
    """

    importer_path = resolve_project_path(project_root, importer_rel)
    if importer_path is None or not importer_path.is_file():
        return []
    directory = importer_path.parent
    root = Path(project_root).resolve()
    try:
        directory.relative_to(root)
    except ValueError:
        return []

    if not _java_directory_in_scan_roots(directory, root, _load_optional_config(project_root)):
        return []

    parts = full_callee.split(".") if full_callee else []
    receiver = parts[-2] if len(parts) >= 2 else ""

    candidates = _java_sibling_matches(
        directory, importer_path, receiver, _JAVA_MAX_FALLBACK_CANDIDATES
    )
    if candidates:
        return candidates

    # One subdirectory level down (see docstring) — consulted ONLY because the
    # same-directory scan above found NOTHING for this receiver/call shape.
    try:
        subdirs = sorted(p for p in directory.iterdir() if p.is_dir())
    except OSError:
        return candidates

    for sub in subdirs:
        remaining = _JAVA_MAX_FALLBACK_CANDIDATES - len(candidates)
        if remaining <= 0:
            break
        sub_resolved = resolve_project_path(project_root, sub)
        if sub_resolved is None:
            continue  # symlinked subdirectory escaping the project — never follow.
        candidates.extend(_java_sibling_matches(sub_resolved, importer_path, receiver, remaining))
    return candidates


#: A Java ``import`` (or ``import static``) statement's dotted path, with an
#: optional trailing ``.*`` (star import) stripped from the captured group.
_JAVA_IMPORT_RE = re.compile(
    r"^\s*import\s+(?:static\s+)?(?P<path>[\w.]+?)(?:\.\*)?\s*;",
    re.MULTILINE,
)


def _java_imports_path(importer_text: str, import_path: str) -> bool:
    """Whether ``importer_text`` imports ``import_path`` itself or a member/
    subpackage under it.

    Lexical (no full Java import resolution needed) — this is condition (a) of
    the library-terminal check below: the test file must ACTUALLY import the
    declared library before any of its calls can credit as that library's
    terminal (this is what keeps adversarial case 8 — an unrelated
    ``check(File f)`` method with NO ArchUnit import — correctly rejected).
    Comment-stripped first so a commented-out import does not count.
    """

    import_path = (import_path or "").strip()
    if not import_path:
        return False
    for m in _JAVA_IMPORT_RE.finditer(_go_strip_comments(importer_text)):
        candidate = m.group("path")
        if candidate == import_path or candidate.startswith(import_path + "."):
            return True
    return False


def _java_library_terminal_evidence(
    block: TestBlock, *, importer_text: str, assertion_hints: Mapping[str, Any]
) -> AssertionEvidence | None:
    """Credit a call to a DECLARED library fluent terminal (e.g. ArchUnit's ``.check()``).

    Data-driven off ``tests.assertion_hints.library_assertion_terminals`` in the
    active language YAML (see the module docstring's "fluent-terminal problem")
    — NO library name is ever hardcoded here; every ``import_path``/``methods``
    pair is DATA read from the profile. A call credits iff ALL THREE hold:

    (a) ``importer_text`` actually imports the declared ``import_path``
        (:func:`_java_imports_path`);
    (b) the call's LEAF method name is in the declared ``methods`` list;
    (c) the call's own argument text, OR its receiver-chain (the qualifier
        before the leaf — empty for a bare/chain-broken call like ArchUnit's
        typical ``noClasses().that()....check(classes)``, where the receiver is
        unreachable through an intervening ``()``), references a non-ignored
        name — the SAME :func:`_go_reference_idents` reference-identifier check
        :func:`_java_direct_assertion_evidence` already uses; no new anchor
        logic is introduced for this.

    Returns ``None`` (not a verdict — "this mechanism does not apply") when no
    terminal is declared for this profile or no call in the body matches; the
    caller then keeps whatever verdict it already had. Never returns a NOT-ok
    verdict itself (a mismatch here means "not a terminal", not "reject") —
    called ONLY after the shared helper-hop engine already failed to resolve
    the block's calls (see :func:`_resolve_java_evidence`), so only a credited
    pass here can change the outcome.
    """

    terminals = assertion_hints.get("library_assertion_terminals") if isinstance(
        assertion_hints, Mapping
    ) else None
    if not terminals:
        return None
    try:
        calls = _extract_helper_calls(_go_strip_comments(block.body_text))
    except Exception:  # noqa: BLE001 — extraction is best-effort; never raise.
        return None
    if not calls:
        return None

    for spec in terminals:
        if not isinstance(spec, Mapping):
            continue
        import_path = str(spec.get("import_path") or "").strip()
        methods_raw = spec.get("methods")
        if not import_path or not methods_raw:
            continue
        try:
            methods = {str(m) for m in methods_raw if str(m).strip()}
        except TypeError:
            continue
        if not methods or not _java_imports_path(importer_text, import_path):
            continue
        for full_callee, leaf, args in calls:
            if leaf not in methods:
                continue
            parts = full_callee.split(".")
            receiver = parts[-2] if len(parts) >= 2 else ""
            idents = _go_reference_idents(args) | _go_reference_idents(receiver)
            if idents - _JAVA_IGNORED_NAMES:
                return AssertionEvidence(ok=True, reason="library_terminal", confidence="declared")
    return None


def _java_combined_primitive_re() -> re.Pattern[str]:
    """Helper-BODY primitive matcher: unqualified names UNION declared qualified
    entry-class shapes.

    Mirrors :func:`_java_body_has_primitive_assertion`'s direct-side union so a
    HELPER that delegates via a QUALIFIED call (``Assertions.assertEquals(...)``
    with only a plain, non-static import) is recognized the same way a direct
    qualified call already is — the shared engine's ``primitive_re`` slot only
    accepts one compiled pattern, so the two are combined into one here.
    """

    qualified = _java_qualified_primitive_re(_java_entry_classes())
    if qualified is None:
        return _JAVA_HELPER_PRIMITIVE_RE
    return re.compile(f"(?:{_JAVA_HELPER_PRIMITIVE_RE.pattern})|(?:{qualified.pattern})")


def _resolve_java_evidence(
    block: TestBlock, *, importer_text: str, importer_rel: str, project_root: Path
) -> AssertionEvidence:
    """Java DELEGATED-assertion (helper) resolution — mirrors the Go/PY/TS engine.

    A Java test that delegates its check to a same-package / statically-imported
    helper (``HarnessAssertions.assertSuccess(result, "14.0")`` whose body runs a
    real ``assertEquals(...)``) is resolved one hop through the shared
    :func:`_resolve_evidence` engine, plugged with THIN Java adapters:
    :func:`_java_imported_lookup` (static-import binding),
    :func:`_java_resolve_module` (FQCN → file: the project's OWN declared
    ``scan.test_dirs``/``source_dirs`` roots first — up to two subdirectory
    levels beneath them — then Maven ``src/test/java``/``src/main/java`` as a
    fallback), and :func:`_java_fallback_candidates`
    (E2 — the same-directory-or-one-level-down sibling guess a plain/absent
    import cannot express). Java has no barrel-reexport convention, so
    ``reexport_edges`` is ``None`` (same as Go).

    When the helper-hop engine still cannot resolve the call (e.g. a fluent
    library terminal like ArchUnit's ``rule.check(classes)`` — not a helper
    DEFINITION at all), :func:`_java_library_terminal_evidence` is consulted as
    a data-driven fallback BEFORE the engine's failure verdict is returned to
    the gate.
    """

    evidence = _resolve_evidence(
        block,
        importer_text=importer_text,
        importer_rel=importer_rel,
        project_root=project_root,
        primitive_re=_java_combined_primitive_re(),
        imported_lookup=_java_imported_lookup,
        module_resolver=_java_resolve_module,
        def_finder=_java_find_method_def,
        reexport_edges=None,
        fallback_module_candidates=_java_fallback_candidates,
    )
    if evidence.ok:
        return evidence
    terminal = _java_library_terminal_evidence(
        block, importer_text=importer_text, assertion_hints=_java_assertion_hints()
    )
    return terminal if terminal is not None else evidence


# ---------------------------------------------------------------------------
# C++ (GoogleTest / Catch2) structural adapter
#
# Modeled on :class:`GoTestBlockProfile`. The C++ comment grammar (``//`` line +
# ``/* */`` block) is identical to Go's, so the brace matcher
# (:func:`_go_match_brace`), the comment stripper (:func:`_go_strip_comments`),
# the balanced-argument reader (:func:`_balanced_args`), the comma splitter
# (:func:`_go_split_args`) and the reference-identifier harvester
# (:func:`_go_reference_idents`) are REUSED verbatim — there is no C++-specific
# lexing here beyond the test-MACRO recognizers below.
#
# The key structural difference from Go: a C++ test block is not a plain
# function but a test-framework MACRO invocation —
#   * GoogleTest:  ``TEST(Suite, Name) { ... }`` / ``TEST_F(Fixture, Name){...}``
#                  / ``TEST_P(Suite, Name){...}``. A test is DISABLED (skipped)
#                  when its suite OR name is prefixed ``DISABLED_``
#                  (``TEST(S, DISABLED_Foo)``), or its body calls ``GTEST_SKIP(``.
#   * Catch2:      ``TEST_CASE("name", "[tag]") { ... }`` — the first string
#                  literal is the case name. Skip is a Catch2 v3 ``SKIP(`` in the
#                  body or a hidden ``[.]`` / ``[!mayfail]`` tag.
# A PRIMITIVE assertion is a gtest/Catch2 assertion MACRO (``EXPECT_EQ(`` /
# ``ASSERT_TRUE(`` / ``REQUIRE(`` / ``CHECK(`` / …). A bare helper call is NOT
# primitive (resolved separately, fail-closed — see ``resolve_assertion_evidence``).
# ---------------------------------------------------------------------------

#: A GoogleTest test-defining macro: ``TEST`` / ``TEST_F`` / ``TEST_P`` followed
#: by ``( <suite> , <name> )`` then a body ``{``. The two macro args are captured
#: so the label is ``Suite.Name`` and the DISABLED_ skip prefix (on EITHER the
#: suite or the name) can be detected. ``[ \t\r\n]*`` between the ``)`` and ``{``
#: tolerates a brace on the next line.
_CPP_GTEST_MACRO_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?P<macro>TEST|TEST_F|TEST_P)\s*"
    r"\(\s*(?P<suite>[A-Za-z_][A-Za-z0-9_]*)\s*,\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\)"
    r"[ \t\r\n]*\{",
)
#: A Catch2 ``TEST_CASE("name"[, "[tag]"]) { ... }``. Only the OPENING is matched
#: here (callee + ``(``); the name string and tags are read from the balanced
#: argument text so a multi-line / many-tag header is handled. ``SCENARIO`` (the
#: BDD spelling) shares Catch2's body grammar and is recognized too.
_CPP_CATCH2_MACRO_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?P<macro>TEST_CASE|SCENARIO)\s*\(",
)
#: GoogleTest + Catch2 assertion MACROS that are PRIMITIVE (a call that, on its
#: own, can fail the test). Word-bounded + immediately-``(`` so ``EXPECT_EQ`` the
#: token is matched but ``MY_EXPECT_EQ`` / ``EXPECT_EQ_THING`` are not. ``SUCCEED``
#: is deliberately EXCLUDED — it proves nothing (the C++ analogue of a constant
#: assertion), so a body whose only "assertion" is ``SUCCEED()`` is NOT primitive.
_CPP_ASSERT_MACROS = (
    # gtest equality / comparison / boolean
    "EXPECT_EQ", "ASSERT_EQ", "EXPECT_NE", "ASSERT_NE",
    "EXPECT_LT", "ASSERT_LT", "EXPECT_LE", "ASSERT_LE",
    "EXPECT_GT", "ASSERT_GT", "EXPECT_GE", "ASSERT_GE",
    "EXPECT_TRUE", "ASSERT_TRUE", "EXPECT_FALSE", "ASSERT_FALSE",
    # gtest string
    "EXPECT_STREQ", "ASSERT_STREQ", "EXPECT_STRNE", "ASSERT_STRNE",
    "EXPECT_STRCASEEQ", "ASSERT_STRCASEEQ",
    # gtest float
    "EXPECT_FLOAT_EQ", "ASSERT_FLOAT_EQ", "EXPECT_DOUBLE_EQ", "ASSERT_DOUBLE_EQ",
    "EXPECT_NEAR", "ASSERT_NEAR",
    # gtest exceptions / death
    "EXPECT_THROW", "ASSERT_THROW", "EXPECT_NO_THROW", "ASSERT_NO_THROW",
    "EXPECT_ANY_THROW", "ASSERT_ANY_THROW",
    "EXPECT_DEATH", "ASSERT_DEATH",
    # gtest matchers + unconditional failure
    "EXPECT_THAT", "ASSERT_THAT", "FAIL", "ADD_FAILURE",
    # Catch2
    "REQUIRE", "CHECK", "REQUIRE_FALSE", "CHECK_FALSE",
    "REQUIRE_THROWS", "CHECK_THROWS", "REQUIRE_THROWS_AS", "CHECK_THROWS_AS",
    "REQUIRE_NOTHROW", "CHECK_NOTHROW", "REQUIRE_THAT", "CHECK_THAT",
)
#: ``<MACRO>(`` for any assertion macro, word-bounded (no leading ident char), so
#: it never matches a SUFFIXED identifier. Longest-first alternation so e.g.
#: ``REQUIRE_FALSE`` wins over ``REQUIRE`` at the same position.
_CPP_ASSERT_MACRO_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    + "|".join(re.escape(m) for m in sorted(_CPP_ASSERT_MACROS, key=len, reverse=True))
    + r")\s*\("
)
#: gtest's in-body skip directive. Word-bounded + ``(`` so a comment/identifier
#: mention is not matched (comments are stripped before the scan anyway).
_CPP_SKIP_RE = re.compile(r"(?<![A-Za-z0-9_])(?:GTEST_SKIP|SKIP)\s*\(")
#: The gtest ``DISABLED_`` skip prefix on a suite or test name.
_CPP_DISABLED_PREFIX = "DISABLED_"
#: A C++ string literal (double-quoted, escapes honoured) + a raw string
#: ``R"(...)"`` (no escapes). Used only to find a Catch2 case NAME / detect a
#: hidden ``[.]`` tag; reference harvesting reuses Go's string-aware scanner.
_CPP_STRING_RE = re.compile(r'R"\((?:.*?)\)"|"(?:\\.|[^"\\])*"', re.DOTALL)

#: C++ keywords / literals / common std type & macro tokens that are NOT
#: credit-worthy references inside an assertion's argument text — the C++ analogue
#: of :data:`_GO_IGNORED_NAMES`. A SUT call name, a local variable, an expected
#: value a test declares are NEVER here, so ``EXPECT_EQ(5, add(2, 3))`` (``add``)
#: stays GREEN while ``EXPECT_EQ(1, 1)`` / ``EXPECT_TRUE(true)`` (only ignored
#: tokens / literals) become RED. The assertion macro names themselves are ignored
#: too (a nested ``EXPECT_THAT(x, Eq(y))`` anchors on ``x``/``y``, not the macro).
_CPP_IGNORED_NAMES = frozenset(
    {
        # boolean / null literals
        "true", "false", "nullptr", "NULL", "TRUE", "FALSE",
        # keywords
        "if", "else", "for", "while", "do", "switch", "case", "default",
        "return", "break", "continue", "goto", "const", "constexpr",
        "static", "volatile", "mutable", "auto", "decltype", "typename",
        "template", "class", "struct", "union", "enum", "namespace", "using",
        "public", "private", "protected", "virtual", "override", "final",
        "new", "delete", "this", "sizeof", "operator", "throw", "try",
        "catch", "noexcept", "explicit", "friend", "inline", "extern",
        # fundamental / common std types
        "void", "bool", "char", "wchar_t", "char8_t", "char16_t", "char32_t",
        "short", "int", "long", "signed", "unsigned", "float", "double",
        "size_t", "ssize_t", "ptrdiff_t", "intptr_t", "uintptr_t",
        "int8_t", "int16_t", "int32_t", "int64_t",
        "uint8_t", "uint16_t", "uint32_t", "uint64_t",
        "std", "string", "wstring", "string_view", "vector", "map",
        "unordered_map", "set", "unordered_set", "pair", "tuple", "array",
        "optional", "variant", "shared_ptr", "unique_ptr", "make_shared",
        "make_unique", "move", "forward",
        # gtest/Catch2 fixture/util names that are API 器, not observations
        "GetParam", "GTEST_SKIP", "SKIP", "SUCCEED",
    }
    | {m for m in _CPP_ASSERT_MACROS}
)


def _cpp_reference_idents(expr: str) -> set[str]:
    """Genuine identifier REFERENCES in a C++ expression (strings/comments stripped).

    Delegates to :func:`_go_reference_idents` — the lexical rules that matter here
    (drop ``//`` / ``/* */`` comments, blank double-quoted strings, drop a member
    field after ``.`` so ``got.value`` anchors on ``got``) are identical between Go
    and C++. The residual differences (``->`` member access, ``::`` scope) are
    handled coarsely: ``a->b`` harvests ``a`` and ``b`` (both kept ⇒ fail-OPEN
    toward credit, never a false-RED), and ``ns::Type`` harvests ``ns`` and
    ``Type`` (the scope-resolved names are filtered by :data:`_CPP_IGNORED_NAMES`
    when they are std/keyword tokens). Best-effort and fail-open by construction.
    """

    return _go_reference_idents(expr)


def _cpp_body_has_primitive_assertion(body_text: str) -> bool:
    """Whether ``body_text`` contains a C++ PRIMITIVE assertion macro (lexical).

    A primitive is any gtest/Catch2 assertion MACRO in :data:`_CPP_ASSERT_MACROS`
    (``EXPECT_EQ(`` / ``REQUIRE(`` / …). A bare named-helper call (``verifyRun(r)``)
    is NOT primitive — it is resolved one hop via the evidence graph. Comments are
    stripped first (reusing :func:`_go_strip_comments`, whose ``//`` + ``/* */``
    grammar is shared with C++) so a fake assertion written in a COMMENT
    (``// EXPECT_EQ(got, want);``) is never counted (the false-GREEN guard).
    ``SUCCEED()`` is excluded from the macro set, so a body whose only assertion-ish
    call is ``SUCCEED()`` is correctly NOT primitive.
    """

    skeleton = _go_strip_comments(body_text)
    return bool(_CPP_ASSERT_MACRO_RE.search(skeleton))


def _cpp_body_is_skipped(body_text: str) -> bool:
    """Whether ``body_text`` unconditionally skips via ``GTEST_SKIP(`` / ``SKIP(``.

    Comments stripped first so a ``GTEST_SKIP()`` mentioned in a COMMENT does not
    mark a real test skipped (a false-RED — the opposite hazard). The gtest
    ``DISABLED_`` NAME prefix is a separate signal handled at parse time (it lives
    in the macro header, not the body).
    """

    skeleton = _go_strip_comments(body_text)
    return bool(_CPP_SKIP_RE.search(skeleton))


def _cpp_catch2_case_name(args: str) -> str:
    """The Catch2 case NAME (first string-literal arg), or ``""``.

    ``TEST_CASE("adds two numbers", "[math]")`` → ``adds two numbers``. Best-effort;
    a non-string first arg (rare) yields ``""`` (used only for the diagnostic label).
    """

    m = _CPP_STRING_RE.search(args)
    if not m:
        return ""
    raw = m.group(0)
    # Strip a raw-string ``R"(...)"`` wrapper or the plain quotes for the label.
    if raw.startswith('R"('):
        return raw[3:-2]
    return raw[1:-1]


def _cpp_catch2_is_hidden(args: str) -> bool:
    """Whether a Catch2 case's TAGS mark it hidden / non-running.

    A leading-``.`` tag (``"[.]"`` / ``"[.integration]"``) hides a case from the
    default run, and ``"[!mayfail]"`` / ``"[!shouldfail]"`` invert the verdict — in
    all of these the case does not straightforwardly PROVE the behavior, so it is
    treated as not-executable (skipped) for authenticity, mirroring gtest
    ``DISABLED_``. Only the tag STRING args are inspected.
    """

    for sm in _CPP_STRING_RE.finditer(args):
        tag = sm.group(0)
        if "[." in tag or "[!mayfail" in tag or "[!shouldfail" in tag:
            return True
    return False


def _cpp_direct_assertion_evidence(body_text: str) -> AssertionEvidence:
    """Verdict: does ``body_text`` carry a NON-constant C++ primitive assertion?

    For each assertion macro in the comment-stripped body, extract its argument
    text via :func:`_balanced_args` and decide REAL vs CONSTANT-only by the same
    reference rule the Go/PY/TS direct stages use: the args must reference at least
    one NON-ignored identifier (a SUT call, a local, an expected value). So
    ``EXPECT_EQ(5, add(2, 3))`` references ``add`` ⇒ REAL (``direct``), while
    ``EXPECT_EQ(1, 1)`` / ``EXPECT_TRUE(true)`` reference only literals ⇒
    constant-only. If ANY macro is REAL ⇒ ``direct`` (ok). If at least one macro
    was seen and ALL are constant-only ⇒ ``constant_direct`` (not ok). If the
    regex matched ``has_assertion`` upstream but THIS scanner classifies no macro
    (an assertion shape it cannot read) ⇒ fail OPEN (``direct``), never a
    false-RED — the same anti-false-RED discipline as
    :func:`_go_direct_assertion_evidence`.
    """

    # Scan a comment-stripped SKELETON so an assertion written in a COMMENT is not
    # read as real code (offsets preserved — comments become spaces — so
    # ``_balanced_args`` positions still align).
    skeleton = _go_strip_comments(body_text)
    saw_primitive = False
    for m in _CPP_ASSERT_MACRO_RE.finditer(skeleton):
        saw_primitive = True
        # ``m.end() - 1`` is the macro's ``(``; read its balanced argument text.
        args = _balanced_args(skeleton, m.end() - 1)
        if _cpp_reference_idents(args) - _CPP_IGNORED_NAMES:
            return AssertionEvidence(ok=True, reason="direct")
    if not saw_primitive:
        # ``has_assertion`` matched upstream but no macro classified here → fail OPEN.
        return AssertionEvidence(ok=True, reason="direct")
    return AssertionEvidence(ok=False, reason="constant_direct")


@dataclass(frozen=True)
class CppTestBlockProfile:
    """C++ (GoogleTest + Catch2) structural adapter.

    A test block is a framework test MACRO with a brace-matched body:

    * GoogleTest — ``TEST(Suite, Name) { ... }`` / ``TEST_F(Fixture, Name){...}`` /
      ``TEST_P(Suite, Name){...}``. The label is ``Suite.Name``. The test is
      NOT executable (skipped) when its suite OR name carries the ``DISABLED_``
      prefix (``TEST(MySuite, DISABLED_Foo)``) or its body calls ``GTEST_SKIP()``.
    * Catch2 — ``TEST_CASE("name", "[tag]") { ... }`` (and the ``SCENARIO`` BDD
      spelling). The label is the case name. The case is not executable when it
      carries a hidden ``[.]`` / ``[!mayfail]`` tag or its body calls ``SKIP()``.

    Each macro is a LEAF coverage target (C++ test macros do not nest the way Go
    ``t.Run`` subtests / TS ``describe`` groups do — Catch2 ``SECTION``s share the
    enclosing case's assertions and are intentionally not split out, which is
    conservative: a marker on the case credits only when the case body itself
    asserts). A PRIMITIVE assertion is a gtest/Catch2 assertion macro
    (:data:`_CPP_ASSERT_MACROS`); a bare named-helper call is resolved one hop via
    :meth:`resolve_assertion_evidence` (fail-closed). PURE + best-effort: an
    unparseable file yields ``[]`` (the gate then degrades), never an exception.
    """

    def handles_file(self, rel_path: str) -> bool:
        """Whether ``rel_path`` is a C++ TEST SOURCE this adapter parses.

        C++ tests live in SOURCE files (``.cpp`` / ``.cc`` / ``.cxx``), not headers
        — a header has no ``TEST(...)`` definitions to execute. Permissive on
        naming: any source-extension path that either contains ``test`` (case-
        insensitive, so ``foo_test.cpp`` / ``test_foo.cc`` / ``FooTests.cpp`` all
        match) OR sits under a ``tests/`` directory. This recognizes the conformance
        fixture ``tests/x_test.cpp`` on BOTH counts.
        """

        if not rel_path:
            return False
        norm = rel_path.replace("\\", "/")
        lower = norm.lower()
        if not lower.endswith((".cpp", ".cc", ".cxx")):
            return False
        if "test" in lower:
            return True
        # Under a ``tests/`` (or ``test/``) directory anywhere in the path.
        parts = lower.split("/")
        return any(p in ("tests", "test") for p in parts[:-1])

    def parse_test_blocks(self, text: str) -> list[TestBlock]:
        """Parse gtest + Catch2 test macros into executable-test-block records.

        Best-effort: a brace that does not match (truncated file) extends the block
        to EOF rather than raising; an unrecognizable file yields ``[]``.
        """

        def _line_of(pos: int) -> int:
            return text.count("\n", 0, pos) + 1

        blocks: list[TestBlock] = []

        # ── GoogleTest: TEST / TEST_F / TEST_P (Suite, Name) { ... } ──
        for fm in _CPP_GTEST_MACRO_RE.finditer(text):
            brace = text.index("{", fm.end() - 1)
            close = _go_match_brace(text, brace)
            if close < 0:
                close = len(text) - 1
            inner = text[brace + 1 : close]
            suite = fm.group("suite")
            name = fm.group("name")
            # gtest skip: DISABLED_ prefix on EITHER suite or name, OR GTEST_SKIP()
            # in the body. The DISABLED_ prefix is the conformance fixture's skip.
            disabled = suite.startswith(_CPP_DISABLED_PREFIX) or name.startswith(
                _CPP_DISABLED_PREFIX
            )
            blocks.append(
                TestBlock(
                    start_line=_line_of(fm.start()),
                    end_line=_line_of(close),
                    is_executable=not (disabled or _cpp_body_is_skipped(inner)),
                    has_assertion=_cpp_body_has_primitive_assertion(inner),
                    label=f"{suite}.{name}",
                    body_text=inner,
                )
            )

        # ── Catch2: TEST_CASE("name", "[tag]") { ... } / SCENARIO(...) { ... } ──
        for cm in _CPP_CATCH2_MACRO_RE.finditer(text):
            args = _balanced_args(text, cm.end() - 1)
            # The body ``{`` follows the macro's balanced ``(...)``. Find the close
            # paren, then the next ``{``.
            paren_open = text.find("(", cm.end() - 1)
            if paren_open < 0:
                continue
            # Re-derive the close-paren index by brace-independent paren matching.
            close_paren = _cpp_match_paren(text, paren_open)
            if close_paren < 0:
                continue
            brace = text.find("{", close_paren)
            if brace < 0:
                continue
            # Reject a stray ``{`` that is actually a different statement: only treat
            # it as the body when nothing but whitespace separates ``)`` and ``{``.
            if text[close_paren + 1 : brace].strip():
                continue
            close = _go_match_brace(text, brace)
            if close < 0:
                close = len(text) - 1
            inner = text[brace + 1 : close]
            name = _cpp_catch2_case_name(args)
            blocks.append(
                TestBlock(
                    start_line=_line_of(cm.start()),
                    end_line=_line_of(close),
                    is_executable=not (
                        _cpp_catch2_is_hidden(args) or _cpp_body_is_skipped(inner)
                    ),
                    has_assertion=_cpp_body_has_primitive_assertion(inner),
                    label=name or "TEST_CASE",
                    body_text=inner,
                )
            )

        # Document order (start_line) so attachment's smallest-containing and
        # nearest-after scans behave like the Go/TS/PY adapters.
        return sorted(blocks, key=lambda b: (b.start_line, -b.end_line))

    def resolve_assertion_evidence(
        self, block: TestBlock, *, importer_text: str, importer_rel: str, project_root: Path
    ) -> AssertionEvidence:
        """Delegated-assertion (helper) resolution for C++ — fail-closed.

        Called by the gate ONLY for an attached executable block with NO direct
        primitive assertion macro (``block.has_assertion`` is False). A C++ test
        that delegates its check to a helper function (``verifyResult(r);`` whose
        body runs the real ``EXPECT_EQ``) is a legitimate shape, but cross-file C++
        helper resolution requires include-graph + (often) namespace resolution
        that this adapter deliberately does NOT implement yet. So this stays
        FAIL-CLOSED, never spuriously ok=True: if the body contains an
        assertion-LIKE bare call (an unresolved helper) the verdict is
        ``unresolved_helper`` (a hard fail in greenfield strict — an unresolved
        assertion helper is not evidence); otherwise it is ``no_assertion``. This
        matches the design's "unresolved helper = fail" rule and never credits a
        non-asserting body. (A future ``cpp-include`` resolver adapter can widen
        this to real 1-hop resolution like Go/PY/TS.)
        """

        skeleton = _go_strip_comments(block.body_text)
        # An assertion-LIKE bare call: a callee whose leading name matches the
        # shared assertion-helper name set (expect/assert/check/verify/…), but that
        # is NOT one of the recognized primitive MACROS (those are handled by the
        # direct stage). Its presence means "the test tried to delegate a check we
        # cannot resolve" → unresolved_helper (fail-closed), not a silent pass.
        for match in _CALL_RE.finditer(skeleton):
            callee = match.group("callee")
            segment = callee.split(".")[-1].split("::")[-1]
            if segment in _CPP_ASSERT_MACROS:
                continue  # a primitive macro — not a helper (direct stage's job).
            if _looks_like_assertion_helper(segment):
                return AssertionEvidence(
                    ok=False,
                    reason="unresolved_helper",
                    detail="cpp helper resolution not implemented",
                )
        return AssertionEvidence(ok=False, reason="no_assertion")

    def resolve_direct_assertion_evidence(
        self,
        block: TestBlock,
        *,
        importer_text: str = "",
        importer_rel: str = "",
        project_root: Path | None = None,
        config: dict[str, Any] | None = None,
        profile: Any = None,
    ) -> AssertionEvidence:
        """Whether the block's DIRECT C++ assertion macro references a real name.

        ``constant_direct`` when every assertion macro is constant-only
        (``EXPECT_TRUE(true)`` / ``EXPECT_EQ(1, 1)``); ``direct`` when a macro's
        arguments reference a non-constant identifier (``EXPECT_EQ(5, add(2, 3))``
        references ``add``). Called by the gate only when ``block.has_assertion`` is
        True. Delegates to :func:`_cpp_direct_assertion_evidence`.
        """

        return _cpp_direct_assertion_evidence(block.body_text)


def _cpp_match_paren(text: str, open_idx: int) -> int:
    """Index of the ``)`` matching the ``(`` at ``open_idx`` (string/comment-aware), or -1.

    The C++ analogue of :func:`_go_match_brace` for parentheses — needed to locate
    the end of a Catch2 ``TEST_CASE( ... )`` header (which may contain nested parens
    inside tag/name expressions or commas inside a raw string) before its body
    ``{``. Tracks ``"`` / ``'`` literals and ``//`` + ``/* */`` comments so a paren
    inside a string or comment does not unbalance the count. Best-effort: returns -1
    if no match before EOF.
    """

    depth = 0
    in_str: str | None = None
    prev = ""
    i = open_idx
    n = len(text)
    while i < n:
        ch = text[i]
        if in_str is not None:
            if ch == in_str and prev != "\\":
                in_str = None
            prev = ch
            i += 1
            continue
        # Comments (only outside a string).
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            i = n if j < 0 else j
            prev = ""
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            prev = ""
            continue
        if ch in ("'", '"'):
            in_str = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        prev = ch
        i += 1
    return -1
