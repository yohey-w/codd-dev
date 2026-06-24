"""Node completeness check for DAG expected implementation files."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codd.dag.checks import register_dag_check
from codd.path_safety import resolve_project_path


@dataclass
class NodeCompletenessResult:
    check_name: str = "node_completeness"
    severity: str = "red"
    missing_impl_files: list[str] = field(default_factory=list)
    passed: bool = True
    # ``status``/``skipped``/``checked_count`` close a vacuous-pass hole: with no
    # ``expects`` edge this gate used to return a green ``PASS [red]`` having
    # verified nothing. ``checked_count`` is the number of ``expects`` edges
    # actually examined; when it is 0 the run is a SKIP (verified nothing on
    # purpose), not a clean pass. The materiality overlay reads ``checked_count``.
    status: str = "pass"
    skipped: bool = False
    checked_count: int = 0


@register_dag_check("node_completeness")
class NodeCompletenessCheck:
    """Verify that every ``expects`` edge points to an existing impl file."""

    def __init__(self, dag=None, project_root=None, settings: dict[str, Any] | None = None):
        self.dag = dag
        self.project_root = Path(project_root) if project_root is not None else None
        self.settings = settings or {}

    def run(self, dag=None, project_root=None, settings: dict[str, Any] | None = None) -> NodeCompletenessResult:
        dag = dag if dag is not None else self.dag
        if dag is None:
            raise ValueError("dag is required")

        root = Path(project_root) if project_root is not None else self.project_root
        if root is None:
            root = Path.cwd()

        missing: list[str] = []
        seen: set[str] = set()
        checked = 0

        for edge in dag.edges:
            if edge.kind != "expects":
                continue
            checked += 1

            node = dag.nodes.get(edge.to_id)
            if node is None or node.kind != "impl_file":
                if node is not None and node.kind == "expected":
                    continue
                if node is not None and node.kind == "common":
                    if node.path and not _node_path_exists(root, node.path):
                        _append_once(missing, seen, edge.to_id)
                    continue
                _append_once(missing, seen, edge.to_id)
                continue

            if node.path and not _node_path_exists(root, node.path):
                _append_once(missing, seen, edge.to_id)

        if checked == 0:
            # No ``expects`` edge to verify against → this gate examined nothing.
            # Returning a clean PASS here was a vacuous false-green; a no-input run
            # is a SKIP (deploy still allowed, but nothing was verified).
            return NodeCompletenessResult(
                missing_impl_files=[],
                passed=True,
                status="skip",
                skipped=True,
                checked_count=0,
            )

        return NodeCompletenessResult(
            missing_impl_files=missing,
            passed=not missing,
            status="pass" if not missing else "fail",
            skipped=False,
            checked_count=checked,
        )


def _node_path_exists(project_root: Path, node_path: str) -> bool:
    """Root-jailed existence check for a user-controllable ``node.path``.

    ``node.path`` originates from DAG data the user authors, so it can be an
    out-of-root absolute path (``/etc/hosts``), a ``../`` traversal, or an in-root
    symlink whose target escapes the tree. Such a path may exist on the real
    filesystem yet is never the project's own impl/common artifact; counting it as
    "exists" is a path-escape false-green. ``resolve_project_path`` returns ``None``
    for any escaped path, so an out-of-root file is treated as missing (red),
    matching the existing severity. An in-root path (relative or absolute) is
    resolved and its actual existence is checked, exactly as before.
    """
    resolved = resolve_project_path(project_root, node_path)
    if resolved is None:
        return False
    return resolved.exists()


def _append_once(items: list[str], seen: set[str], value: str) -> None:
    if value in seen:
        return
    seen.add(value)
    items.append(value)
