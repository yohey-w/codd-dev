"""AST test-helper SYMBOL-import coherence gate — a sibling to import_coherence.

A-core anti-false-green gate. ``codd/import_coherence.py`` proves that a test and
the SOURCE package agree on import context (no bare-basename source imports). This
module proves a DIFFERENT, complementary invariant entirely WITHIN the test tree:

  every name a generated test imports from another in-test-tree module
  (a sibling test module, a test-helper package / subpackage ``__init__``, or
  ``conftest``) must actually be DEFINED or RE-EXPORTED by that target.

The motivating finding (2026-06 greenfield AUTOPILOT dogfood): a generated Python
test suite mixed three incompatible import dialects and imported 8+ helper symbols
(``combined_output``, ``line_for_task_id``, ``load_todo_json``, ``write_todo_json``,
``assert_usage_or_error_output``, ``find_task_line``, ``output_text``,
``task_lines``, ``todo_file``) that NO helper module defined. ``import_coherence``
does not catch this — it only checks the test→source-PACKAGE import dialect and
explicitly excludes ``conftest`` from its source set, and it has no notion of
cross-test-helper imports or of whether an imported symbol actually exists. So the
incoherence sails past the coherence gate and dies inside pytest as an opaque
COLLECTION error (exit 2), not as an honest, actionable diagnosis.

This gate runs BEFORE pytest (at the same hook as ``import_coherence``) and FAILS
HONESTLY with a precise diagnosis — which test file, which symbol, which in-test
target, and that the target does not define/re-export it — so the failure feeds
the EXISTING regenerate path (DIAGNOSE → REGENERATE; the harness will not rewrite
generated files on --resume). It is STATIC (``ast`` only — nothing is imported or
executed) and NEVER auto-creates a stub helper: fabricating the helper API would
itself be a false-green vector. It only diagnoses; regeneration fixes it.

Anti-false-RED (load-bearing): a name is flagged ONLY when the target is fully
resolvable AND the symbol is PROVABLY absent (no top-level ``def``/``class``/
assignment, not in ``__all__``, not reachable through a statically-resolvable
re-export / ``from Y import *`` chain). Whenever resolution is uncertain — a
dynamic ``__all__``, an unresolved ``import *`` source (third-party, a namespace
package, or a star-source the gate cannot read), or a target it cannot map to a
file — the gate treats the target as "provides unknown" and does NOT flag. The
gate is also SCOPED to the test tree: it never re-flags the source-package imports
that ``import_coherence`` already governs.

Stack-neutral by construction: the public entry resolves a
:class:`~codd.project_types.LayoutProfile` exactly like ``import_coherence`` and
no-ops for stacks without one. The symbol resolver is Python-AST today (that is
where the finding and the existing coherence machinery live); a non-Python
resolver can be added later behind the same entry without touching callers.

Opt-out: shares ``import_coherence``'s explicit opt-out
(``coherence.import_coherence: false``) — these are two halves of one
anti-false-green coherence concern, never weakened silently or by default.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from codd.import_coherence import import_coherence_opt_out
from codd.project_types import LayoutProfile, resolve_layout_profile


__all__ = [
    "TestImportCoherenceFinding",
    "TestImportCoherenceResult",
    "check_test_import_coherence",
]


# A target module whose provided-name set cannot be statically determined (a
# dynamic ``__all__``, an unresolved ``import *`` chain, or a file the gate cannot
# read/parse). Such a target is "provides unknown" → never flagged (anti-false-RED).
_UNKNOWN = object()


@dataclass(frozen=True)
class TestImportCoherenceFinding:
    """One test-helper symbol-import violation, with a precise message."""

    kind: str
    path: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class TestImportCoherenceResult:
    """Outcome of the test-helper symbol-import coherence gate."""

    passed: bool
    findings: list[TestImportCoherenceFinding] = field(default_factory=list)
    profile: LayoutProfile | None = None
    detail: str = ""

    def summary(self) -> str:
        if self.passed:
            return self.detail or "test-import coherence: OK"
        lines = [
            f"test-import-coherence gate FAILED ({len(self.findings)} finding(s)):"
        ]
        for finding in self.findings:
            lines.append(f"  - [{finding.kind}] {finding.path}: {finding.message}")
        # Same DIAGNOSE → REGENERATE stance as import_coherence: the suite is
        # internally incoherent (tests import helper symbols nothing defines).
        # The scaffold is create-only and will not rewrite model-authored files,
        # so --resume cannot fix it — REGENERATE. We deliberately do NOT
        # auto-create the missing helpers: fabricating the API would be a
        # false-green vector. (Diagnose-only; opt out via
        # coherence.import_coherence: false, which re-opens the false-green risk.)
        lines.append(
            "  → Generated tests import helper symbols that no in-test-tree module "
            "defines or re-exports. REGENERATE the project (fresh greenfield) so "
            "the helpers and their imports agree; the harness will not rewrite "
            "generated files on --resume, and stubs are never auto-created."
        )
        return "\n".join(lines)


def _norm(rel: str) -> str:
    return str(rel).strip().replace("\\", "/").strip("/")


def _iter_py_files(base: Path) -> list[Path]:
    if not base.is_dir():
        return []
    out: list[Path] = []
    for path in sorted(base.rglob("*.py")):
        if any(part == "__pycache__" for part in path.parts):
            continue
        out.append(path)
    return out


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return PurePosixPath(str(path).replace("\\", "/")).as_posix()


def _parse(path: Path) -> ast.AST | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, SyntaxError):
        return None


# ── module-key registry over the test tree ──────────────────────────────────
#
# Every importable module/package under the test root is registered under EVERY
# dotted key a coherent test could plausibly import it by, so an import target
# resolves whether the suite addresses helpers as ``from helpers import x``,
# ``from tests.helpers import x``, ``from pkg.sub import x`` or ``import conftest``.
# A package contributes its ``__init__`` under the package key; a module file
# contributes under its own key. Keys are the LAST 1..N dotted segments of the
# module's path RELATIVE TO THE TEST ROOT (so both the bare and test-root-
# prefixed dialects resolve), plus the bare leaf name.


def _module_keys(rel_to_test_root: PurePosixPath, test_root_name: str) -> set[str]:
    """Dotted import keys a test could address this in-test module/package by."""
    parts = list(rel_to_test_root.parts)
    if not parts:
        return set()
    is_init = parts[-1] == "__init__.py"
    if is_init:
        segments = [p for p in parts[:-1]]  # the package dir path
    else:
        leaf = parts[-1]
        leaf = leaf[:-3] if leaf.endswith(".py") else leaf
        segments = [*parts[:-1], leaf]
    segments = [s for s in segments if s]
    if not segments:
        # tests/__init__.py — the test root package itself.
        return {test_root_name} if test_root_name else set()
    keys: set[str] = set()
    # Suffix keys: full path, and every trailing slice (covers bare + nested forms).
    for start in range(len(segments)):
        keys.add(".".join(segments[start:]))
    # Test-root-prefixed form: ``tests.helpers`` even though ``tests`` is the root.
    if test_root_name:
        keys.add(".".join([test_root_name, *segments]))
    return keys


@dataclass
class _ModuleInfo:
    rel: str
    tree: ast.AST
    is_package_init: bool


class _TestTree:
    """Index of every module/package in the test tree, keyed by import dialect."""

    def __init__(self) -> None:
        self.by_key: dict[str, _ModuleInfo] = {}
        self._provided_cache: dict[str, Any] = {}

    def register(self, key: str, info: _ModuleInfo) -> None:
        # First registration wins for a key; ambiguity (two files under one key)
        # is itself unusual and we stay conservative by keeping the first.
        self.by_key.setdefault(key, info)

    def resolves(self, key: str) -> bool:
        return key in self.by_key

    def provided_names(self, key: str, _stack: tuple[str, ...] = ()) -> Any:
        """Names a target provides: a set[str], or ``_UNKNOWN`` if undecidable.

        Resolves, statically and conservatively:
          * top-level ``def`` / ``class`` / simple assignments / ann-assignments;
          * imported names bound at module top level (``import x``, ``from y
            import a, b``, ``... as alias``) — a re-export;
          * an explicit ``__all__`` of string literals (authoritative WHEN every
            name it lists is otherwise resolvable here; a dynamic/computed
            ``__all__`` makes the target UNKNOWN);
          * ``from Y import *`` where ``Y`` is ANOTHER in-test-tree module —
            recursively (bounded), unioning its provided names. A ``*`` whose
            source is NOT in the test tree (third-party / namespace package /
            unreadable) makes the target UNKNOWN.
        """
        if key in self._provided_cache:
            return self._provided_cache[key]
        if key in _stack:  # import cycle — bail conservatively
            return _UNKNOWN
        info = self.by_key.get(key)
        if info is None:
            return _UNKNOWN
        result = self._compute_provided(info, _stack + (key,))
        self._provided_cache[key] = result
        return result

    def _compute_provided(self, info: _ModuleInfo, stack: tuple[str, ...]) -> Any:
        names: set[str] = set()
        star_unknown = False
        dunder_all: list[str] | None = None
        dunder_all_dynamic = False

        for node in ast.iter_child_nodes(info.tree):
            if isinstance(node, ast.FunctionDef):
                names.add(node.name)
            elif isinstance(node, ast.AsyncFunctionDef):
                names.add(node.name)
            elif isinstance(node, ast.ClassDef):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    self._add_assign_target(target, names)
                if _is_dunder_all(node.targets):
                    extracted = _string_list(node.value)
                    if extracted is None:
                        dunder_all_dynamic = True
                    else:
                        dunder_all = (dunder_all or []) + extracted
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name):
                    names.add(node.target.id)
                    if node.target.id == "__all__":
                        extracted = _string_list(node.value) if node.value else None
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
                star = any(a.name == "*" for a in node.names)
                if star:
                    sub = self._resolve_star_source(info, node, stack)
                    if sub is _UNKNOWN:
                        star_unknown = True
                    else:
                        names |= sub  # type: ignore[arg-type]
                else:
                    for alias in node.names:
                        bound = alias.asname or alias.name
                        if bound:
                            names.add(bound)

        # A dynamic/computed ``__all__`` makes the public surface undecidable.
        if dunder_all_dynamic:
            return _UNKNOWN
        # ``__all__`` is authoritative for the public surface ONLY when every name
        # it lists is otherwise resolvable in this module (defs/assigns/imports);
        # an ``__all__`` that re-exports names pulled in via an UNRESOLVED ``*``
        # would otherwise let us "provide" names we cannot prove exist. If a star
        # import we could not resolve is present, fall back to UNKNOWN rather than
        # trusting a possibly-incomplete name set.
        if star_unknown:
            return _UNKNOWN
        if dunder_all is not None:
            # Union: ``__all__`` plus everything concretely defined/imported.
            # (Names in ``__all__`` not otherwise resolvable still count as
            # "provided" — the author asserted them; we are not the linter for
            # the helper itself, only for the IMPORTING test.)
            return names | set(dunder_all)
        return names

    @staticmethod
    def _add_assign_target(target: ast.AST, names: set[str]) -> None:
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                _TestTree._add_assign_target(elt, names)

    def _resolve_star_source(
        self, info: _ModuleInfo, node: ast.ImportFrom, stack: tuple[str, ...]
    ) -> Any:
        """Resolve ``from Y import *`` to Y's provided names, or ``_UNKNOWN``.

        Only an in-test-tree ``Y`` is resolvable. Relative (``from . import *`` /
        ``from .mod import *``) is mapped against this module's package; absolute
        is matched against the key registry. Anything else → UNKNOWN.
        """
        target_key = self._import_target_key(info, node)
        if target_key is None or not self.resolves(target_key):
            return _UNKNOWN
        return self.provided_names(target_key, stack)

    def _import_target_key(self, info: _ModuleInfo, node: ast.ImportFrom) -> str | None:
        """Best-effort dotted key for an ``ImportFrom`` target, within the tree."""
        if node.level and node.level > 0:
            # Relative: resolve against this module's package path.
            base_parts = _package_parts(info.rel)
            # ``level`` 1 = current package; >1 climbs.
            climb = node.level - 1
            if climb > len(base_parts):
                return None
            anchor = base_parts[: len(base_parts) - climb] if climb else base_parts
            mod_suffix = node.module.split(".") if node.module else []
            segments = [*anchor, *mod_suffix]
            segments = [s for s in segments if s]
            if not segments:
                return None
            # Try the most-specific suffix forms against the registry.
            for start in range(len(segments)):
                key = ".".join(segments[start:])
                if self.resolves(key):
                    return key
            return ".".join(segments)
        if node.module:
            return node.module
        return None


def _package_parts(rel: str) -> list[str]:
    """Dotted package path for a module rel-path RELATIVE TO THE TEST ROOT.

    ``helpers/io.py`` → ``["helpers"]``; ``helpers/__init__.py`` → ``["helpers"]``;
    ``test_x.py`` → ``[]``.
    """
    parts = list(PurePosixPath(rel).parts)
    if not parts:
        return []
    if parts[-1] == "__init__.py":
        return [p for p in parts[:-1] if p]
    return [p for p in parts[:-1] if p]


def _is_dunder_all(targets: list[ast.expr]) -> bool:
    return any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets)


def _string_list(value: ast.AST | None) -> list[str] | None:
    """Extract a list/tuple of string literals, or ``None`` if not static."""
    if not isinstance(value, (ast.List, ast.Tuple)):
        return None
    out: list[str] = []
    for elt in value.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            out.append(elt.value)
        else:
            return None  # a computed element → not statically a string list
    return out


def _build_test_tree(test_dir: Path, test_root_name: str) -> _TestTree:
    tree = _TestTree()
    for path in _iter_py_files(test_dir):
        parsed = _parse(path)
        if parsed is None:
            continue
        try:
            rel_test = path.resolve().relative_to(test_dir.resolve())
        except (ValueError, OSError):
            continue
        rel_test_pp = PurePosixPath(rel_test.as_posix())
        is_init = rel_test_pp.name == "__init__.py"
        info = _ModuleInfo(rel=rel_test_pp.as_posix(), tree=parsed, is_package_init=is_init)
        for key in _module_keys(rel_test_pp, test_root_name):
            tree.register(key, info)
    return tree


def _is_test_file(rel_test_path: str) -> bool:
    """A pytest-collected TEST module (vs a helper/conftest)."""
    name = PurePosixPath(rel_test_path).name
    return name.startswith("test_") or name.endswith("_test.py")


def _imports_from_intree(tree: ast.AST) -> list[tuple[str | None, int, list[str], int]]:
    """Collect ``from X import a, b`` sites: (module, level, names, lineno).

    ``import *`` and alias-only star forms are skipped here (the IMPORTING test
    receiving ``*`` cannot be symbol-checked — it pulls whatever the source has).
    """
    sites: list[tuple[str | None, int, list[str], int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = [a.name for a in node.names if a.name != "*"]
            if not names:
                continue
            sites.append((node.module, node.level or 0, names, node.lineno))
    return sites


def check_test_import_coherence(
    project_root: Path | str,
    *,
    language: str | None,
    project_name: str | None = None,
    source_dirs: Any = None,
    test_dirs: Any = None,
    config: Mapping[str, Any] | None = None,
    profile: LayoutProfile | None = None,
) -> TestImportCoherenceResult:
    """Run the test-helper SYMBOL-import coherence gate for a profiled stack.

    For every generated TEST file, every ``from <X> import a, b, c`` whose ``<X>``
    resolves WITHIN the test tree (a sibling test module / helper package /
    subpackage / ``conftest``) is checked: each imported name must be DEFINED or
    RE-EXPORTED by ``<X>``. A provably-missing name is a coherence violation
    flagged with a precise message; everything uncertain is left UNFLAGGED
    (anti-false-RED). Stacks without a layout profile (or with the gate opted out)
    return a passing no-op — the verify honesty gate remains the backstop.
    """
    root = Path(project_root)
    if import_coherence_opt_out(config):
        return TestImportCoherenceResult(
            passed=True,
            detail="test-import coherence: disabled (coherence.import_coherence: false)",
        )

    if profile is None:
        profile = resolve_layout_profile(
            language=language,
            project_name=project_name,
            source_dirs=source_dirs,
            test_dirs=test_dirs,
        )
    if profile is None:
        return TestImportCoherenceResult(
            passed=True,
            detail=f"test-import coherence: no layout profile for language {language!r} (skipped)",
        )

    test_dir = root / profile.test_root
    if not test_dir.is_dir():
        return TestImportCoherenceResult(
            passed=True, detail=f"test-import coherence: no test root '{profile.test_root}' (skipped)"
        )

    test_root_name = _norm(profile.test_root).split("/")[-1] if profile.test_root else ""
    index = _build_test_tree(test_dir, test_root_name)

    findings: list[TestImportCoherenceFinding] = []
    checked_imports = 0
    for path in _iter_py_files(test_dir):
        try:
            rel_test = path.resolve().relative_to(test_dir.resolve()).as_posix()
        except (ValueError, OSError):
            continue
        if not _is_test_file(rel_test):
            continue  # only pytest-collected TEST modules import helpers
        tree = _parse(path)
        if tree is None:
            continue
        rel_proj = _rel(path, root)
        info = _ModuleInfo(rel=rel_test, tree=tree, is_package_init=False)
        for module, level, names, lineno in _imports_from_intree(tree):
            target_key = index._import_target_key(info, _FakeImportFrom(module, level))
            if target_key is None or not index.resolves(target_key):
                continue  # not an in-test-tree target → out of this gate's scope
            provided = index.provided_names(target_key)
            checked_imports += 1
            if provided is _UNKNOWN:
                continue  # undecidable target → conservatively do not flag
            missing = [n for n in names if n not in provided]
            for symbol in missing:
                findings.append(
                    TestImportCoherenceFinding(
                        kind="missing_test_helper_symbol",
                        path=rel_proj,
                        message=(
                            f"imports '{symbol}' from in-test-tree module "
                            f"'{module or ('.' * level)}' (line {lineno}), but that "
                            f"module does not define or re-export '{symbol}'. The "
                            f"shared test helper must define/re-export every symbol "
                            f"the tests import; a missing helper symbol crashes "
                            f"pytest at collection."
                        ),
                        details={
                            "symbol": symbol,
                            "target": module or ("." * level),
                            "target_key": target_key,
                            "lineno": lineno,
                        },
                    )
                )

    passed = not findings
    detail = (
        f"test-import coherence: OK ({checked_imports} in-test import site(s) checked, "
        f"test_root={profile.test_root})"
        if passed
        else f"test-import coherence: {len(findings)} finding(s)"
    )
    return TestImportCoherenceResult(
        passed=passed, findings=findings, profile=profile, detail=detail
    )


@dataclass
class _FakeImportFrom:
    """Minimal duck-typed stand-in so ``_import_target_key`` can be reused.

    ``_import_target_key`` only reads ``.level`` and ``.module``; constructing a
    real :class:`ast.ImportFrom` for the lookup would be needless ceremony.
    """

    module: str | None
    level: int
