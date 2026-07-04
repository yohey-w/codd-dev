"""AST import-coherence gate — source + tests must share ONE package context.

A-core anti-false-green gate. A cross-vendor ``codd greenfield`` run can produce
source and tests that DISAGREE on package/import context:

* source uses package-relative imports (``from .todo_store import X``) — it
  expects to live INSIDE a named package;
* tests flat-import the same module by BARE BASENAME (``import todo_store`` /
  ``importlib.import_module("todo_store")``) — which only resolves when the
  source directory is on ``PYTHONPATH`` (an environment-dependent FALSE GREEN).

This gate runs BEFORE pytest and FAILS HONESTLY on that incoherence, instead of
letting it surface as a confusing pytest import crash (or, worse, a fake pass).
It is STATIC (``ast`` only — no imports executed) and PROFILE-DRIVEN: every root
and the package name come from the resolved :class:`~codd.project_types.LayoutProfile`,
so there are no hardcoded ``src``/``tests``/``<package>`` literals.

Checks (Python ``package_absolute`` profile):

1. **Bare-basename test import.** A test that imports a generated source module
   by its bare basename (``import <mod>`` / ``from <mod> import ...`` /
   ``importlib.import_module("<mod>")``) instead of the package-absolute form
   ``from <package_name>.<mod> import ...``.
2. **Source outside the package root.** A source module under ``source_root``
   but not under ``package_root`` (a flat ``src/foo.py`` instead of
   ``src/<package_name>/foo.py``) — the layout the harness owns is violated.
3. **Missing ``__init__``.** ``package_root`` (or an intermediate dir) lacks the
   ``__init__.py`` the profile requires to be an importable package.
4. **Duplicate / shadowing module names** across roots (e.g. a module name that
   exists both under the package and at a flat location), which makes resolution
   order-dependent.
5. **Manifest disagreement.** ``pyproject.toml`` declares a package /
   setuptools ``where`` / ``[project] name`` that contradicts the profile's
   ``package_name`` / ``source_root``.

The gate is opt-out via ``coherence.import_coherence: false`` (consistent with
other gates); it is NEVER weakened by default. Like the verify honesty rule, an
opt-out is an explicit author decision, not the default.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from codd.path_safety import PathEscapeError, require_project_path, resolve_project_path
from codd.project_types import LayoutProfile, resolve_layout_profile


__all__ = [
    "ImportCoherenceFinding",
    "ImportCoherenceResult",
    "check_import_coherence",
    "import_coherence_opt_out",
    "render_import_coherence_contract",
]


def render_import_coherence_contract(profile: LayoutProfile | None) -> str:
    """Project the import-coherence gate's package-layout + import-style rules onto
    a generation/implement prompt, DATA-DRIVEN from the resolved
    :class:`~codd.project_types.LayoutProfile` — the SAME truth source
    :func:`check_import_coherence` reads.

    The generation prompt otherwise never conveys the harness-owned topology, so
    the model freelances the package name + import style and the gate correctly
    rejects it (the recurring meta-pattern: a deterministic gate enforces a
    contract the prompt never stated). Rendering the profile the gate reads makes
    the prompt-side contract unable to drift from the gate-side one — the same
    same-truth-source principle behind ``resolve_test_framework_guidance`` /
    :func:`~codd.verifiable_behavior_audit.render_vb_contract`.

    Language-free: the package root, package name, source root, and the
    package-absolute import idiom are all read from ``profile`` — there is NO
    language-name branch and NO hardcoded package name. The two rules are gated
    INDEPENDENTLY on the exact profile fields the corresponding gate checks read:

    * the source-under-package rule iff ``requires_package_init`` (the guard on
      :func:`_check_source_outside_package`);
    * the package-absolute-import rule iff
      ``test_import_policy == "package_absolute"`` (the guard on
      :func:`_check_bare_basename_imports`).

    Returns ``""`` when ``profile`` is ``None`` or when neither rule applies — a
    path-relative stack (TypeScript: ``requires_package_init=False`` and
    ``test_import_policy="relative"``, ``package_root == source_root``) has no
    named-package contract to project and the gate's package checks are a strict
    no-op for it, so nothing is added (no non-opt-in default).
    """
    if profile is None:
        return ""

    rules: list[str] = []
    if profile.requires_package_init:
        rules.append(
            f"{len(rules) + 1}. PACKAGE ROOT — the harness OWNS a src-layout package "
            f"rooted at `{profile.package_root}` (the scaffold has already created it "
            f"with its package-init file). Put EVERY source module you generate UNDER "
            f"`{profile.package_root}/` (e.g. `{profile.package_root}/<module>`). Do NOT "
            f"place a source module directly under `{profile.source_root}/`, and do NOT "
            f"invent a differently-named sibling package: a source module under "
            f"`{profile.source_root}` but outside `{profile.package_root}` is rejected as "
            f"`source_outside_package`. The package name is FIXED at "
            f"`{profile.package_name}`; do not rename, shorten, or re-spell it."
        )
    if profile.test_import_policy == "package_absolute":
        rules.append(
            f"{len(rules) + 1}. PACKAGE-ABSOLUTE IMPORTS — import every source module by "
            f"its package-absolute path, `from {profile.package_name}.<module> import "
            f"...` (or `import {profile.package_name}.<module>`), NEVER by bare basename "
            f"(`import <module>` / `from <module> import ...`). A bare basename only "
            f"resolves when the source directory happens to be on the module search path "
            f"(an environment-dependent false-green); the gate rejects it as "
            f"`bare_basename_import`. This applies especially to tests importing the code "
            f"under test."
        )
    if not rules:
        return ""

    header = (
        "Package-layout & import-coherence CONTRACT (release-blocking — the "
        "deterministic import-coherence gate enforces EXACTLY these rules at verify; "
        "a violation fails the build, and the harness will NOT rewrite your files to "
        "fix it — an incoherent build is regenerated from scratch, so get the layout "
        "right the first time):"
    )
    return "\n".join([header, "", *rules])


@dataclass(frozen=True)
class ImportCoherenceFinding:
    """One coherence violation, with a clear, actionable message."""

    kind: str
    path: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ImportCoherenceResult:
    """Outcome of the import-coherence gate."""

    passed: bool
    findings: list[ImportCoherenceFinding] = field(default_factory=list)
    profile: LayoutProfile | None = None
    detail: str = ""

    def summary(self) -> str:
        if self.passed:
            return self.detail or "import coherence: OK"
        lines = [f"import-coherence gate FAILED ({len(self.findings)} finding(s)):"]
        for finding in self.findings:
            lines.append(f"  - [{finding.kind}] {finding.path}: {finding.message}")
        # The harness owns the layout; an already-generated build that violates
        # it predates the profile. The fix is to REGENERATE (a fresh greenfield),
        # not to --resume — the scaffold is create-only and will not rewrite
        # model-authored files. (Diagnose-only; set coherence.import_coherence:
        # false to opt out, but that re-opens the false-green risk.)
        lines.append(
            "  → This source/test set is incoherent under the layout profile "
            "(harness-owned topology). REGENERATE the project (fresh greenfield) "
            "rather than --resume; the harness will not rewrite generated files."
        )
        return "\n".join(lines)


def import_coherence_opt_out(config: Mapping[str, Any] | None) -> bool:
    """``coherence.import_coherence: false`` — explicit opt-out (default: on)."""
    if not isinstance(config, Mapping):
        return False
    coherence = config.get("coherence")
    if not isinstance(coherence, Mapping):
        return False
    value = coherence.get("import_coherence", True)
    return value is False


def _norm(rel: str) -> str:
    return str(rel).strip().replace("\\", "/").strip("/")


def _under(rel_path: str, root: str) -> bool:
    norm = _norm(rel_path)
    root = _norm(root)
    if not norm or not root:
        return False
    return norm == root or norm.startswith(root + "/")


def _iter_py_files(
    base: Path,
    project_root: Path,
    exclude_names: tuple[str, ...] = ("__pycache__",),
) -> list[Path]:
    """Python files under ``base``, confined to ``project_root`` (path-escape jail).

    ``base`` is ``project_root`` joined with a PROFILE root (``source_root`` /
    ``test_root`` / ``package_root``) that derives from user-controllable
    ``scan.source_dirs`` / ``scan.test_dirs``. The profile layer drops ``../`` /
    absolute-out-of-root entries, so the surviving escape vector for the ROOT is
    an IN-ROOT root that is a SYMLINK whose target escapes the project. That is
    an INVALID evidence root — FAIL-CLOSED (``require_project_path`` raises
    :class:`PathEscapeError`) rather than silently returning ``[]``, because a
    silent empty walk lets the coherence gate "pass" while a smuggled off-root
    tree goes unchecked (a false-green in another form: GPT). The caller catches
    the error and turns it into an honest RED. A non-existent in-root root is NOT
    an escape (benign empty list). Every ``rglob`` match is still re-confined and
    an escaping symlink FILE inside a valid in-root tree is DROPPED (skip) — that
    finer case stays anti-false-red.
    """
    require_project_path(project_root, base, context="layout root")
    if not base.is_dir():
        return []
    out: list[Path] = []
    for path in sorted(base.rglob("*.py")):
        if any(part in exclude_names for part in path.parts):
            continue
        if resolve_project_path(project_root, path) is None:
            continue  # in-root tree may contain a symlink escaping the root
        out.append(path)
    return out


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return PurePosixPath(str(path).replace("\\", "/")).as_posix()


def _module_basename(rel_path: str) -> str:
    """``src/pkg/todo_store.py`` → ``todo_store`` (the importable leaf name)."""
    name = PurePosixPath(rel_path).name
    return name[:-3] if name.endswith(".py") else name


def _source_module_basenames(
    project_root: Path,
    profile: LayoutProfile,
) -> set[str]:
    """Bare basenames of generated source modules (package + flat under src).

    These are exactly the names a coherent test must import package-absolutely
    (``from <package_name>.<mod> import ...``), never by bare basename. The set
    includes modules under ``package_root`` AND any flat module under
    ``source_root`` (the latter is itself a layout violation, but a test that
    flat-imports it is independently a false-green risk and must be flagged even
    before the source is moved into the package).
    """
    source_dir = project_root / profile.source_root
    names: set[str] = set()
    for path in _iter_py_files(source_dir, project_root):
        stem = path.stem
        if stem in {"__init__", "__main__", "setup", "conftest"}:
            continue
        names.add(stem)
    return names


# AMBIENT (runtime/stdlib-provided) module-name sets, keyed by an opt-in profile
# sentinel. A source module whose bare name collides with an ambient module — a
# first-party ``ast.py`` shadowing Python's stdlib ``ast`` — makes a bare ``import
# ast`` in a test UNCONFIRMABLE as a first-party reference: the gate cannot tell it
# apart from a legitimate use of the ambient module (and in every normal env it
# resolves to the ambient one, so any author error is environment-INDEPENDENT and
# outside this gate's "environment-dependent false-green" charter). Anti-false-red:
# it must not be flagged. The sentinel→set dispatch keeps the core language-free —
# no module-name list is hardcoded; each set is derived at runtime (Python: from the
# running interpreter). A stack that declares no sentinel gets the empty set.
_AMBIENT_MODULE_SETS: dict[str, Callable[[], set[str]]] = {
    "python-stdlib": lambda: set(sys.stdlib_module_names) | set(sys.builtin_module_names),
}


def _resolve_ambient_modules(profile: LayoutProfile) -> set[str]:
    """Ambient module names for the profile's stack, from its opt-in sentinel.

    Returns the empty set when the profile declares no sentinel
    (``ambient_modules`` is ``None``) — the bare-import check is then unchanged, so
    this is a strict no-op for every stack that does not opt in.
    """
    sentinel = getattr(profile, "ambient_modules", None)
    if not sentinel:
        return set()
    resolver = _AMBIENT_MODULE_SETS.get(sentinel)
    return resolver() if resolver else set()


def _is_module_token(value: str) -> bool:
    """A string that LOOKS like a module path: dotted segments, each an identifier."""
    if not value or value != value.strip():
        return False
    parts = value.split(".")
    return all(part.isidentifier() for part in parts)


def _single_segment_module_token(value: str) -> str | None:
    """A bare (single-segment) module-name string literal, or ``None``.

    A dotted token (``"pkg.mod"``) reaches its leaf package-absolutely and is
    never a bare reference, so it is rejected here.
    """
    if "." not in value and _is_module_token(value):
        return value
    return None


def _resolve_name_bound_module_strings(tree: ast.AST, names: set[str]) -> set[str]:
    """Single-segment module strings bound to ``names`` by a literal iterable/value.

    Resolves the ``for m in ("a", "b"): import_module(m)`` (plus comprehension and
    simple-assignment) pattern: when a dynamic-import argument is a bare Name, the
    module strings it can take are the literal elements iterated or assigned onto
    it. This is the CONFIRMED-flow counterpart to collecting a literal call arg —
    an unrelated data string (a dict key, an assertion table) is never bound to an
    import argument, so it is never collected.
    """
    refs: set[str] = set()

    def _collect_literal(node: ast.expr | None) -> None:
        elements: list[ast.expr] = []
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            elements = list(node.elts)
        elif node is not None:
            elements = [node]
        for elt in elements:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                token = _single_segment_module_token(elt.value)
                if token:
                    refs.add(token)

    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.comprehension)) and isinstance(
            node.target, ast.Name
        ):
            if node.target.id in names:
                _collect_literal(node.iter)
        elif isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id in names for t in node.targets):
                _collect_literal(node.value)
    return refs


def _bare_module_refs_from_dynamic_imports(tree: ast.AST) -> set[str]:
    """Single-segment module names passed to ``import_module`` / ``__import__``.

    Collects a string ONLY from a CONFIRMED dynamic-import flow: the literal first
    argument (or ``name=`` kwarg) of an ``importlib.import_module(...)`` /
    ``__import__(...)`` call, or — when that argument is a bare Name — the literal
    values bound to that name in the same module (the tuple-iterated pattern). A
    module-name string that merely appears elsewhere in the file (a dict key, an
    assertion table) is NOT an import and is never collected: that coincidence was
    the source of a false-RED in structural/graph tests that hold module names as
    data while using ``import_module`` for a coherent package-absolute purpose
    (whose ``f"{pkg}.{mod}"`` argument is an f-string, contributing no bare token).
    """
    literal_refs: set[str] = set()
    name_targets: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            (isinstance(func, ast.Attribute) and func.attr == "import_module")
            or (isinstance(func, ast.Name) and func.id == "__import__")
        ):
            continue
        arg: ast.expr | None = node.args[0] if node.args else None
        if arg is None:
            for kw in node.keywords:
                if kw.arg == "name":
                    arg = kw.value
                    break
        if arg is None:
            continue
        # Every single-segment module-name constant reachable in the argument
        # subtree (handles a bare literal, a ternary, a subscript on a literal).
        for sub in ast.walk(arg):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                token = _single_segment_module_token(sub.value)
                if token:
                    literal_refs.add(token)
        # A bare Name argument — resolve its literal bindings in this module.
        if isinstance(arg, ast.Name):
            name_targets.add(arg.id)
    if name_targets:
        literal_refs |= _resolve_name_bound_module_strings(tree, name_targets)
    return literal_refs


def _collect_test_imports(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Return (top_level_module_names, dynamic_import_module_refs) referenced by a test.

    * ``import todo_store`` / ``import a.b`` → top-level ``todo_store`` / ``a``.
    * ``from todo_store import X`` (absolute, level 0) → ``todo_store``.
    * A module name passed to ``importlib.import_module`` / ``__import__`` — as a
      literal argument, or via a Name bound to literal values — → the bare reference.
    Package-relative ``from . import`` / ``from .mod import`` (level >= 1) are NOT
    flagged — coherent by construction. A module-name string that is NOT a dynamic
    import argument (test data) is NOT collected — that would false-RED.
    """
    top_level: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                head = alias.name.split(".")[0]
                if head:
                    top_level.add(head)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue  # relative import — coherent by construction
            if node.module:
                top_level.add(node.module.split(".")[0])
    return top_level, _bare_module_refs_from_dynamic_imports(tree)


def _check_bare_basename_imports(
    project_root: Path,
    profile: LayoutProfile,
    source_modules: set[str],
) -> list[ImportCoherenceFinding]:
    """Flag tests importing a source module by bare basename (policy violation)."""
    if profile.test_import_policy != "package_absolute":
        return []
    findings: list[ImportCoherenceFinding] = []
    test_dir = project_root / profile.test_root
    for path in _iter_py_files(test_dir, project_root):
        rel = _rel(path, project_root)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, SyntaxError):
            continue
        top_level, bare_string_refs = _collect_test_imports(tree)
        offenders = sorted((top_level | bare_string_refs) & source_modules)
        for module in offenders:
            findings.append(
                ImportCoherenceFinding(
                    kind="bare_basename_import",
                    path=rel,
                    message=(
                        f"imports source module '{module}' by bare basename; the "
                        f"layout profile requires package-absolute imports — use "
                        f"'from {profile.package_name}.{module} import ...' "
                        f"(or 'import {profile.package_name}.{module}'). A bare "
                        f"'{module}' only resolves via PYTHONPATH and is a "
                        f"false-green risk."
                    ),
                    details={"module": module, "expected_package": profile.package_name},
                )
            )
    return findings


def _check_source_outside_package(
    project_root: Path,
    profile: LayoutProfile,
) -> list[ImportCoherenceFinding]:
    """Flag source modules under source_root but outside the package root."""
    if not profile.requires_package_init:
        return []
    source_dir = project_root / profile.source_root
    findings: list[ImportCoherenceFinding] = []
    for path in _iter_py_files(source_dir, project_root):
        rel = _rel(path, project_root)
        if _under(rel, profile.package_root):
            continue
        # pyproject/setup files at the source root are not "source modules".
        if PurePosixPath(rel).name in {"setup.py", "conftest.py"}:
            continue
        findings.append(
            ImportCoherenceFinding(
                kind="source_outside_package",
                path=rel,
                message=(
                    f"source module lives under '{profile.source_root}' but outside "
                    f"the package root '{profile.package_root}'. The harness owns a "
                    f"src-layout package: move it to "
                    f"'{profile.package_root}/{PurePosixPath(rel).name}'."
                ),
                details={"package_root": profile.package_root},
            )
        )
    return findings


def _check_missing_init(
    project_root: Path,
    profile: LayoutProfile,
) -> list[ImportCoherenceFinding]:
    """Flag a missing ``__init__.py`` where the profile requires a package."""
    findings: list[ImportCoherenceFinding] = []
    package_dir = project_root / profile.package_root
    # Path-escape jail: ``package_root`` derives from user-controllable
    # ``scan.source_dirs``; an in-root package dir may be a symlink whose target
    # escapes the tree. A package ROOT that escapes is INVALID evidence —
    # FAIL-CLOSED (raise) rather than treated as "not a package here" (a silent
    # skip would let the missing-init check pass over a smuggled off-root dir).
    require_project_path(project_root, package_dir, context="package_root")
    if profile.requires_package_init and package_dir.is_dir():
        if not (package_dir / "__init__.py").exists():
            findings.append(
                ImportCoherenceFinding(
                    kind="missing_package_init",
                    path=f"{profile.package_root}/__init__.py",
                    message=(
                        f"package root '{profile.package_root}' has no __init__.py; "
                        f"it is not an importable package. Add an __init__.py."
                    ),
                )
            )
    return findings


def _check_shadowing(
    project_root: Path,
    profile: LayoutProfile,
) -> list[ImportCoherenceFinding]:
    """Flag a module basename that exists BOTH in the package and flat under src.

    A duplicate basename across roots makes import resolution order-dependent
    (the classic ``src/foo.py`` shadowing ``src/<pkg>/foo.py``). Compares the
    FLAT source modules against the PACKAGE modules specifically (not the
    combined set, which would self-match a flat module).
    """
    source_dir = project_root / profile.source_root
    package_dir = project_root / profile.package_root
    package_names: set[str] = {
        path.stem
        for path in _iter_py_files(package_dir, project_root)
        if path.stem not in {"__init__", "__main__"}
    }
    flat_names: dict[str, str] = {}
    for path in _iter_py_files(source_dir, project_root):
        rel = _rel(path, project_root)
        if _under(rel, profile.package_root):
            continue
        stem = path.stem
        if stem in {"__init__", "__main__", "setup", "conftest"}:
            continue
        flat_names[stem] = rel
    findings: list[ImportCoherenceFinding] = []
    for stem in sorted(set(flat_names) & package_names):
        findings.append(
            ImportCoherenceFinding(
                kind="shadowing_module",
                path=flat_names[stem],
                message=(
                    f"module '{stem}' exists both in the package "
                    f"('{profile.package_root}/{stem}.py') and flat "
                    f"('{flat_names[stem]}'); import resolution is ambiguous. "
                    f"Keep only the package copy."
                ),
                details={"module": stem},
            )
        )
    return findings


def _detect_backend(parsed: dict[str, Any]) -> str:
    """Classify the declared ``[build-system] build-backend`` (setuptools/hatchling/other)."""
    build_system = parsed.get("build-system") if isinstance(parsed.get("build-system"), dict) else {}
    backend = build_system.get("build-backend") if isinstance(build_system, dict) else None
    token = backend.strip().lower() if isinstance(backend, str) else ""
    if token.startswith("setuptools"):
        return "setuptools"
    if token.startswith("hatchling") or token.startswith("hatch"):
        return "hatchling"
    return token


def _check_manifest_agreement(
    project_root: Path,
    profile: LayoutProfile,
) -> list[ImportCoherenceFinding]:
    """Flag a pyproject whose PACKAGING contradicts the profile (backend-aware).

    Validates the packaging declaration for BOTH supported backends — closing the
    latent false-green where a HATCH project (with a setuptools-incoherent or
    package-wrong wheel target) passed a setuptools-only check:

    * **setuptools** — a declared ``[tool.setuptools.packages.find] where`` that
      does not include the profile ``source_root``.
    * **hatchling** — a declared ``[tool.hatch.build.targets.wheel] packages``
      that does not include the profile ``package_root`` (``<src>/<pkg>``).

    Conservative on the SETUPTOOLS side (only a declared ``where`` is checked) so
    a pyproject without those keys imposes no requirement; but a hatch project is
    held to its wheel-target packages when declared. A non-empty disagreement is
    NEVER auto-passed — it is fed to the DIAGNOSE → REGENERATE path.
    """
    pyproject = project_root / "pyproject.toml"
    if not pyproject.exists():
        return []
    try:
        text = pyproject.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    parsed = _parse_toml(text)
    if parsed is None:
        return []
    findings: list[ImportCoherenceFinding] = []
    backend = _detect_backend(parsed)

    tool = parsed.get("tool") if isinstance(parsed.get("tool"), dict) else {}

    # ── setuptools: validate packages.find where ⊇ source_root ──
    setuptools_cfg = tool.get("setuptools") if isinstance(tool, dict) else {}
    packages = setuptools_cfg.get("packages") if isinstance(setuptools_cfg, dict) else None
    if isinstance(packages, dict):
        find = packages.get("find")
        where = find.get("where") if isinstance(find, dict) else None
        if isinstance(where, list) and where:
            declared = [_norm(str(item)) for item in where if str(item).strip()]
            if declared and profile.source_root not in declared:
                findings.append(
                    ImportCoherenceFinding(
                        kind="manifest_source_root_mismatch",
                        path="pyproject.toml",
                        message=(
                            f"[tool.setuptools.packages.find] where={declared} does not "
                            f"include the profile source_root '{profile.source_root}'."
                        ),
                        details={"declared": declared, "expected": profile.source_root},
                    )
                )

    # ── hatchling: validate wheel-target packages ⊇ package_root ──
    # Only enforced for a hatch backend (a setuptools project may legitimately
    # carry no [tool.hatch...] table). Closes the latent false-green: a hatch
    # project previously passed the setuptools-only check while topology-wrong.
    if backend == "hatchling":
        hatch = tool.get("hatch") if isinstance(tool, dict) else {}
        build = hatch.get("build") if isinstance(hatch, dict) else {}
        targets = build.get("targets") if isinstance(build, dict) else {}
        wheel = targets.get("wheel") if isinstance(targets, dict) else {}
        wheel_packages = wheel.get("packages") if isinstance(wheel, dict) else None
        if isinstance(wheel_packages, list) and wheel_packages:
            declared_pkgs = [_norm(str(item)) for item in wheel_packages if str(item).strip()]
            if declared_pkgs and profile.package_root not in declared_pkgs:
                findings.append(
                    ImportCoherenceFinding(
                        kind="manifest_hatch_packages_mismatch",
                        path="pyproject.toml",
                        message=(
                            f"[tool.hatch.build.targets.wheel] packages={declared_pkgs} does not "
                            f"include the profile package_root '{profile.package_root}'."
                        ),
                        details={"declared": declared_pkgs, "expected": profile.package_root},
                    )
                )
    return findings


def _parse_toml(text: str) -> dict[str, Any] | None:
    try:  # tomllib is stdlib from 3.11; tomli is the 3.10 backport.
        import tomllib as parser  # type: ignore[import-not-found]
    except ModuleNotFoundError:  # pragma: no cover - py<3.11
        try:
            import tomli as parser  # type: ignore[import-not-found, no-redef]
        except ModuleNotFoundError:
            return None
    try:
        loaded = parser.loads(text)
    except Exception:  # noqa: BLE001 - a broken pyproject is the parse gate's job, not ours.
        return None
    return loaded if isinstance(loaded, dict) else None


def check_import_coherence(
    project_root: Path | str,
    *,
    language: str | None,
    project_name: str | None = None,
    source_dirs: Any = None,
    test_dirs: Any = None,
    config: Mapping[str, Any] | None = None,
    profile: LayoutProfile | None = None,
) -> ImportCoherenceResult:
    """Run the import-coherence gate for a stack with a layout profile.

    Stacks WITHOUT a layout profile (or with the gate opted out) return a passing
    no-op result — the gate never invents failures it cannot reason about, and
    the verify honesty rule remains the backstop. The opt-out is explicit
    (``coherence.import_coherence: false``); the gate is never weakened silently.
    """
    root = Path(project_root)
    if import_coherence_opt_out(config):
        return ImportCoherenceResult(
            passed=True, detail="import coherence: disabled (coherence.import_coherence: false)"
        )

    if profile is None:
        profile = resolve_layout_profile(
            language=language,
            project_name=project_name,
            source_dirs=source_dirs,
            test_dirs=test_dirs,
            config=config,
            project_root=root,
        )
    if profile is None:
        return ImportCoherenceResult(
            passed=True,
            detail=f"import coherence: no layout profile for language {language!r} (skipped)",
        )

    ambient_exempt: list[str] = []
    try:
        source_modules = _source_module_basenames(root, profile)
        # Ambient-shadowed source modules (a first-party name that also names a
        # runtime/stdlib module) are exempt from the bare-import check — a bare
        # ``import <name>`` cannot be CONFIRMED as a first-party reference, so
        # flagging it would be a false-RED. The full source-module count in the
        # detail line is unaffected (only the bare-import intersection shrinks).
        ambient = _resolve_ambient_modules(profile)
        ambient_exempt = sorted(source_modules & ambient)

        findings: list[ImportCoherenceFinding] = []
        findings.extend(_check_source_outside_package(root, profile))
        findings.extend(_check_missing_init(root, profile))
        findings.extend(_check_shadowing(root, profile))
        findings.extend(
            _check_bare_basename_imports(root, profile, source_modules - ambient)
        )
        findings.extend(_check_manifest_agreement(root, profile))
    except PathEscapeError as exc:
        # A configured/profile evidence ROOT (source_root / test_root /
        # package_root) resolved OUTSIDE the project (e.g. an in-root symlink
        # whose target escapes). Fail-closed: an honest RED, never a silent skip
        # that "passes" by checking a smuggled off-root tree as if it were empty.
        return ImportCoherenceResult(
            passed=False,
            findings=[
                ImportCoherenceFinding(
                    kind="evidence_root_escape",
                    path=str(getattr(exc, "path", "") or ""),
                    message=(
                        f"a layout evidence root escapes the project tree: {exc}. "
                        f"The import-coherence gate cannot validate a smuggled "
                        f"out-of-root tree — fix the escaping source/test root."
                    ),
                )
            ],
            profile=profile,
            detail="import coherence: evidence root escapes project root",
        )

    passed = not findings
    detail = (
        f"import coherence: OK (package={profile.package_root}, "
        f"{len(source_modules)} source module(s), policy={profile.test_import_policy})"
        if passed
        else f"import coherence: {len(findings)} finding(s)"
    )
    if ambient_exempt:
        detail += (
            f" [{len(ambient_exempt)} ambient-shadowed source module(s) exempt "
            f"from the bare-import check: {', '.join(ambient_exempt)}]"
        )
    return ImportCoherenceResult(passed=passed, findings=findings, profile=profile, detail=detail)
