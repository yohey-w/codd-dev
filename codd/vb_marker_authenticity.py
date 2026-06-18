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

      direct primitive assertion in the test body
      OR
      a call to a RESOLVED helper whose body contains a primitive assertion/fail
      AND references the helper's argument(s) (an "argument anchor")

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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from codd.operational_e2e_audit import (
    _COVER_MARKER_RE,
    _iter_test_files,
    _load_optional_config,
    _rel_path,
    _resolve_vb_scan_dirs,
)
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
#:   ``helper_resolved``     — a resolved helper with primitive + arg anchor (pass)
#:   ``no_assertion``        — neither a primitive nor any assertion-like call
#:   ``unresolved_helper``   — an assertion-like call not resolvable 1-hop
#:   ``helper_no_primitive`` — resolved helper body has no primitive assertion/fail
#:   ``constant_helper``     — resolved helper asserts only constants (no anchor)
@dataclass(frozen=True)
class AssertionEvidence:
    ok: bool
    reason: str
    detail: str = ""


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
    ``unobservable_test_structure`` VIOLATION rather than a silent degrade — a
    SUPPORTED stack that yields no observable test is a false-green, not an
    unparseable one. An UNSUPPORTED file (no adapter / not handled) still degrades
    in either mode (never a false-RED). The greenfield autopilot passes True; the
    default is False for back-compat with non-autopilot callers.
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
            if adapter_handles and strict_observability:
                # OBSERVABILITY (contract authenticity.observable_in_supported_stack.v1):
                # the adapter RECOGNIZES this file type but extracted ZERO executable
                # test blocks though live coverage markers are present. That is NOT an
                # unsupported stack (which legitimately degrades) — it is an
                # unobservable coverage claim in a SUPPORTED stack, so it honest-fails
                # instead of silently degrading to a pass (the false-green this
                # contract closes).
                for marker in live_markers:
                    violations.append(
                        AuthenticityViolation(
                            kind="unobservable_test_structure",
                            vb_id=marker.vb_id,
                            path=rel,
                            line=marker.line,
                            message=(
                                f"{rel}:{marker.line} `codd: covers vb={marker.vb_id}` sits in a "
                                "recognized test file from which the structural parser extracted NO "
                                "executable test block — the coverage claim is unobservable (a marker "
                                "with no parseable test proves nothing). Write a real test case the "
                                "runner executes, or use `codd: blocked vb=… reason=…` if it genuinely "
                                "cannot run yet."
                            ),
                        )
                    )
                continue
            # Graceful degradation: an UNSUPPORTED stack/file (no adapter, or the
            # adapter does not handle this file), or strict observability off.
            # Stage 1 already ran; skip stages 2-3 with a warning (never false-RED).
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
            if block.has_assertion:
                evidence = AssertionEvidence(ok=True, reason="direct")
            else:
                try:
                    evidence = adapter.resolve_assertion_evidence(
                        block,
                        importer_text=text,
                        importer_rel=rel,
                        project_root=project_root,
                    )
                except Exception:  # noqa: BLE001 — resolver that throws ⇒ no evidence.
                    evidence = AssertionEvidence(
                        ok=False, reason="no_assertion", detail="resolver error"
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
    if evidence.reason == "unresolved_helper":
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
_PY_PRIMITIVE_ASSERT_RE = re.compile(
    r"(^|[^A-Za-z0-9_])(assert\b|pytest\.raises\b|pytest\.fail\s*\(|"
    r"self\.assert[A-Za-z]+\s*\(|self\.fail\s*\(|np\.testing\.assert)",
)
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
            has_assertion = bool(_PY_PRIMITIVE_ASSERT_RE.search(body_text))
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


def _extract_helper_calls(body_text: str) -> list[tuple[str, str]]:
    """Assertion-like ``name(args)`` calls in a test body → (name, args) pairs.

    Returns calls whose callee leading identifier matches the assertion-helper
    name set. ``args`` is the raw argument text (used for the argument-anchor
    check). Member calls (``foo.bar(...)``) are reduced to the final segment
    (``bar``) for the name test, but only assertion-ish names are considered —
    this is the candidate-selection step, never a pass.
    """

    out: list[tuple[str, str]] = []
    for match in _CALL_RE.finditer(body_text):
        callee = match.group("callee")
        segment = callee.split(".")[-1]
        if not _looks_like_assertion_helper(segment):
            continue
        args = _balanced_args(body_text, match.end("callee"))
        out.append((segment, args))
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


def _ts_imported_specifier(importer_text: str, symbol: str) -> str | None:
    """The module specifier that imports ``symbol`` into the importing file."""

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


def _py_imported_module(importer_text: str, symbol: str) -> str | None:
    """The module path a ``from <mod> import <symbol>`` binds ``symbol`` from."""

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
    imported_lookup: Callable[[str, str], str | None],
    module_resolver: Callable[[str, str, Path], Path | None],
    def_finder: Callable[[str, str], "tuple[str, list[str]] | None"],
    reexport_edges: Callable[[str, str], list[tuple[str, str]]] | None = None,
) -> AssertionEvidence:
    """Shared 1-hop helper-resolution engine (language plug-ins supply the rest).

    ``primitive_re`` detects a primitive assertion/fail in a helper body;
    ``imported_lookup(importer_text, symbol)`` returns the binding specifier/module;
    ``module_resolver(importer_rel, spec, root)`` resolves it to a file;
    ``def_finder(module_text, name)`` returns ``(body, params)`` for the helper;
    ``reexport_edges(module_text, symbol)`` (optional) returns the barrel
    re-export edges that can carry ``symbol`` onward, so a helper imported from a
    barrel index that only RE-EXPORTS its real definition is still reachable. A
    profile that supplies no follower simply never crosses a barrel (degrades to
    the 2.31.0 direct/simple-import behavior).
    """

    calls = _extract_helper_calls(block.body_text)
    if not calls:
        return AssertionEvidence(ok=False, reason="no_assertion")

    saw_unresolved = False
    saw_no_primitive = False
    saw_constant = False
    last_helper = ""
    for name, _args in calls:
        last_helper = name
        verdict = _resolve_one_helper(
            name=name,
            importer_text=importer_text,
            importer_rel=importer_rel,
            project_root=project_root,
            primitive_re=primitive_re,
            imported_lookup=imported_lookup,
            module_resolver=module_resolver,
            def_finder=def_finder,
            reexport_edges=reexport_edges,
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
    importer_text: str,
    importer_rel: str,
    project_root: Path,
    primitive_re: re.Pattern[str],
    imported_lookup: Callable[[str, str], str | None],
    module_resolver: Callable[[str, str, Path], Path | None],
    def_finder: Callable[[str, str], "tuple[str, list[str]] | None"],
    reexport_edges: Callable[[str, str], list[tuple[str, str]]] | None = None,
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
    """

    if hops <= 0 or name in seen:
        return AssertionEvidence(ok=False, reason="unresolved_helper", detail=name)
    seen = seen | {name}

    spec = imported_lookup(importer_text, name)
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
    if found is None:
        # Not defined in the binding module, no same-file def, and no resolvable
        # re-export chain reaches a def → unresolved (greenfield strict ⇒ fail).
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
    for inner_name, inner_args in _extract_helper_calls(body):
        inner_anchor = _arg_identifiers(inner_args)
        if not (inner_anchor & param_set):
            continue  # the inner call must carry THIS helper's data forward
        deeper = _resolve_one_helper(
            name=inner_name,
            importer_text=module_text,
            importer_rel=resolved_rel,
            project_root=project_root,
            primitive_re=primitive_re,
            imported_lookup=imported_lookup,
            module_resolver=module_resolver,
            def_finder=def_finder,
            reexport_edges=reexport_edges,
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
