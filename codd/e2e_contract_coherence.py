"""AST e2e-contract (no-runtime-import) coherence gate — a sibling to
``test_import_coherence`` on the e2e-import-CONTRACT axis.

A-core anti-false-green gate. ``codd/import_coherence.py`` proves source + tests
agree on package/import context; ``codd/test_import_coherence.py`` proves every
helper symbol a test imports actually exists in the test tree. THIS module proves
a THIRD, complementary invariant about the e2e LAYER specifically:

  for a CLI / subprocess e2e modality, the e2e tests AND their shared e2e helpers
  must invoke the built/installed entrypoint as a SUBPROCESS and must NOT import
  the application/runtime (source) package. An in-process helper that imports the
  runtime belongs in the UNIT test tree, NOT under the e2e helper package.

The motivating finding (2026-06 greenfield AUTOPILOT dogfood, a py-CLI build):
the design docs mandate the e2e-no-runtime-import contract (AC-14); the model
generated the GOVERNANCE test for it (``tests/test_runtime_constraints.py`` asserts
``"todo_cli" not in imported_roots(path)`` over ``tests/e2e/**``) — then VIOLATED
that very contract in a shared e2e helper: ``tests/e2e/helpers/cli.py`` had a
function-scoped ``from todo_cli import cli``. Result: verify fails at a RUN-PHASE
assertion, and auto-repair is (correctly) blocked by the scope guard (test files
are read-only for an ``assertion_failure``) → REPAIR_FAILED. The governance test
is GOOD — the fix is to stop GENERATING the violation, and to fail HONESTLY with a
precise diagnosis BEFORE pytest so it feeds the EXISTING regenerate path
(DIAGNOSE → REGENERATE; the harness will not rewrite generated files on --resume,
and stubs are never auto-created).

It is STATIC (``ast`` only — nothing imported or executed) and scoped strictly to
the e2e test tree (``<test_root>/e2e``). Both module-level AND function-scoped
imports are inspected (the dogfood violation was function-scoped). It NEVER
auto-edits, NEVER stubs, and NEVER reclassifies a run-phase assertion failure —
the repair scope-guard / attribution stay UNTOUCHED (deferred-B stays deferred).

Modality carve-out (load-bearing anti-false-RED): the no-runtime-import contract
is a property of CLI / subprocess e2e ONLY. A browser or device e2e suite
LEGITIMATELY imports a client / runtime (a Playwright page object, a device SDK),
so this gate is a passing NO-OP for those modalities. It activates ONLY when the
project's e2e modality is PROVABLY ``cli`` (or the project explicitly declares its
e2e-import contract as "no runtime import"); an untyped / undecidable modality is
left UNFLAGGED. It also flags ONLY a PROVABLE runtime/source-package import — if
the source-package identity is undecidable, it does not flag.

Opt-out: shares ``import_coherence``'s explicit opt-out
(``coherence.import_coherence: false``) — these gates are facets of one
anti-false-green coherence concern, never weakened silently or by default.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from codd.import_coherence import import_coherence_opt_out
from codd.project_types import (
    LayoutProfile,
    load_capabilities,
    resolve_layout_profile,
    resolve_project_type,
)


__all__ = [
    "E2EContractFinding",
    "E2EContractResult",
    "check_e2e_contract_coherence",
    "resolve_e2e_import_contract",
]


@dataclass(frozen=True)
class E2EContractFinding:
    """One e2e-contract (no-runtime-import) violation, with a precise message."""

    kind: str
    path: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class E2EContractResult:
    """Outcome of the e2e-contract (no-runtime-import) coherence gate."""

    passed: bool
    findings: list[E2EContractFinding] = field(default_factory=list)
    profile: LayoutProfile | None = None
    detail: str = ""

    def summary(self) -> str:
        if self.passed:
            return self.detail or "e2e-contract coherence: OK"
        lines = [
            f"e2e-contract-coherence gate FAILED ({len(self.findings)} finding(s)):"
        ]
        for finding in self.findings:
            lines.append(f"  - [{finding.kind}] {finding.path}: {finding.message}")
        # Same DIAGNOSE → REGENERATE stance as the sibling coherence gates: the
        # generated e2e layer violates the project's OWN e2e-no-runtime-import
        # contract (the governance test the model itself derived). The scaffold is
        # create-only and will not rewrite model-authored files, so --resume cannot
        # fix it — REGENERATE. We deliberately do NOT auto-edit the helper (moving
        # it to the unit tree or rewriting it to a subprocess call would be an
        # opaque auto-edit of generated tests, a false-green vector), and we do NOT
        # touch the governance test or the repair scope-guard. (Diagnose-only; opt
        # out via coherence.import_coherence: false, which re-opens the risk.)
        lines.append(
            "  → Generated e2e tests/helpers import the application/runtime package, "
            "violating the project's e2e-subprocess (no-runtime-import) contract. An "
            "e2e test or shared e2e helper must invoke the built/installed entrypoint "
            "as a SUBPROCESS; an in-process helper that imports the runtime belongs in "
            "the UNIT test tree. REGENERATE the project (fresh greenfield) so the e2e "
            "layer honors the contract; the harness will not rewrite generated files "
            "on --resume, and tests are never auto-edited."
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


# ── e2e-import contract resolution (modality carve-out) ──────────────────────


def _config_get_str(config: Mapping[str, Any] | None, *path: str) -> str:
    """Best-effort nested string lookup (lowercased/stripped), else ``""``."""
    node: Any = config
    for key in path:
        if not isinstance(node, Mapping):
            return ""
        node = node.get(key)
    return str(node or "").strip().lower() if node is not None else ""


def _declared_e2e_contract(config: Mapping[str, Any] | None) -> str | None:
    """An explicit ``coherence.e2e_import_contract`` declaration, if present.

    A project may state its e2e-import contract directly (independent of the type
    profile). ``"no_runtime_import"`` (aliases ``no-runtime-import`` /
    ``subprocess``) ACTIVATES the gate; ``"allow_runtime_import"`` (alias
    ``import_allowed``) explicitly DEACTIVATES it (a browser/HTTP project that
    legitimately imports a client). Anything else / absent → ``None`` (defer to
    the modality).
    """
    raw = _config_get_str(config, "coherence", "e2e_import_contract")
    if not raw:
        return None
    if raw in {"no_runtime_import", "no-runtime-import", "subprocess"}:
        return "no_runtime_import"
    if raw in {"allow_runtime_import", "allow-runtime-import", "import_allowed", "allowed"}:
        return "allow_runtime_import"
    return None


def _resolve_modality(config: Mapping[str, Any] | None, project_root: Path) -> str | None:
    """Resolve the project's e2e modality from its configured type, or ``None``.

    Mirrors generator/implementer modality resolution: read the configured project
    type from ``required_artifacts.project_type`` / ``project.type`` /
    ``project_type`` and load its capability profile's ``e2e_modality``. An
    UNTYPED project returns ``None`` (modality undecidable) — the gate then stays a
    no-op (anti-false-RED), rather than guessing.
    """
    configured = (
        _config_get_str(config, "required_artifacts", "project_type")
        or _config_get_str(config, "project", "type")
        or _config_get_str(config, "project_type")
    )
    if not configured:
        return None
    resolved, _reason = resolve_project_type(configured, None, project_root)
    capabilities = load_capabilities(resolved, project_root)
    return capabilities.e2e_modality


def resolve_e2e_import_contract(
    config: Mapping[str, Any] | None, project_root: Path
) -> bool:
    """Whether the project's e2e layer is under a no-runtime-import contract.

    ``True`` ONLY when the contract is PROVABLE: either the project explicitly
    declares ``coherence.e2e_import_contract: no_runtime_import``, or its
    configured type resolves to the ``cli`` e2e modality. A browser/device
    modality, an explicit ``allow_runtime_import``, or an UNTYPED/undecidable
    project all yield ``False`` — the gate is then a no-op (anti-false-RED: a
    browser e2e suite legitimately imports a client/runtime).
    """
    declared = _declared_e2e_contract(config)
    if declared == "no_runtime_import":
        return True
    if declared == "allow_runtime_import":
        return False
    modality = _resolve_modality(config, project_root)
    return modality == "cli"


# ── source-package identity (the import ROOT a violation resolves to) ─────────


def _source_package_roots(profile: LayoutProfile) -> set[str]:
    """Import ROOTs that name the application/runtime (source) package.

    The provable identity of the runtime package the e2e layer must NOT import.
    For the Python ``package_absolute`` profile this is the layout's
    ``package_name`` (``from <package_name>... import ...``). When the package
    identity is not statically decidable, the caller treats the project as having
    an UNDECIDABLE source identity and does not flag (anti-false-RED).
    """
    roots: set[str] = set()
    name = (profile.package_name or "").strip()
    if name and name.isidentifier():
        roots.add(name)
    return roots


def _e2e_runtime_imports(tree: ast.AST, source_roots: set[str]) -> list[tuple[str, int, bool]]:
    """Collect imports of a runtime/source ROOT in an e2e file: (root, lineno, scoped).

    Inspects BOTH module-level and function/method-scoped imports (the dogfood
    violation was function-scoped). For ``import X`` / ``import X.y`` the ROOT is
    ``X``; for ``from X import ...`` (absolute, level 0) the ROOT is ``X``'s first
    dotted segment. A package-RELATIVE ``from . import`` / ``from .mod import``
    (level >= 1) is never a runtime import (it addresses the e2e package itself),
    so it is skipped. ``scoped`` is ``True`` when the import is nested inside a
    function/method body (vs the module top level) — used only to enrich the
    diagnosis.
    """
    func_types = (ast.FunctionDef, ast.AsyncFunctionDef)
    # Pre-compute the set of import nodes that live inside ANY function body, so a
    # single ``ast.walk`` can label each import as scoped vs module-level without
    # re-traversing. (A nested function still counts as "scoped".)
    scoped_imports: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, func_types):
            for descendant in ast.walk(node):
                if isinstance(descendant, (ast.Import, ast.ImportFrom)):
                    scoped_imports.add(id(descendant))

    hits: list[tuple[str, int, bool]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in source_roots:
                    hits.append((root, node.lineno, id(node) in scoped_imports))
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue  # relative — addresses the e2e package itself, not runtime
            if not node.module:
                continue
            root = node.module.split(".")[0]
            if root in source_roots:
                hits.append((root, node.lineno, id(node) in scoped_imports))
    return hits


def check_e2e_contract_coherence(
    project_root: Path | str,
    *,
    language: str | None,
    project_name: str | None = None,
    source_dirs: Any = None,
    test_dirs: Any = None,
    config: Mapping[str, Any] | None = None,
    profile: LayoutProfile | None = None,
) -> E2EContractResult:
    """Run the e2e-contract (no-runtime-import) coherence gate for a profiled stack.

    Active ONLY when the project's e2e layer is under a PROVABLE no-runtime-import
    contract (a ``cli`` e2e modality, or an explicit declaration) AND the source
    package identity is statically decidable. It AST-scans every ``*.py`` under the
    e2e test root (``<test_root>/e2e`` — tests AND shared helpers; the helper is the
    actual violator), flagging any import (module-level OR function-scoped) whose
    ROOT resolves to the runtime/source package. A violation is a coherence
    failure flagged with a precise message and fed to the existing DIAGNOSE →
    REGENERATE path; nothing is auto-edited or stubbed. Browser/device modalities,
    an untyped project, an undecidable source identity, or the explicit opt-out all
    return a passing no-op (anti-false-RED). The repair scope-guard / attribution
    are never touched.
    """
    root = Path(project_root)
    if import_coherence_opt_out(config):
        return E2EContractResult(
            passed=True,
            detail="e2e-contract coherence: disabled (coherence.import_coherence: false)",
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
        return E2EContractResult(
            passed=True,
            detail=f"e2e-contract coherence: no layout profile for language {language!r} (skipped)",
        )

    # Modality carve-out: only a PROVABLE no-runtime-import contract activates the
    # gate. Browser/device e2e legitimately imports a client/runtime; an untyped
    # project is undecidable → no-op (anti-false-RED).
    if not resolve_e2e_import_contract(config, root):
        return E2EContractResult(
            passed=True,
            profile=profile,
            detail="e2e-contract coherence: e2e modality is not a no-runtime-import contract (skipped)",
        )

    # Provable source-package identity — undecidable → do not flag (anti-false-RED).
    source_roots = _source_package_roots(profile)
    if not source_roots:
        return E2EContractResult(
            passed=True,
            profile=profile,
            detail="e2e-contract coherence: source package identity undecidable (skipped)",
        )

    e2e_dir = root / profile.test_root / "e2e"
    if not e2e_dir.is_dir():
        return E2EContractResult(
            passed=True,
            profile=profile,
            detail=f"e2e-contract coherence: no e2e tree '{_norm(profile.test_root)}/e2e' (skipped)",
        )

    findings: list[E2EContractFinding] = []
    scanned = 0
    for path in _iter_py_files(e2e_dir):
        tree = _parse(path)
        if tree is None:
            continue
        scanned += 1
        rel_proj = _rel(path, root)
        for import_root, lineno, scoped in _e2e_runtime_imports(tree, source_roots):
            where = "function-scoped" if scoped else "module-level"
            findings.append(
                E2EContractFinding(
                    kind="e2e_runtime_import",
                    path=rel_proj,
                    message=(
                        f"e2e file imports the runtime/source package '{import_root}' "
                        f"({where}, line {lineno}), violating the e2e-subprocess "
                        f"(no-runtime-import) contract. An e2e test or shared e2e "
                        f"helper must invoke the built/installed entrypoint as a "
                        f"SUBPROCESS; an in-process helper that imports the runtime "
                        f"belongs in the UNIT test tree, not under the e2e helper "
                        f"package."
                    ),
                    details={
                        "import_root": import_root,
                        "lineno": lineno,
                        "scoped": scoped,
                        "package_name": profile.package_name,
                    },
                )
            )

    passed = not findings
    detail = (
        f"e2e-contract coherence: OK ({scanned} e2e file(s) scanned, "
        f"test_root={_norm(profile.test_root)}/e2e, runtime_root={profile.package_name})"
        if passed
        else f"e2e-contract coherence: {len(findings)} finding(s)"
    )
    return E2EContractResult(
        passed=passed, findings=findings, profile=profile, detail=detail
    )
