"""TypeScript ``typescript-tsc`` implement-oracle adapter (Contract Kernel oracle
dispatch §7 — the LAST per-language oracle switch).

The TypeScript tool SEMANTICS — relocated VERBATIM from the gate's hand-written TS
path (``codd.implement_oracle``: ``certify_oracle_scope`` tsconfig branch +
``normalize_oracle_output`` + the ``_TS_*`` regexes/codes + the legacy
``_run_oracle_command`` ``rc==0 and not TS18003`` false-green guard). The gate used
to dispatch TS on ``profile.language in ("typescript","node")`` and run ``npx
--no-install tsc --noEmit`` through ``_run_oracle_command``; from step 7 TS runs on
the Contract-Kernel contract path: the generic
:func:`codd.languages.oracle_executor.run_command_sequence` spawns the profile's
``typecheck`` command (``npx --no-install tsc --noEmit``) and hands the raw
``(returncode, stdout, stderr)`` to this adapter's
:meth:`TypeScriptTscOracleAdapter.normalize_command_result` for a language-neutral
verdict. Scope is certified by :meth:`TypeScriptTscOracleAdapter.certify_scope`.

A ``kind="command"`` oracle (one static checker = ``tsc --noEmit``) — the SAME
shape as Go's ``kind="composite"`` (both run through ``run_command_sequence`` and
implement ``certify_scope`` + ``normalize_command_result``; they differ only in the
number of command steps). It does NOT implement ``execute`` (that is Python's
in-process ``kind="adapter"`` composite).

THE load-bearing false-green guard (preserved EXACTLY): ``tsc`` can exit 0 yet
typecheck NOTHING — it emits ``TS18003`` / "No inputs were found in config file"
when its ``include``/``files`` resolve to zero inputs. A green ``tsc`` that compiled
nothing proves nothing, so **``returncode==0`` AND TS18003 present → NOT clean**
(``is_clean=False``), never a benign pass. (The scope certifier should catch an
uncovered scope first; this is the belt-and-suspenders.)

LEAF rule (no import cycle): imports ONLY stdlib + the oracle value-objects leaf
(:mod:`codd.implement_oracle_types`) + the adapter protocol leaf
(:mod:`codd.languages.adapters.implement_oracle`) + the profile model
(:mod:`codd.languages.profile`). It MUST NOT import the gate
(:mod:`codd.implement_oracle`), the registry, or the generic executor — the
dependency edge runs gate → executor → adapters → leaf types, never back.
"""

from __future__ import annotations

import json
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


# ── TypeScript diagnostic regexes + code maps (relocated verbatim from the gate) ──

#: TypeScript diagnostic codes → evidence category. The src↔src / test↔helper
#: symbol-coherence bugs the gate targets are exactly the "missing member /
#: cannot find name" family. ``module_resolution`` is the "cannot find module"
#: family. Mapping is conservative: an unmapped ``TSxxxx`` is a real type error
#: (``type_error`` = EVIDENCE_OTHER) — still a HARD failure, just not one of the
#: named buckets.
_TS_MISSING_SYMBOL_CODES = frozenset(
    {
        "TS2305",  # Module '"X"' has no exported member 'Y'.
        "TS2724",  # '"X"' has no exported member named 'Y'. Did you mean 'Z'?
        "TS2459",  # Module '"X"' declares 'Y' locally, but it is not exported.
        "TS2614",  # Module '"X"' has no exported member 'Y'. (no default vs named)
        "TS2552",  # Cannot find name 'Y'. Did you mean 'Z'?
        "TS2304",  # Cannot find name 'Y'.
        "TS2339",  # Property 'Y' does not exist on type 'X'.
    }
)
_TS_MODULE_RESOLUTION_CODES = frozenset(
    {
        "TS2307",  # Cannot find module 'X' or its corresponding type declarations.
        "TS2792",  # Cannot find module 'X'. Did you mean to set 'moduleResolution'?
        "TS6053",  # File 'X' not found.
        "TS5083",  # Cannot read file 'X'.
    }
)

#: A tsc diagnostic line: ``path(line,col): error TSxxxx: message`` (pretty) or
#: ``path:line:col - error TSxxxx: message`` (``--pretty false``). Captures the
#: code + the trailing message so the per-error category + a compact SUT-facing
#: summary can be built without re-parsing.
_TS_DIAG_LINE = re.compile(
    r"error\s+(?P<code>TS\d+)\s*:\s*(?P<message>.+?)\s*$",
    re.MULTILINE,
)

#: ``tsc`` emits this when its include/files resolve to nothing — a config-scope
#: failure that must NEVER read as "0 errors → coherent" (it typechecked nothing).
_TS_NO_INPUTS_RE = re.compile(r"TS18003|No inputs were found in config file", re.IGNORECASE)


def _categorize_ts_code(code: str) -> str:
    if code in _TS_MISSING_SYMBOL_CODES:
        return EVIDENCE_MISSING_SYMBOL
    if code in _TS_MODULE_RESOLUTION_CODES:
        return EVIDENCE_MODULE_RESOLUTION
    return EVIDENCE_OTHER


def _diag_path(line: str, project_root: Path) -> str | None:
    """Extract the file path from a tsc diagnostic line, project-relative."""
    paren = re.match(r"^\s*(?P<path>[^\s(][^(\n]*\.(?:ts|tsx|mts|cts))\(\d+,\d+\)", line)
    colon = re.match(r"^\s*(?P<path>[^\s:][^:\n]*\.(?:ts|tsx|mts|cts)):\d+:\d+", line)
    match = paren or colon
    if match is None:
        return None
    raw = match.group("path").strip()
    try:
        resolved = (project_root / raw).resolve()
        return resolved.relative_to(Path(project_root).resolve()).as_posix()
    except (ValueError, OSError):
        return PurePosixPath(raw.replace("\\", "/")).as_posix()


def _parse_ts_diagnostics(
    output: str, project_root: Path
) -> tuple[list[ImplementOracleFinding], list[str]]:
    """Parse tsc output → (findings, editable failed paths) — the relocated normalizer.

    Mirrors the legacy gate's ``normalize_oracle_output`` EXACTLY:
      * a ``No inputs were found`` (TS18003) result is surfaced as a single
        ``environment_build_error`` finding (tsc ran but typechecked nothing — never
        coherence; the scope certifier should have caught it, this is the
        belt-and-suspenders);
      * every ``error TSxxxx: message`` line becomes an
        :class:`ImplementOracleFinding` with a language-neutral category, paired with
        the file on the SAME line;
      * the editable ``failed_paths`` are resolved through the SAME tsc attribution
        (:func:`codd.repair.test_failure_attribution.attribute_command_failure`) the
        verify stage uses, so attribution stays consistent across stages.
    """
    text = output or ""
    findings: list[ImplementOracleFinding] = []

    if _TS_NO_INPUTS_RE.search(text):
        findings.append(
            ImplementOracleFinding(
                category=EVIDENCE_ENVIRONMENT_BUILD,
                code="TS18003",
                message="tsc found no inputs — the typecheck covered zero files (scope error).",
            )
        )

    for line in text.splitlines():
        m = _TS_DIAG_LINE.search(line)
        if m is None:
            continue
        code = m.group("code")
        message = m.group("message").strip()
        path = _diag_path(line, project_root)
        findings.append(
            ImplementOracleFinding(
                category=_categorize_ts_code(code),
                code=code,
                message=message,
                path=path,
            )
        )

    failed_paths: list[str] = []
    try:
        from codd.repair.test_failure_attribution import attribute_command_failure

        attribution = attribute_command_failure(
            command="tsc --noEmit",
            output=text,
            project_root=project_root,
            check_name="implement_oracle",
        )
        if attribution is not None:
            failed_paths = list(attribution.failed_nodes)
    except Exception:  # noqa: BLE001 — attribution is best-effort enrichment.
        failed_paths = []

    return findings, failed_paths


def _structured_ts_diagnostics(output: str, project_root: Path) -> list:
    """Structured per-diagnostic objects for the scoped-rerun derivation (best-effort).

    Reuses the gate's ``_parse_ts_diagnostics`` in ``implement_oracle_scope`` (the
    SAME structured parser the legacy path threaded into the result). A parse miss
    degrades to an empty list — the scope layer then falls to a broad rerun, which
    is safe (never a false-green).
    """
    try:
        from codd.implement_oracle_scope import _parse_ts_diagnostics as _scope_parse

        return list(_scope_parse(output, project_root))
    except Exception:  # noqa: BLE001 — structured-diag parsing is enrichment only.
        return []


# ── tsconfig scope certification (anti-false-green: tsc must SEE src + tests) ──


def _norm(rel: str) -> str:
    return str(rel).strip().replace("\\", "/").strip("/")


def _glob_covers_root(patterns: list[str], root: str) -> bool:
    """Does any tsconfig include/files glob cover everything under ``root``/?

    Conservative TEXTUAL test (we do not run tsc's resolver): a pattern covers
    ``root`` when it starts at (or above) ``root`` and is recursive enough to reach
    nested files — i.e. ``root`` itself, ``root/**``, ``root/**/*`` and ``**/*``
    (the catch-all). A pattern restricted to a sub-path of ``root`` (a single file,
    or ``root/sub/**``) does NOT certify the whole ``root`` (e2e / helpers under
    another sub-dir would be unseen). Anything we cannot prove covers ``root``
    returns False → the caller HARD-FAILS rather than guessing.
    """
    root = _norm(root)
    if not root:
        return False
    for raw in patterns:
        pat = _norm(raw)
        if not pat:
            continue
        # The universal recursive catch-all.
        if pat in {"**", "**/*"} or pat.startswith("**/"):
            if pat in {"**", "**/*"}:
                return True
            if pat.startswith("**/*"):
                return True
        # ``root`` exactly, or a recursive glob anchored at root.
        if pat == root:
            return True
        if pat.startswith(root + "/"):
            tail = pat[len(root) + 1 :]
            if tail.startswith("**"):
                return True
            # A single-level ``root/*`` does NOT reach nested e2e/helpers; only a
            # ``**`` recursive glob certifies the whole subtree.
    return False


def _strip_jsonc(text: str) -> str:
    """Strip ``//`` and ``/* */`` comments so a JSONC tsconfig parses as JSON.

    Conservative: removes line comments not inside a string and block comments.
    tsconfig is JSONC; our own scaffold uses a ``"//"`` KEY (valid JSON) but a
    hand-authored config may use real comments, and JSON's parser would choke.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    in_line_comment = False
    in_block_comment = False
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                out.append(ch)
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_string:
            out.append(ch)
            if ch == "\\" and nxt:
                out.append(nxt)
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        # not in string/comment
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _ts_layout_roots(ctx: OracleContext) -> tuple[str, str]:
    """``(source_root, test_root)`` from the resolved ``LanguageProfile.layout``.

    Reads the FIRST declared source-set / test-set root from the contract LayoutSpec
    (TS: ``src`` / ``tests``) — language-free, no legacy ``LayoutProfile`` dependency
    (the contract path hands the executor the LayoutSpec, not the gate's
    LayoutProfile). Defensive defaults mirror CoDD's TS layout so a minimal layout
    view still anchors the scope check.
    """
    layout = ctx.language_profile.layout
    source_sets = tuple(getattr(layout, "source_sets", ()) or ())
    test_sets = tuple(getattr(layout, "test_sets", ()) or ())
    source_root = _norm(source_sets[0].root) if source_sets else "src"
    test_root = _norm(test_sets[0].root) if test_sets else "tests"
    return source_root or "src", test_root or "tests"


class TypeScriptTscOracleAdapter:
    """``implement_oracle`` adapter for the TypeScript compiler (``adapter: typescript-tsc``).

    A ``kind="command"`` adapter: it implements :meth:`certify_scope` (called once
    before the command) and :meth:`normalize_command_result` (called for the single
    ``typecheck`` command = ``tsc --noEmit``). It does NOT implement ``execute`` — TS
    is a shell-command oracle run by the generic executor, not an in-process
    composite (that is Python's ``kind="adapter"``).
    """

    def certify_scope(self, ctx: OracleContext) -> str:
        """Certify ``tsconfig.json`` covers source + tests, else raise OracleScopeError.

        Parse ``<project_root>/tsconfig.json`` and prove its ``include`` (or
        ``files``) covers the ``source_root`` and ``test_root`` subtrees (which
        contain e2e + helpers). Returns a human-readable certification detail on
        success.

        Anti-false-green: a missing/unparseable tsconfig, or one whose scope provably
        EXCLUDES the test tree, is a HARD FAIL (:class:`OracleScopeError`) — a green
        ``tsc`` over an unknown or partial scope would be a false-green (the #1 design
        failure mode: "tests outside compile scope = false green"). The require flags
        are FIXED for the TS oracle (source + tests both required) — the whole point
        of the implement-time typecheck is to catch test/helper incoherence.
        """
        project_root = ctx.project_root
        source_root, test_root = _ts_layout_roots(ctx)

        tsconfig = project_root / "tsconfig.json"
        if not tsconfig.is_file():
            raise OracleScopeError(
                "implement-time oracle cannot be certified: no tsconfig.json at the project "
                "root, so tsc's typecheck scope is unknown — a green typecheck would prove "
                "nothing about src/tests. The greenfield scaffold creates a tsconfig with "
                "include=[<src>/**/*, <tests>/**/*]; ensure the layout was scaffolded."
            )
        try:
            payload = json.loads(_strip_jsonc(tsconfig.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise OracleScopeError(
                f"implement-time oracle cannot be certified: tsconfig.json is unreadable/"
                f"invalid JSON ({exc}); tsc's scope is undecidable, so a green typecheck "
                f"cannot be trusted to cover src/tests."
            ) from exc

        if not isinstance(payload, dict):
            raise OracleScopeError(
                "implement-time oracle cannot be certified: tsconfig.json is not a JSON object."
            )

        include = payload.get("include")
        files = payload.get("files")
        patterns: list[str] = []
        if isinstance(include, list):
            patterns.extend(str(item) for item in include)
        if isinstance(files, list):
            patterns.extend(str(item) for item in files)

        # A tsconfig with neither ``include`` nor ``files`` defaults to "every .ts
        # under the config dir" — which DOES cover src + tests. But ``exclude`` or a
        # narrow project layout could still hide the test tree; rather than reason
        # about tsc's full default-resolution, we REQUIRE an explicit include we can
        # prove covers the test root. Intentionally strict: the scaffold always emits
        # one, so a missing include means a hand-authored config we will not certify
        # blind.
        if not patterns:
            raise OracleScopeError(
                "implement-time oracle cannot be certified: tsconfig.json declares no "
                "`include` or `files`, so it is not provable that tsc's scope covers the "
                "test tree (where test/helper symbol incoherence lives). Declare "
                f'include: ["{source_root}/**/*", "{test_root}/**/*"].'
            )

        missing: list[str] = []
        if not _glob_covers_root(patterns, source_root):
            missing.append(source_root)
        if not _glob_covers_root(patterns, test_root):
            missing.append(test_root)
        if missing:
            raise OracleScopeError(
                "implement-time oracle cannot be certified: tsconfig.json `include`/`files` "
                f"({patterns}) does not provably cover the required root(s) {missing}. The "
                "whole point of the implement-time typecheck is to catch test/helper symbol "
                "incoherence, so an uncovered test tree is a HARD FAIL (anti-false-green). "
                f'Add a recursive glob, e.g. "{missing[0]}/**/*", to include.'
            )

        return (
            f"oracle scope certified: tsconfig include/files {patterns} cover "
            f"source_root='{source_root}' + test_root='{test_root}'"
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
        """Normalize the ``tsc --noEmit`` result → a language-neutral observation.

        Semantics (preserved EXACTLY from the legacy ``_run_oracle_command``):

        * **``returncode==0`` AND no TS18003 → ``is_clean=True``** (no findings). The
          scope was certified by :meth:`certify_scope`, so a clean exit is a TRUE
          green.
        * **``returncode==0`` AND TS18003 / "No inputs were found" → ``is_clean=False``**
          (the load-bearing false-green guard): tsc "passed" but compiled NOTHING, so
          it is a false-green to avoid (NOT a benign pass). The TS18003
          ``environment_build_error`` finding is carried.
        * ``returncode!=0`` with parseable diagnostics → ``is_clean=False`` with the
          findings (TS2305/2724/2459/… → missing_symbol; TS2307/2792/… →
          module_resolution; any other positioned tsc diagnostic → a coherence finding
          ``other``).
        * ``returncode!=0`` with NO parseable diagnostic → ``is_clean=False`` with
          EMPTY findings; the generic executor then synthesizes an opaque
          ``environment_build_error`` RED (never a benign pass — the canonical
          false-green this gate exists to kill).
        """
        project_root = ctx.project_root
        full_output = "\n".join(part for part in (stdout, stderr) if part)
        no_inputs = bool(_TS_NO_INPUTS_RE.search(full_output))

        # TRUE green: clean exit over a certified scope that actually compiled inputs.
        if returncode == 0 and not no_inputs:
            return OracleStepObservation(is_clean=True)

        findings, failed_paths = _parse_ts_diagnostics(full_output, project_root)
        diagnostics = _structured_ts_diagnostics(full_output, project_root)

        # The false-green guard already produced a TS18003 finding on rc==0; for a
        # nonzero exit with no parseable diagnostic, return EMPTY findings so the
        # executor synthesizes the opaque environment_build_error (never a benign pass).
        detail = (
            "tsc typechecked nothing (TS18003) — a green over an empty scope is a false-green"
            if (returncode == 0 and no_inputs)
            else f"tsc exited {returncode} with {len(findings)} diagnostic(s)"
        )
        return OracleStepObservation(
            is_clean=False,
            findings=tuple(findings),
            failed_paths=tuple(failed_paths),
            diagnostics=tuple(diagnostics),
            detail=detail,
        )


__all__ = ["TypeScriptTscOracleAdapter"]
