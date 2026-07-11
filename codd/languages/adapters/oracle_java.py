"""Java ``java-toolchain`` implement-oracle adapter (Contract Kernel oracle dispatch §5).

The Java tool SEMANTICS — modeled EXACTLY on the Go ``go-toolchain`` adapter
(:mod:`codd.languages.adapters.oracle_go`). Java's implement-time oracle is the
``compile`` command (the profile's ``mvn -q -e compile``): the generic
:func:`codd.languages.oracle_executor.run_command_sequence` spawns it and hands the
raw ``(returncode, stdout, stderr)`` to this adapter's
:meth:`JavaToolchainOracleAdapter.normalize_command_result` for a language-neutral
verdict. Scope is certified by :meth:`JavaToolchainOracleAdapter.certify_scope`.

Two anti-false-green cores preserved VERBATIM from Go (never weakened):

* SCOPE certification fails CLOSED. A green oracle over a scope with no ``pom.xml``
  (the Java module manifest, mirroring Go's ``go.mod``) or no ``.java`` file proves
  nothing — both are HARD FAILS (:class:`OracleScopeError`), exactly like Go's
  "no go.mod / no .go file is a hard fail". An un-built / empty module is RED, never
  a silent pass.
* the per-line BENIGN ACCOUNTING (the anti-false-green core) is the SAME shape as
  Go's ``_go_residual_is_benign``, ported here as ``_java_residual_is_benign``: a
  nonzero exit is clean ONLY when EVERY non-blank residual line is recognizable
  toolchain noise (maven ``[INFO]``/``[WARNING]`` banners, ``BUILD FAILURE``/``BUILD
  SUCCESS`` summaries, ``Downloading``/``Downloaded`` progress, ``---`` separators,
  and the ``[ERROR]`` summary-echo lines that DON'T carry a ``File.java:`` position).
  The MOMENT any line is unaccounted-for → NOT clean with EMPTY findings (the
  executor synthesizes an opaque ``environment_build_error`` RED — never a benign
  pass). A real positioned diagnostic is NEVER swallowed by the noise filter: a
  line carrying a ``File.java:`` position is classified as a finding, not noise
  (regression-tested, mirroring Go's "ok-named-package" test).

LEAF rule (no import cycle): imports ONLY stdlib + the oracle value-objects leaf
(:mod:`codd.implement_oracle_types`) + the adapter protocol leaf
(:mod:`codd.languages.adapters.implement_oracle`) + the profile model
(:mod:`codd.languages.profile`). It MUST NOT import the gate
(:mod:`codd.implement_oracle`), the registry, or the generic executor — the
dependency edge runs gate → executor → adapters → leaf types, never back.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from codd.implement_oracle_types import (
    EVIDENCE_MISSING_SYMBOL,
    EVIDENCE_MODULE_RESOLUTION,
    EVIDENCE_OTHER,
    ImplementOracleFinding,
    OracleScopeError,
)
from codd.languages.adapters.implement_oracle import (
    OracleContext,
    OracleStepObservation,
)
from codd.languages.profile import CommandSpec


# ── Java diagnostic regexes ──────────────────────────────────────────────────

#: A javac / maven-compiler diagnostic with a file position. The canonical javac
#: shape is ``File.java:LINE: error: <message>``. We tolerate (a) a leading path
#: segment (``src/main/java/Foo.java:7: ...``), (b) a maven ``[ERROR] `` prefix
#: (maven echoes compiler diagnostics with that prefix), and the trailing message
#: is captured so the symbol/module sub-classifier can run. ``severity`` is captured
#: but only ``error`` positioned lines become findings (a positioned ``warning:`` is
#: not a coherence failure — mirrors Go's "only real diagnostics are findings").
_JAVA_DIAG_LINE = re.compile(
    r"^(?:\[ERROR\]\s*)?(?P<path>[^\s:][^:\n]*\.java):(?P<line>\d+):\s*"
    r"(?P<severity>error|warning):\s*(?P<message>.+?)\s*$"
)

#: The bracket form maven's compiler plugin sometimes emits:
#: ``File.java:[LINE,COL] <message>`` (no ``error:`` token — maven already grouped
#: it under a ``COMPILATION ERROR`` banner). Tolerates a leading ``[ERROR] `` prefix
#: and a leading path. There is no ``severity`` token here; maven only emits the
#: bracket form for ERRORS (warnings keep the ``warning:`` word form), so a matched
#: bracket line is a compile ERROR → classified, never tolerated as noise.
_JAVA_BRACKET_DIAG_LINE = re.compile(
    r"^(?:\[ERROR\]\s*)?(?P<path>[^\s:][^:\n]*\.java):\[(?P<line>\d+),(?P<col>\d+)\]\s*"
    r"(?P<message>.+?)\s*$"
)

#: ``cannot find symbol`` — javac's "a name this file demands is not defined". The
#: Java analogue of Go's ``undefined:`` / TS2304 → missing_symbol (RED).
_JAVA_CANNOT_FIND_SYMBOL_RE = re.compile(r"\bcannot find symbol\b")

#: ``package <P> does not exist`` — javac's import-not-resolvable diagnostic (an
#: import of a package no source/dependency provides). The Java analogue of Go's
#: "cannot find module providing package" → module_resolution_error (RED).
#:
#: NOTE (anti-false-RED divergence from Go, see report §d): Go TOLERATES a missing
#: THIRD-PARTY package (an uninstalled dependency is an env concern under
#: -mod=readonly). The Java ``compile`` oracle does NOT pre-tolerate by import path:
#: javac under maven only emits ``package X does not exist`` AFTER maven resolved the
#: classpath, so a surviving ``does not exist`` is a genuine first-party coherence
#: gap (the file imports a package the project does not produce), NOT an uninstalled
#: dependency. Classifying it RED matches javac's own semantics and never invents a
#: false-RED for a dependency that maven would have placed on the classpath.
_JAVA_PACKAGE_NOT_EXIST_RE = re.compile(
    r"\bpackage\s+(?P<pkg>[\w.]+)\s+does not exist\b"
)

#: Maven's INFO/WARNING banner lines (``[INFO] ...`` / ``[WARNING] ...``) — context
#: markers, not diagnostics. ``[ERROR] `` is DELIBERATELY excluded: an ``[ERROR] ``
#: line that ALSO carries a ``File.java:`` position is a real diagnostic (parsed by
#: ``_JAVA_DIAG_LINE``); only the ``[ERROR] `` summary-echo lines WITHOUT a position
#: are accounted as noise (see ``_JAVA_ERROR_SUMMARY_RE``).
_JAVA_INFO_BANNER_RE = re.compile(r"^\[(?:INFO|WARNING)\]")

#: Maven's ``BUILD SUCCESS`` / ``BUILD FAILURE`` summary line (sometimes prefixed
#: ``[INFO] ``). A run-result SUMMARY, NOT a diagnostic — the REAL reason for a
#: failure is the positioned ``File.java:`` line above it (independently classified).
_JAVA_BUILD_SUMMARY_RE = re.compile(r"^(?:\[INFO\]\s*)?BUILD (?:SUCCESS|FAILURE)\b")

#: Maven's dependency-download progress (``Downloading from ...`` / ``Downloaded
#: from ...`` / ``Progress (1): ...``). Pure environment/network noise.
_JAVA_DOWNLOAD_RE = re.compile(r"^(?:\[INFO\]\s*)?(?:Downloading|Downloaded|Progress)\b")

#: Maven's ``--- maven-compiler-plugin:... ---`` / ``------`` separator banners.
_JAVA_SEPARATOR_RE = re.compile(r"^(?:\[INFO\]\s*)?-{3,}")

#: Maven's ``[ERROR] `` SUMMARY-echo lines that do NOT carry a ``File.java:``
#: position: the ``COMPILATION ERROR`` banner, the ``BUILD FAILURE`` echo, the
#: ``-> [Help 1]`` pointer, the ``Re-run Maven ...`` / ``For more information ...``
#: advice, and the bare ``[ERROR]`` separators maven prints around the diagnostic
#: block. These restate that SOMETHING failed; the positioned ``File.java:`` line
#: (parsed above) is the authoritative diagnostic, so the summary echo is noise.
#: CRITICAL: this is only consulted for lines that did NOT match a positioned
#: diagnostic regex — a real ``[ERROR] File.java:..`` line is a finding first, so
#: this never swallows a genuine diagnostic (regression-tested).
_JAVA_ERROR_SUMMARY_RE = re.compile(
    r"^\[ERROR\]\s*(?:$|"  # bare ``[ERROR]`` separator
    r"COMPILATION ERROR|"
    r"BUILD FAILURE|"
    r"Failed to execute goal|"
    r"-> \[Help|"
    r"Re-run Maven|"
    r"For more information|"
    r"To see the full stack trace|"
    r"after the errors? )",
)

#: A POSITIONLESS fatal compiler-plugin failure. javac/maven emit it WITHOUT any
#: ``File.java:`` anchor — canonical shapes:
#:   * ``[ERROR] Failed to execute goal …maven-compiler-plugin…: Fatal error
#:     compiling: error: release version 21 not supported -> [Help 1]``
#:   * ``[ERROR] Fatal error compiling: error: invalid target release: 21``
#: The tail after ``Fatal error compiling:`` is the authoritative message (the
#: trailing ``-> [Help N]`` pointer is stripped). Without this catch the whole run
#: had NO parseable diagnostic — the ``Failed to execute goal`` wrapper is
#: summary-echo noise — and collapsed to an opaque environment_build_error that
#: aborts the repair loop (java2 exprcalc dogfood, 2026-07-11). The C# adapter's
#: positionless ``error CS####`` catch (``_CS_DIAG_NO_POS``) is the parity
#: precedent for a positionless first-class diagnostic.
_JAVA_FATAL_COMPILING_RE = re.compile(
    r"\bFatal error compiling:\s*(?P<message>.+?)\s*(?:->\s*\[Help[^\]]*\]?\s*)?$"
)


# ── pure helpers ─────────────────────────────────────────────────────────────


def _classify_java_diagnostic(message: str) -> tuple[str, str]:
    """Classify one Java compile-error message → ``(category, code)``.

    Mirrors :func:`codd.languages.adapters.oracle_go._classify_go_diagnostic`'s
    structure, minus Go's third-party tolerance (see ``_JAVA_PACKAGE_NOT_EXIST_RE``
    docstring for why javac's ``package X does not exist`` is always a first-party
    coherence gap under maven). Unlike Go this never returns ``None`` — every
    positioned ``error:`` IS a coherence failure (a missing symbol, an unresolved
    package, or some other type error), classified into one of the three buckets.
    """
    if _JAVA_CANNOT_FIND_SYMBOL_RE.search(message):
        return EVIDENCE_MISSING_SYMBOL, "JAVA_CANNOT_FIND_SYMBOL"
    if _JAVA_PACKAGE_NOT_EXIST_RE.search(message):
        return EVIDENCE_MODULE_RESOLUTION, "JAVA_PACKAGE_DOES_NOT_EXIST"
    # A real positioned compile diagnostic that is neither of the above (a type
    # mismatch, an incompatible-types error, a method-not-found, …) is still a
    # coherence failure → other.
    return EVIDENCE_OTHER, "JAVA_COMPILE_ERROR"


def _java_rel_path(raw: str, project_root: Path) -> str | None:
    """Normalize a javac diagnostic path → project-relative POSIX (or best-effort)."""
    cleaned = (raw or "").strip()
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if not cleaned:
        return None
    try:
        resolved = (project_root / cleaned).resolve()
        return resolved.relative_to(Path(project_root).resolve()).as_posix()
    except (ValueError, OSError):
        return PurePosixPath(cleaned.replace("\\", "/")).as_posix()


def _match_java_diagnostic(line: str):
    """Return ``(path, message)`` for a positioned ERROR diagnostic line, else ``None``.

    Recognizes BOTH the ``File.java:LINE: error: <msg>`` word form and the
    ``File.java:[LINE,COL] <msg>`` maven bracket form. A positioned ``warning:`` is
    NOT a diagnostic (returns ``None``) — only an ``error`` is a coherence failure,
    mirroring Go's "only real diagnostics are findings".
    """
    m = _JAVA_DIAG_LINE.match(line)
    if m is not None:
        if m.group("severity") != "error":
            return None  # a positioned warning is not a coherence failure
        return m.group("path"), m.group("message").strip()
    b = _JAVA_BRACKET_DIAG_LINE.match(line)
    if b is not None:
        return b.group("path"), b.group("message").strip()
    return None


def _parse_java_tool_output(
    output: str, *, tool: str, project_root: Path
) -> tuple[list[ImplementOracleFinding], list[str]]:
    """Parse javac/maven-compiler output → (findings, editable failed paths).

    Walks positioned ``File.java:LINE: error: message`` (and the maven bracket
    ``File.java:[LINE,COL] message``) lines and classifies each via
    :func:`_classify_java_diagnostic`. Non-positioned noise (maven banners/summaries)
    is skipped here (it is accounted for separately by ``_java_residual_is_benign``
    when the exit is nonzero with no parsed finding).
    """
    findings: list[ImplementOracleFinding] = []
    failed_paths: list[str] = []
    seen_fatal: set[str] = set()
    for raw_line in (output or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        matched = _match_java_diagnostic(line)
        if matched is None:
            # A positionless ``Fatal error compiling: …`` (toolchain-level compile
            # failure, e.g. a --release the installed JDK cannot satisfy) is a
            # first-class diagnostic even without a File.java anchor. Deduped by
            # message: maven prints the same fatal in both the Failed-to-execute-
            # goal wrapper and the bare echo form.
            fatal = _JAVA_FATAL_COMPILING_RE.search(line)
            if fatal is not None:
                message = fatal.group("message").strip()
                if message not in seen_fatal:
                    seen_fatal.add(message)
                    findings.append(
                        ImplementOracleFinding(
                            category=EVIDENCE_OTHER,
                            code="JAVA_FATAL_COMPILING",
                            message=f"[{tool}] Fatal error compiling: {message}",
                            path=None,
                        )
                    )
            continue
        path_raw, message = matched
        category, code = _classify_java_diagnostic(message)
        rel = _java_rel_path(path_raw, project_root)
        findings.append(
            ImplementOracleFinding(
                category=category,
                code=code,
                message=f"[{tool}] {message}",
                path=rel,
            )
        )
        if rel and rel not in failed_paths:
            failed_paths.append(rel)
    return findings, failed_paths


def _java_residual_is_benign(output: str) -> bool:
    """True iff a non-zero maven exit's output is ONLY recognizable toolchain noise.

    Returns True (benign — let the non-zero exit pass) when EVERY non-blank line is
    a maven ``[INFO]``/``[WARNING]`` banner, a ``BUILD SUCCESS``/``BUILD FAILURE``
    summary, a download-progress line, a ``---`` separator, or an ``[ERROR] ``
    summary-echo line that does NOT carry a ``File.java:`` position. Returns False —
    i.e. surface an honest opaque failure — the moment any line is unaccounted-for
    (a non-noise, non-diagnostic line), so a genuinely opaque toolchain error never
    hides behind a benign verdict (anti-false-green: conservative by construction,
    mirroring Go's ``_go_residual_is_benign``).

    A real POSITIONED diagnostic is, by construction, NOT benign: it matches a
    diagnostic regex (so ``_match_java_diagnostic`` would have produced a finding) —
    it is NEVER classified as noise here (the caller only reaches this helper when
    NO finding was parsed; and even then a positioned line returns False, keeping the
    run RED). This is the analogue of Go's "ok-named-package must not be filtered".
    """
    for raw_line in (output or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        # A positioned diagnostic is never noise (anti-false-green): if a line carries
        # a ``File.java:`` position it is a real failure signal, not an accounted-for
        # banner — so it is unaccounted-for as NOISE → not benign.
        if _match_java_diagnostic(line) is not None:
            return False
        if (
            _JAVA_INFO_BANNER_RE.match(line)
            or _JAVA_BUILD_SUMMARY_RE.match(line)
            or _JAVA_DOWNLOAD_RE.match(line)
            or _JAVA_SEPARATOR_RE.match(line)
            or _JAVA_ERROR_SUMMARY_RE.match(line)
        ):
            continue  # recognizable maven banner / summary / progress noise
        return False  # an unaccounted-for line → not benign
    return True


def _java_module_root(ctx: OracleContext) -> Path:
    """The directory the maven commands run from = ``project_root / layout.module_root``.

    The Java profile's ``layout.module_root`` is ``"."`` (``pom.xml`` at the repo
    root), so this is normally the project root. The generic executor independently
    substitutes ``{module_root}`` into each command's ``cwd``; this mirrors that
    resolution so the adapter inspects the SAME directory the commands run in
    (mirrors Go's ``_go_module_root``).
    """
    module_root = (getattr(ctx.layout_profile, "module_root", ".") or ".").strip()
    if module_root in ("", "."):
        return ctx.project_root
    return ctx.project_root / module_root


class JavaToolchainOracleAdapter:
    """``implement_oracle`` adapter for the Java toolchain (``adapter: java-toolchain``).

    A ``kind="composite"`` adapter: it implements :meth:`certify_scope` (called once
    before any command) and :meth:`normalize_command_result` (called per command of
    the ``compile`` sequence). It does NOT implement ``execute`` — Java is a
    shell-command-sequence oracle, run by the generic executor, not an in-process
    composite (that is Python's ``kind="adapter"``). Mirrors
    :class:`codd.languages.adapters.oracle_go.GoToolchainOracleAdapter`.
    """

    def certify_scope(self, ctx: OracleContext) -> str:
        """Certify the Java module's scope, else raise :class:`OracleScopeError`.

        Anti-false-green: a green oracle over an UNSCOPED or EMPTY module proves
        nothing. So this hard-fails (RED, never a silent pass) when:

        * there is no ``pom.xml`` at the module root — without the maven manifest
          ``mvn compile`` is not a module build at all, and a green result would
          prove nothing (mirrors Go's "no go.mod is a hard fail"); OR
        * there is no ``.java`` file under the module root — a green compile over an
          empty module is meaningless (mirrors Go's "no .go file is a hard fail" and
          the Python certifier's "empty required root is a hard fail").

        Returns a human-readable certification detail on success.
        """
        module_root = _java_module_root(ctx)
        pom = module_root / "pom.xml"
        if not pom.is_file():
            raise OracleScopeError(
                "java implement-time oracle cannot be certified: no pom.xml at the "
                f"module root ({module_root!s}), so `mvn -q -e compile` is not a maven "
                "module build and a green result would prove nothing. Ensure the Java "
                "module was scaffolded (pom.xml present)."
            )
        has_java_file = False
        if module_root.is_dir():
            for path in module_root.rglob("*.java"):
                parts = set(path.parts)
                # Skip VCS metadata and maven's build-output tree (mirrors Go's skip
                # of .git/vendor) — a generated/compiled artifact is not source proof.
                if ".git" in parts or "target" in parts:
                    continue
                if path.is_file():
                    has_java_file = True
                    break
        if not has_java_file:
            raise OracleScopeError(
                "java implement-time oracle cannot be certified: no .java files under "
                f"the module root ({module_root!s}) (excluding .git/target). A green "
                "`mvn -q -e compile` over an empty module proves nothing — an empty "
                "scope is a HARD FAIL (anti-false-green). Ensure the units were "
                "generated."
            )
        return (
            "java oracle scope certified: pom.xml present + ≥1 .java file under "
            f"module_root='{module_root!s}' (mvn -q -e compile covers the module sources)"
        )

    def normalize_command_result(
        self,
        ctx: OracleContext,
        *,
        command_id: str,
        command: CommandSpec,  # noqa: ARG002 — signature parity with the protocol.
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> OracleStepObservation:
        """Normalize one ``mvn`` command's raw result → a language-neutral observation.

        Semantics (preserved EXACTLY from Go's ``normalize_command_result``):

        * returncode == 0 → ``is_clean=True``, no findings.
        * returncode != 0 with PARSEABLE positioned diagnostics (cannot find symbol /
          package does not exist / other compile error) → ``is_clean=False`` with
          those findings.
        * returncode != 0 with NO parseable finding → the per-line benign accounting
          (:func:`_java_residual_is_benign`): ``is_clean=True`` ONLY IF every residual
          line is recognizable maven banner / summary / progress / ``[ERROR]``-echo
          noise. The moment ANY line is unaccounted-for, ``is_clean=False`` with EMPTY
          findings — the generic executor then synthesizes an opaque
          ``environment_build_error`` RED (never a benign pass). This per-line
          conservatism is the anti-false-green guard: it is NOT a blanket "no findings
          → clean".
        """
        module_root = _java_module_root(ctx)
        output = "\n".join(part for part in (stdout, stderr) if part)

        findings, failed_paths = _parse_java_tool_output(
            output, tool=command_id, project_root=module_root
        )

        if returncode == 0:
            # A clean exit is the positive proof of coherence. (Findings on a zero
            # exit are not expected for maven; if any were parsed they still win — a
            # not-clean signal beats a clean-looking exit.)
            if findings:
                return OracleStepObservation(
                    is_clean=False,
                    findings=tuple(findings),
                    failed_paths=tuple(failed_paths),
                    detail=f"mvn {command_id} exited 0 but parsed {len(findings)} diagnostic(s)",
                )
            return OracleStepObservation(is_clean=True)

        if findings:
            return OracleStepObservation(
                is_clean=False,
                findings=tuple(findings),
                failed_paths=tuple(failed_paths),
                detail=f"mvn {command_id} reported {len(findings)} diagnostic(s)",
            )

        # Non-zero exit, NO parseable code finding: per-line benign accounting.
        if _java_residual_is_benign(output):
            # Every residual line is maven banner / summary / progress / [ERROR]-echo
            # noise → benign (env/build state with no diagnosable code failure) → CLEAN.
            return OracleStepObservation(is_clean=True)

        # Unaccounted-for non-zero exit → NOT clean, EMPTY findings. The executor
        # synthesizes an opaque environment_build_error RED (anti-false-green: a
        # non-zero exit the adapter cannot name is never a benign pass).
        return OracleStepObservation(
            is_clean=False,
            detail=(
                f"`mvn {command_id}` exited {returncode} with no parseable diagnostic and "
                "non-benign residual output — opaque environment/build error"
            ),
        )


__all__ = ["JavaToolchainOracleAdapter"]
