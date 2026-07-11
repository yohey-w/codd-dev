"""C++ ``cpp-toolchain`` implement-oracle adapter (Contract Kernel oracle dispatch §5).

Modeled VERBATIM on the Go ``go-toolchain`` adapter
(:mod:`codd.languages.adapters.oracle_go`): a ``kind="composite"`` oracle whose
scope is certified by :meth:`CppToolchainOracleAdapter.certify_scope` and whose
per-command verdict is produced by
:meth:`CppToolchainOracleAdapter.normalize_command_result`. The C++ profile's
implement-oracle is the two-step ``configure`` (``cmake -S . -B build``) THEN
``build`` (``cmake --build build``) sequence — exactly the way Go's oracle is
``typecheck`` + ``vet``: each raw ``(returncode, stdout, stderr)`` is handed here
for a language-neutral verdict.

THE ANTI-FALSE-GREEN CORE (identical philosophy to Go's ``_go_residual_is_benign``)
==================================================================================
A compiler/build step can fail for two unrelated reasons, and the oracle must tell
them apart WITHOUT ever inventing a false-RED or swallowing a real one:

* a SUT-incoherence failure — the independently-generated files do not agree on the
  symbols/headers they demand of each other (a missing first-party header, an
  undeclared symbol, a type error). This is RED — it is exactly what the oracle
  exists to catch.
* an ENVIRONMENT failure — a system toolchain component is simply not installed
  (a ``<vector>``/``<iostream>`` system header missing because libstdc++ headers
  are absent; a CMake "compiler not found"). This is NOT incoherence — it is env
  state — so, like Go tolerating an uninstalled THIRD-PARTY module under
  ``-mod=readonly``, it must be TOLERATED (treated as benign/env), never RED.

The verdict rules (preserved EXACTLY from the Go adapter's ``normalize_command_result``):

* returncode == 0 → ``is_clean=True``, no findings.
* returncode != 0 with PARSEABLE first-party diagnostics → ``is_clean=False`` with
  those findings.
* returncode != 0 with NO parseable finding → per-line benign accounting
  (:func:`_cpp_residual_is_benign`): ``is_clean=True`` ONLY IF EVERY non-blank
  residual line is recognizable noise (cmake progress, make chatter, a ``warning:``,
  a ``note:``/caret/``In file included from`` context line) OR a TOLERATED
  system-header-not-found. The moment ANY line is unaccounted-for →
  ``is_clean=False`` with EMPTY findings, and the generic executor synthesizes an
  opaque ``environment_build_error`` RED (never a benign pass). This per-line
  conservatism is the guard: it is NOT a blanket "no findings → clean".

THE LOAD-BEARING GENERALITY DECISION — system header vs first-party header
==========================================================================
A header-not-found diagnostic (``fatal error: <name>: No such file or directory``
from g++, or ``'<name>' file not found`` from clang) is the SUBTLE case, and it is
the direct C++ analogue of Go's ``_go_is_first_party_import`` third-party tolerance.
At the diagnostic level g++/clang give us only the header NAME (not whether it lives
in a system include dir), so we classify by a heuristic on the name
(:func:`_cpp_header_is_system`):

* a header whose name has NO path separator AND is a recognized C++/C STANDARD
  library header (``vector``, ``string``, ``iostream``, ``memory``, ``cstdint``,
  the extensionless ``<...>`` std set, plus the classic ``.h`` C headers like
  ``stdio.h``) is treated as a SYSTEM header → its absence is an env concern →
  TOLERATED (return ``None``, like Go tolerating a missing third-party module).
* anything else — a relative path like ``"foo/bar.h"``, or a plain ``.h``/``.hpp``
  name that is NOT a known std header (``myheader.h``, ``app/widget.hpp``) — is
  treated as a FIRST-PARTY header → ``EVIDENCE_MODULE_RESOLUTION`` /
  ``CPP_HEADER_NOT_FOUND``, which is RED (the C++ analogue of Go's "missing
  FIRST-PARTY package is RED").

Conservatism on the undecidable boundary mirrors Go's ``_go_is_first_party_import``
returning ``False`` (never-a-false-RED) when the module path is unknown: when we
cannot positively recognize a header as a known std header we DO red it as
first-party — because a first-party-looking header that the SUT itself was supposed
to generate is exactly the incoherence the oracle must catch, and a genuine system
header is overwhelmingly likely to be in our recognized std set (so we do NOT
vacuously pass it). Crucially, even a header we (wrongly) decline to name as a
finding does NOT vacuously pass: an unaccounted-for ``fatal error:`` line that is
NOT a tolerated system header is still surfaced via the benign-accounting path as an
opaque RED. We never both fail-to-classify AND fail-to-account — every line lands on
exactly one side.

LEAF rule (no import cycle): imports ONLY stdlib + the oracle value-objects leaf
(:mod:`codd.implement_oracle_types`) + the adapter protocol leaf
(:mod:`codd.languages.adapters.implement_oracle`) + the profile model
(:mod:`codd.languages.profile`). It MUST NOT import the gate
(:mod:`codd.implement_oracle`), the registry, or the generic executor — the
dependency edge runs gate → executor → adapters → leaf types, never back.
"""

from __future__ import annotations

import dataclasses
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


# ── C/C++ source extensions (mirrors the profile's file_extensions) ──────────
#: Extensions that make a directory "a C/C++ module with sources" for scope
#: certification. Headers count too: a header-only library is still real source
#: the oracle's configure+build must compile/parse (the certifier only proves the
#: scope is non-empty — the analogue of Go's "≥1 .go file under module_root").
_CPP_SOURCE_EXTS = frozenset(
    {".h", ".hpp", ".hh", ".hxx", ".cc", ".cpp", ".cxx", ".c", ".cppm", ".ixx"}
)

#: Directories never walked when looking for sources (build output / VCS) — the
#: C++ analogue of Go skipping ``.git``/``vendor``.
_CPP_SKIP_DIRS = frozenset({"build", ".git", ".codd", "cmake-build-debug", "cmake-build-release"})


# ── C++ / C standard-library header set (the system-vs-first-party heuristic) ─
#
# The recognized SYSTEM (standard-library) header names. A header-not-found
# diagnostic naming one of these (with NO path separator) is treated as an
# uninstalled-toolchain ENVIRONMENT concern and TOLERATED — the direct analogue of
# Go's ``_go_is_first_party_import`` returning False for a stdlib/third-party import
# so its "cannot find module" is not a coherence RED. This set is deliberately the
# well-known C++ standard headers (C++23-ish) plus the classic ``<cXXX>`` C-compat
# headers and the legacy ``.h`` C headers; it is a HEURISTIC, not an exhaustive
# toolchain inventory (an exhaustive list is neither knowable nor language-free),
# and it errs toward recognizing real std headers so a genuine env-missing system
# header is not turned into a false-RED.
_CPP_STD_HEADERS: frozenset[str] = frozenset(
    {
        # containers / sequences
        "array", "deque", "forward_list", "list", "map", "queue", "set", "stack",
        "unordered_map", "unordered_set", "vector", "span", "flat_map", "flat_set",
        "mdspan",
        # strings / text
        "string", "string_view", "cctype", "cstring", "cwchar", "cwctype",
        "charconv", "format", "regex",
        # streams / io
        "iostream", "istream", "ostream", "fstream", "sstream", "iomanip",
        "ios", "iosfwd", "streambuf", "syncstream", "print", "spanstream",
        # general utilities
        "utility", "tuple", "optional", "variant", "any", "bitset", "functional",
        "memory", "memory_resource", "scoped_allocator", "type_traits",
        "typeindex", "typeinfo", "compare", "version", "source_location",
        "expected", "initializer_list", "concepts", "coroutine", "stdexcept",
        # numerics / math
        "cmath", "complex", "valarray", "numeric", "random", "ratio", "cfenv",
        "cinttypes", "cstdint", "limits", "numbers", "bit", "cstdlib",
        # algorithms / iterators / ranges
        "algorithm", "iterator", "ranges", "execution", "generator",
        # time / locale
        "chrono", "ctime", "locale", "clocale", "codecvt",
        # concurrency
        "atomic", "thread", "mutex", "shared_mutex", "condition_variable",
        "future", "barrier", "latch", "semaphore", "stop_token",
        # diagnostics / system
        "exception", "system_error", "cerrno", "cassert", "stacktrace",
        "contracts",
        # language support / misc
        "csetjmp", "csignal", "cstdarg", "cstddef", "cstdio", "cuchar",
        "filesystem", "new", "typeinfo",
        # classic C headers (a project may include these directly)
        "stdio.h", "stdlib.h", "string.h", "math.h", "assert.h", "ctype.h",
        "errno.h", "float.h", "limits.h", "locale.h", "setjmp.h", "signal.h",
        "stdarg.h", "stddef.h", "stdint.h", "time.h", "wchar.h", "wctype.h",
        "inttypes.h", "stdbool.h", "stdalign.h", "stdnoreturn.h", "uchar.h",
        "iso646.h", "complex.h", "fenv.h", "tgmath.h", "threads.h",
    }
)


# ── C++ / cmake diagnostic regexes ───────────────────────────────────────────

#: A g++/clang diagnostic with a file position. Two shapes are tolerated:
#:   * ``file:line:col: <severity>: <message>``  (g++/clang canonical), and
#:   * ``file:line: <severity>: <message>``       (column omitted).
#: A leading absolute path (``/abs/path/file.cpp:..``) is fine — the path group is
#: greedy up to the position. The severity (``error``/``warning``/``note``/
#: ``fatal error``) and the trailing message are captured so the sub-classifier and
#: the benign filter can both reason about the line.
_CPP_DIAG_LINE = re.compile(
    r"^(?P<path>(?:[A-Za-z]:)?[^\s:][^:\n]*\.(?:h|hpp|hh|hxx|c|cc|cpp|cxx|cppm|ixx))"
    r":(?P<line>\d+)(?::(?P<col>\d+))?:\s*"
    r"(?P<severity>fatal error|error|warning|note):\s*(?P<message>.+?)\s*$"
)

#: The ``CMake Error at <file>:<line> (<command>):`` block header that opens a cmake
#: CONFIGURE-time error. Captures the referenced file so a "missing first-party
#: file" cmake error can be attributed; the human message spans the following
#: indented lines (cmake does not put it on the header line).
_CPP_CMAKE_ERROR_AT = re.compile(
    r"^CMake Error at\s+(?P<file>[^:]+):(?P<line>\d+)\s*\((?P<command>[^)]*)\)\s*:?\s*$"
)

#: A bare ``CMake Error:`` (no ``at <file>``) — a configure-time error not anchored
#: to a CMakeLists line (e.g. "Could not find ..." toolchain/component problems).
_CPP_CMAKE_ERROR_BARE = re.compile(r"^CMake Error(?:\s*\([^)]*\))?:\s*(?P<message>.*)$")

#: g++'s undeclared-symbol family (the C++ analogue of Go's ``undefined:`` →
#: missing_symbol). These appear in the message tail of an ``error:`` diagnostic.
_CPP_UNDECLARED_RE = re.compile(
    r"was not declared in this scope|has not been declared|use of undeclared identifier"
)

#: A header-not-found diagnostic message tail, BOTH compiler dialects:
#:   * g++:   ``<name>: No such file or directory``           (after ``fatal error:``)
#:   * clang: ``'<name>' file not found``                     (after ``fatal error:``)
#: ``<name>`` is the header as written in the ``#include`` (e.g. ``vector``,
#: ``myheader.h``, ``foo/bar.h``). It is classified system-vs-first-party by
#: :func:`_cpp_header_is_system`.
_CPP_HEADER_NOT_FOUND_GCC = re.compile(
    r"^(?P<header>.+?):\s*No such file or directory\b"
)
_CPP_HEADER_NOT_FOUND_CLANG = re.compile(
    r"^'(?P<header>[^']+)'\s+file not found\b"
)

#: GNU ld's undefined-reference diagnostic — the LINK-time analogue of the
#: undeclared-symbol family (an implementation artifact fails to DEFINE a symbol
#: another artifact demands). Two shapes, both anchored on the message tail:
#:   * ``/usr/bin/ld: file.cpp:(.text+0x40): undefined reference to `sym'``
#:   * ``file.cpp:(.text+0x40): undefined reference to `sym'``   (no ld prefix)
#: The referencing TU (``file.cpp``) and the SYMBOL are captured: the symbol is the
#: repair-feedback identity (WHAT is missing), the TU the best-available file
#: attribution (WHO demands it). lld/mold keep the same message tail. Without this
#: regex a pure link failure had NO parseable diagnostic and collapsed to an opaque
#: environment_build_error, aborting the repair loop (cpp2 exprcalc dogfood,
#: 2026-07-11).
_CPP_LD_UNDEFINED_RE = re.compile(
    r"^(?:\S*ld(?:\.\w+)?:\s*)?"
    r"(?:(?P<path>[^\s:][^:\n]*\.(?:h|hpp|hh|hxx|c|cc|cpp|cxx|cppm|ixx)):\(\S*\):\s*)?"
    r"undefined reference to [`'](?P<symbol>[^']+)'"
)

#: ld's two-line context header (``…/main.cpp.o: in function `main':``) — context
#: for the positioned undefined-reference that follows, never a diagnostic itself.
_CPP_LD_CONTEXT_RE = re.compile(
    r"^(?:\S*ld(?:\.\w+)?:\s*)?\S+\.o(?:bj)?:\s*in function\s"
)

#: collect2's sign-off (``collect2: error: ld returned 1 exit status``) — a SUMMARY
#: epilog after the explicit ld error lines above it. Skipped in PARSING only (the
#: undefined-reference findings are the authority); deliberately NOT recognized in
#: the benign-accounting path, so a collect2 line with NO accompanying parsed
#: diagnostic stays an honest opaque RED (anti-false-green — same reasoning as the
#: ``ninja:`` note above).
_CPP_LD_EPILOG_RE = re.compile(r"^collect2:\s*(?:fatal error|error):\s*ld returned")

#: cmake progress / informational chatter that is NOT a failure — the C++ analogue
#: of Go's ``# pkg`` headers + ``ok``/``?`` run-summary noise. ``--`` status lines
#: (``-- Configuring done``, ``-- Detecting C compiler ...``, ``-- Build files have
#: been written to ...``), the ``[ 50%] Building CXX object ...`` build-progress
#: lines, and ``make``/``gmake`` recipe chatter. These prove nothing and fail
#: nothing → benign noise (never a finding, never an unaccounted line).
_CPP_CMAKE_PROGRESS_RE = re.compile(r"^\s*--\s")
_CPP_BUILD_PERCENT_RE = re.compile(r"^\s*\[\s*\d+%\]")
#: make/ninja/cmake build-driver chatter + g++/clang's post-fatal sign-off lines.
#: ``compilation terminated.`` (g++) / ``N errors generated.`` (clang) are the
#: compiler's SUMMARY epilog after a positioned ``fatal error:``/``error:`` — they
#: prove nothing on their own (the positioned diagnostic above is the authority and
#: is independently classified), so they are benign noise, NOT an unaccounted line.
#: make/cmake build-driver chatter + g++/clang's post-fatal sign-off lines.
#: ``compilation terminated.`` (g++) / ``N errors generated.`` (clang) are the
#: compiler's SUMMARY epilog after a positioned ``fatal error:``/``error:`` — they
#: prove nothing on their own (the positioned diagnostic above is the authority and
#: is independently classified), so they are benign noise, NOT an unaccounted line.
#: NOTE: ``ninja:`` lines are deliberately NOT recognized here — ``ninja: error:
#: loading 'build.ninja'`` is a genuine build-driver failure that must stay an
#: opaque RED (anti-false-green), not be waved through as chatter.
_CPP_MAKE_CHATTER_RE = re.compile(
    r"^\s*g?make(?:\[\d+\])?:\s|"
    r"^\s*(?:Scanning dependencies|Consolidate compiler|"
    r"Built target|Linking (?:CXX|C) (?:static |shared )?(?:executable|library))\b|"
    r"^\s*compilation terminated\.\s*$|"
    r"^\s*\d+\s+(?:error|warning)s?\s+generated\.\s*$"
)

#: g++/clang context lines that are NOT diagnostics: ``In file included from a.h:1``
#: (and its ``                 from b.h:2:`` continuations), the source-excerpt and
#: ``^``/``~~~^`` caret-marker lines, and ``required from``/``instantiation of``
#: template backtrace context. Skipped in parsing AND benign in accounting — they
#: are context for an adjacent positioned diagnostic, never a failure of their own.
_CPP_CONTEXT_RE = re.compile(
    r"^\s*(?:In file included from\b|from\s+\S+:\d+|required from\b|"
    r"In (?:instantiation|member function|function|constructor|destructor)\b|"
    r"At global scope\b|At top level\b|recursively required\b)"
)
#: A caret/tilde marker line or a ``|``-gutter source excerpt line that g++ prints
#: under a diagnostic. Three shapes, all pure context (no diagnostic of their own):
#:   * a bare marker line ``      ^~~~~`` (whitespace + caret/tilde glyphs);
#:   * a numbered gutter excerpt ``    1 | #include <vector>`` (``<n> | <source>``);
#:   * an unnumbered gutter marker ``      |          ^~~~~`` (``| ... ^~~~``) — the
#:     ``|`` gutter, then arbitrary spaces, then only caret/tilde/space glyphs.
_CPP_CARET_RE = re.compile(
    r"^\s*[\^~]+\s*$"  # bare caret/tilde marker
    r"|^\s*\d+\s*\|"  # numbered source-excerpt gutter line
    r"|^\s*\|[\s\^~]*$"  # unnumbered gutter line: pipe + spaces + caret/tilde
)


# ── pure helpers ─────────────────────────────────────────────────────────────


def _cpp_header_is_system(header: str) -> bool:
    """True when a not-found header NAME looks like a SYSTEM (std-library) header.

    The load-bearing generality call (mirror of Go's ``_go_is_first_party_import``).
    A header is treated as SYSTEM (→ tolerate its absence as an env concern) ONLY
    when it has NO path separator AND its exact name is in the recognized C/C++
    standard-library set (:data:`_CPP_STD_HEADERS`). Everything else — a relative
    path (``"foo/bar.h"`` / ``foo/bar.hpp``) or a plain name that is not a known std
    header (``myheader.h``) — is treated as FIRST-PARTY (→ RED), because a
    first-party-looking header that one generated file demands of another is exactly
    the incoherence the oracle exists to catch.

    Conservatism: when undecidable we fall to the FIRST-PARTY (RED) side, matching
    Go's "unknown module path ⇒ nothing is first-party ⇒ never a false-RED" only in
    SPIRIT-INVERTED-FOR-C++: Go's tolerated default is third-party (its diagnostics
    name an import PATH it can prefix-test); C++ gives us only a bare header name, so
    the safe-against-false-GREEN default is to RED an unrecognized first-party-looking
    header rather than wave it through. A genuine system header is overwhelmingly in
    the recognized set (so this does NOT manufacture false-REDs for real std headers),
    and the benign-accounting path still treats a tolerated system header as env.
    """
    name = (header or "").strip().strip('"').strip("<>").strip()
    if not name:
        return False
    # A path separator means a relative/first-party include (``foo/bar.h``); never
    # a bare system header reference, so it is FIRST-PARTY (not tolerated).
    if "/" in name or "\\" in name:
        return False
    return name in _CPP_STD_HEADERS


def _classify_cpp_compiler_diagnostic(
    severity: str, message: str
) -> tuple[str, str] | None:
    """Classify ONE positioned g++/clang ``error:``/``fatal error:`` → (category, code).

    Returns ``None`` to TOLERATE the line (a system-header-not-found — an env
    concern, mirroring Go tolerating an uninstalled third-party import). A
    ``warning:``/``note:`` is never a failure and is handled by the caller (skipped),
    so this only ever sees ``error``/``fatal error``.

    Order of tests mirrors Go's ``_classify_go_diagnostic``: the specific
    symbol/header families first, then a catch-all real-compile-error.
    """
    sev = (severity or "").strip().lower()
    msg = (message or "").strip()

    # 1) header-not-found (the subtle system-vs-first-party case). g++ emits it as
    #    ``fatal error: <name>: No such file or directory``; clang as
    #    ``fatal error: '<name>' file not found``. Either dialect's header NAME is
    #    classified by _cpp_header_is_system.
    hdr = _CPP_HEADER_NOT_FOUND_GCC.match(msg) or _CPP_HEADER_NOT_FOUND_CLANG.match(msg)
    if hdr is not None:
        header = hdr.group("header")
        if _cpp_header_is_system(header):
            # Recognized std/system header missing → uninstalled toolchain (env),
            # NOT SUT incoherence → tolerate (the anti-false-RED side).
            return None
        # First-party-looking header that does not resolve at implement time → the
        # generated files disagree on a header one demands of another → RED.
        return EVIDENCE_MODULE_RESOLUTION, "CPP_HEADER_NOT_FOUND"

    # 2) undeclared symbol family (only meaningful on an ``error``, not ``fatal
    #    error`` — but accept either; the regex is specific to the phrasing).
    if _CPP_UNDECLARED_RE.search(msg):
        return EVIDENCE_MISSING_SYMBOL, "CPP_UNDECLARED"

    # 3) any other real positioned ``error:``/``fatal error:`` (a type error, a
    #    redefinition, a parse error, …) is a coherence failure → other.
    if sev in ("error", "fatal error"):
        return EVIDENCE_OTHER, "CPP_COMPILE_ERROR"

    # Defensive: a positioned line whose severity is neither error nor a tolerated
    # header miss (should not happen — warning/note are filtered upstream) is not a
    # finding. Returning a non-None catch-all here would risk a false-RED on a
    # warning; returning None would risk a false-GREEN. The caller only passes
    # error-severity lines here, so this branch is unreachable in practice; choose
    # the never-false-RED side and let benign accounting decide.
    return None


def _cpp_classify_cmake_command(command: str) -> tuple[str, str]:
    """Classify a ``CMake Error at CMakeLists.txt:NN (<command>):`` by its command.

    A cmake configure error anchored to a CMakeLists line is, by default, a
    coherence problem: the build description references something the generated tree
    does not provide. The most common ``add_executable``/``add_library``/
    ``target_sources``/``add_subdirectory`` errors about a missing first-party file
    are coherence REDs (``EVIDENCE_OTHER`` / ``CPP_CMAKE_ERROR``).

    The ONE family we treat as ENVIRONMENT is ``find_package``/``find_library``/
    ``find_path``/``find_program`` — "could not find <X>" for an external
    package/toolchain component is the cmake analogue of an uninstalled third-party
    dependency (Go tolerates that), so it is ``EVIDENCE_ENVIRONMENT_BUILD`` /
    ``CPP_CMAKE_DEPENDENCY``. (It is still surfaced as a finding — the executor reds
    on any finding — but the env CATEGORY keeps the SUT feedback honest about what
    kind of problem it is; this is stricter than Go, which fully tolerates the
    third-party miss. We deliberately do NOT make a missing find_package vanish: a
    configure that cannot find a declared dependency did not prove coherence.)
    """
    cmd = (command or "").strip().lower()
    if cmd.startswith("find_"):
        return EVIDENCE_ENVIRONMENT_BUILD, "CPP_CMAKE_DEPENDENCY"
    return EVIDENCE_OTHER, "CPP_CMAKE_ERROR"


def _cpp_rel_path(raw: str, project_root: Path) -> str | None:
    """Normalize a diagnostic path (possibly absolute or ``./x``) → project-relative."""
    cleaned = (raw or "").strip()
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if not cleaned:
        return None
    try:
        resolved = (project_root / cleaned).resolve()
        return resolved.relative_to(Path(project_root).resolve()).as_posix()
    except (ValueError, OSError):
        # An absolute path outside the tree, or a non-resolvable path: keep it POSIX
        # but do not force it under the root (mirror Go's _go_rel_path fallback).
        return PurePosixPath(cleaned.replace("\\", "/")).as_posix()


def _parse_cpp_tool_output(
    output: str, *, tool: str, project_root: Path
) -> tuple[list[ImplementOracleFinding], list[str]]:
    """Parse g++/clang + cmake output → (findings, editable failed paths).

    Walks every line:
      * skips blank / caret / source-excerpt / ``note:`` / ``In file included from``
        context (NOT diagnostics);
      * skips cmake progress + build-percent + make chatter (informational noise);
      * skips ``warning:`` diagnostics (a warning is not a failure);
      * classifies a positioned ``error:``/``fatal error:`` via
        :func:`_classify_cpp_compiler_diagnostic` (system-header tolerant);
      * recognizes a ``CMake Error at <file>:<line> (<command>):`` block header and
        classifies it via :func:`_cpp_classify_cmake_command` (the human message is
        on following indented lines — we attribute to the referenced file and use a
        synthetic message); a bare ``CMake Error:`` becomes a cmake configure RED.

    Mirrors Go's ``_parse_go_tool_output`` structure exactly.
    """
    findings: list[ImplementOracleFinding] = []
    failed_paths: list[str] = []
    seen_ld_refs: set[tuple[str | None, str]] = set()

    def _add(category: str, code: str, message: str, rel: str | None) -> None:
        findings.append(
            ImplementOracleFinding(
                category=category, code=code, message=f"[{tool}] {message}", path=rel
            )
        )
        if rel and rel not in failed_paths:
            failed_paths.append(rel)

    for raw_line in (output or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        # context / excerpt / caret lines are never diagnostics.
        if _CPP_CONTEXT_RE.match(line) or _CPP_CARET_RE.match(line):
            continue
        # cmake progress / build percent / make chatter — informational noise.
        if (
            _CPP_CMAKE_PROGRESS_RE.match(line)
            or _CPP_BUILD_PERCENT_RE.match(line)
            or _CPP_MAKE_CHATTER_RE.match(line)
        ):
            continue

        # ld undefined-reference → missing_symbol, deduped per (TU, symbol): ld
        # repeats the same reference once per call site; per-site duplicates add
        # no repair signal. The symbol identity rides the message (feedback).
        ld = _CPP_LD_UNDEFINED_RE.match(line)
        if ld is not None:
            symbol = ld.group("symbol").strip()
            raw_path = ld.group("path")
            rel = _cpp_rel_path(raw_path, project_root) if raw_path else None
            key = (rel, symbol)
            if key not in seen_ld_refs:
                seen_ld_refs.add(key)
                _add(
                    EVIDENCE_MISSING_SYMBOL,
                    "CPP_LD_UNDEFINED_REFERENCE",
                    f"undefined reference to `{symbol}' (link-time; referenced "
                    f"from {rel or 'unknown TU'})",
                    rel,
                )
            continue
        if _CPP_LD_CONTEXT_RE.match(line) or _CPP_LD_EPILOG_RE.match(line):
            continue  # ld context / collect2 epilog — the reference lines above are the authority

        m = _CPP_DIAG_LINE.match(line)
        if m is not None:
            severity = (m.group("severity") or "").strip().lower()
            if severity == "warning":
                continue  # a warning is not a failure
            if severity == "note":
                # A positioned ``note:`` is CONTEXT for the PRECEDING error — the
                # other side of a two-sided diagnostic (``previously defined
                # here`` names the header a redefinition collides with; candidate
                # sites for an overload miss). Attach it to the last finding's
                # message and attribution so the repair feedback carries BOTH
                # sides of the disagreement (cpp3 exprcalc dogfood, 2026-07-11:
                # feedback saw only the .cpp side and oscillated). An orphan note
                # (no preceding finding) stays noise — a note is never a failure
                # of its own. Capped at 2 notes per finding (overload-candidate
                # spam adds no repair signal past the first sites).
                if findings and findings[-1].code.startswith("CPP_"):
                    last = findings[-1]
                    if last.message.count(": note: ") < 2:
                        note_txt = (
                            f"{m.group('path')}:{m.group('line')}: note: "
                            f"{m.group('message').strip()}"
                        )
                        findings[-1] = dataclasses.replace(
                            last, message=f"{last.message} [{note_txt}]"
                        )
                        rel_note = _cpp_rel_path(m.group("path"), project_root)
                        if rel_note and rel_note not in failed_paths:
                            failed_paths.append(rel_note)
                continue
            classified = _classify_cpp_compiler_diagnostic(
                severity, m.group("message").strip()
            )
            if classified is None:
                continue  # tolerated (system header not found)
            category, code = classified
            rel = _cpp_rel_path(m.group("path"), project_root)
            _add(category, code, m.group("message").strip(), rel)
            continue

        cmake_at = _CPP_CMAKE_ERROR_AT.match(line)
        if cmake_at is not None:
            category, code = _cpp_classify_cmake_command(cmake_at.group("command"))
            rel = _cpp_rel_path(cmake_at.group("file"), project_root)
            _add(
                category,
                code,
                f"CMake Error at {cmake_at.group('file')}:{cmake_at.group('line')} "
                f"({cmake_at.group('command')})",
                rel,
            )
            continue

        cmake_bare = _CPP_CMAKE_ERROR_BARE.match(line)
        if cmake_bare is not None:
            # A bare ``CMake Error:`` is a configure-time failure not anchored to a
            # CMakeLists line. "Could not find" → env (the find_* analogue); anything
            # else → a cmake coherence RED. Either way a finding (executor reds).
            msg = cmake_bare.group("message").strip()
            if re.search(r"could not find|not able to find|no .* found", msg, re.IGNORECASE):
                _add(EVIDENCE_ENVIRONMENT_BUILD, "CPP_CMAKE_DEPENDENCY", f"CMake Error: {msg}", None)
            else:
                _add(EVIDENCE_OTHER, "CPP_CMAKE_ERROR", f"CMake Error: {msg}", None)
            continue

        # not a diagnostic / not recognized noise — left for benign accounting.
    return findings, failed_paths


def _cpp_residual_is_benign(output: str) -> bool:
    """True iff a non-zero exit's output is ONLY noise / a tolerated system-header miss.

    The anti-false-green core, identical in spirit to Go's ``_go_residual_is_benign``.
    Returns True (benign — let the non-zero exit pass as env state) when EVERY
    non-blank line is one of:

      * a context/caret/source-excerpt line (``In file included from``, ``note:``,
        the ``^~~~`` markers, the ``  3 | ...`` gutter excerpt);
      * cmake progress / build-percent / make chatter;
      * a ``warning:`` diagnostic (warnings are not failures);
      * a positioned ``fatal error:`` header-not-found that the classifier deliberately
        TOLERATES (a recognized system header — an uninstalled toolchain, env).

    Returns False — surface an honest opaque RED — the moment ANY line is
    unaccounted-for (a non-noise, non-tolerated line), so a genuinely opaque
    toolchain error never hides behind a benign verdict. Account for EVERY line: a
    real first-party diagnostic line is, by construction, classified (not None) by
    :func:`_classify_cpp_compiler_diagnostic`, so it is NOT benign here → the caller
    would already have produced a finding for it (this function only runs when no
    finding was parsed), and even a defensively-unclassified positioned ``error:``
    falls through to ``return False`` → opaque RED (never swallowed).
    """
    for raw_line in (output or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if _CPP_CONTEXT_RE.match(line) or _CPP_CARET_RE.match(line):
            continue
        if (
            _CPP_CMAKE_PROGRESS_RE.match(line)
            or _CPP_BUILD_PERCENT_RE.match(line)
            or _CPP_MAKE_CHATTER_RE.match(line)
        ):
            continue
        m = _CPP_DIAG_LINE.match(line)
        if m is not None:
            severity = (m.group("severity") or "").strip().lower()
            if severity in ("warning", "note"):
                continue  # warnings/notes are not failures
            # A positioned error: it is benign ONLY if the classifier TOLERATES it
            # (a recognized system-header-not-found). Any other positioned error is
            # a real diagnostic → not benign (it would have been a finding upstream).
            if (
                severity == "fatal error"
                and _classify_cpp_compiler_diagnostic(severity, m.group("message").strip())
                is None
            ):
                continue  # tolerated system-header-not-found (env)
            return False  # a real positioned diagnostic → not benign
        # A ``CMake Error`` line is never benign (it is a configure failure that the
        # parser turns into a finding; if we are here with no finding it is still
        # unaccounted-for → opaque RED).
        return False  # an unaccounted-for line → not benign
    return True


def _cpp_module_root(ctx: OracleContext) -> Path:
    """The directory the cmake commands run from = ``project_root / layout.module_root``.

    The C++ profile's ``layout.module_root`` is ``"."`` (CMakeLists.txt at the repo
    root), so this is normally the project root. The generic executor independently
    substitutes ``{module_root}`` into each command's ``cwd``; this mirrors that
    resolution so the adapter looks for ``CMakeLists.txt`` + sources in the SAME
    directory the commands run in. (Identical to Go's ``_go_module_root``.)
    """
    module_root = (getattr(ctx.layout_profile, "module_root", ".") or ".").strip()
    if module_root in ("", "."):
        return ctx.project_root
    return ctx.project_root / module_root


def _cpp_has_source(module_root: Path) -> bool:
    """True iff ≥1 C/C++ source/header file exists under ``module_root``.

    Walks the tree, skipping build-output / VCS dirs (:data:`_CPP_SKIP_DIRS`). The
    C++ analogue of Go's "≥1 .go file under module_root" non-empty-scope check.
    """
    if not module_root.is_dir():
        return False
    for path in module_root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in _CPP_SOURCE_EXTS:
            continue
        parts = set(path.parts)
        if parts & _CPP_SKIP_DIRS:
            continue
        return True
    return False


class CppToolchainOracleAdapter:
    """``implement_oracle`` adapter for the C++ toolchain (``adapter: cpp-toolchain``).

    A ``kind="composite"`` adapter, exactly like :class:`GoToolchainOracleAdapter`:
    it implements :meth:`certify_scope` (called once before any command) and
    :meth:`normalize_command_result` (called per command of the ``configure`` +
    ``build`` sequence). It does NOT implement ``execute`` — C++ is a
    shell-command-sequence oracle run by the generic executor, not an in-process
    composite (that is Python's ``kind="adapter"``).
    """

    def certify_scope(self, ctx: OracleContext) -> str:
        """Certify the C++ module's scope, else raise :class:`OracleScopeError`.

        FAIL-CLOSED (anti-false-green: a green oracle over an unscoped/empty module
        proves nothing), hard-failing (RED, never a silent pass) when:

        * there is no ``CMakeLists.txt`` at the module root — without the build
          description ``cmake -S . -B build`` is not a build of this module at all
          and a green result would prove nothing (the C++ analogue of Go's missing
          ``go.mod``); OR
        * there is no C/C++ source/header file under the module root — a green
          configure+build over an empty tree is meaningless (mirrors Go's "no .go
          file under module_root is a hard fail").

        Returns a human-readable certification detail on success.
        """
        module_root = _cpp_module_root(ctx)
        cmakelists = module_root / "CMakeLists.txt"
        if not cmakelists.is_file():
            raise OracleScopeError(
                "cpp implement-time oracle cannot be certified: no CMakeLists.txt at "
                f"the module root ({module_root!s}), so `cmake -S . -B build` is not a "
                "build of this module and a green result would prove nothing. Ensure the "
                "C++ project was scaffolded (CMakeLists.txt present)."
            )
        if not _cpp_has_source(module_root):
            raise OracleScopeError(
                "cpp implement-time oracle cannot be certified: no C/C++ source or header "
                f"file (.h/.hpp/.hh/.cc/.cpp/.cxx) under the module root ({module_root!s}). "
                "A green configure+build over an empty module proves nothing — an empty "
                "scope is a HARD FAIL (anti-false-green). Ensure the units were generated."
            )
        return (
            "cpp oracle scope certified: CMakeLists.txt present + ≥1 C/C++ source/header "
            f"file under module_root='{module_root!s}' (cmake configure + cmake --build "
            "cover the whole module)"
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
        """Normalize one cmake command's raw result → a language-neutral observation.

        Semantics (preserved EXACTLY from Go's ``normalize_command_result``):

        * returncode == 0 → ``is_clean=True``, no findings.
        * returncode != 0 with PARSEABLE diagnostics (undeclared symbol / first-party
          header-not-found / a real compile error / a cmake configure error) →
          ``is_clean=False`` with those findings.
        * returncode != 0 with NO parseable finding → per-line benign accounting
          (:func:`_cpp_residual_is_benign`): ``is_clean=True`` ONLY IF every residual
          line is recognized noise (cmake/make/build-progress, warning, context/caret)
          OR a TOLERATED system-header-not-found. The moment ANY line is
          unaccounted-for, ``is_clean=False`` with EMPTY findings — the generic
          executor then synthesizes an opaque ``environment_build_error`` RED (never a
          benign pass). This per-line conservatism is the anti-false-green guard: it is
          NOT a blanket "no findings → clean".
        """
        module_root = _cpp_module_root(ctx)
        output = "\n".join(part for part in (stdout, stderr) if part)

        findings, failed_paths = _parse_cpp_tool_output(
            output, tool=command_id, project_root=module_root
        )

        if returncode == 0:
            # A clean exit is the positive proof of coherence. (Findings on a zero
            # exit are not expected for cmake; if any were parsed they still win — a
            # not-clean signal beats a clean-looking exit.)
            if findings:
                return OracleStepObservation(
                    is_clean=False,
                    findings=tuple(findings),
                    failed_paths=tuple(failed_paths),
                    detail=f"cmake {command_id} exited 0 but parsed {len(findings)} diagnostic(s)",
                )
            return OracleStepObservation(is_clean=True)

        if findings:
            return OracleStepObservation(
                is_clean=False,
                findings=tuple(findings),
                failed_paths=tuple(failed_paths),
                detail=f"cmake {command_id} reported {len(findings)} diagnostic(s)",
            )

        # Non-zero exit, NO parseable code finding: per-line benign accounting.
        if _cpp_residual_is_benign(output):
            # Every residual line is noise / a tolerated system-header-not-found →
            # benign (env state, not incoherence) → CLEAN.
            return OracleStepObservation(is_clean=True)

        # Unaccounted-for non-zero exit → NOT clean, EMPTY findings. The executor
        # synthesizes an opaque environment_build_error RED (anti-false-green: a
        # non-zero exit the adapter cannot name is never a benign pass).
        return OracleStepObservation(
            is_clean=False,
            detail=(
                f"`cmake {command_id}` exited {returncode} with no parseable diagnostic "
                "and non-benign residual output — opaque environment/build error"
            ),
        )


__all__ = ["CppToolchainOracleAdapter"]
