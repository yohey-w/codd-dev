"""Python ``python-composite`` implement-oracle adapter (Contract Kernel oracle
dispatch §6 — the PYTHON SWITCH).

The Python tool SEMANTICS — relocated VERBATIM from the gate's hand-written Python
path (``codd.implement_oracle._run_python_composite_oracle`` & helpers). Python has
NO single compiler that proves all-paths symbol coherence (no ``tsc --noEmit``), so
its implement-time anti-false-green oracle is a COMPOSITE of THREE hard layers run
BEFORE pytest while the SUT can still edit every file:

  1. **compile** — in-process ``compile()`` over ALL source+test ``.py``
     (SyntaxError / IndentationError / TabError / decode errors).
  2. **first-party imports** (THE CORE / KEYSTONE) — a static AST resolver over ALL
     source+test ``.py`` that proves every FIRST-PARTY module + imported symbol
     exists. The ONLY layer that catches a ``src/app/hidden.py: from .missing import
     X`` that NO test imports — invisible to py_compile (no resolution) and to
     ``--collect-only`` (never imported).
  3. **pytest --collect-only** — the test-surface importability layer.

UNLIKE Go (a shell-command sequence run by the generic
:func:`codd.languages.oracle_executor.run_command_sequence`), this is NOT a command
sequence: the layers run IN-PROCESS and inspect the file lists each layer observed.
So the Python profile declares ``kind="adapter"`` and this adapter implements
:meth:`PythonCompositeOracleAdapter.execute`, which the dispatch calls instead of
the generic executor (Contract Kernel §3: ``if decl.kind=="adapter": require
execute; return adapter.execute(ctx)``).

FALSE-RED avoidance (these PASS / are ignored, NEVER a hard fail — anti-false-RED):
  * ``if TYPE_CHECKING:`` block imports (a runtime oracle ignores type-only edges).
  * ``try: import X except ImportError:`` guarded imports (optional by intent).
  * third-party imports (not in the first-party index — only first-party hard).
  * non-literal dynamic imports; only a LITERAL ``import_module("a.b")`` is checked.
  * a module whose provided-name set is undecidable (dynamic ``__all__`` / unresolved
    ``import *`` / module-level ``__getattr__``) → symbol provider UNKNOWN → no fail.
Policy mirrors ``codd/test_import_coherence.py``: PROVABLY absent → fail; unknown →
never fail.

ANTI-FALSE-GREEN (the cardinal rule — a non-coherent module must NEVER pass):
  * a py_compile SyntaxError on a first-party file → RED.
  * a first-party import provably absent → RED; third-party/unknown unresolved →
    TOLERATE (env state, never RED).
  * a pytest collection error whose cause is a first-party import/symbol failure →
    RED; a collection failure that is ONLY an uninstalled third-party import →
    TOLERATE; pytest itself missing/un-spawnable → environment_build_error (NOT a
    benign pass — the collect layer could not run, so it proves nothing).
  * any spawn failure / timeout → environment_build_error RED (executed but not a
    pass), never silently green.
  * the observability gate: each REQUIRED tool MUST have OBSERVED every expected
    ``.py`` (compile + first-party imports) / EXECUTED (collect); a gap is an
    environment_build_error, never a silent green.

LEAF rule (no import cycle): imports ONLY stdlib + the oracle value-objects leaf
(:mod:`codd.implement_oracle_types`) + the adapter protocol leaf
(:mod:`codd.languages.adapters.implement_oracle`). It MUST NOT import the gate
(:mod:`codd.implement_oracle`), the registry, or the generic executor — the
dependency edge runs gate → executor → adapters → leaf types, never back. The gate
keeps DELEGATING SHIMS (``certify_python_oracle_scope`` / ``normalize_python_tool_
output`` / ``_collection_failure_is_third_party_only`` / ``PythonToolRun`` /
``PythonOracleScope``) that re-export from here, so existing imports keep working.
"""

from __future__ import annotations

import ast
import shlex
import subprocess  # noqa: S404 — argv is sys.executable + pytest, shell run is the legacy parity behaviour
import sys
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from codd.implement_oracle_types import (
    EVIDENCE_ENVIRONMENT_BUILD,
    EVIDENCE_MISSING_SYMBOL,
    EVIDENCE_MODULE_RESOLUTION,
    EVIDENCE_OTHER,
    EVIDENCE_TEST_NOT_COLLECTED,
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
# Config knobs (read from ctx.config directly — the adapter is a leaf and does not
# import the gate just for a knob; same keys + magnitudes the gate used).
# ═════════════════════════════════════════════════════════════════════════════

#: ``implement.python_name_lint`` modes. ``optional`` (default) runs ruff/pyflakes
#: if present, else SKIPS (no undefined-local-name claim); ``required`` makes its
#: absence an environment_build_error; ``off`` never runs it. Lint is a SEPARATE
#: registry contract (``python.undefined_name_lint.v1``) — when it is skipped the
#: composite oracle does NOT claim undefined-local-name coverage.
_PY_LINT_MODES = ("off", "optional", "required")
DEFAULT_PYTHON_NAME_LINT = "optional"

#: Bounded wall-clock for ``pytest --collect-only`` (cold collect of a large
#: suite). Override via ``implement.python_collect_timeout_seconds``.
DEFAULT_PYTHON_COLLECT_TIMEOUT_SECONDS = 300.0

#: Directories never enumerated by the Python oracle (caches, vcs, venvs, builds).
_PY_ORACLE_SKIP_DIRS = frozenset(
    {
        "__pycache__",
        ".git",
        ".codd",
        ".pytest_cache",
        ".venv",
        "venv",
        "env",
        "build",
        "dist",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".eggs",
        "node_modules",
    }
)

#: A module whose provided-name set cannot be statically decided (dynamic
#: ``__all__`` / unresolved ``import *`` / unreadable). Importing any symbol from
#: such a module is NEVER flagged (anti-false-RED) — mirrors test_import_coherence.
_PY_PROVIDES_UNKNOWN = object()


def _python_lint_mode(config: Mapping[str, Any] | None) -> str:
    section = (config or {}).get("implement") if isinstance(config, Mapping) else None
    if isinstance(section, Mapping) and "python_name_lint" in section:
        value = str(section["python_name_lint"]).strip().lower()
        if value in _PY_LINT_MODES:
            return value
    return DEFAULT_PYTHON_NAME_LINT


def _python_collect_timeout_seconds(config: Mapping[str, Any] | None) -> float:
    section = (config or {}).get("implement") if isinstance(config, Mapping) else None
    if isinstance(section, Mapping):
        raw = section.get("python_collect_timeout_seconds")
        try:
            value = float(raw)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return DEFAULT_PYTHON_COLLECT_TIMEOUT_SECONDS


# ═════════════════════════════════════════════════════════════════════════════
# Value objects (relocated verbatim from the gate).
# ═════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class PythonOracleScope:
    """The concrete ``.py`` file-list a Python composite oracle is certified over.

    ``source_files`` / ``test_files`` are project-relative POSIX paths; the
    executor re-derives the SAME enumeration and asserts each required in-process
    tool OBSERVED all of them (the observability gate — anti-false-green).
    """

    source_files: tuple[str, ...]
    test_files: tuple[str, ...]

    @property
    def expected_files(self) -> tuple[str, ...]:
        """All in-scope files, deduped, source-then-test order preserved."""
        return tuple(dict.fromkeys(self.source_files + self.test_files))


@dataclass(frozen=True)
class PythonToolRun:
    """One Python oracle tool's result + its observation trail (for the gate)."""

    name: str
    executed: bool
    observed_files: tuple[str, ...] = ()
    findings: tuple[ImplementOracleFinding, ...] = ()
    output: str = ""
    optional: bool = False
    skipped_reason: str = ""


@dataclass(frozen=True)
class _PyImportDemand:
    """One runtime import edge an AST visitor found (module + optional symbol).

    ``level`` > 0 is a relative import; ``module`` is the dotted target (may be
    ``None`` for ``from . import name``); ``symbol`` is the imported name for the
    symbol-existence check (``None`` for a plain ``import a.b`` — module-only).
    ``guarded`` marks a ``try/except ImportError`` import (never hard-failed).
    """

    module: str | None
    level: int
    symbol: str | None
    lineno: int
    guarded: bool = False


@dataclass
class _PyModuleInfo:
    """An indexed first-party module/package + its lazily-computed provided names."""

    rel: str
    is_package: bool  # an __init__.py (a package) vs a plain module
    tree: ast.AST | None


# ═════════════════════════════════════════════════════════════════════════════
# pure helpers (relocated verbatim from the gate).
# ═════════════════════════════════════════════════════════════════════════════


def _py_norm(rel: str) -> str:
    return str(rel).strip().replace("\\", "/").strip("/")


def _py_rel_project(path: Path, project_root: Path) -> str:
    try:
        return path.resolve().relative_to(Path(project_root).resolve()).as_posix()
    except (ValueError, OSError):
        return PurePosixPath(str(path).replace("\\", "/")).as_posix()


def _iter_python_oracle_files(project_root: Path, root_rel: str) -> tuple[str, ...]:
    """Every ``.py`` under ``root_rel`` (project-relative), skip-dirs excluded."""
    rel = _py_norm(root_rel)
    if not rel:
        return ()
    root = project_root / rel
    if not root.is_dir():
        return ()
    out: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in _PY_ORACLE_SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        out.append(_py_rel_project(path, project_root))
    return tuple(out)


def _python_oracle_scope(
    project_root: Path, source_root: str, test_root: str
) -> PythonOracleScope:
    """Enumerate the source+test ``.py`` the oracle will check (deduped)."""
    source_files = _iter_python_oracle_files(project_root, source_root)
    test_root_files = _iter_python_oracle_files(project_root, test_root)
    source_set = set(source_files)
    # test_root may nest under source_root in odd layouts — keep test files that are
    # not already counted as source so the same file is observed under one bucket.
    test_files = tuple(f for f in test_root_files if f not in source_set)
    return PythonOracleScope(source_files=source_files, test_files=test_files)


def _py_module_dotted_parts(rel: str) -> list[str]:
    """Dotted segments for a module rel-path (relative to the FIRST-PARTY root).

    ``app/sub/io.py`` → ``["app", "sub", "io"]``; ``app/__init__.py`` →
    ``["app"]``; the dotted key namespace is rooted at the indexed top-level name.
    """
    parts = list(PurePosixPath(rel).parts)
    if not parts:
        return []
    if parts[-1] == "__init__.py":
        return [p for p in parts[:-1] if p]
    leaf = parts[-1]
    leaf = leaf[:-3] if leaf.endswith(".py") else leaf
    return [p for p in (*parts[:-1], leaf) if p]


def _py_string_list(value: ast.AST | None) -> list[str] | None:
    if not isinstance(value, (ast.List, ast.Tuple)):
        return None
    out: list[str] = []
    for elt in value.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            out.append(elt.value)
        else:
            return None
    return out


def _py_parse_file(path: Path) -> ast.AST | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, ValueError, UnicodeDecodeError):
        return None


def _module_level_statements(tree: ast.AST) -> list[ast.stmt]:
    """Flatten module-level statements, descending into compound bodies.

    Yields every statement at MODULE scope — including those nested inside
    top-level ``if`` / ``try`` / ``with`` / ``for`` / ``while`` blocks (their
    bodies/else/handlers/finally are still module scope, so a name bound there is
    a real module attribute). Does NOT descend into ``def`` / ``class`` bodies
    (those open a new, local scope whose names are NOT module attributes).
    """
    out: list[ast.stmt] = []
    body = getattr(tree, "body", [])

    def _walk(stmts: list[ast.stmt]) -> None:
        for stmt in stmts:
            out.append(stmt)
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # new scope — its names are not module attributes
            if isinstance(stmt, ast.If):
                _walk(stmt.body)
                _walk(stmt.orelse)
            elif isinstance(stmt, ast.Try):
                _walk(stmt.body)
                for handler in stmt.handlers:
                    _walk(handler.body)
                _walk(stmt.orelse)
                _walk(stmt.finalbody)
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                _walk(stmt.body)
            elif isinstance(stmt, (ast.For, ast.AsyncFor)):
                _walk(stmt.body)
                _walk(stmt.orelse)
            elif isinstance(stmt, ast.While):
                _walk(stmt.body)
                _walk(stmt.orelse)

    _walk(list(body))
    return out


# ── first-party module index (built from the profile's package) ──────────────


class _PyFirstPartyIndex:
    """First-party dotted-module → file index, with statically-provided symbols.

    First-party == under the profile's ``package_root`` (addressable as
    ``<package_name>.<...>``) PLUS any module under ``source_root`` that is not
    under ``package_root`` (a flat ``src/foo.py`` → ``foo``). Test-tree modules
    are indexed too (so a test importing a sibling test/helper module is checked).
    A dotted key resolves to a module file OR (for a package key) its ``__init__``.
    """

    def __init__(self) -> None:
        self.modules: dict[str, _PyModuleInfo] = {}
        #: dotted keys that are PACKAGES (have a dir, with or without __init__).
        self.packages: set[str] = set()
        #: project-rel path → its first-party dotted module key (the anchor for
        #: resolving that file's RELATIVE imports; computed at index-build time so
        #: the namespace root matches the index keys, not the raw project path).
        self.rel_to_key: dict[str, str] = {}
        self._provided_cache: dict[str, Any] = {}
        self._module_getattr_cache: dict[str, bool] = {}

    def register(self, key: str, info: _PyModuleInfo) -> None:
        self.modules.setdefault(key, info)
        self.rel_to_key.setdefault(_py_norm(info.rel), key)

    def dotted_key_for_rel(self, rel: str) -> str | None:
        """The first-party dotted module key for an indexed file, or ``None``."""
        return self.rel_to_key.get(_py_norm(rel))

    def has_module(self, key: str) -> bool:
        return key in self.modules

    def has_package(self, key: str) -> bool:
        return key in self.packages or key in self.modules

    def resolves(self, key: str) -> bool:
        """A dotted key resolves when it is a known module OR a known package."""
        return self.has_module(key) or self.has_package(key)

    def has_module_getattr(self, key: str) -> bool:
        """True when the module file declares a MODULE-LEVEL ``__getattr__`` (PEP 562).

        A module-level ``def __getattr__(name)`` provides attributes dynamically, so
        ``from mod import X`` for such a module is statically UNDECIDABLE — flagging
        X missing would be a false-RED. Kept SEPARATE from :meth:`provided_names`
        (a star/re-export of such a module must NOT become UNKNOWN — that would widen
        the false-GREEN surface); only a direct named import from the bearer is
        excused, at the call site. Module-level only (a nested / class ``__getattr__``
        is not PEP 562). ``__dir__`` does NOT count (it controls ``dir()`` display,
        not attribute lookup).
        """
        cached = self._module_getattr_cache.get(key)
        if cached is not None:
            return cached
        info = self.modules.get(key)
        result = False
        if info is not None and info.tree is not None:
            result = any(
                isinstance(node, ast.FunctionDef) and node.name == "__getattr__"
                for node in _module_level_statements(info.tree)
            )
        self._module_getattr_cache[key] = result
        return result

    def is_first_party_prefix(self, key: str) -> bool:
        """True when ``key`` is, or is under, a known first-party top-level name."""
        head = key.split(".", 1)[0]
        if head in self._roots:
            return True
        return False

    _roots: frozenset[str] = frozenset()

    def provided_names(self, key: str, _stack: tuple[str, ...] = ()) -> Any:
        """Names a module provides (set[str]) or ``_PY_PROVIDES_UNKNOWN``.

        Resolves top-level ``def``/``class``/assignments + module-top imported
        names (a re-export) + a static ``__all__`` + ``from Y import *`` where Y
        is another first-party module (bounded recursion). A dynamic ``__all__``
        or an unresolved star makes the module UNKNOWN (never flagged).
        """
        if key in self._provided_cache:
            return self._provided_cache[key]
        if key in _stack:  # import cycle — bail conservatively
            return _PY_PROVIDES_UNKNOWN
        info = self.modules.get(key)
        if info is None or info.tree is None:
            return _PY_PROVIDES_UNKNOWN
        result = self._compute_provided(info, _stack + (key,))
        self._provided_cache[key] = result
        return result

    def _compute_provided(self, info: _PyModuleInfo, stack: tuple[str, ...]) -> Any:
        names: set[str] = set()
        star_unknown = False
        dunder_all: list[str] | None = None
        dunder_all_dynamic = False
        # Walk ALL module-LEVEL statements — including names bound inside top-level
        # ``try``/``if``/``with``/``for``/``while`` blocks (a conditional/guarded
        # definition is still a real module attribute, e.g.
        # ``try: from .optional import x except ImportError: x = None`` provides
        # ``x``). Function/class BODIES are NOT descended (those are local scopes).
        for node in _module_level_statements(info.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    self._add_assign_target(target, names)
                if any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
                    extracted = _py_string_list(node.value)
                    if extracted is None:
                        dunder_all_dynamic = True
                    else:
                        dunder_all = (dunder_all or []) + extracted
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name):
                    names.add(node.target.id)
                    if node.target.id == "__all__":
                        extracted = _py_string_list(node.value) if node.value else None
                        if extracted is None:
                            dunder_all_dynamic = True
                        else:
                            dunder_all = (dunder_all or []) + extracted
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    bound = alias.asname or alias.name.split(".")[0]
                    if bound:
                        names.add(bound)
            elif isinstance(node, ast.ImportFrom):
                if any(a.name == "*" for a in node.names):
                    sub = self._resolve_star_source(info, node, stack)
                    if sub is _PY_PROVIDES_UNKNOWN:
                        star_unknown = True
                    else:
                        names |= sub  # type: ignore[arg-type]
                else:
                    for alias in node.names:
                        bound = alias.asname or alias.name
                        if bound:
                            names.add(bound)
        if dunder_all_dynamic:
            return _PY_PROVIDES_UNKNOWN
        if star_unknown:
            return _PY_PROVIDES_UNKNOWN
        if dunder_all is not None:
            return names | set(dunder_all)
        return names

    @staticmethod
    def _add_assign_target(target: ast.AST, names: set[str]) -> None:
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                _PyFirstPartyIndex._add_assign_target(elt, names)

    def _resolve_star_source(self, info: _PyModuleInfo, node: ast.ImportFrom, stack: tuple[str, ...]) -> Any:
        importer_key = self.dotted_key_for_rel(info.rel)
        target_key = self.resolve_import_target_key(
            importer_key=importer_key,
            importer_is_package=info.is_package,
            module=node.module,
            level=node.level,
        )
        if target_key is None or not self.has_module(target_key):
            return _PY_PROVIDES_UNKNOWN
        return self.provided_names(target_key, stack)

    def resolve_import_target_key(
        self,
        *,
        importer_key: str | None,
        importer_is_package: bool,
        module: str | None,
        level: int,
    ) -> str | None:
        """Resolve a (possibly relative) import target to a first-party dotted key.

        Absolute (``level == 0``) → the dotted ``module`` itself. Relative
        (``level > 0``) → resolved against the IMPORTER's OWN dotted key (the
        first-party namespace anchor, NOT the raw project path): ``level`` 1 = the
        importer's package, each extra level climbs one parent. A module's own key
        ends at its leaf; its package is that key minus the leaf (a package
        ``__init__`` is already its package key).
        """
        if level and level > 0:
            if importer_key is None:
                return None  # importer not indexed → cannot anchor a relative import
            base_parts = importer_key.split(".")
            pkg_parts = base_parts if importer_is_package else base_parts[:-1]
            climb = level - 1
            if climb > len(pkg_parts):
                return None
            anchor = pkg_parts[: len(pkg_parts) - climb] if climb else pkg_parts
            suffix = module.split(".") if module else []
            segments = [s for s in (*anchor, *suffix) if s]
            if not segments:
                return None
            return ".".join(segments)
        if module:
            return module
        return None


def _build_python_module_index(
    project_root: Path,
    source_root: str,
    package_root: str,
    test_root: str,
    scope: PythonOracleScope,
) -> _PyFirstPartyIndex:
    """Build the first-party dotted-module index from the profile + the file list.

    The dotted NAMESPACE is rooted so that:
      * a module under ``package_root`` (``src/<pkg>/mod.py``) is keyed
        ``<pkg>.mod`` (the package-absolute name the runtime + tests use);
      * a flat module under ``source_root`` but outside ``package_root``
        (``src/foo.py``) is keyed ``foo``;
      * a test-tree module (``tests/helpers/io.py``) is keyed by its path
        relative to ``test_root`` (``helpers.io``) AND the bare leaf — so a test's
        ``from helpers import io`` / sibling import resolves.
    Every intermediate package dir is registered so ``import <pkg>.sub`` resolves.
    """
    index = _PyFirstPartyIndex()
    roots: set[str] = set()
    source_root = _py_norm(source_root)
    package_root = _py_norm(package_root)
    test_root = _py_norm(test_root)

    def _register_module(dotted_parts: list[str], rel: str) -> None:
        if not dotted_parts:
            return
        roots.add(dotted_parts[0])
        is_pkg = rel.endswith("__init__.py")
        info = _PyModuleInfo(rel=rel, is_package=is_pkg, tree=_py_parse_file(project_root / rel))
        key = ".".join(dotted_parts)
        index.register(key, info)
        # Register every ancestor dir as a package key (so ``import a.b`` where b is
        # a subpackage resolves even when a/__init__.py is the registered module).
        for i in range(1, len(dotted_parts)):
            index.packages.add(".".join(dotted_parts[:i]))
        if is_pkg:
            index.packages.add(key)

    for rel in scope.source_files:
        rel_n = _py_norm(rel)
        if package_root and (rel_n == package_root or rel_n.startswith(package_root + "/")):
            # Under the named package: dotted name rooted at the package dir's PARENT
            # so the package itself becomes the head segment (``<pkg>.mod``).
            inside = rel_n[len(_py_norm(source_root)) + 1 :] if source_root and rel_n.startswith(source_root + "/") else rel_n
            parts = _py_module_dotted_parts(inside)
            _register_module(parts, rel_n)
        elif source_root and rel_n.startswith(source_root + "/"):
            # Flat module under source_root but outside package_root → bare name.
            inside = rel_n[len(source_root) + 1 :]
            parts = _py_module_dotted_parts(inside)
            _register_module(parts, rel_n)
        else:
            # source_root is "" or the file is at project root — key by its own path.
            parts = _py_module_dotted_parts(rel_n)
            _register_module(parts, rel_n)

    for rel in scope.test_files:
        rel_n = _py_norm(rel)
        if test_root and rel_n.startswith(test_root + "/"):
            inside = rel_n[len(test_root) + 1 :]
        else:
            inside = rel_n
        parts = _py_module_dotted_parts(inside)
        _register_module(parts, rel_n)

    index._roots = frozenset(roots)
    return index


# ── layer 1: in-process compile (syntax / indentation / encoding) ────────────


def _run_python_compile_layer(project_root: Path, files: tuple[str, ...]) -> PythonToolRun:
    """Compile every ``.py`` in-process; emit a finding per syntax/encoding error.

    ``compile(text, path, "exec")`` does NOT resolve imports — its job is to catch
    the syntax class (SyntaxError/IndentationError/TabError) + read/decode errors.
    A SyntaxError is a real coherence error (``EVIDENCE_OTHER``); a decode/read
    error is an environment problem (``EVIDENCE_ENVIRONMENT_BUILD``).
    """
    findings: list[ImplementOracleFinding] = []
    observed: list[str] = []
    for rel in files:
        observed.append(rel)
        path = project_root / rel
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            findings.append(
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="decode_error",
                    message=f"could not decode as UTF-8: {exc}",
                    path=rel,
                )
            )
            continue
        except OSError as exc:
            findings.append(
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="read_error",
                    message=f"could not read file: {exc}",
                    path=rel,
                )
            )
            continue
        try:
            compile(text, str(path), "exec", dont_inherit=True)
        except SyntaxError as exc:  # IndentationError/TabError are SyntaxError subclasses
            findings.append(
                ImplementOracleFinding(
                    category=EVIDENCE_OTHER,
                    code=type(exc).__name__,
                    message=(exc.msg or str(exc)),
                    path=rel,
                )
            )
        except ValueError as exc:
            # e.g. source containing a NUL byte — a real, honest syntax/source error.
            findings.append(
                ImplementOracleFinding(
                    category=EVIDENCE_OTHER,
                    code="compile_error",
                    message=str(exc),
                    path=rel,
                )
            )
    return PythonToolRun(
        name="python_compile",
        executed=True,
        observed_files=tuple(observed),
        findings=tuple(findings),
    )


# ── layer 2: first-party import resolver (THE CORE) ──────────────────────────


def _iter_runtime_import_demands(tree: ast.AST) -> list[_PyImportDemand]:
    """Collect runtime import edges; SKIP ``TYPE_CHECKING`` blocks; MARK guarded.

    * ``if TYPE_CHECKING:`` / ``if typing.TYPE_CHECKING:`` bodies are NOT walked
      (a runtime oracle ignores type-only imports).
    * imports inside a ``try`` whose handlers catch ``ImportError`` /
      ``ModuleNotFoundError`` are marked ``guarded`` (optional by intent).
    * literal ``importlib.import_module("a.b")`` / ``__import__("a.b")`` become a
      module-only demand; a non-literal arg is ignored (unknown).
    """
    demands: list[_PyImportDemand] = []

    def _is_type_checking_test(test: ast.expr) -> bool:
        if isinstance(test, ast.Name):
            return test.id == "TYPE_CHECKING"
        if isinstance(test, ast.Attribute):
            return test.attr == "TYPE_CHECKING"
        return False

    def _handler_catches_importerror(handlers: list[ast.excepthandler]) -> bool:
        for h in handlers:
            etype = h.type
            if etype is None:
                return True  # bare ``except:`` — swallows ImportError too
            candidates = etype.elts if isinstance(etype, ast.Tuple) else [etype]
            for c in candidates:
                name = c.id if isinstance(c, ast.Name) else (c.attr if isinstance(c, ast.Attribute) else "")
                if name in ("ImportError", "ModuleNotFoundError", "Exception", "BaseException"):
                    return True
        return False

    def _visit(node: ast.AST, *, guarded: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If) and _is_type_checking_test(child.test):
                # Skip the TYPE_CHECKING body; still walk the ELSE branch (runtime).
                for sub in child.orelse:
                    _visit(sub, guarded=guarded)
                continue
            if isinstance(child, ast.Try):
                body_guarded = guarded or _handler_catches_importerror(child.handlers)
                for sub in child.body:
                    _visit(sub, guarded=body_guarded)
                for sub in (*child.handlers, *child.orelse, *child.finalbody):
                    _visit(sub, guarded=guarded)
                continue
            if isinstance(child, ast.Import):
                for alias in child.names:
                    demands.append(
                        _PyImportDemand(
                            module=alias.name,
                            level=0,
                            symbol=None,
                            lineno=child.lineno,
                            guarded=guarded,
                        )
                    )
            elif isinstance(child, ast.ImportFrom):
                for alias in child.names:
                    if alias.name == "*":
                        # star import — symbol set is the source's; module-only check.
                        demands.append(
                            _PyImportDemand(
                                module=child.module,
                                level=child.level or 0,
                                symbol=None,
                                lineno=child.lineno,
                                guarded=guarded,
                            )
                        )
                    else:
                        demands.append(
                            _PyImportDemand(
                                module=child.module,
                                level=child.level or 0,
                                symbol=alias.name,
                                lineno=child.lineno,
                                guarded=guarded,
                            )
                        )
            elif isinstance(child, ast.Call):
                dynamic = _dynamic_import_demand(child, guarded=guarded)
                if dynamic is not None:
                    demands.append(dynamic)
                _visit(child, guarded=guarded)
            else:
                _visit(child, guarded=guarded)

    _visit(tree, guarded=False)
    return demands


def _dynamic_import_demand(call: ast.Call, *, guarded: bool) -> _PyImportDemand | None:
    """A LITERAL ``importlib.import_module("a.b")`` / ``__import__("a.b")`` demand.

    Only a single string-literal first argument is checked; any non-literal arg
    (a variable / f-string / concat) is unknown → ``None`` (never flagged).
    """
    func = call.func
    is_import_module = isinstance(func, ast.Attribute) and func.attr == "import_module"
    is_dunder_import = isinstance(func, ast.Name) and func.id == "__import__"
    if not (is_import_module or is_dunder_import):
        return None
    if not call.args:
        return None
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value:
        return _PyImportDemand(
            module=first.value,
            level=0,
            symbol=None,
            lineno=getattr(call, "lineno", 0),
            guarded=guarded,
        )
    return None


def _resolve_python_import_demand(
    demand: _PyImportDemand,
    *,
    importer_rel: str,
    importer_is_package: bool,
    index: _PyFirstPartyIndex,
) -> tuple[str, str, str]:
    """Resolve one demand → ``(kind, module_key, message)``.

    ``kind`` ∈ {``"ok"``, ``"ignore"``, ``"module_missing"``, ``"symbol_missing"``}.
    Only FIRST-PARTY demands are hard-checked; a third-party / unknown target is
    ``ignore`` (anti-false-RED). A guarded import is never ``*_missing``.
    """
    importer_key = index.dotted_key_for_rel(importer_rel)
    module_key = index.resolve_import_target_key(
        importer_key=importer_key,
        importer_is_package=importer_is_package,
        module=demand.module,
        level=demand.level,
    )
    if module_key is None:
        return "ignore", "", ""
    # Is this a first-party target at all? Relative imports are first-party by
    # construction (they resolve within the importer's own package tree); an
    # absolute import is first-party only when its head is an indexed root.
    first_party = demand.level > 0 or index.is_first_party_prefix(module_key)
    if not first_party:
        return "ignore", module_key, ""  # third-party → not our concern
    if not index.resolves(module_key):
        if demand.guarded:
            return "ignore", module_key, ""  # optional first-party plugin — warn-not-fail
        return (
            "module_missing",
            module_key,
            f"first-party module {module_key!r} does not resolve to any generated "
            f"source/test module",
        )
    # Module resolves. Symbol check (only for ``from X import sym`` — module-only
    # demands have symbol=None). The symbol may live in the module OR, if the key
    # is a PACKAGE, name a SUBMODULE (``from pkg import sub`` where sub is sub.py).
    if demand.symbol is None:
        return "ok", module_key, ""
    submodule_key = f"{module_key}.{demand.symbol}"
    if index.resolves(submodule_key):
        return "ok", module_key, ""  # ``from pkg import submodule``
    if index.has_module_getattr(module_key):
        # PEP 562: the target module declares a module-level ``__getattr__`` that
        # provides attributes dynamically, so this symbol's presence is statically
        # UNDECIDABLE — do not flag it missing (false-RED avoidance). Module
        # resolution above is still required (a missing module stays module_missing),
        # and re-exports do NOT inherit this (provided_names stays precise): only a
        # DIRECT named import from the ``__getattr__`` bearer is excused.
        return "ok", module_key, ""
    provided = index.provided_names(module_key)
    if provided is _PY_PROVIDES_UNKNOWN:
        return "ok", module_key, ""  # provider undecidable → never flag (anti-false-RED)
    if demand.symbol in provided:
        return "ok", module_key, ""
    if demand.guarded:
        return "ignore", module_key, ""
    return (
        "symbol_missing",
        module_key,
        f"first-party module {module_key!r} does not define or re-export symbol "
        f"{demand.symbol!r}",
    )


def _run_python_first_party_import_layer(
    project_root: Path,
    source_root: str,
    package_root: str,
    test_root: str,
    scope: PythonOracleScope,
    files: tuple[str, ...],
) -> PythonToolRun:
    """Resolve every first-party import + imported symbol over ALL source+test .py.

    THE keystone layer: a ``src/app/hidden.py: from .missing import X`` that no
    test imports is invisible to py_compile (no resolution) and to collect-only
    (never imported) — only this static resolver proves the module/symbol absent.
    """
    index = _build_python_module_index(project_root, source_root, package_root, test_root, scope)
    findings: list[ImplementOracleFinding] = []
    observed: list[str] = []
    pkg_rels = {m.rel for m in index.modules.values() if m.is_package}
    for rel in files:
        observed.append(rel)
        tree = _py_parse_file(project_root / rel)
        if tree is None:
            continue  # the compile layer owns unparseable files
        importer_is_package = rel in pkg_rels or rel.endswith("__init__.py")
        for demand in _iter_runtime_import_demands(tree):
            kind, _module_key, message = _resolve_python_import_demand(
                demand,
                importer_rel=rel,
                importer_is_package=importer_is_package,
                index=index,
            )
            if kind == "module_missing":
                findings.append(
                    ImplementOracleFinding(
                        category=EVIDENCE_MODULE_RESOLUTION,
                        code="PY_MODULE_NOT_FOUND",
                        message=message,
                        path=rel,
                    )
                )
            elif kind == "symbol_missing":
                findings.append(
                    ImplementOracleFinding(
                        category=EVIDENCE_MISSING_SYMBOL,
                        code="PY_IMPORT_NAME_NOT_FOUND",
                        message=message,
                        path=rel,
                    )
                )
    return PythonToolRun(
        name="python_first_party_imports",
        executed=True,
        observed_files=tuple(observed),
        findings=tuple(findings),
    )


# ── layer 3: pytest --collect-only (test-surface importability) ──────────────

#: pytest collection-error patterns (multiple errors per run are allowed).
_PYTEST_ERROR_COLLECTING = re.compile(
    r"^_+\s+ERROR collecting (?P<path>.+?)\s+_+$",
    re.MULTILINE,
)
_PYTEST_IMPORT_WHILE = re.compile(
    r"(?:Error|error) while importing test module ['\"](?P<path>.+?)['\"]",
)
_PYTEST_MOD_NOT_FOUND = re.compile(
    r"(?:E\s+)?ModuleNotFoundError:\s+No module named ['\"](?P<module>[^'\"]+)['\"]",
)
_PYTEST_CANNOT_IMPORT_NAME = re.compile(
    r"(?:E\s+)?ImportError:\s+cannot import name ['\"](?P<symbol>[^'\"]+)['\"] from ['\"](?P<module>[^'\"]+)['\"]",
)
_PYTEST_SYNTAX_ERROR = re.compile(
    r"(?:E\s+)?(?P<kind>SyntaxError|IndentationError|TabError):\s+(?P<msg>.+)",
)
#: pytest itself missing / un-spawnable.
_PYTEST_NO_MODULE = re.compile(r"No module named pytest", re.IGNORECASE)


def normalize_python_tool_output(
    output: str,
    *,
    command: str,
    project_root: Path,
    profile: Any = None,
    is_first_party: Callable[[str], bool] | None = None,
) -> tuple[list[ImplementOracleFinding], list[str]]:
    """Normalize ``pytest --collect-only`` output → (findings, failed paths).

    Parses every collection error (ModuleNotFoundError / cannot-import-name /
    SyntaxError / generic ERROR-collecting) into a language-neutral finding. Used
    by the collect layer; also the gate's ``normalize_python_tool_output`` shim
    (the public ``__all__`` name) re-exports this.

    ``is_first_party`` (when supplied) gates module-resolution findings to
    FIRST-PARTY targets only: a collection ``ModuleNotFoundError`` / cannot-import
    -name whose target module is third-party / stdlib is an IMPLEMENT-TIME
    ENVIRONMENT concern (the dependency is simply not installed yet), NOT a
    coherence failure — first-party importability is already proven by the static
    first-party import resolver layer. Emitting it would be a false-RED. A
    first-party target (the SUT genuinely missing a module/symbol) is always
    reported. When ``is_first_party`` is None the legacy behaviour (emit every
    parsed error) is preserved for callers without a module index.

    ``profile`` is accepted for legacy signature parity (the old gate-level
    function took it); it is unused — attribution is path-based.
    """
    text = output or ""
    findings: list[ImplementOracleFinding] = []
    failed_paths: list[str] = []

    def _add_path(raw: str | None) -> str | None:
        if not raw:
            return None
        rel = _py_rel_project(project_root / raw.strip(), project_root)
        if rel not in failed_paths:
            failed_paths.append(rel)
        return rel

    for m in _PYTEST_ERROR_COLLECTING.finditer(text):
        _add_path(m.group("path"))
    for m in _PYTEST_IMPORT_WHILE.finditer(text):
        _add_path(m.group("path"))

    primary_path = failed_paths[0] if failed_paths else None
    for m in _PYTEST_MOD_NOT_FOUND.finditer(text):
        module = m.group("module")
        if is_first_party is not None and not is_first_party(module):
            continue  # third-party / stdlib not installed at implement time — env, not coherence
        findings.append(
            ImplementOracleFinding(
                category=EVIDENCE_MODULE_RESOLUTION,
                code="PY_MODULE_NOT_FOUND",
                message=f"No module named {module!r} (pytest collection)",
                path=primary_path,
            )
        )
    for m in _PYTEST_CANNOT_IMPORT_NAME.finditer(text):
        source_module = m.group("module")
        if is_first_party is not None and not is_first_party(source_module):
            continue  # symbol missing from a third-party module — env/version, not SUT coherence
        findings.append(
            ImplementOracleFinding(
                category=EVIDENCE_MISSING_SYMBOL,
                code="PY_IMPORT_NAME_NOT_FOUND",
                message=f"cannot import name {m.group('symbol')!r} from {source_module!r}",
                path=primary_path,
            )
        )
    for m in _PYTEST_SYNTAX_ERROR.finditer(text):
        findings.append(
            ImplementOracleFinding(
                category=EVIDENCE_OTHER,
                code=m.group("kind"),
                message=m.group("msg").strip(),
                path=primary_path,
            )
        )
    return findings, failed_paths


def _collection_failure_is_third_party_only(
    output: str, is_first_party: Callable[[str], bool], errored_file_count: int
) -> bool:
    """True iff EVERY pytest collection error is an uninstalled third-party / stdlib
    import — so the non-zero collect exit is an implement-time ENV concern, not a
    coherence failure.

    ``errored_file_count`` is the number of DISTINCT errored test files (the
    deduped, project-relative ``failed_paths`` the normalizer already computed —
    parsing it here would double-count, since one file surfaces both an
    ``ERROR collecting <rel>`` header and an absolute ``importing test module`` line).

    Anti-false-green (conservative by construction): returns False — i.e. let the
    honest ``pytest_collect_exit_N`` failure stand — UNLESS every errored file is
    accounted for by a non-first-party ``ModuleNotFoundError``, AND there is NO
    first-party module-not-found, NO cannot-import-name from a first-party module,
    and NO SyntaxError. First-party importability/coherence is independently proven
    by the static first-party import-resolver layer (the keystone layer), so a real
    SUT defect cannot hide behind a benign verdict here.
    """
    text = output or ""
    if errored_file_count <= 0:
        return False  # non-zero exit with no identifiable errored file — stay honest
    if _PYTEST_SYNTAX_ERROR.search(text):
        return False
    for m in _PYTEST_CANNOT_IMPORT_NAME.finditer(text):
        if is_first_party(m.group("module")):
            return False
    third_party_module_errors = 0
    for m in _PYTEST_MOD_NOT_FOUND.finditer(text):
        if is_first_party(m.group("module")):
            return False
        third_party_module_errors += 1
    # every errored file must be accounted for by a third-party module-not-found
    return third_party_module_errors >= errored_file_count


def _output_tail(stdout: str | None, stderr: str | None, limit: int = 4000) -> str:
    combined = "\n".join(part.strip() for part in (stdout, stderr) if part and part.strip())
    if len(combined) <= limit:
        return combined
    return f"... (truncated) ...\n{combined[-limit:]}"


def _run_python_pytest_collect_layer(
    project_root: Path,
    source_root: str,
    package_root: str,
    test_root: str,
    scope: PythonOracleScope,
    config: Mapping[str, Any] | None,
) -> PythonToolRun:
    """``python -m pytest <test_root> --collect-only -q`` — test importability.

    REQUIRED (not optional): the Python profile's runner IS pytest, so pytest
    absent / un-spawnable is an ``environment_build_error`` (NEVER a silent skip —
    a generated system whose tests cannot even be collected is not verified).
    Exit 0 ⇒ the test surface imports cleanly. Non-zero ⇒ parse the collection
    errors; a collection failure caused ONLY by uninstalled third-party imports is
    an env concern (benign — first-party coherence is proven by the resolver
    layer); a non-zero exit with no parseable / unattributable error is still an
    honest failure.
    """
    timeout = _python_collect_timeout_seconds(config)
    test_root_rel = _py_norm(test_root) or "."
    command = (
        f"{shlex.quote(sys.executable)} -m pytest {shlex.quote(test_root_rel)} "
        f"--collect-only -q -p no:cacheprovider"
    )
    try:
        completed = subprocess.run(
            command,
            shell=True,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return PythonToolRun(
            name="pytest_collect",
            executed=True,
            findings=(
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="pytest_collect_timeout",
                    message=f"pytest --collect-only exceeded {timeout:g}s",
                ),
            ),
        )
    except (OSError, ValueError) as exc:
        return PythonToolRun(
            name="pytest_collect",
            executed=False,
            findings=(
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="pytest_spawn_error",
                    message=f"could not spawn pytest: {exc}",
                ),
            ),
        )
    output = "\n".join(p for p in (completed.stdout, completed.stderr) if p)
    if _PYTEST_NO_MODULE.search(output):
        return PythonToolRun(
            name="pytest_collect",
            executed=False,
            output=output,
            findings=(
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="pytest_not_installed",
                    message=(
                        "pytest is not installed, so the generated test surface cannot "
                        "be collected (the Python profile's runner IS pytest); this is "
                        "an environment failure, not a pass."
                    ),
                ),
            ),
        )
    if completed.returncode == 0:
        return PythonToolRun(name="pytest_collect", executed=True, output=output)
    index = _build_python_module_index(project_root, source_root, package_root, test_root, scope)
    findings, failed_paths = normalize_python_tool_output(
        output,
        command=command,
        project_root=project_root,
        is_first_party=index.is_first_party_prefix,
    )
    if not findings:
        if _collection_failure_is_third_party_only(
            output, index.is_first_party_prefix, len(failed_paths)
        ):
            # Collection failed ONLY because a third-party dependency is not
            # installed at implement time. First-party importability is proven by
            # the static resolver layer; this is an environment concern, not a
            # coherence failure → benign (never a false-RED on uninstalled deps).
            return PythonToolRun(name="pytest_collect", executed=True, output=output)
        findings = [
            ImplementOracleFinding(
                category=EVIDENCE_TEST_NOT_COLLECTED,
                code=f"pytest_collect_exit_{completed.returncode}",
                message=(
                    _output_tail(completed.stdout, completed.stderr)
                    or f"pytest --collect-only exited {completed.returncode} with no parseable diagnostic"
                ),
            )
        ]
    return PythonToolRun(
        name="pytest_collect",
        executed=True,
        observed_files=tuple(failed_paths),
        findings=tuple(findings),
        output=output,
    )


# ── optional layer: ruff/pyflakes name lint (SEPARATE registry contract) ─────


def _which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


def _run_python_lint_layer(
    project_root: Path,
    source_root: str,
    files: tuple[str, ...],
    *,
    required: bool,
) -> PythonToolRun:
    """Optional ruff/pyflakes undefined-name lint (F821 family).

    OPTIONAL by default: if neither ruff nor pyflakes is importable/runnable, this
    SKIPS (``optional`` mode) — and the composite oracle then does NOT claim
    undefined-local-name coverage (that stays the separate, UNCOVERED
    ``python.undefined_name_lint.v1`` contract). ``required`` mode turns absence
    into an ``environment_build_error``.
    """
    import importlib.util

    have_ruff = _which("ruff") is not None
    have_pyflakes = importlib.util.find_spec("pyflakes") is not None
    if not have_ruff and not have_pyflakes:
        if required:
            return PythonToolRun(
                name="python_name_lint",
                executed=False,
                optional=True,
                findings=(
                    ImplementOracleFinding(
                        category=EVIDENCE_ENVIRONMENT_BUILD,
                        code="name_lint_unavailable",
                        message=(
                            "implement.python_name_lint=required but neither ruff nor "
                            "pyflakes is available to check undefined names"
                        ),
                    ),
                ),
            )
        return PythonToolRun(
            name="python_name_lint",
            executed=False,
            optional=True,
            skipped_reason="ruff/pyflakes not available (optional lint skipped)",
        )
    target_root = _py_norm(source_root) or "."
    if have_ruff:
        command = f"{shlex.quote(_which('ruff'))} check --select F821,F822 --output-format concise {shlex.quote(target_root)}"
    else:
        command = f"{shlex.quote(sys.executable)} -m pyflakes {shlex.quote(target_root)}"
    try:
        completed = subprocess.run(
            command, shell=True, cwd=project_root, capture_output=True, text=True, timeout=300
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        if required:
            return PythonToolRun(
                name="python_name_lint",
                executed=False,
                optional=True,
                findings=(
                    ImplementOracleFinding(
                        category=EVIDENCE_ENVIRONMENT_BUILD,
                        code="name_lint_spawn_error",
                        message=f"could not run name lint: {exc}",
                    ),
                ),
            )
        return PythonToolRun(
            name="python_name_lint",
            executed=False,
            optional=True,
            skipped_reason=f"name lint did not run ({exc}); optional, skipped",
        )
    output = "\n".join(p for p in (completed.stdout, completed.stderr) if p)
    findings: list[ImplementOracleFinding] = []
    if completed.returncode != 0:
        for line in output.splitlines():
            if "F821" in line or "undefined name" in line:
                findings.append(
                    ImplementOracleFinding(
                        category=EVIDENCE_OTHER,
                        code="PY_UNDEFINED_NAME",
                        message=line.strip(),
                    )
                )
    return PythonToolRun(
        name="python_name_lint",
        executed=True,
        observed_files=files,
        findings=tuple(findings),
        output=output,
        optional=True,
    )


# ── observability gate (anti-false-green: each required tool MUST observe all) ─


def _certify_python_tool_observability(
    scope: PythonOracleScope, tools: list[PythonToolRun]
) -> list[ImplementOracleFinding]:
    """Honest-fail if a REQUIRED tool did not observe every expected file / run.

    compile + first-party-imports MUST have OBSERVED all expected .py; pytest
    collect MUST have EXECUTED. A gap is an ``environment_build_error`` finding —
    NEVER a silent green (the executor adds these to the result's findings).
    """
    expected = set(scope.expected_files)
    findings: list[ImplementOracleFinding] = []
    by_name = {t.name: t for t in tools}
    for required_name in ("python_compile", "python_first_party_imports"):
        tool = by_name.get(required_name)
        if tool is None or not tool.executed:
            findings.append(
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="python_oracle_tool_not_executed",
                    message=f"required Python oracle tool {required_name!r} did not execute",
                )
            )
            continue
        missing = sorted(expected - set(tool.observed_files))
        if missing:
            findings.append(
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="python_oracle_scope_gap",
                    message=(
                        f"{required_name} did not observe {len(missing)} expected .py "
                        f"file(s): " + ", ".join(missing[:12])
                    ),
                )
            )
    collect = by_name.get("pytest_collect")
    if collect is None or not collect.executed:
        # A non-executed collect already carries its own environment finding from the
        # collect layer; add the observability finding only when the layer is wholly
        # absent (defensive — the executor always appends a collect tool).
        if collect is None:
            findings.append(
                ImplementOracleFinding(
                    category=EVIDENCE_ENVIRONMENT_BUILD,
                    code="pytest_collect_not_executed",
                    message="pytest --collect-only did not execute; test importability is unobserved",
                )
            )
    return findings


# ═════════════════════════════════════════════════════════════════════════════
# Layout reading (the Python-specific scope knowledge baked into the adapter).
#
# Cut A.3: the adapter derives ``(source_root, package_root, test_root)`` from the
# resolved ``LayoutSpec`` (``ctx.language_profile.layout`` — ``source_sets`` /
# ``test_sets`` / ``package_root`` template) + the gate-resolved ``ctx.package_name``,
# NOT from a legacy ``LayoutProfile.source_root`` layout VIEW (that bridge is gone).
# This MIRRORS the TS adapter's ``_ts_layout_roots`` (reads ``layout.source_sets`` /
# ``test_sets``), but with Python's named-package nuance: Python's single source-set
# root is ``src/{package_name}`` (the PACKAGE dir), so the bare ``source_root`` is
# the PARENT of the substituted ``package_root`` (``src/app`` → ``src``) — the import
# index keys a module under ``package_root`` as ``<pkg>.mod`` and a flat module under
# ``source_root`` outside it as a bare name, so the two roots are NOT interchangeable.
#
# Python's scope policy (require source root; require test root for the collect
# layer) is Python-specific knowledge → baked here, not depending on a legacy
# OracleScopeSpec.
# ═════════════════════════════════════════════════════════════════════════════

#: Python's composite oracle requires BOTH a source scope (the keystone resolver +
#: compile run over it) AND a test scope (the collect layer + test-import resolver).
#: Baked-in (Python-specific) — the legacy spec declared the same.
_REQUIRE_SOURCE_ROOT = True
_REQUIRE_TEST_ROOT = True


def _py_parent_dir(path: str) -> str:
    """Parent directory of a POSIX relative path (``src/app`` → ``src``).

    Returns ``"."`` when there is no parent segment. Mirrors
    :func:`codd.languages.compat._parent_dir` (the proven-byte-identical derivation):
    a ``named_package`` layout always nests ``<src>/<pkg>``, so the legacy
    ``source_root`` is the package_root's parent.
    """
    norm = _py_norm(path)
    if "/" not in norm:
        return "."
    return norm.rsplit("/", 1)[0]


def _py_layout_from_layout_spec(ctx: OracleContext) -> tuple[str, str, str]:
    """``(source_root, package_root, test_root)`` derived from the resolved ``LayoutSpec``.

    Cut A.3 (the Python oracle's layout AUTHORITY): the adapter reads the v3
    ``LayoutSpec`` (``ctx.language_profile.layout``) + the gate-resolved
    ``ctx.package_name``, NOT a legacy ``LayoutProfile.source_root``. Anti-false-green
    is paramount — a silent wrong layout here is a false-green — so this is strict:

    * **Multi-source-set / multi-(required-)test-set → HARD FAIL** (RED via
      :class:`OracleScopeError`). Python declares exactly ONE source-set + ONE unit
      test-set today; the import index + collect scope are single-root. Reading only
      ``source_sets[0]`` would SILENTLY drop the v3 set-semantics (a dropped root =
      unproven code = false-green), so >1 is an explicit unsupported RED, never a
      silent first-root collapse. (Multi-root observe-all is a later increment +
      Stage-5 poison test; this increment is behavior-preserving on single-root.)
    * **package_root** = the ``package_root`` template with ``{package_name}``
      SUBSTITUTED from ``ctx.package_name`` (``named_package`` → ``src/app``).
    * **source_root** = the PARENT of ``package_root`` for ``named_package``
      (``src/app`` → ``src``); the bare set root for ``path_root``.
    * **test_root** = the single test-set's root (``tests``).
    * **Unresolved ``{package_name}`` → HARD FAIL** (no ``src`` fallback): a leftover
      ``{package_name}`` (gate did not resolve a name) means the package_root is
      UNKNOWN; a ``src`` fallback would point the oracle at the wrong tree (the
      biggest drift risk Cut A.3 flags) → RED, never a silent wrong package_root.
    """
    layout = ctx.language_profile.layout
    source_sets = tuple(getattr(layout, "source_sets", ()) or ())
    test_sets = tuple(getattr(layout, "test_sets", ()) or ())

    # EXACTLY one source-set is required (anti-false-green, both directions):
    #   * >1  → reading only ``source_sets[0]`` would SILENTLY drop the others
    #     (unproven code = false-green); a multi-source-set Python layout is
    #     UNSUPPORTED today (RED, never a silent first-root collapse).
    #   * ==0 → there is NO source root to derive; a silent ``src`` fallback would point
    #     the oracle at a GUESSED tree (a wrong/empty source root = false-green). The
    #     Cut A.3 posture is "if reached, hard-fail" — never a silent default.
    if len(source_sets) != 1:
        raise OracleScopeError(
            "python implement-time oracle cannot be certified: the resolved layout "
            f"declares {len(source_sets)} source_sets "
            f"({[s.id for s in source_sets]}), but the Python composite oracle requires "
            "EXACTLY ONE source-set (its first-party import index + compile + collect "
            "scope are rooted at ONE source tree). >1 would SILENTLY drop the others "
            "(unproven code = false-green); 0 would force a GUESSED 'src' fallback "
            "(a wrong source root = false-green). Either is a HARD FAIL — never a silent "
            "first-root collapse, never a silent default."
        )
    # EXACTLY one REQUIRED (non-optional) test-set: the collect scope is single-root.
    # ``optional``/``colocated`` sets are not REQUIRED roots, so they do not count.
    #   * >1  → a multi-required-test-set layout is UNSUPPORTED (RED).
    #   * ==0 → no required test root to collect; a silent ``tests`` fallback would point
    #     the collect layer at a GUESSED tree (false-green). HARD FAIL.
    required_test_sets = tuple(s for s in test_sets if not getattr(s, "optional", False))
    if len(required_test_sets) != 1:
        raise OracleScopeError(
            "python implement-time oracle cannot be certified: the resolved layout "
            f"declares {len(required_test_sets)} required test_sets "
            f"({[s.id for s in required_test_sets]}), but the Python composite oracle "
            "collects a SINGLE test root and requires EXACTLY ONE required test-set. >1 "
            "is UNSUPPORTED; 0 would force a GUESSED 'tests' fallback (a wrong test root "
            "= false-green). Either is a HARD FAIL — never a silent first-root collapse, "
            "never a silent default."
        )

    pkg_spec = getattr(layout, "package_root", None)
    pkg_kind = getattr(pkg_spec, "kind", "none")
    pkg_path = getattr(pkg_spec, "path", None) or ""
    package_name = ctx.package_name

    def _subst(template: str) -> str:
        """Substitute ``{package_name}``; a leftover placeholder is a HARD FAIL."""
        rendered = template
        if "{package_name}" in template:
            if not package_name:
                raise OracleScopeError(
                    "python implement-time oracle cannot be certified: the layout "
                    f"template {template!r} carries an UNRESOLVED '{{package_name}}' "
                    "(the gate did not resolve a canonical package name), so the "
                    "package_root is UNKNOWN. A 'src' fallback would point the oracle "
                    "at the WRONG package tree (silent wrong layout = false-green) — "
                    "this is a HARD FAIL, not a fallback. Ensure the project's package "
                    "name resolves (project.package_name / a single top-level package "
                    "on disk / the project name)."
                )
            rendered = template.replace("{package_name}", package_name)
        if "{" in rendered and "}" in rendered:
            # Any OTHER leftover placeholder ({module_path}, …) is equally undecidable.
            raise OracleScopeError(
                "python implement-time oracle cannot be certified: the layout template "
                f"{template!r} has an unresolved placeholder after substitution "
                f"({rendered!r}); the scope is undecidable (HARD FAIL, never a guess)."
            )
        return _py_norm(rendered)

    if pkg_kind == "named_package":
        if not pkg_path:
            raise OracleScopeError(
                "python implement-time oracle cannot be certified: package_root.kind="
                "named_package but no path (the layout is incomplete; HARD FAIL)."
            )
        package_root = _subst(pkg_path)
        source_root = _py_parent_dir(package_root)
        # Internal consistency (anti-false-green): for a named_package layout the single
        # source-set root IS the package dir. A LayoutSpec that says the package root is
        # ``lib/{package_name}`` but the source-set is ``src/{package_name}`` is
        # self-contradictory — the oracle would not know which tree to observe. HARD FAIL
        # rather than silently trust one over the other (a wrong root = false-green).
        src_set_root = _subst(source_sets[0].root)
        if src_set_root != package_root:
            raise OracleScopeError(
                "python implement-time oracle cannot be certified: the resolved layout is "
                f"internally contradictory — source_sets[0].root resolves to "
                f"{src_set_root!r} but package_root resolves to {package_root!r} (for a "
                "named_package layout the single source-set IS the package dir; they must "
                "be identical). The oracle cannot decide which tree to observe — HARD FAIL "
                "(never a silent pick, which would risk proving the wrong tree)."
            )
    elif pkg_kind == "path_root":
        if not pkg_path:
            raise OracleScopeError(
                "python implement-time oracle cannot be certified: package_root.kind="
                "path_root but no path (the layout is incomplete; HARD FAIL — never a "
                "silent 'src' fallback, which would point the oracle at a guessed tree)."
            )
        package_root = _subst(pkg_path)
        source_root = package_root
    else:
        # ``none`` (no single package root) or unknown: derive the source root from the
        # single (guaranteed-present) source-set; package_root == source_root.
        source_root = _subst(source_sets[0].root)
        package_root = source_root

    # No silent ``src``/``tests`` fallback: the exactly-one source/test-set checks above
    # guarantee a set is present, and an empty DERIVED root after substitution is a real
    # layout defect (a guessed default would be a false-green). HARD FAIL instead.
    if not source_root:
        raise OracleScopeError(
            "python implement-time oracle cannot be certified: the resolved source root "
            "is empty after substitution (the layout is incomplete; HARD FAIL — never a "
            "silent 'src' default that would point the oracle at a guessed tree)."
        )
    test_root = _subst(required_test_sets[0].root)
    if not test_root:
        raise OracleScopeError(
            "python implement-time oracle cannot be certified: the resolved test root is "
            "empty after substitution (the layout is incomplete; HARD FAIL — never a "
            "silent 'tests' default that would point the collect layer at a guessed tree)."
        )
    return source_root, package_root, test_root


def certify_python_oracle_scope(
    project_root: Path,
    profile: Any,
    spec: Any = None,
) -> str:
    """Certify the Python oracle's CONCRETE file-list covers the required root(s).

    A required root (source always; test always for the Python composite) with
    ZERO ``.py`` is a HARD FAIL (:class:`OracleScopeError`) — a green oracle over an
    empty scope proves nothing (the #1 anti-false-green failure mode). Returns a
    human-readable detail on success. (The executor re-enumerates + asserts each
    tool observed the whole list; this is the front gate.)

    Legacy gate-level signature ``(project_root, LayoutProfile, ImplementOracleSpec)``
    is preserved so the existing public shim / tests keep working: ``profile`` carries
    ``source_root`` / ``test_root``; ``spec.scope`` (when present) supplies the
    require flags, else the Python composite defaults (both required) apply.
    """
    source_root = _py_norm(getattr(profile, "source_root", "") or "src") or "src"
    test_root = _py_norm(getattr(profile, "test_root", "") or "tests") or "tests"
    scope = _python_oracle_scope(project_root, source_root, test_root)
    require_source = _REQUIRE_SOURCE_ROOT
    require_test = _REQUIRE_TEST_ROOT
    scope_spec = getattr(spec, "scope", None)
    if scope_spec is not None:
        require_source = bool(getattr(scope_spec, "require_source_root", require_source))
        require_test = bool(getattr(scope_spec, "require_test_root", require_test))
    missing_roots: list[str] = []
    if require_source and not scope.source_files:
        missing_roots.append(source_root)
    if require_test and not scope.test_files:
        missing_roots.append(test_root)
    if missing_roots:
        raise OracleScopeError(
            "python implement-time oracle cannot be certified: no .py files observed "
            f"under required root(s) {missing_roots}. A green oracle over an empty "
            "scope proves nothing — the whole point of the implement-time oracle is "
            "to check the generated source AND tests, so an empty required root is a "
            "HARD FAIL (anti-false-green). Ensure the layout was scaffolded and the "
            "units were generated."
        )
    return (
        "python oracle scope certified: "
        f"{len(scope.source_files)} source .py + {len(scope.test_files)} test .py "
        f"observed under source_root='{source_root}' + "
        f"test_root='{test_root}'"
    )


# ═════════════════════════════════════════════════════════════════════════════
# The adapter (the contract-path entry: certify_scope + execute + normalize).
# ═════════════════════════════════════════════════════════════════════════════


class PythonCompositeOracleAdapter:
    """``implement_oracle`` adapter for Python's composite (``adapter: python-composite``).

    A ``kind="adapter"`` adapter (Contract Kernel §3): the dispatch calls
    :meth:`certify_scope` (before the run) and :meth:`execute` (the whole in-process
    composite) — NOT the generic command-sequence executor (Python's compile +
    first-party import resolver + ``pytest --collect-only`` is not a shell command
    sequence; it inspects the file lists each layer observed). It also implements
    :meth:`normalize_command_result` for protocol parity (kind=adapter runs no shell
    steps, but the protocol declares it; the gate's public
    ``normalize_python_tool_output`` shim re-exports the same parser).
    """

    def certify_scope(self, ctx: OracleContext) -> str:
        """Certify the Python oracle's concrete file-list covers the required root(s).

        Anti-false-green: a required source/test root with ZERO ``.py`` is a HARD
        FAIL (:class:`OracleScopeError`, never a silent pass). Derives source_root /
        test_root from the resolved ``LayoutSpec`` (``ctx.language_profile.layout`` +
        ``ctx.package_name``) — Cut A.3: NO legacy ``LayoutProfile.source_root`` read.
        """
        source_root, _package_root, test_root = _py_layout_from_layout_spec(ctx)
        scope = _python_oracle_scope(ctx.project_root, source_root, test_root)
        missing_roots: list[str] = []
        if _REQUIRE_SOURCE_ROOT and not scope.source_files:
            missing_roots.append(source_root)
        if _REQUIRE_TEST_ROOT and not scope.test_files:
            missing_roots.append(test_root)
        if missing_roots:
            raise OracleScopeError(
                "python implement-time oracle cannot be certified: no .py files observed "
                f"under required root(s) {missing_roots}. A green oracle over an empty "
                "scope proves nothing — the whole point of the implement-time oracle is "
                "to check the generated source AND tests, so an empty required root is a "
                "HARD FAIL (anti-false-green). Ensure the layout was scaffolded and the "
                "units were generated."
            )
        return (
            "python oracle scope certified: "
            f"{len(scope.source_files)} source .py + {len(scope.test_files)} test .py "
            f"observed under source_root='{source_root}' + "
            f"test_root='{test_root}'"
        )

    def execute(self, ctx: OracleContext) -> ImplementOracleResult:
        """Run compile + first-party imports + pytest collect (+ optional lint).

        The union of findings gates green: ANY finding ⇒ failed. The observability
        gate is folded in (a tool that didn't observe all files / didn't run is an
        honest environment failure, never a silent pass). ``passed = not findings``
        (and not any spawn/env failure — those surface AS findings). ``executed=True``.
        ``diagnostics=[]`` (no scoped Python rerun — the bounded loop falls to broad
        rerun, which is safe). Preserves EVERY tolerance precisely (third-party
        import tolerate, third-party-only collection tolerate, pytest-missing →
        environment_build_error). Derives its roots from the resolved ``LayoutSpec``
        (``ctx.language_profile.layout`` + ``ctx.package_name``) — Cut A.3: NO legacy
        ``LayoutProfile.source_root`` read.
        """
        source_root, package_root, test_root = _py_layout_from_layout_spec(ctx)
        project_root = ctx.project_root
        config = ctx.config
        scope = _python_oracle_scope(project_root, source_root, test_root)
        all_files = scope.expected_files
        tools: list[PythonToolRun] = [
            _run_python_compile_layer(project_root, all_files),
            _run_python_first_party_import_layer(
                project_root, source_root, package_root, test_root, scope, all_files
            ),
            _run_python_pytest_collect_layer(
                project_root, source_root, package_root, test_root, scope, config
            ),
        ]
        lint_mode = _python_lint_mode(config)
        if lint_mode != "off":
            tools.append(
                _run_python_lint_layer(
                    project_root, source_root, all_files, required=(lint_mode == "required")
                )
            )

        findings: list[ImplementOracleFinding] = []
        failed_paths: list[str] = []
        raw_parts: list[str] = []
        for tool in tools:
            findings.extend(tool.findings)
            body = tool.output or tool.skipped_reason or ("(no findings)" if tool.executed else "(not executed)")
            raw_parts.append(f"## {tool.name} (executed={tool.executed})\n{body}")
            for f in tool.findings:
                if f.path and f.path not in failed_paths:
                    failed_paths.append(f.path)
        findings.extend(_certify_python_tool_observability(scope, tools))

        passed = not findings
        return ImplementOracleResult(
            passed=passed,
            executed=True,
            command="python-composite: compile + first-party-imports + pytest --collect-only",
            findings=findings,
            failed_paths=failed_paths,
            detail=(
                f"python composite oracle {'passed' if passed else 'failed'}; "
                f"{len(scope.source_files)} source file(s), {len(scope.test_files)} test "
                f"file(s), {len(findings)} finding(s)"
            ),
            raw_output="\n\n".join(raw_parts),
            diagnostics=[],
        )

    def normalize_command_result(
        self,
        ctx: OracleContext,
        *,
        command_id: str,  # noqa: ARG002 — signature parity with the protocol.
        command: CommandSpec,  # noqa: ARG002
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> OracleStepObservation:
        """Protocol parity: a ``kind="adapter"`` oracle runs no shell steps.

        Python's oracle is wholly in-process (:meth:`execute`); the generic
        command-sequence executor never calls this. It is implemented (rather than
        absent) only for protocol completeness — it parses the ``pytest
        --collect-only`` output the same way :func:`normalize_python_tool_output`
        does and returns a CONSERVATIVE observation: ``is_clean`` only when the exit
        was zero AND no finding was parsed (a non-zero exit, or any parsed
        diagnostic, is never clean — anti-false-green). The public
        ``normalize_python_tool_output`` shim re-exports the parser for external use.
        """
        output = "\n".join(part for part in (stdout, stderr) if part)
        findings, failed_paths = normalize_python_tool_output(
            output, command="pytest --collect-only", project_root=ctx.project_root
        )
        is_clean = returncode == 0 and not findings
        return OracleStepObservation(
            is_clean=is_clean,
            findings=tuple(findings),
            failed_paths=tuple(failed_paths),
            detail=("" if is_clean else f"pytest collect exited {returncode}"),
        )


__all__ = [
    "PythonCompositeOracleAdapter",
    "PythonOracleScope",
    "PythonToolRun",
    "certify_python_oracle_scope",
    "normalize_python_tool_output",
    "_collection_failure_is_third_party_only",
    "_run_python_pytest_collect_layer",
]
