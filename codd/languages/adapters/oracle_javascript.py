"""JavaScript ``javascript-composite`` implement-oracle adapter (Contract Kernel
oracle dispatch — the JAVASCRIPT SWITCH, closing the §9 UNSUPPORTED_EXPLICIT RED
that ``javascript.yaml`` previously left open by declaring no ``implement_oracle``
at all).

WHY A COMPOSITE (mirrors ``oracle_python``, not ``oracle_typescript``/``oracle_go``)
=====================================================================================
Plain JavaScript has NO compiler / type-checker — there is no ``tsc``-equivalent
static checker to shell out to. Its implement-time anti-false-green oracle is
therefore a COMPOSITE of TWO hard, in-process layers, run BEFORE the test runner
while the SUT can still edit every file (source AND tests):

  1. **syntax** — ``node --check <file>`` (Node's OWN parser, honoring the nearest
     ``package.json``'s ``"type"`` field) over every source+test ``.js``/``.mjs``/
     ``.cjs`` file. Catches SyntaxError. Deliberately EXCLUDES ``.jsx`` (plain Node
     has no JSX transform — checking a valid ``.jsx`` file with ``node --check``
     would be a systematic FALSE-RED, not a real syntax proof).
  2. **first-party import/export resolver** (THE CORE / KEYSTONE) — a static,
     regex-based scan over ALL source+test ``.js``/``.jsx``/``.mjs``/``.cjs`` that
     proves every RELATIVE (first-party) import/require specifier resolves to a
     real file, and — for a NAMED ESM import/re-export — that the target file
     actually exports that name. This is the ONLY layer that catches the exact bug
     class the TS oracle's own docstring names as its motivating case: a test that
     imports ``{ repoRoot }`` from a helper that actually exports ``projectRoot``.
     Invisible to layer 1 (pure syntax, no cross-file knowledge).

Since JS has no static type system, nothing here claims to catch a type error —
only "this file doesn't parse" and "this import doesn't resolve / doesn't export
what's demanded". That is an honest, bounded, but genuinely useful floor: it is
the SAME "the same checks, minus type-checking" shape as Python's composite
(:mod:`codd.languages.adapters.oracle_python`), adapted from Python's dotted
package-namespace resolution to JavaScript's PATH-based module resolution (a JS
import specifier resolves to a file path directly — there is no package/namespace
indirection to model, which makes this adapter structurally simpler than Python's).

FALSE-RED avoidance (these are NEVER flagged — anti-false-RED, mirrors Python's
"first-party provably absent -> fail; third-party/unknown -> never fail" policy):
  * a bare specifier (no ``./`` or ``../`` prefix: an npm package, a ``node:``
    built-in, a bare ``#subpath`` import-map entry) — third-party/stdlib, never
    first-party, never checked.
  * a specifier that is not a literal string (a template literal, a variable) —
    only a LITERAL ``'...'``/``"..."`` specifier is checked.
  * a DEFAULT / namespace / side-effect-only / ``require()`` / dynamic ``import()``
    demand — RESOLUTION-checked (the target file must exist) but never
    SYMBOL-checked (CommonJS/ESM default-export interop has too many legitimate
    shapes — e.g. ``module.exports = fn`` becomes an ESM default under Node's
    interop — to honestly claim a missing default without a real module loader).
  * an export whose name set cannot be statically decided (a destructured
    ``export const { a, b } = ...``, ``module.exports = <non-object-literal>``,
    an ``export * from`` whose target is third-party or itself undecidable) marks
    that module's provided-name set UNKNOWN — a named import from it is NEVER
    flagged missing (never guess).
  * comments (``//`` and ``/* */``) are blanked before scanning, so a commented-out
    ``// import { x } from './y'`` is never mistaken for a real edge. String/
    template-literal CONTENT is left untouched (an import specifier lives inside
    quotes, so its text must survive) — the residual risk of a string literal that
    happens to look like import syntax is accepted, exactly as the TS/Go adapters'
    own regex-based diagnostic parsers already accept analogous text-matching risk.

ANTI-FALSE-GREEN (the cardinal rule — a non-coherent module must NEVER pass):
  * a syntax error on any first-party file -> RED (``type_error``).
  * a first-party import/require target that does not resolve to a file -> RED
    (``module_resolution_error``).
  * a first-party NAMED import/re-export whose target resolves but does not
    export the demanded name (and the target's export set IS decidable) -> RED
    (``missing_symbol``).
  * a ``node --check`` spawn failure / timeout, or ``node`` missing from PATH,
    is an ``environment_build_error`` -> RED, never a silent skip.
  * the observability gate: each layer MUST have OBSERVED every file in its OWN
    scope (layer 1: non-``.jsx`` files; layer 2: every file) — a gap is an
    ``environment_build_error``, never a silent green.

LEAF rule (no import cycle): imports ONLY stdlib + the oracle value-objects leaf
(:mod:`codd.implement_oracle_types`) + the adapter protocol leaf
(:mod:`codd.languages.adapters.implement_oracle`) + the profile model
(:mod:`codd.languages.profile`). It MUST NOT import the gate
(:mod:`codd.implement_oracle`), the registry, or the generic executor.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from codd.implement_oracle_types import (
    EVIDENCE_ENVIRONMENT_BUILD,
    EVIDENCE_MISSING_SYMBOL,
    EVIDENCE_MODULE_RESOLUTION,
    EVIDENCE_OTHER,
    ImplementOracleFinding,
    ImplementOracleResult,
    OracleScopeError,
)
from codd.languages.adapters.implement_oracle import (
    OracleContext,
    OracleStepObservation,
)
from codd.languages.profile import CommandSpec

# ═════════════════════════════════════════════════════════════════════════════
# Config knobs
# ═════════════════════════════════════════════════════════════════════════════

#: Bounded wall-clock for ONE ``node --check <file>`` spawn. Overridable via
#: ``implement.javascript_check_timeout_seconds``.
DEFAULT_JS_CHECK_TIMEOUT_SECONDS = 30.0


def _js_check_timeout_seconds(config: Mapping[str, Any] | None) -> float:
    section = (config or {}).get("implement") if isinstance(config, Mapping) else None
    if isinstance(section, Mapping):
        raw = section.get("javascript_check_timeout_seconds")
        try:
            value = float(raw)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return DEFAULT_JS_CHECK_TIMEOUT_SECONDS


#: Directories never enumerated by the JS oracle (VCS, harness, build outputs).
_JS_ORACLE_SKIP_DIRS = frozenset(
    {
        "node_modules",
        ".git",
        ".codd",
        "dist",
        "build",
        "coverage",
        ".turbo",
        ".next",
    }
)

#: Extensions the RESOLVER layer (layer 2, pure text scan) enumerates — every
#: JS-family source/test file, including ``.jsx`` (regex-based scanning does not
#: care that a file also contains JSX markup; import/export statements are plain
#: JS syntax regardless).
_JS_RESOLVER_EXTS = (".js", ".jsx", ".mjs", ".cjs")

#: Extensions the SYNTAX layer (layer 1, ``node --check``) enumerates. Deliberately
#: EXCLUDES ``.jsx`` — plain Node has no JSX transform, so checking a syntactically
#: valid ``.jsx`` file with ``node --check`` would systematically false-RED every
#: JSX project (anti-false-RED: never claim a check this adapter cannot honestly run).
_JS_SYNTAX_CHECK_EXTS = (".js", ".mjs", ".cjs")

#: A first-party (relative) import target resolves by trying, in order: the exact
#: path, the path + each extension, then ``<path>/index<ext>`` for each extension
#: (a directory import). Mirrors Node's own resolution algorithm closely enough
#: for generated-project layouts (no ``package.json`` "exports" map resolution —
#: that only matters for THIRD-PARTY packages, which this adapter never resolves).
_RESOLUTION_EXTS = (".js", ".mjs", ".cjs", ".jsx", ".json")


def _js_norm(rel: str) -> str:
    return str(rel).strip().replace("\\", "/").strip("/")


def _js_rel_project(path: Path, project_root: Path) -> str:
    try:
        return path.resolve().relative_to(Path(project_root).resolve()).as_posix()
    except (ValueError, OSError):
        return PurePosixPath(str(path).replace("\\", "/")).as_posix()


# ═════════════════════════════════════════════════════════════════════════════
# Scope (the concrete file-list the oracle certifies + observes).
# ═════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class JavaScriptOracleScope:
    """The concrete JS-family file-list a JavaScript composite oracle covers."""

    source_files: tuple[str, ...]
    test_files: tuple[str, ...]

    @property
    def expected_files(self) -> tuple[str, ...]:
        """All in-scope files, deduped, source-then-test order preserved."""
        return tuple(dict.fromkeys(self.source_files + self.test_files))

    @property
    def syntax_checkable_files(self) -> tuple[str, ...]:
        """The subset layer 1 (``node --check``) can honestly check (no ``.jsx``)."""
        return tuple(f for f in self.expected_files if PurePosixPath(f).suffix in _JS_SYNTAX_CHECK_EXTS)


def _iter_js_oracle_files(project_root: Path, root_rel: str) -> tuple[str, ...]:
    """Every JS-family file under ``root_rel`` (project-relative), skip-dirs excluded."""
    rel = _js_norm(root_rel)
    if not rel:
        return ()
    root = project_root / rel
    if not root.is_dir():
        return ()
    out: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.suffix not in _JS_RESOLVER_EXTS:
            continue
        if any(part in _JS_ORACLE_SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        out.append(_js_rel_project(path, project_root))
    return tuple(out)


def _javascript_oracle_scope(project_root: Path, source_root: str, test_root: str) -> JavaScriptOracleScope:
    """Enumerate the source+test JS-family files the oracle will check (deduped)."""
    source_files = _iter_js_oracle_files(project_root, source_root)
    test_root_files = _iter_js_oracle_files(project_root, test_root)
    source_set = set(source_files)
    test_files = tuple(f for f in test_root_files if f not in source_set)
    return JavaScriptOracleScope(source_files=source_files, test_files=test_files)


# ═════════════════════════════════════════════════════════════════════════════
# Layout reading — mirrors ``oracle_typescript._ts_layout_roots`` (JS's
# ``package_root.kind`` is ``path_root``, the SAME flat shape as TS, not Python's
# nested ``named_package`` — so no dotted-namespace derivation is needed here).
# ═════════════════════════════════════════════════════════════════════════════


def _js_layout_roots(ctx: OracleContext) -> tuple[str, str]:
    """``(source_root, test_root)`` from the resolved ``LanguageProfile.layout``."""
    layout = ctx.language_profile.layout
    source_sets = tuple(getattr(layout, "source_sets", ()) or ())
    test_sets = tuple(getattr(layout, "test_sets", ()) or ())
    source_root = _js_norm(source_sets[0].root) if source_sets else "src"
    test_root = _js_norm(test_sets[0].root) if test_sets else "tests"
    return source_root or "src", test_root or "tests"


# ═════════════════════════════════════════════════════════════════════════════
# One tool layer's result (mirrors ``oracle_python.PythonToolRun``).
# ═════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class JsToolRun:
    """One JS oracle layer's result + its observation trail (for the gate)."""

    name: str
    executed: bool
    observed_files: tuple[str, ...] = ()
    findings: tuple[ImplementOracleFinding, ...] = ()
    output: str = ""


# ═════════════════════════════════════════════════════════════════════════════
# layer 1: node --check (syntax only, no execution, no module resolution)
# ═════════════════════════════════════════════════════════════════════════════

_NODE_CHECK_ERROR_TYPE_RE = re.compile(r"^\s*([A-Za-z][\w.]*Error):\s*(.+)$")
_NODE_VERSION_LINE_RE = re.compile(r"^Node\.js\s+v\d")
_NODE_STACK_FRAME_RE = re.compile(r"^\s*at\s")


def _first_node_check_error_line(stderr: str) -> str:
    """Best-effort single-line summary of a ``node --check`` failure's stderr."""
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    for line in lines:
        m = _NODE_CHECK_ERROR_TYPE_RE.match(line)
        if m:
            return f"{m.group(1)}: {m.group(2)}"
    for line in lines:
        if _NODE_VERSION_LINE_RE.match(line) or _NODE_STACK_FRAME_RE.match(line):
            continue
        return line
    return "node --check reported a syntax error with no parseable message"


def _run_js_syntax_layer(
    project_root: Path, files: tuple[str, ...], *, timeout: float
) -> JsToolRun:
    """``node --check <file>`` per file — syntax/parse errors ONLY.

    Run with ``cwd=project_root`` so Node's module-type detection reads the
    project's own ``package.json`` ``"type"`` field (a ``.js`` file under a
    ``"type": "module"`` package is parsed as ESM; otherwise as CommonJS/sloppy —
    the SAME rule Node applies when actually running the file). Does NOT execute
    the file (``--check`` never runs the script) and does NOT resolve imports
    (confirmed empirically: ``node --check`` exits 0 even when a static import
    specifier points at a file that does not exist) — that is layer 2's job.
    """
    findings: list[ImplementOracleFinding] = []
    observed: list[str] = []
    for rel in files:
        observed.append(rel)
        abspath = project_root / rel
        try:
            completed = subprocess.run(  # noqa: S603,S607 — trusted repo-relative path, shell=False
                ["node", "--check", str(abspath)],
                shell=False,
                cwd=str(project_root),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            findings.append(
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="node_check_timeout",
                    message=f"`node --check` exceeded {timeout:g}s",
                    path=rel,
                )
            )
            continue
        except (FileNotFoundError, OSError, ValueError) as exc:
            findings.append(
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="node_check_spawn_error",
                    message=f"could not run `node --check` (is node on PATH?): {exc}",
                    path=rel,
                )
            )
            continue
        if completed.returncode != 0:
            message = _first_node_check_error_line(completed.stderr or completed.stdout or "")
            findings.append(
                ImplementOracleFinding(
                    category=EVIDENCE_OTHER,
                    code="JS_SYNTAX_ERROR",
                    message=message,
                    path=rel,
                )
            )
    return JsToolRun(name="javascript_syntax_check", executed=True, observed_files=tuple(observed), findings=tuple(findings))


# ═════════════════════════════════════════════════════════════════════════════
# layer 2: first-party import/export resolver (THE CORE / KEYSTONE)
# ═════════════════════════════════════════════════════════════════════════════

#: A module whose provided-name set cannot be statically decided (destructured
#: export, ``module.exports = <non-object-literal>``, an undecidable re-export).
#: Importing ANY symbol from such a module is NEVER flagged (anti-false-RED).
_JS_PROVIDES_UNKNOWN = object()


def _strip_js_comments(text: str) -> str:
    """Blank ``//`` and ``/* */`` comment CONTENT (length + newlines preserved).

    NOT a JS parser — just enough that a commented-out ``// import { x } from
    './y'`` is never scanned as a real edge. String/template-literal literal
    CONTENT is left untouched (an import specifier lives inside quotes, so it
    must survive); only quote/comment BOUNDARIES are tracked, mirroring
    ``oracle_typescript._strip_jsonc``'s hand-rolled scanner.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_line_comment = False
    in_block_comment = False
    in_string: str | None = None
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                out.append(ch)
            else:
                out.append(" ")
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                out.append("  ")
                in_block_comment = False
                i += 2
            else:
                out.append("\n" if ch == "\n" else " ")
                i += 1
            continue
        if in_string is not None:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == in_string:
                in_string = None
            i += 1
            continue
        if ch == "/" and nxt == "/":
            in_line_comment = True
            out.append("  ")
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            out.append("  ")
            i += 2
            continue
        if ch in ("'", '"', "`"):
            in_string = ch
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _is_first_party_specifier(spec: str) -> bool:
    """Only an explicit relative specifier (``./`` / ``../``) is first-party.

    A bare specifier (npm package), a ``node:`` built-in, an absolute path, or a
    ``#subpath`` import-map entry is third-party/stdlib/unresolvable-by-us — never
    checked (anti-false-RED, mirrors Go's/Python's "first-party is the only thing
    we hard-check" policy).
    """
    return spec.startswith("./") or spec.startswith("../")


def _resolve_relative_specifier(spec: str, importer_rel: str, project_root: Path) -> str | None:
    """Resolve a relative import/require specifier to a project-relative path.

    Tries, in order: the EXACT path, the path + each of ``_RESOLUTION_EXTS``, then
    ``<path>/index<ext>`` for each extension (a directory import). ``None`` when
    nothing on disk matches, or when the specifier escapes the project root
    (unresolvable by this adapter — never guessed at).
    """
    importer_dir = (project_root / importer_rel).parent
    target = (importer_dir / spec).resolve()
    resolved_root = project_root.resolve()
    try:
        target.relative_to(resolved_root)
    except ValueError:
        return None
    if target.is_file():
        return _js_rel_project(target, project_root)
    for ext in _RESOLUTION_EXTS:
        candidate = target.parent / f"{target.name}{ext}"
        if candidate.is_file():
            return _js_rel_project(candidate, project_root)
    for ext in _RESOLUTION_EXTS:
        candidate = target / f"index{ext}"
        if candidate.is_file():
            return _js_rel_project(candidate, project_root)
    return None


# ── import/require DEMANDS (what a file needs from another file) ─────────────


@dataclass(frozen=True)
class _JsImportDemand:
    """One static import/require edge: a specifier + the named bindings demanded.

    ``names`` is empty for a default/namespace/side-effect/``require()``/dynamic
    ``import()`` demand (resolution-only — never symbol-checked, see module
    docstring). Non-empty ``names`` are the SOURCE names an ESM named import/
    re-export demands (the local alias, if any, is irrelevant to this check).
    """

    spec: str
    names: tuple[str, ...]
    lineno: int


_IMPORT_DEFAULT_AND_NAMED_RE = re.compile(
    r"import\s+[A-Za-z_$][\w$]*\s*,\s*\{(?P<named>[^}]*)\}\s*from\s*['\"](?P<spec>[^'\"]+)['\"]"
)
_IMPORT_NAMED_RE = re.compile(r"import\s+\{(?P<named>[^}]*)\}\s*from\s*['\"](?P<spec>[^'\"]+)['\"]")
_IMPORT_NAMESPACE_RE = re.compile(r"import\s*\*\s*as\s+[A-Za-z_$][\w$]*\s*from\s*['\"](?P<spec>[^'\"]+)['\"]")
_IMPORT_DEFAULT_ONLY_RE = re.compile(
    r"import\s+(?!\{)(?!\*)[A-Za-z_$][\w$]*\s*from\s*['\"](?P<spec>[^'\"]+)['\"]"
)
_IMPORT_SIDE_EFFECT_RE = re.compile(r"import\s*['\"](?P<spec>[^'\"]+)['\"]\s*;")
_DYNAMIC_IMPORT_RE = re.compile(r"\bimport\(\s*['\"](?P<spec>[^'\"]+)['\"]\s*\)")
_REQUIRE_RE = re.compile(r"\brequire\(\s*['\"](?P<spec>[^'\"]+)['\"]\s*\)")
_EXPORT_LIST_FROM_RE = re.compile(r"export\s*\{(?P<named>[^}]*)\}\s*from\s*['\"](?P<spec>[^'\"]+)['\"]")
_EXPORT_STAR_FROM_RE = re.compile(r"export\s*\*\s*from\s*['\"](?P<spec>[^'\"]+)['\"]")


def _parse_name_clause_list(raw: str) -> list[str]:
    """``{a, b as c, type d}`` -> ``["a", "b", "d"]`` (the SOURCE names only).

    Best-effort: tolerates a stray TS-only ``type``/``typeof`` modifier (harmless
    if a ``.js``/``.jsx`` file carries one); a malformed entry is simply skipped
    (never raises — anti-false-RED, an unparsed entry is just not checked).
    """
    out: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        part = re.sub(r"^(?:type|typeof)\s+", "", part)
        m = re.match(r"^([A-Za-z_$][\w$]*)\s+as\s+[A-Za-z_$][\w$]*$", part)
        if m:
            out.append(m.group(1))
            continue
        m = re.match(r"^([A-Za-z_$][\w$]*)$", part)
        if m:
            out.append(m.group(1))
    return out


def _iter_import_demands(text: str) -> list[_JsImportDemand]:
    """Every static import/require/re-export-from edge in ``text`` (comments blanked)."""
    demands: list[_JsImportDemand] = []
    consumed_spans: list[tuple[int, int]] = []

    def _overlaps(span: tuple[int, int]) -> bool:
        return any(span[0] < e and span[1] > s for s, e in consumed_spans)

    # Named forms FIRST (they are strictly more specific than the bare-default
    # pattern, so matching them first and recording their span stops the
    # default-only regex from double-counting the same statement).
    for pattern, name_group in (
        (_IMPORT_DEFAULT_AND_NAMED_RE, "named"),
        (_IMPORT_NAMED_RE, "named"),
        (_EXPORT_LIST_FROM_RE, "named"),
    ):
        for m in pattern.finditer(text):
            consumed_spans.append(m.span())
            names = tuple(_parse_name_clause_list(m.group(name_group)))
            demands.append(_JsImportDemand(spec=m.group("spec"), names=names, lineno=text.count("\n", 0, m.start()) + 1))

    for pattern in (
        _IMPORT_NAMESPACE_RE,
        _IMPORT_DEFAULT_ONLY_RE,
        _IMPORT_SIDE_EFFECT_RE,
        _DYNAMIC_IMPORT_RE,
        _REQUIRE_RE,
        _EXPORT_STAR_FROM_RE,
    ):
        for m in pattern.finditer(text):
            if _overlaps(m.span()):
                continue
            demands.append(_JsImportDemand(spec=m.group("spec"), names=(), lineno=text.count("\n", 0, m.start()) + 1))

    return demands


# ── export PROVISIONS (what a file makes available to others) ────────────────

_EXPORT_FUNCTION_RE = re.compile(r"export\s+(?:default\s+)?(?:async\s+)?function\*?\s+([A-Za-z_$][\w$]*)")
_EXPORT_CLASS_RE = re.compile(r"export\s+(?:default\s+)?class\s+([A-Za-z_$][\w$]*)")
_EXPORT_BINDING_RE = re.compile(r"export\s+(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=")
_EXPORT_BINDING_DESTRUCTURE_RE = re.compile(r"export\s+(?:const|let|var)\s*[\{\[]")
_EXPORT_LIST_LOCAL_RE = re.compile(r"export\s*\{(?P<named>[^}]*)\}(?!\s*from)")
_EXPORT_STAR_AS_FROM_RE = re.compile(r"export\s*\*\s*as\s+([A-Za-z_$][\w$]*)\s*from\s*['\"][^'\"]+['\"]")
_MODULE_EXPORTS_PROP_RE = re.compile(r"(?:module\.exports|exports)\.([A-Za-z_$][\w$]*)\s*=")
_MODULE_EXPORTS_OBJECT_RE = re.compile(r"module\.exports\s*=\s*\{")
_MODULE_EXPORTS_OTHER_RE = re.compile(r"module\.exports\s*=\s*(?!\{)\S")


def _parse_export_list_bound_names(raw: str) -> list[str]:
    """``{a, b as c}`` in an EXPORT position -> the BOUND (exported-as) names."""
    out: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^[A-Za-z_$][\w$]*\s+as\s+([A-Za-z_$][\w$]*)$", part)
        if m:
            out.append(m.group(1))
            continue
        m = re.match(r"^([A-Za-z_$][\w$]*)$", part)
        if m:
            out.append(m.group(1))
    return out


class _JsExportIndex:
    """Lazy, memoized, cycle-safe ``rel path -> provided export names`` index.

    Unlike Python's dotted package-namespace index, JS resolution is PATH-based:
    the index key IS the project-relative file path (no namespace derivation
    needed). ``provided_names(rel)`` returns ``set[str]`` or
    :data:`_JS_PROVIDES_UNKNOWN` (never guesses — anti-false-RED).
    """

    def __init__(self, project_root: Path, files: tuple[str, ...]) -> None:
        self._project_root = project_root
        self._files = set(files)
        self._text_cache: dict[str, str | None] = {}
        self._provided_cache: dict[str, Any] = {}

    def has_file(self, rel: str) -> bool:
        return rel in self._files

    def _text(self, rel: str) -> str | None:
        if rel not in self._text_cache:
            try:
                raw = (self._project_root / rel).read_text(encoding="utf-8")
                self._text_cache[rel] = _strip_js_comments(raw)
            except (OSError, UnicodeDecodeError):
                self._text_cache[rel] = None
        return self._text_cache[rel]

    def provided_names(self, rel: str, _stack: tuple[str, ...] = ()) -> Any:
        if rel in self._provided_cache:
            return self._provided_cache[rel]
        if rel in _stack:  # import cycle — bail conservatively (never guess)
            return _JS_PROVIDES_UNKNOWN
        text = self._text(rel)
        if text is None:
            return _JS_PROVIDES_UNKNOWN
        result = self._compute_provided(rel, text, _stack + (rel,))
        self._provided_cache[rel] = result
        return result

    def _compute_provided(self, rel: str, text: str, stack: tuple[str, ...]) -> Any:
        names: set[str] = set()
        unknown = False

        for m in _EXPORT_FUNCTION_RE.finditer(text):
            names.add(m.group(1))
        for m in _EXPORT_CLASS_RE.finditer(text):
            names.add(m.group(1))
        for m in _EXPORT_BINDING_RE.finditer(text):
            names.add(m.group(1))
        if _EXPORT_BINDING_DESTRUCTURE_RE.search(text):
            unknown = True  # a destructured export's name set is not cheaply decidable
        for m in _EXPORT_LIST_LOCAL_RE.finditer(text):
            names.update(_parse_export_list_bound_names(m.group("named")))
        for m in _EXPORT_STAR_AS_FROM_RE.finditer(text):
            names.add(m.group(1))

        for m in _EXPORT_LIST_FROM_RE.finditer(text):
            spec = m.group("spec")
            bound_names = _parse_export_list_bound_names(m.group("named"))
            if not _is_first_party_specifier(spec):
                names.update(bound_names)  # a third-party re-export: trust it, not our concern
                continue
            target = _resolve_relative_specifier(spec, rel, self._project_root)
            if target is None or not self.has_file(target):
                # The re-export's OWN source is broken — that is flagged as a
                # normal module_resolution demand when THIS file is scanned as an
                # importer (below); here we just cannot confirm what it provides,
                # so add the bound names optimistically (never punish an importer
                # of THIS file for a defect that already surfaces at its source).
                names.update(bound_names)
                continue
            sub = self.provided_names(target, stack)
            if sub is _JS_PROVIDES_UNKNOWN:
                names.update(bound_names)
            else:
                names.update(bound_names)  # bound name is always provided by this file...
                # ...regardless of whether the upstream source name checks out —
                # that mismatch is a defect IN THE RE-EXPORT STATEMENT, a distinct
                # (and rarer) bug class this adapter does not separately model.

        for m in _EXPORT_STAR_FROM_RE.finditer(text):
            spec = m.group("spec")
            if not _is_first_party_specifier(spec):
                continue  # a third-party barrel re-export — cannot see its names, not our concern
            target = _resolve_relative_specifier(spec, rel, self._project_root)
            if target is None or not self.has_file(target):
                continue  # broken star-target surfaces via the normal import-demand scan
            sub = self.provided_names(target, stack)
            if sub is _JS_PROVIDES_UNKNOWN:
                unknown = True
            else:
                names |= sub

        if _MODULE_EXPORTS_OBJECT_RE.search(text) or _MODULE_EXPORTS_OTHER_RE.search(text):
            # ``module.exports = {...}`` / ``= <expr>``: the shape is not cheaply
            # decidable from text alone (a nested object literal's top-level keys
            # would need a bracket-depth-aware scan; a bare identifier could be
            # anything). Conservative: UNKNOWN rather than a fragile hand-rolled
            # object-literal parser (never guess a false-RED).
            unknown = True
        for m in _MODULE_EXPORTS_PROP_RE.finditer(text):
            names.add(m.group(1))  # unambiguous: module.exports.NAME = / exports.NAME =

        if unknown:
            return _JS_PROVIDES_UNKNOWN
        return names


def _run_js_import_resolution_layer(
    project_root: Path, files: tuple[str, ...]
) -> JsToolRun:
    """Resolve every first-party import/require + named symbol over ALL JS-family files.

    THE keystone layer: a test importing ``{ repoRoot }`` from a helper that
    exports ``projectRoot`` is invisible to layer 1 (pure syntax) — only this
    static resolver proves the module/symbol absent.
    """
    index = _JsExportIndex(project_root, files)
    findings: list[ImplementOracleFinding] = []
    observed: list[str] = []

    for rel in files:
        observed.append(rel)
        text = index._text(rel)  # noqa: SLF001 — same module, shared cache
        if text is None:
            continue  # unreadable — layer 1 (or a read-error finding there) owns this
        for demand in _iter_import_demands(text):
            if not _is_first_party_specifier(demand.spec):
                continue  # third-party/stdlib — never checked (anti-false-RED)
            target = _resolve_relative_specifier(demand.spec, rel, project_root)
            if target is None:
                findings.append(
                    ImplementOracleFinding(
                        category=EVIDENCE_MODULE_RESOLUTION,
                        code="JS_MODULE_NOT_FOUND",
                        message=(
                            f"import specifier {demand.spec!r} does not resolve to any "
                            f"generated source/test file"
                        ),
                        path=rel,
                    )
                )
                continue
            if not demand.names:
                continue  # resolution-only demand (default/namespace/require/dynamic)
            provided = index.provided_names(target)
            if provided is _JS_PROVIDES_UNKNOWN:
                continue  # provider undecidable -> never flag (anti-false-RED)
            for name in demand.names:
                if name not in provided:
                    findings.append(
                        ImplementOracleFinding(
                            category=EVIDENCE_MISSING_SYMBOL,
                            code="JS_IMPORT_NAME_NOT_FOUND",
                            message=(
                                f"{target!r} does not export {name!r} (imported from {rel!r} "
                                f"via {demand.spec!r})"
                            ),
                            path=rel,
                        )
                    )

    return JsToolRun(
        name="javascript_first_party_imports",
        executed=True,
        observed_files=tuple(observed),
        findings=tuple(findings),
    )


# ═════════════════════════════════════════════════════════════════════════════
# observability gate (anti-false-green: each layer MUST observe its own scope)
# ═════════════════════════════════════════════════════════════════════════════


def _certify_js_tool_observability(scope: JavaScriptOracleScope, tools: list[JsToolRun]) -> list[ImplementOracleFinding]:
    """Honest-fail if a REQUIRED layer did not observe every file in its scope."""
    findings: list[ImplementOracleFinding] = []
    by_name = {t.name: t for t in tools}
    expectations = {
        "javascript_syntax_check": set(scope.syntax_checkable_files),
        "javascript_first_party_imports": set(scope.expected_files),
    }
    for name, expected in expectations.items():
        tool = by_name.get(name)
        if tool is None or not tool.executed:
            findings.append(
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="javascript_oracle_tool_not_executed",
                    message=f"required JavaScript oracle layer {name!r} did not execute",
                )
            )
            continue
        missing = sorted(expected - set(tool.observed_files))
        if missing:
            findings.append(
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="javascript_oracle_scope_gap",
                    message=(
                        f"{name} did not observe {len(missing)} expected file(s): "
                        + ", ".join(missing[:12])
                    ),
                )
            )
    return findings


# ═════════════════════════════════════════════════════════════════════════════
# The adapter (the contract-path entry: certify_scope + execute).
# ═════════════════════════════════════════════════════════════════════════════


class JavaScriptCompositeOracleAdapter:
    """``implement_oracle`` adapter for plain JS (``adapter: javascript-composite``).

    A ``kind="adapter"`` adapter (Contract Kernel §3): the dispatch calls
    :meth:`certify_scope` (before the run) and :meth:`execute` (the whole
    in-process composite) — NOT the generic command-sequence executor (layer 1
    needs one ``node --check`` subprocess PER FILE — there is no single "check the
    whole project" node flag the way ``tsc``/``go build`` check a whole module in
    one invocation — and layer 2 is pure in-process static analysis).
    """

    def certify_scope(self, ctx: OracleContext) -> str:
        """Certify the JS oracle's concrete file-list covers the required roots.

        Anti-false-green: a required source/test root with ZERO JS-family files is
        a HARD FAIL (:class:`OracleScopeError`, never a silent pass) — a green
        oracle over an empty scope proves nothing.
        """
        source_root, test_root = _js_layout_roots(ctx)
        scope = _javascript_oracle_scope(ctx.project_root, source_root, test_root)
        missing_roots: list[str] = []
        if not scope.source_files:
            missing_roots.append(source_root)
        if not scope.test_files:
            missing_roots.append(test_root)
        if missing_roots:
            raise OracleScopeError(
                "javascript implement-time oracle cannot be certified: no JS-family "
                f"files observed under required root(s) {missing_roots}. A green oracle "
                "over an empty scope proves nothing — the whole point of the "
                "implement-time oracle is to check the generated source AND tests, so "
                "an empty required root is a HARD FAIL (anti-false-green). Ensure the "
                "layout was scaffolded and the units were generated."
            )
        return (
            "javascript oracle scope certified: "
            f"{len(scope.source_files)} source file(s) + {len(scope.test_files)} test "
            f"file(s) observed under source_root='{source_root}' + test_root='{test_root}'"
        )

    def execute(self, ctx: OracleContext) -> ImplementOracleResult:
        """Run ``node --check`` (per file) + the first-party import/export resolver.

        The union of findings gates green: ANY finding => failed. The
        observability gate is folded in. ``passed = not findings``, ``executed =
        True`` (the whole point of the §9 closure is that a NON-executed oracle is
        never how a DECLARED language passes). ``diagnostics=[]`` (no scoped JS
        rerun derivation yet — a bounded loop falls to the broad rerun, which is
        safe, exactly Python's current shape).
        """
        source_root, test_root = _js_layout_roots(ctx)
        project_root = ctx.project_root
        config = ctx.config
        scope = _javascript_oracle_scope(project_root, source_root, test_root)
        all_files = scope.expected_files
        timeout = _js_check_timeout_seconds(config)

        tools: list[JsToolRun] = [
            _run_js_syntax_layer(project_root, scope.syntax_checkable_files, timeout=timeout),
            _run_js_import_resolution_layer(project_root, all_files),
        ]

        findings: list[ImplementOracleFinding] = []
        failed_paths: list[str] = []
        raw_parts: list[str] = []
        for tool in tools:
            findings.extend(tool.findings)
            body = tool.output or ("(no findings)" if tool.executed else "(not executed)")
            raw_parts.append(f"## {tool.name} (executed={tool.executed})\n{body}")
            for f in tool.findings:
                if f.path and f.path not in failed_paths:
                    failed_paths.append(f.path)
        findings.extend(_certify_js_tool_observability(scope, tools))

        passed = not findings
        return ImplementOracleResult(
            passed=passed,
            executed=True,
            command="javascript-composite: node --check + first-party import/export resolver",
            findings=findings,
            failed_paths=failed_paths,
            detail=(
                f"javascript composite oracle {'passed' if passed else 'failed'}; "
                f"{len(scope.source_files)} source file(s), {len(scope.test_files)} test "
                f"file(s), {len(findings)} finding(s)"
            ),
            raw_output="\n\n".join(raw_parts),
            diagnostics=[],
        )

    def normalize_command_result(
        self,
        ctx: OracleContext,  # noqa: ARG002 — signature parity with the protocol.
        *,
        command_id: str,  # noqa: ARG002
        command: CommandSpec,  # noqa: ARG002
        returncode: int,
        stdout: str,  # noqa: ARG002
        stderr: str,  # noqa: ARG002
    ) -> OracleStepObservation:
        """Protocol parity only: a ``kind="adapter"`` oracle runs no shell steps.

        JS's oracle is wholly in-process (:meth:`execute`); the generic
        command-sequence executor never calls this. Implemented only so the
        adapter satisfies :class:`~codd.languages.adapters.implement_oracle.
        ImplementOracleAdapter` in full (mirrors ``PythonCompositeOracleAdapter``'s
        stub).
        """
        return OracleStepObservation(is_clean=returncode == 0, detail="" if returncode == 0 else f"exited {returncode}")


__all__ = [
    "JavaScriptCompositeOracleAdapter",
    "JavaScriptOracleScope",
    "JsToolRun",
]
