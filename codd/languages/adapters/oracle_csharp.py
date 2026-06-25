"""C# ``dotnet-toolchain`` implement-oracle adapter (Contract Kernel oracle dispatch §5).

Modeled VERBATIM on the Go ``go-toolchain`` adapter (:mod:`codd.languages.adapters.oracle_go`):
a ``kind="composite"`` adapter whose scope is certified by
:meth:`DotnetToolchainOracleAdapter.certify_scope` and whose per-command verdict is
produced by :meth:`DotnetToolchainOracleAdapter.normalize_command_result`. The generic
:func:`codd.languages.oracle_executor.run_command_sequence` spawns the profile's
``build`` command (``dotnet build -c Release``) and hands each raw
``(returncode, stdout, stderr)`` here for a language-neutral verdict.

The anti-false-green discipline is IDENTICAL to Go (preserved / strengthened, never
weakened):

* returncode == 0 → ``is_clean=True`` with no findings (the positive proof of coherence).
* returncode != 0 with PARSEABLE MSBuild diagnostics (``error CS####``) →
  ``is_clean=False`` with those findings, each mapped to a language-neutral category
  by a DATA dict keyed on the CS code (NOT a ``language == "csharp"`` branch).
* returncode != 0 with NO parseable finding → per-line benign accounting
  (:func:`_dotnet_residual_is_benign`, the analogue of ``_go_residual_is_benign``):
  ``is_clean=True`` ONLY when EVERY non-blank residual line is recognizable MSBuild
  noise (the restore/version/build-result/timing banner, a ``warning CS####`` line —
  a warning is not a coherence failure, the first-run ``Welcome to .NET`` banner, …).
  The moment ANY line is unaccounted-for → ``is_clean=False`` with EMPTY findings; the
  generic executor then synthesizes an opaque ``environment_build_error`` RED. NEVER a
  vacuous green — a real positioned ``error CS####`` line is never swallowed by the
  noise filter (see the regression tests).

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


# ── C# CS-code → language-neutral category (DATA, not a language== branch) ────
#
# The MSBuild C# compiler reports diagnostics with a stable ``CS####`` code. This
# DATA dict maps the codes whose MEANING is known into the language-neutral evidence
# vocabulary; anything not in the table defaults to ``EVIDENCE_OTHER`` (a real
# coherence error that is neither an import/namespace miss nor a missing-symbol miss —
# e.g. a type mismatch, a duplicate definition). Mirrors the Go classifier's
# "undefined symbol → missing_symbol / cannot-find-package → module_resolution /
# everything else → other" policy, but expressed as DATA so the normalizer never
# branches on the language id.
#
#   * CS0246 (type or namespace name could not be found) and
#     CS0234 (the type/namespace does not exist in the namespace) are the
#     import/using-resolution family → module_resolution_error.
#   * CS0103 (name does not exist in the current context),
#     CS0117 (type has no definition for member) and
#     CS1061 (no accessible extension/member for the type) are the missing-symbol
#     family → missing_symbol.
_CS_CODE_CATEGORY: dict[str, str] = {
    "CS0246": EVIDENCE_MODULE_RESOLUTION,
    "CS0234": EVIDENCE_MODULE_RESOLUTION,
    "CS0103": EVIDENCE_MISSING_SYMBOL,
    "CS0117": EVIDENCE_MISSING_SYMBOL,
    "CS1061": EVIDENCE_MISSING_SYMBOL,
}

#: Default category for any ``error CS####`` not named in :data:`_CS_CODE_CATEGORY`
#: — a real positioned compile error that is still a coherence failure → ``other``.
_CS_DEFAULT_CATEGORY = EVIDENCE_OTHER


# ── C# / MSBuild diagnostic regexes ──────────────────────────────────────────

#: An MSBuild C# compiler ERROR with a file position:
#: ``Path.cs(LINE,COL): error CS####: <message> [project.csproj]``. The trailing
#: ``[project.csproj]`` is OPTIONAL (csc emits it under ``dotnet build`` but not in
#: every context), so it is tolerated/stripped. The path, line, col, code and message
#: are captured for classification + path attribution.
_CS_DIAG_LINE = re.compile(
    r"^(?P<path>[^\s(][^(\n]*\.cs)\((?P<line>\d+),(?P<col>\d+)\):\s*"
    r"error\s+(?P<code>CS\d+):\s*(?P<message>.+?)\s*$"
)

#: An MSBuild C# ERROR WITHOUT a file position — some errors (e.g. an assembly-level
#: or build-config error) are emitted as ``error CS####: <message>`` with no
#: ``Path.cs(line,col)`` prefix. Still a real coherence error → classified (a
#: position-less finding, ``path=None``). The leading anchor forbids a ``warning``
#: from matching (warnings are handled as noise, never as findings).
_CS_DIAG_NO_POS = re.compile(
    r"^(?:.*?:\s*)?error\s+(?P<code>CS\d+):\s*(?P<message>.+?)\s*$"
)

#: An MSBuild C# WARNING (positioned or not). A warning is NOT a coherence failure
#: (the same stance as Go's: ``go vet`` warnings never RED on their own) — so a
#: ``warning CS####`` line is accounted-for NOISE in the benign filter, never a
#: finding. Matched explicitly so a warning is never mistaken for an unaccounted line.
_CS_WARNING_LINE = re.compile(r"\bwarning\s+CS\d+:", re.IGNORECASE)

#: MSBuild banner / restore / build-result / timing envelope lines — emitted by
#: ``dotnet build`` around the real diagnostics. These are context/summary markers,
#: NOT diagnostics; SAFE to treat as noise in the per-line benign accounting because
#: a genuine compile failure ALWAYS co-emits a positioned ``error CS####`` line
#: (caught + classified RED) — filtering the envelope removes a false-RED WITHOUT
#: hiding a real failure. Mirrors Go's ``# pkg`` header + ``go test`` run-summary
#: envelope filtering.
_DOTNET_NOISE_RES: tuple[re.Pattern[str], ...] = (
    # `dotnet`/MSBuild restore + version banner
    re.compile(r"^Determining projects to restore\b"),
    re.compile(r"^All projects are up-to-date for restore\b"),
    re.compile(r"^\s*Restored\b"),  # "Restored /path/foo.csproj (in 1.2 sec)."
    re.compile(r"^\s*Restore\b.*\bcomplete\b", re.IGNORECASE),  # "Restore complete (0.1s)"
    re.compile(r"^MSBuild version\b"),
    re.compile(r"^Microsoft \(R\) Build Engine\b"),
    re.compile(r"^Copyright \(C\) Microsoft Corporation\b", re.IGNORECASE),
    # the project's own progress line: "  foo -> /path/bin/Release/net8.0/foo.dll"
    re.compile(r"->.*\.(?:dll|exe)\s*$"),
    # build-result envelope
    re.compile(r"^Build succeeded\.?\s*$", re.IGNORECASE),
    re.compile(r"^Build succeeded with\b", re.IGNORECASE),  # "Build succeeded with N warning(s)"
    re.compile(r"^Build FAILED\.?\s*$", re.IGNORECASE),
    re.compile(r"^\s*\d+\s+Warning\(s\)\s*$", re.IGNORECASE),
    re.compile(r"^\s*\d+\s+Error\(s\)\s*$", re.IGNORECASE),
    re.compile(r"^\s*Time Elapsed\b", re.IGNORECASE),
    # SDK first-run banner ("Welcome to .NET ...", and its telemetry/usage follow-up)
    re.compile(r"^Welcome to \.NET\b", re.IGNORECASE),
    re.compile(r"^Tools\b", re.IGNORECASE),
    re.compile(r"^Determining\b"),  # generic "Determining ..." restore chatter
    # a bare rule/separator line
    re.compile(r"^-{2,}\s*$"),
)


# ── pure helpers (mirroring oracle_go.py's helpers) ───────────────────────────


def _dotnet_is_noise_line(line: str) -> bool:
    """True when one stripped line is recognizable MSBuild banner/restore/summary noise.

    A ``warning CS####`` line is ALSO noise (a warning is not a coherence failure) — it
    is handled here so the benign filter accounts for it without ever treating it as a
    finding. (The error-diagnostic regexes are checked by the CALLER before this, so a
    real ``error CS####`` line is classified, never reaches the noise test.)
    """
    if _CS_WARNING_LINE.search(line):
        return True
    return any(rx.search(line) for rx in _DOTNET_NOISE_RES)


def _classify_cs_diagnostic(code: str) -> tuple[str, str]:
    """Classify one C# diagnostic ``code`` → ``(category, code)`` via the DATA dict.

    The category comes from :data:`_CS_CODE_CATEGORY` (import/namespace family →
    module_resolution; missing-name/member family → missing_symbol), defaulting to
    :data:`_CS_DEFAULT_CATEGORY` (``other``) for any other ``error CS####``. The
    returned ``code`` is the CS number itself (e.g. ``"CS0246"``) — the C#-specific
    code carried alongside the language-neutral category, exactly as the Go adapter
    carries ``"GO_UNDEFINED"`` etc. This is DATA-driven, never a language== branch.
    """
    category = _CS_CODE_CATEGORY.get(code, _CS_DEFAULT_CATEGORY)
    return category, code


def _cs_rel_path(raw: str, project_root: Path) -> str | None:
    """Normalize a C# diagnostic path → project-relative POSIX (best-effort)."""
    cleaned = (raw or "").strip()
    if not cleaned:
        return None
    try:
        resolved = (project_root / cleaned).resolve()
        return resolved.relative_to(Path(project_root).resolve()).as_posix()
    except (ValueError, OSError):
        return PurePosixPath(cleaned.replace("\\", "/")).as_posix()


def _strip_trailing_project(message: str) -> str:
    """Drop a trailing ``[project.csproj]`` (or ``[.../foo.csproj]``) annotation.

    csc appends the owning project file in brackets at the end of a diagnostic under
    ``dotnet build``. It is build-context noise, not part of the human message — strip
    it so the surfaced message is the compiler's actual text.
    """
    return re.sub(r"\s*\[[^\]]*\.(?:csproj|sln|slnx)\]\s*$", "", message).strip()


def _parse_dotnet_tool_output(
    output: str, *, tool: str, project_root: Path
) -> tuple[list[ImplementOracleFinding], list[str]]:
    """Parse ``dotnet build`` output → (findings, editable failed paths).

    Walks every line: a positioned ``Path.cs(line,col): error CS####: msg`` line and a
    position-less ``error CS####: msg`` line both become findings (classified by the
    CS-code DATA dict); ``warning CS####`` lines and banner/summary lines are skipped.
    Only ``error`` lines (never ``warning``) produce findings — a warning is not a
    coherence failure.
    """
    findings: list[ImplementOracleFinding] = []
    failed_paths: list[str] = []
    for raw_line in (output or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        # A warning is never a finding (checked first so a positioned warning that
        # happens to resemble a diagnostic shape is excluded).
        if _CS_WARNING_LINE.search(line):
            continue
        positioned = _CS_DIAG_LINE.match(line)
        if positioned is not None:
            code = positioned.group("code")
            category, norm_code = _classify_cs_diagnostic(code)
            message = _strip_trailing_project(positioned.group("message").strip())
            rel = _cs_rel_path(positioned.group("path"), project_root)
            findings.append(
                ImplementOracleFinding(
                    category=category,
                    code=norm_code,
                    message=f"[{tool}] {message}",
                    path=rel,
                )
            )
            if rel and rel not in failed_paths:
                failed_paths.append(rel)
            continue
        no_pos = _CS_DIAG_NO_POS.match(line)
        if no_pos is not None:
            code = no_pos.group("code")
            category, norm_code = _classify_cs_diagnostic(code)
            message = _strip_trailing_project(no_pos.group("message").strip())
            findings.append(
                ImplementOracleFinding(
                    category=category,
                    code=norm_code,
                    message=f"[{tool}] {message}",
                    path=None,
                )
            )
    return findings, failed_paths


def _dotnet_residual_is_benign(output: str) -> bool:
    """True iff a non-zero ``dotnet`` exit's output is ONLY noise (no real error line).

    Returns True (benign — let the non-zero exit pass) when EVERY non-blank line is a
    recognizable MSBuild banner/restore/build-result/timing line or a ``warning CS####``
    line (a warning is not a coherence failure). Returns False — surface an honest
    opaque failure — the moment ANY line is unaccounted-for, so a genuinely opaque
    toolchain error never hides behind a benign verdict (anti-false-green: conservative
    by construction). CRITICALLY, a real ``error CS####`` line (positioned or not) is
    NOT noise: it makes this return False, so it can never be swallowed (regression-
    tested). Mirrors Go's ``_go_residual_is_benign``.
    """
    for raw_line in (output or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        # A real error diagnostic is NEVER benign — account for it as "not noise" so a
        # nonzero exit whose only residual is an error line is correctly non-clean.
        if _CS_DIAG_LINE.match(line) is not None or (
            _CS_DIAG_NO_POS.match(line) is not None and not _CS_WARNING_LINE.search(line)
        ):
            return False
        if _dotnet_is_noise_line(line):
            continue
        return False  # an unaccounted-for line → not benign
    return True


def _dotnet_module_root(ctx: OracleContext) -> Path:
    """The directory the dotnet commands run from = ``project_root / layout.module_root``.

    The C# profile's ``layout.module_root`` is ``"."`` (the project/solution at the
    repo root), so this is normally the project root. Mirrors Go's
    ``_go_module_root``: the generic executor independently substitutes
    ``{module_root}`` into each command's ``cwd``; this resolves the SAME directory so
    scope certification inspects the directory the commands run in.
    """
    module_root = (getattr(ctx.layout_profile, "module_root", ".") or ".").strip()
    if module_root in ("", "."):
        return ctx.project_root
    return ctx.project_root / module_root


def _dotnet_has_project_file(module_root: Path) -> bool:
    """True when a ``.csproj`` OR a ``.sln``/``.slnx`` exists under the module root.

    Skips ``bin``/``obj``/``.git`` directories (build output + VCS). A C# scope with
    NO project/solution file is not a buildable scope — :meth:`certify_scope` hard-fails
    on it (a green ``dotnet build`` over a project-less scope would be a false-green).
    """
    if not module_root.is_dir():
        return False
    for pattern in ("*.csproj", "*.sln", "*.slnx"):
        for path in module_root.rglob(pattern):
            parts = set(path.parts)
            if {"bin", "obj", ".git"} & parts:
                continue
            if path.is_file():
                return True
    return False


def _dotnet_has_cs_source(module_root: Path) -> bool:
    """True when at least one ``.cs`` source file exists under the module root.

    Skips ``bin``/``obj``/``.git`` directories. A green ``dotnet build`` over a scope
    with no ``.cs`` proves nothing — :meth:`certify_scope` hard-fails on it (mirrors
    Go's "no .go file → hard fail").
    """
    if not module_root.is_dir():
        return False
    for path in module_root.rglob("*.cs"):
        parts = set(path.parts)
        if {"bin", "obj", ".git"} & parts:
            continue
        if path.is_file():
            return True
    return False


class DotnetToolchainOracleAdapter:
    """``implement_oracle`` adapter for the .NET toolchain (``adapter: dotnet-toolchain``).

    A ``kind="composite"`` adapter mirroring :class:`~codd.languages.adapters.oracle_go.GoToolchainOracleAdapter`:
    it implements :meth:`certify_scope` (called once before any command) and
    :meth:`normalize_command_result` (called per command of the ``build`` sequence). It
    does NOT implement ``execute`` — C# is a shell-command-sequence oracle run by the
    generic executor, not an in-process composite (that is Python's ``kind="adapter"``).
    """

    def certify_scope(self, ctx: OracleContext) -> str:
        """Certify the C# project's scope, else raise :class:`OracleScopeError`.

        Anti-false-green: a green oracle over an UNSCOPED or EMPTY scope proves
        nothing. So this hard-fails (RED, never a silent pass) when:

        * there is NO ``.csproj`` AND NO ``.sln``/``.slnx`` under the module root —
          without a project/solution file ``dotnet build`` is not a project build at
          all, so a green result would prove nothing; OR
        * there is no ``.cs`` source file under the module root — a green
          ``dotnet build`` over a source-less scope is meaningless (mirrors Go's
          "empty module is a hard fail" and the Python certifier's "empty required
          root is a hard fail").

        Returns a human-readable certification detail on success.
        """
        module_root = _dotnet_module_root(ctx)
        if not _dotnet_has_project_file(module_root):
            raise OracleScopeError(
                "dotnet implement-time oracle cannot be certified: no .csproj and no "
                f".sln/.slnx under the module root ({module_root!s}), so `dotnet build` "
                "is not a project build and a green result would prove nothing. Ensure "
                "the C# project/solution was scaffolded (a .csproj or .sln present)."
            )
        if not _dotnet_has_cs_source(module_root):
            raise OracleScopeError(
                "dotnet implement-time oracle cannot be certified: no .cs files under "
                f"the module root ({module_root!s}). A green `dotnet build` over a scope "
                "with no source proves nothing — an empty scope is a HARD FAIL "
                "(anti-false-green). Ensure the units were generated."
            )
        return (
            "dotnet oracle scope certified: a .csproj/.sln(.slnx) project file + ≥1 .cs "
            f"source file present under module_root='{module_root!s}' "
            "(dotnet build -c Release covers the whole project)"
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
        """Normalize one ``dotnet`` command's raw result → a language-neutral observation.

        Semantics IDENTICAL to the Go adapter (``GoToolchainOracleAdapter.normalize_command_result``):

        * returncode == 0 → ``is_clean=True``, no findings (the positive proof of
          coherence; a finding parsed on a zero exit still wins — not-clean beats a
          clean-looking exit).
        * returncode != 0 with PARSEABLE ``error CS####`` diagnostics → ``is_clean=False``
          with those findings.
        * returncode != 0 with NO parseable finding → per-line benign accounting
          (:func:`_dotnet_residual_is_benign`): ``is_clean=True`` ONLY IF every residual
          line is recognizable MSBuild noise (banner/restore/build-result/timing or a
          ``warning CS####`` line). The moment ANY line is unaccounted-for,
          ``is_clean=False`` with EMPTY findings — the generic executor then synthesizes
          an opaque ``environment_build_error`` RED (never a benign pass). This per-line
          conservatism is the anti-false-green guard; it is NOT a blanket "no findings →
          clean".
        """
        module_root = _dotnet_module_root(ctx)
        output = "\n".join(part for part in (stdout, stderr) if part)

        findings, failed_paths = _parse_dotnet_tool_output(
            output, tool=command_id, project_root=module_root
        )

        if returncode == 0:
            # A clean exit is the positive proof of coherence. (Findings on a zero
            # exit are not expected for dotnet; if any were parsed they still win — a
            # not-clean signal beats a clean-looking exit.)
            if findings:
                return OracleStepObservation(
                    is_clean=False,
                    findings=tuple(findings),
                    failed_paths=tuple(failed_paths),
                    detail=f"dotnet {command_id} exited 0 but parsed {len(findings)} diagnostic(s)",
                )
            return OracleStepObservation(is_clean=True)

        if findings:
            return OracleStepObservation(
                is_clean=False,
                findings=tuple(findings),
                failed_paths=tuple(failed_paths),
                detail=f"dotnet {command_id} reported {len(findings)} diagnostic(s)",
            )

        # Non-zero exit, NO parseable code finding: per-line benign accounting.
        if _dotnet_residual_is_benign(output):
            # Every residual line is MSBuild noise / a warning → benign (env/banner
            # state, not incoherence) → CLEAN.
            return OracleStepObservation(is_clean=True)

        # Unaccounted-for non-zero exit → NOT clean, EMPTY findings. The executor
        # synthesizes an opaque environment_build_error RED (anti-false-green: a
        # non-zero exit the adapter cannot name is never a benign pass).
        return OracleStepObservation(
            is_clean=False,
            detail=(
                f"`dotnet {command_id}` exited {returncode} with no parseable diagnostic "
                "and non-benign residual output — opaque environment/build error"
            ),
        )


__all__ = ["DotnetToolchainOracleAdapter"]
