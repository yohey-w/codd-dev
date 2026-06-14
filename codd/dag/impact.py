"""Impact resolution: map a design document to affected implementation/test files.

Used by PHENOMENON-mode ``codd fix`` to decide deterministically which
implementation files (and their tests) must follow an applied design-doc
update. Resolution is DAG-first:

1. Forward ``expects`` edges from the design node identify expected
   implementation files (including one hop through lexicon ``expected``
   nodes via their ``represents`` edges).
2. Each implementation node's ``tested_by`` edges identify its test files.
3. When a design doc declares no ``expects`` edges, the frontmatter
   ``modules`` list falls back to filesystem candidate matching
   (:func:`find_impl_candidates` — the generalized form of the glob
   inference that ``codd.fixer`` has always used).

Everything here is generic: no project names, no framework assumptions
beyond conventional file layouts already encoded in the legacy fixer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_DOC_SUFFIX = ".md"


@dataclass
class ImplTargets:
    """Implementation/test files affected by one design document."""

    design_node_id: str
    impl_paths: list[str] = field(default_factory=list)
    test_paths: list[str] = field(default_factory=list)
    # "expects" (DAG edges), "frontmatter_modules" (glob fallback), "none"
    source: str = "none"


def affected_impl_targets(
    dag: Any,
    design_node_id: str,
    *,
    project_root: Path | None = None,
) -> ImplTargets:
    """Resolve implementation and test files affected by ``design_node_id``.

    Args:
        dag: a built :class:`codd.dag.DAG` (or duck-typed equivalent with
            ``nodes`` mapping and ``edges`` list).
        design_node_id: node id of the design document (relative posix path).
        project_root: required for the frontmatter ``modules`` filesystem
            fallback; when ``None`` the fallback is skipped.
    """
    targets = ImplTargets(design_node_id=design_node_id)
    nodes = getattr(dag, "nodes", {}) or {}
    design_node = nodes.get(design_node_id)
    if design_node is None:
        return targets

    forward: dict[str, dict[str, list[str]]] = {}
    for edge in getattr(dag, "edges", []) or []:
        forward.setdefault(edge.from_id, {}).setdefault(edge.kind, []).append(edge.to_id)

    impl_ids: list[str] = []
    seen_impl: set[str] = set()
    for target_id in forward.get(design_node_id, {}).get("expects", []):
        node = nodes.get(target_id)
        if node is None:
            continue
        if node.kind == "expected":
            # Lexicon expected-artifact node: hop through `represents`.
            for represented_id in forward.get(target_id, {}).get("represents", []):
                _append_code_node(nodes.get(represented_id), impl_ids, seen_impl)
            continue
        _append_code_node(node, impl_ids, seen_impl)

    source = "expects" if impl_ids else "none"

    if not impl_ids and project_root is not None:
        for module in _frontmatter_modules(design_node):
            for candidate in find_impl_candidates(Path(project_root), str(module)):
                normalized = Path(candidate).as_posix()
                if normalized not in seen_impl:
                    seen_impl.add(normalized)
                    impl_ids.append(normalized)
        if impl_ids:
            source = "frontmatter_modules"

    test_ids: list[str] = []
    seen_tests: set[str] = set()
    for impl_id in impl_ids:
        for test_id in forward.get(impl_id, {}).get("tested_by", []):
            node = nodes.get(test_id)
            if node is None:
                continue
            node_path = str(node.path or node.id)
            if node_path.endswith(_DOC_SUFFIX):
                continue
            if test_id not in seen_tests:
                seen_tests.add(test_id)
                test_ids.append(test_id)

    targets.impl_paths = sorted(impl_ids)
    targets.test_paths = sorted(test_ids)
    targets.source = source
    return targets


def _append_code_node(node: Any, impl_ids: list[str], seen: set[str]) -> None:
    """Collect a node when it represents implementation *code*.

    ``kind="common"`` is shared by frontmatter-declared common documents and
    ``common_node_patterns``-matched code files; ``.md`` is the codebase-wide
    doc discriminator (same principle as ``dependency_freshness``).
    """
    if node is None:
        return
    node_path = str(node.path or node.id)
    if node.kind == "impl_file" or (node.kind == "common" and not node_path.endswith(_DOC_SUFFIX)):
        path = Path(node_path).as_posix()
        if path not in seen:
            seen.add(path)
            impl_ids.append(path)


def _frontmatter_modules(node: Any) -> list[Any]:
    attributes = getattr(node, "attributes", None) or {}
    frontmatter = attributes.get("frontmatter") or {}
    if not isinstance(frontmatter, dict):
        return []
    modules = frontmatter.get("modules") or []
    if isinstance(modules, (list, tuple)):
        return list(modules)
    return [modules]


# ---------------------------------------------------------------------------
# Filesystem candidate matching (shared with codd.fixer — behavior-identical)
# ---------------------------------------------------------------------------


def is_test_path(path: str) -> bool:
    """Check if a path looks like a test file."""
    parts = path.replace("\\", "/").split("/")
    # Directory-based: tests/, __tests__/, test/, spec/
    if any(p in ("tests", "__tests__", "test", "spec") for p in parts):
        return True
    # File-based: *.spec.*, *.test.*, *.e2e.*, test_*
    basename = parts[-1] if parts else ""
    if (
        ".spec." in basename
        or ".test." in basename
        or ".e2e." in basename
        or basename.startswith("test_")
    ):
        return True
    return False


def find_impl_candidates(project_root: Path, domain: str) -> list[str]:
    """Find implementation files matching a domain name."""
    candidates: list[str] = []
    domain_lower = domain.lower().replace("-", "_")

    # Strategy 1: API route files — **/api/{domain}/route.{ts,js}
    # Handles both standard (src/app/api/) and generated (src/generated/*/app/api/)
    domain_kebab = domain_lower.replace("_", "-")
    for domain_variant in {domain_lower, domain_kebab}:
        for ext in ("ts", "tsx", "js"):
            for match in project_root.glob(f"**/api/{domain_variant}/route.{ext}"):
                if match.is_file():
                    rel = str(match.relative_to(project_root))
                    if rel not in candidates:
                        candidates.append(rel)

    # Strategy 3: Generated/service files — src/**/domain*.ts
    for pattern in (
        f"src/**/*{domain_lower}*",
        f"src/**/*{domain_kebab}*",
        f"lib/**/*{domain_lower}*",
    ):
        for match in project_root.glob(pattern):
            if match.is_file() and not is_test_path(str(match.relative_to(project_root))):
                rel = str(match.relative_to(project_root))
                if rel not in candidates:
                    candidates.append(rel)

    # Strategy 4: Python — {domain}.py, app.py in same directory
    for pattern in (
        f"**/{domain_lower}.py",
        f"**/app.py",
        f"src/**/{domain_lower}.py",
    ):
        for match in project_root.glob(pattern):
            if match.is_file() and not is_test_path(str(match.relative_to(project_root))):
                rel = str(match.relative_to(project_root))
                if rel not in candidates:
                    candidates.append(rel)

    return candidates
