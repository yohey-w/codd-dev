"""Go ``go-toolchain`` implement-oracle adapter (Contract Kernel oracle dispatch §5).

The Go tool SEMANTICS — relocated VERBATIM from the gate's hand-written Go path
(``codd.implement_oracle._run_go_composite_oracle`` & helpers). The gate used to
dispatch on ``profile.language == "go"`` and run ``go build ./...`` + ``go vet
./...`` through ``_run_one_go_command``; from step 5 Go runs on the Contract-Kernel
contract path: the generic :func:`codd.languages.oracle_executor.run_command_sequence`
spawns the profile's ``typecheck`` (``go test -run ^$ ./...``) + ``vet`` commands and
hands each raw ``(returncode, stdout, stderr)`` to this adapter's
:meth:`GoToolchainOracleAdapter.normalize_command_result` for a language-neutral
verdict. Scope is certified by :meth:`GoToolchainOracleAdapter.certify_scope`.

Two semantic notes vs the old path (both preserved / strengthened, never weakened):

* ``typecheck`` is ``go test -run ^$ ./...`` (the profile command), which COMPILES
  ``*_test.go`` files — so a test-file compile error is caught by typecheck as a
  first-class scoped command (the old path caught it as a ``go vet`` side effect).
  No verdict regresses: a test-compile error was RED before (via vet) and is RED now.
* the third-party tolerance + per-line benign accounting (the anti-false-green
  core) is the SAME ``_go_residual_is_benign`` logic, moved here: a nonzero exit is
  clean ONLY when EVERY residual line is a ``# pkg`` header / VCS-stamp noise / a
  TOLERATED uninstalled-third-party diagnostic. A missing FIRST-PARTY package is
  RED; only genuinely third-party module paths are tolerated. The moment ANY line
  is unaccounted-for, the step is NOT clean (the executor synthesizes an opaque
  ``environment_build_error`` RED — never a benign pass).

LEAF rule (no import cycle): imports ONLY stdlib + the oracle value-objects leaf
(:mod:`codd.implement_oracle_types`) + the adapter protocol leaf
(:mod:`codd.languages.adapters.implement_oracle`). It MUST NOT import the gate
(:mod:`codd.implement_oracle`), the registry, or the generic executor — the
dependency edge runs gate → executor → adapters → leaf types, never back.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from codd.implement_oracle_types import (
    EVIDENCE_ENVIRONMENT_BUILD,
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


# ── Go diagnostic regexes (relocated verbatim from the gate) ─────────────────

#: A Go tool diagnostic with a file position: ``path:line:col: message`` — the
#: canonical compiler/vet line. An optional leading ``vet: `` prefix (vet emits
#: ``vet: ./file.go:..``) and a leading ``./`` on the path are tolerated. The
#: trailing message is captured so the symbol/module sub-classifier can run.
_GO_DIAG_LINE = re.compile(
    r"^(?:vet:\s*)?(?P<path>\.?/?[^\s:][^:\n]*\.go):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>.+?)\s*$"
)

#: ``go build``/``go vet`` group package diagnostics under a ``# <import-path>``
#: header (and sometimes a ``# [<import-path>]`` variant). These are context
#: markers, not diagnostics — skipped.
_GO_PKG_HEADER = re.compile(r"^#\s")

#: ``undefined: <name>`` — a symbol the package demands that nothing defines. The
#: Go analogue of TS2304/Python PY_IMPORT_NAME_NOT_FOUND → missing_symbol (RED).
_GO_UNDEFINED_RE = re.compile(r"\bundefined:\s*(?P<symbol>[\w.]+)")

#: ``cannot find module providing package <P>[: ...]`` / ``no required module
#: provides package <P>[: ...]`` and ``package <P> is not in std (...)`` — the
#: import-not-resolvable family. ``<P>`` is the IMPORT PATH; it is classified
#: first-party-vs-third-party by the module path (see ``_go_is_first_party_import``)
#: so an uninstalled THIRD-PARTY dep is tolerated. Both ``go build``/``go vet`` and
#: ``go test`` phrasings are accepted (go has emitted both across versions/contexts).
_GO_CANNOT_FIND_PKG_RE = re.compile(
    r"(?:cannot find (?:module providing )?package|no required module provides package)\s+(?P<pkg>[^\s:]+)"
)
_GO_NOT_IN_STD_RE = re.compile(r"package\s+(?P<pkg>\S+)\s+is not in std\b")

#: ``go build``'s VCS-stamping failure (a main package built outside a usable git
#: repo). An ENVIRONMENT artifact — filtered out, never a code-coherence RED.
_GO_VCS_STAMP_RE = re.compile(
    r"error obtaining VCS status|\buse -buildvcs=false\b", re.IGNORECASE
)

#: ``go test``'s per-package + final SUMMARY envelope — emitted by the ``typecheck``
#: command (``go test -run ^$ ./...``) but NOT by ``go build``/``go vet``. These are
#: run-result SUMMARY lines, NOT diagnostics: ``ok  \t<pkg>`` (pass), ``?   \t<pkg>``
#: (no test files), ``FAIL\t<pkg> [build failed]`` / ``[setup failed]`` (the binary
#: could not be built — the REAL reason is the positioned diagnostic ABOVE it, which
#: is independently parsed/classified), and the trailing bare ``FAIL``/``PASS``.
#: SAFE to treat as noise for the typecheck command: ``-run ^$`` runs ZERO tests, so
#: a nonzero exit can ONLY be a build/setup failure, which ALWAYS co-emits a
#: positioned diagnostic (caught) or a tolerated third-party import line. The
#: summary itself proves nothing the positioned line did not already prove — so
#: filtering it removes a false-RED WITHOUT ever hiding a real failure (a genuine
#: compile error's positioned line is still classified RED).
#:
#: The status token MUST be followed by whitespace (``ok  \t<pkg>``, ``FAIL\t<pkg>
#: [build failed]``) or be the whole line (bare ``PASS``/``FAIL``). A bare ``\b``
#: word-boundary would be UNSAFE: go emits sub-package diagnostics WITHOUT a ``./``
#: prefix (``ok/ok.go:2:19: undefined: X`` for a package dir literally named ``ok``),
#: and ``^ok\b.*`` would mis-filter that REAL positioned diagnostic as a summary line
#: → false-GREEN. Requiring ``\s`` after the token (which a ``ok/``-prefixed path
#: never has) classifies the diagnostic correctly (RED) while still matching every
#: real summary line. (Regression: test_positioned_diagnostic_in_ok_named_package_*.)
_GO_TEST_SUMMARY_RE = re.compile(r"^(?:ok|PASS|FAIL|\?)(?:\s.*)?$")


# ── pure helpers (relocated verbatim from the gate) ──────────────────────────


def _go_module_path(module_root: Path) -> str:
    """Read the ``module`` directive from ``<root>/go.mod`` (the first-party prefix).

    Empty string when go.mod is missing/unreadable or has no module line — the
    classifier then treats EVERY import as not-first-party (the conservative,
    never-a-false-RED side; :meth:`certify_scope` already hard-fails a missing go.mod).
    """
    gomod = module_root / "go.mod"
    try:
        text = gomod.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("module "):
            return stripped[len("module ") :].strip().strip('"')
    return ""


def _go_is_first_party_import(import_path: str, module_path: str) -> bool:
    """True when ``import_path`` is the module itself or a package under it.

    Go's first-party rule (design §1.5): ``import == module_path`` OR
    ``import.startswith(module_path + "/")``. Everything else (stdlib, an external
    ``github.com/...`` dep, a relative ``./x``) is NOT first-party — so a
    "cannot find module providing package" for it is an uninstalled-dependency
    ENVIRONMENT concern, not a coherence failure (anti-false-RED). With no known
    module path (undecidable), NOTHING is first-party — the conservative side that
    never invents a false-RED.
    """
    mod = (module_path or "").strip().strip("/")
    pkg = (import_path or "").strip()
    if not mod or not pkg:
        return False
    return pkg == mod or pkg.startswith(mod + "/")


def _classify_go_diagnostic(
    message: str, *, module_path: str
) -> tuple[str, str] | None:
    """Classify one Go diagnostic message → ``(category, code)`` or ``None``.

    ``None`` ⇒ TOLERATE (an uninstalled third-party import — an env concern, not a
    coherence failure). Otherwise the language-neutral category + a Go-specific
    code. Mirrors the Python resolver's "first-party provably absent → fail;
    third-party/unknown → never fail" policy.
    """
    undef = _GO_UNDEFINED_RE.search(message)
    if undef is not None:
        return EVIDENCE_MISSING_SYMBOL, "GO_UNDEFINED"
    cannot_find = _GO_CANNOT_FIND_PKG_RE.search(message)
    not_in_std = _GO_NOT_IN_STD_RE.search(message)
    pkg_match = cannot_find or not_in_std
    if pkg_match is not None:
        pkg = pkg_match.group("pkg")
        if _go_is_first_party_import(pkg, module_path):
            return EVIDENCE_MODULE_RESOLUTION, "GO_PACKAGE_NOT_FOUND"
        # Third-party / stdlib-shaped import that does not resolve at implement
        # time → uninstalled dependency (env), not SUT incoherence → tolerate.
        return None
    # A real positioned compile diagnostic that is neither of the above (a type
    # error, redeclaration, …) is still a coherence failure → other.
    return EVIDENCE_OTHER, "GO_COMPILE_ERROR"


def _go_rel_path(raw: str, project_root: Path) -> str | None:
    """Normalize a Go diagnostic path (possibly ``./x.go``) → project-relative."""
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


def _parse_go_tool_output(
    output: str, *, tool: str, module_path: str, project_root: Path
) -> tuple[list[ImplementOracleFinding], list[str]]:
    """Parse ``go test``/``go vet`` output → (findings, editable failed paths).

    Walks positioned ``path:line:col: message`` lines, skips ``# pkg`` headers and
    VCS-stamping noise, and classifies each via :func:`_classify_go_diagnostic`
    (third-party-tolerant). Lines WITHOUT a file position that still name a
    not-found package are also covered by the positioned form (the build/vet/test
    cases observed all carry positions).
    """
    findings: list[ImplementOracleFinding] = []
    failed_paths: list[str] = []
    for raw_line in (output or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if _GO_PKG_HEADER.match(line):
            continue
        if _GO_VCS_STAMP_RE.search(line):
            continue  # environment noise, never a code-coherence finding
        if _GO_TEST_SUMMARY_RE.match(line):
            continue  # go test run-summary envelope (FAIL/ok/?/PASS), not a diagnostic
        m = _GO_DIAG_LINE.match(line)
        if m is None:
            continue
        message = m.group("message").strip()
        classified = _classify_go_diagnostic(message, module_path=module_path)
        if classified is None:
            continue  # tolerated (uninstalled third-party)
        category, code = classified
        rel = _go_rel_path(m.group("path"), project_root)
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


def _go_residual_is_benign(output: str, *, module_path: str) -> bool:
    """True iff a non-zero go exit's output is ONLY noise / tolerated diagnostics.

    Returns True (benign — let the non-zero exit pass) when EVERY non-blank line is
    a ``# pkg`` header, VCS-stamping noise, or a positioned diagnostic that the
    third-party-tolerant classifier deliberately TOLERATES (an uninstalled
    third-party import). Returns False — i.e. surface an honest opaque failure — the
    moment any line is unaccounted-for (a non-noise, non-diagnostic line), so a
    genuinely opaque toolchain error never hides behind a benign verdict
    (anti-false-green: conservative by construction).
    """
    for raw_line in (output or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if _GO_PKG_HEADER.match(line) or _GO_VCS_STAMP_RE.search(line):
            continue
        if _GO_TEST_SUMMARY_RE.match(line):
            continue  # go test run-summary envelope (FAIL/ok/?/PASS) — see regex note
        m = _GO_DIAG_LINE.match(line)
        if m is not None and _classify_go_diagnostic(
            m.group("message").strip(), module_path=module_path
        ) is None:
            continue  # a tolerated (uninstalled third-party) diagnostic
        return False  # an unaccounted-for line → not benign
    return True


def _go_module_root(ctx: OracleContext) -> Path:
    """The directory the go commands run from = ``project_root / layout.module_root``.

    The Go profile's ``layout.module_root`` is ``"."`` (go.mod at the repo root),
    so this is normally the project root. The generic executor independently
    substitutes ``{module_root}`` into each command's ``cwd``; this mirrors that
    resolution so the adapter reads go.mod from the SAME directory the commands run
    in (the module path that anchors first-party classification).
    """
    module_root = (getattr(ctx.layout_profile, "module_root", ".") or ".").strip()
    if module_root in ("", "."):
        return ctx.project_root
    return ctx.project_root / module_root


class GoToolchainOracleAdapter:
    """``implement_oracle`` adapter for the Go toolchain (``adapter: go-toolchain``).

    A ``kind="composite"`` adapter: it implements :meth:`certify_scope` (called once
    before any command) and :meth:`normalize_command_result` (called per command of
    the ``typecheck`` + ``vet`` sequence). It does NOT implement ``execute`` — Go is
    a shell-command-sequence oracle, run by the generic executor, not an in-process
    composite (that is Python's ``kind="adapter"``).
    """

    def certify_scope(self, ctx: OracleContext) -> str:
        """Certify the Go module's scope, else raise :class:`OracleScopeError`.

        Anti-false-green: a green oracle over an UNSCOPED or EMPTY module proves
        nothing. So this hard-fails (RED, never a silent pass) when:

        * there is no ``go.mod`` at the module root — without a module path the
          first-party boundary cannot be established (the classifier could not tell
          a missing first-party package from a tolerated third-party one), and
          ``go test ./...`` is not a module build at all; OR
        * there is no ``.go`` file under the module root — a green typecheck over an
          empty module is meaningless (mirrors the Python certifier's "empty
          required root is a hard fail").

        Returns a human-readable certification detail on success.
        """
        module_root = _go_module_root(ctx)
        gomod = module_root / "go.mod"
        if not gomod.is_file():
            raise OracleScopeError(
                "go implement-time oracle cannot be certified: no go.mod at the module "
                f"root ({module_root!s}), so `go test -run ^$ ./...` is not a module "
                "build and a green result would prove nothing (the first-party boundary "
                "is undecidable). Ensure the Go module was scaffolded (go.mod present)."
            )
        module_path = _go_module_path(module_root)
        if not module_path:
            # go.mod present but no readable ``module`` directive → the first-party
            # prefix is undecidable; an un-scoped oracle cannot distinguish a missing
            # first-party package from a tolerated third-party one. RED, never green.
            raise OracleScopeError(
                "go implement-time oracle cannot be certified: go.mod at "
                f"{module_root!s} has no readable `module` directive, so the first-party "
                "import prefix is unknown and the oracle cannot tell a missing "
                "first-party package from a tolerated third-party dependency. A green "
                "result over an un-scoped module would be a false-green (HARD FAIL)."
            )
        has_go_file = False
        if module_root.is_dir():
            for path in module_root.rglob("*.go"):
                parts = set(path.parts)
                if ".git" in parts or "vendor" in parts:
                    continue
                if path.is_file():
                    has_go_file = True
                    break
        if not has_go_file:
            raise OracleScopeError(
                "go implement-time oracle cannot be certified: no .go files under the "
                f"module root ({module_root!s}). A green `go test -run ^$ ./...` over an "
                "empty module proves nothing — an empty scope is a HARD FAIL "
                "(anti-false-green). Ensure the units were generated."
            )
        return (
            "go oracle scope certified: go.mod present (module="
            f"'{module_path}') + ≥1 .go file under module_root='{module_root!s}' "
            "(go test -run ^$ ./... + go vet ./... cover the whole module)"
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
        """Normalize one ``go`` command's raw result → a language-neutral observation.

        Semantics (preserved EXACTLY from the legacy ``_run_one_go_command``):

        * returncode == 0 → ``is_clean=True``, no findings.
        * returncode != 0 with PARSEABLE first-party diagnostics (undefined symbol /
          missing first-party pkg / vet diag) → ``is_clean=False`` with those findings.
        * returncode != 0 with NO parseable finding → the per-line benign accounting
          (:func:`_go_residual_is_benign`): ``is_clean=True`` ONLY IF every residual
          line is a ``# pkg`` header / VCS-stamp noise / a TOLERATED
          uninstalled-third-party diagnostic. The moment ANY line is unaccounted-for,
          ``is_clean=False`` with EMPTY findings — the generic executor then
          synthesizes an opaque ``environment_build_error`` RED (never a benign pass).
          This per-line conservatism is the anti-false-green guard: it is NOT a
          blanket "no findings → clean".
        """
        module_root = _go_module_root(ctx)
        module_path = _go_module_path(module_root)
        output = "\n".join(part for part in (stdout, stderr) if part)

        findings, failed_paths = _parse_go_tool_output(
            output, tool=command_id, module_path=module_path, project_root=module_root
        )

        if returncode == 0:
            # A clean exit is the positive proof of coherence. (Findings on a zero
            # exit are not expected for go; if any were parsed they still win — a
            # not-clean signal beats a clean-looking exit.)
            if findings:
                return OracleStepObservation(
                    is_clean=False,
                    findings=tuple(findings),
                    failed_paths=tuple(failed_paths),
                    detail=f"go {command_id} exited 0 but parsed {len(findings)} diagnostic(s)",
                )
            return OracleStepObservation(is_clean=True)

        if findings:
            return OracleStepObservation(
                is_clean=False,
                findings=tuple(findings),
                failed_paths=tuple(failed_paths),
                detail=f"go {command_id} reported {len(findings)} diagnostic(s)",
            )

        # Non-zero exit, NO parseable code finding: per-line benign accounting.
        if _go_residual_is_benign(output, module_path=module_path):
            # Every residual line is noise / a tolerated uninstalled-third-party
            # diagnostic → benign (env state, not incoherence) → CLEAN.
            return OracleStepObservation(is_clean=True)

        # Unaccounted-for non-zero exit → NOT clean, EMPTY findings. The executor
        # synthesizes an opaque environment_build_error RED (anti-false-green: a
        # non-zero exit the adapter cannot name is never a benign pass).
        return OracleStepObservation(
            is_clean=False,
            detail=(
                f"`go {command_id}` exited {returncode} with no parseable diagnostic and "
                "non-benign residual output — opaque environment/build error"
            ),
        )


__all__ = ["GoToolchainOracleAdapter"]
