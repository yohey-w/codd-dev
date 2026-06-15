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
* **Stage 3 — assertion presence.** The attached test block must contain at least
  one ASSERTION (``expect`` for vitest/jest, ``assert`` / ``pytest.raises`` for
  pytest, ``t.Errorf`` / ``t.Fatalf`` for go, ``assert*`` for JUnit). An empty
  test, or a smoke test that runs code but checks nothing, claims a behavior it
  does not verify.

Stages 4 (behavior-anchor) and 5 (mutation probe) from the design are
deliberately NOT implemented here — proving that a natural-language VB row's
*meaning* is exercised by a test, from prose alone, over-constrains and produces
false-RED. They are left as explicit extension hooks (see ``AuthenticityHook``)
so a profile that ships machine-readable anchors / a mutation harness can opt in
later without reworking this gate.

GENERALITY (see ``feedback_codd_generality_preservation``): every language-
specific operation — locating the test block a marker sits in, detecting skip,
detecting an assertion — is delegated to a per-profile
:class:`~codd.project_types.TestBlockProfile` adapter resolved from the active
:class:`~codd.project_types.LayoutProfile`. The gate logic here is language-
agnostic. When NO adapter is available for a stack (an unknown language, or a
file the adapter cannot parse), the gate GRACEFULLY DEGRADES: stage 1 still
applies (it is language-agnostic), stages 2-3 are SKIPPED for that file with a
warning rather than failing — an un-parseable stack must never produce a
false-RED.
"""

from __future__ import annotations

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
    skipped/todo/disabled (a marker attached to it is not coverage). ``label`` is
    a short human name (the test title) for diagnostics.
    """

    start_line: int
    end_line: int
    is_executable: bool
    has_assertion: bool
    label: str = ""


class TestBlockProfile(Protocol):
    """Language-specific test-structure adapter (resolved from LayoutProfile).

    An implementation parses ONE test file's text into the executable test blocks
    it contains, with skip/assertion facts already resolved per block. Returning
    an empty list means "no recognizable test blocks" (the gate then degrades for
    that file). Implementations must be PURE and best-effort: a parse they cannot
    do should return ``[]`` (degrade), never raise.
    """

    def handles_file(self, rel_path: str) -> bool:
        """Whether this adapter recognizes ``rel_path`` as one of its test files."""

    def parse_test_blocks(self, text: str) -> list[TestBlock]:
        """Parse a test file's text into executable-test-block records."""


# ---------------------------------------------------------------------------
# Gate result model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthenticityViolation:
    """One inauthentic coverage CLAIM.

    ``kind`` is one of ``orphan`` (stage 1), ``skipped`` / ``unattached`` (stage
    2), ``no_assertion`` (stage 3). ``vb_id`` is the claimed behavior; ``path``
    and ``line`` locate the marker.
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


def _enclosing_block(marker_line: int, blocks: list[TestBlock]) -> TestBlock | None:
    """The smallest test block whose body contains ``marker_line``.

    A ``covers`` marker is conventionally written on the line(s) immediately
    ABOVE the test it annotates (a leading comment), so a marker that sits just
    before a block's first body line still belongs to it. We therefore accept a
    marker on the block's own lines OR on the lines directly preceding the block
    up to the previous block's end. The SMALLEST containing/owning block wins so
    a marker inside a nested ``it`` is attributed to the ``it``, not the
    enclosing ``describe``.
    """

    best: TestBlock | None = None
    for block in blocks:
        # Inside the block body, or on the comment line(s) immediately above it.
        if block.start_line - _MARKER_LOOKAHEAD <= marker_line <= block.end_line:
            if best is None or (block.end_line - block.start_line) < (best.end_line - best.start_line):
                best = block
    return best


# How many lines a leading ``// codd: covers`` comment may sit above the test it
# annotates. Markers are conventionally on the line directly above (1), but a
# blank line or a second marker line is common, so allow a small window.
_MARKER_LOOKAHEAD = 3


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
) -> AuthenticityReport:
    """Evaluate every ``covers vb=`` marker for structural authenticity.

    ``profile`` is the active :class:`~codd.project_types.LayoutProfile` (or any
    object exposing ``test_block_profile()``); when it is ``None`` or yields no
    adapter, stages 2-3 degrade to a warning and only stage 1 (orphan) applies.
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
        if adapter is not None and adapter.handles_file(rel):
            try:
                blocks = adapter.parse_test_blocks(text)
            except Exception:  # noqa: BLE001 — a parser that throws degrades, never fails.
                blocks = []
        if adapter is None or not adapter.handles_file(rel) or not blocks:
            # Graceful degradation: cannot structurally parse this stack/file.
            # Stage 1 already ran; skip stages 2-3 with a warning (never false-RED).
            degraded.append(rel)
            continue

        for marker in live_markers:
            block = _enclosing_block(marker.line, blocks)
            if block is None:
                # Stage 2 (attachment): marker is not inside/above any test block
                # (e.g. a file-top banner, a helper, dead region).
                violations.append(
                    AuthenticityViolation(
                        kind="unattached",
                        vb_id=marker.vb_id,
                        path=rel,
                        line=marker.line,
                        message=(
                            f"{rel}:{marker.line} `codd: covers vb={marker.vb_id}` is not attached to "
                            "an executable test block (no test case at/below the marker). A coverage "
                            "claim must sit on the test that proves the behavior."
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
            if not block.has_assertion:
                # Stage 3 (assertion presence): empty / smoke-only test.
                label = f" ({block.label})" if block.label else ""
                violations.append(
                    AuthenticityViolation(
                        kind="no_assertion",
                        vb_id=marker.vb_id,
                        path=rel,
                        line=marker.line,
                        message=(
                            f"{rel}:{marker.line} `codd: covers vb={marker.vb_id}` is attached to a "
                            f"test{label} with NO assertion — running code without checking an "
                            "outcome does not prove the behavior. Add an assertion that would FAIL "
                            "if the behavior were broken."
                        ),
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
        "describes and contains an assertion that would FAIL if X were broken. Fix each "
        "below by making the test real (enable it, add the missing assertion, or move the "
        "marker to the test that actually proves the behavior) — never by silencing the gate.",
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
# Built-in per-profile adapters (python, typescript/javascript). A new stack
# adds its adapter here and wires it in LayoutProfile.test_block_profile().
# ---------------------------------------------------------------------------


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


_PY_TEST_DEF_RE = re.compile(r"^(?P<indent>[ \t]*)def\s+(?P<name>test_[A-Za-z0-9_]*)\s*\(")
_PY_METHOD_TEST_RE = re.compile(r"^(?P<indent>[ \t]*)def\s+(?P<name>test_[A-Za-z0-9_]*)\s*\(self")
# A skip applied via decorator on the lines directly above the def.
_PY_SKIP_DECORATOR_RE = re.compile(
    r"@(?:pytest\.mark\.skip|pytest\.mark\.skipif|unittest\.skip|skip)\b", re.IGNORECASE
)
_PY_ASSERT_RE = re.compile(
    r"(^|[^A-Za-z0-9_])(assert\b|pytest\.raises\b|self\.assert[A-Za-z]+\s*\(|"
    r"self\.fail\s*\(|np\.testing\.assert|assert_that\s*\()",
)


@dataclass(frozen=True)
class PythonTestBlockProfile:
    """pytest / unittest structural adapter.

    A test block is a ``def test_*`` function; its body runs until the next line
    at an indent <= the def's indent (standard Python block scoping). Skip is a
    ``@pytest.mark.skip``/``skipif`` / ``unittest.skip`` decorator on the lines
    above the def, or a ``pytest.skip(...)`` call in the body. An assertion is an
    ``assert`` statement, ``pytest.raises``, a ``unittest`` ``self.assert*`` /
    ``self.fail`` call, or a common ``assert_that`` helper.
    """

    def handles_file(self, rel_path: str) -> bool:
        return rel_path.endswith(".py")

    def parse_test_blocks(self, text: str) -> list[TestBlock]:
        lines = text.splitlines()
        marker_lines = _shared_marker_lines(text)
        blocks: list[TestBlock] = []
        index = 0
        total = len(lines)
        while index < total:
            match = _PY_TEST_DEF_RE.match(lines[index])
            if not match:
                index += 1
                continue
            def_line = index + 1  # 1-based
            indent = len(match.group("indent").expandtabs())
            name = match.group("name")
            # Body: from the line after the def until dedent to <= indent.
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
            body_text = "\n".join(lines[def_line:body_end])  # def's body region
            # Skip via decorator above the def (scan upward over decorator lines).
            skipped = bool(re.search(r"\bpytest\.skip\s*\(", body_text)) or bool(
                re.search(r"\bself\.skipTest\s*\(", body_text)
            )
            up = index - 1
            while up >= 0:
                deco = lines[up].strip()
                if not deco:
                    up -= 1
                    continue
                if deco.startswith("@"):
                    if _PY_SKIP_DECORATOR_RE.search(deco):
                        skipped = True
                    up -= 1
                    continue
                break
            has_assertion = bool(_PY_ASSERT_RE.search(body_text))
            blocks.append(
                TestBlock(
                    start_line=def_line,
                    end_line=max(body_end, def_line),
                    is_executable=not skipped,
                    has_assertion=has_assertion,
                    label=name,
                )
            )
            index = scan
        return blocks


# vitest/jest: it(...) / test(...) blocks; describe(...) groups. Skip via
# ``.skip`` / ``.todo`` / ``xit`` / ``xtest`` / ``it.skipIf`` and a
# ``describe.skip`` wrapping. Assertion = ``expect(`` (or chai ``assert``).
_TS_TEST_OPEN_RE = re.compile(
    r"^(?P<indent>[ \t]*)"
    r"(?P<x>x)?"
    r"(?P<fn>it|test|describe)"
    r"(?P<mods>(?:\.(?:skip|only|todo|concurrent|sequential|each|skipIf|runIf|failing))*)"
    r"\s*(?:\.each\s*\([^)]*\)\s*)?\(",
)
_TS_ASSERT_RE = re.compile(r"(^|[^A-Za-z0-9_$])(expect\s*\(|assert\b|assert\.[A-Za-z]+\s*\(|should\b)")


@dataclass(frozen=True)
class TypeScriptTestBlockProfile:
    """vitest / jest structural adapter (TS + JS + JSX/TSX).

    Brace-matched: each ``it``/``test``/``describe`` block runs from its opening
    line to the matching close brace. Skip is ``.skip``/``.todo`` (or ``xit`` /
    ``xtest`` / ``xdescribe``) on the block, OR a skip on an enclosing
    ``describe``. Assertion = ``expect(`` (vitest/jest) or chai ``assert`` /
    ``should``. Only LEAF ``it``/``test`` blocks (and a bare ``test`` with no
    nested cases) are returned as coverage targets — a ``describe`` is a grouping
    container whose skip/non-skip is inherited by its children.
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

        # Coverage targets = it/test blocks, and describe blocks that contain no
        # nested it/test (a bare describe a marker could legitimately sit on).
        blocks: list[TestBlock] = []
        for span in spans:
            if span["fn"] == "describe":
                has_child_case = any(
                    other is not span
                    and other["fn"] in ("it", "test")
                    and span["start"] <= other["start"]
                    and other["end"] <= span["end"]
                    for other in spans
                )
                if has_child_case:
                    continue
            body = "\n".join(lines[span["start"] - 1 : span["end"]])
            blocks.append(
                TestBlock(
                    start_line=span["start"],
                    end_line=span["end"],
                    is_executable=not _is_skipped(span),
                    has_assertion=bool(_TS_ASSERT_RE.search(body)),
                    label=span["fn"],
                )
            )
        return blocks

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
