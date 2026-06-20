"""Profile-aware greenfield output-path routing (the SECOND C-Go defect).

Background
----------
The greenfield implement pipeline routes a task's declared outputs through an
*accept-list of output roots* (``_output_paths_for_task`` →
``_route_source_into_package``). Historically that routing baked in a Python/TS
``source_root=src`` assumption: the bare source root ``"src"`` was upgraded to a
``src/<package>`` package root, and ``src``/``tests`` were appended as accepted
destinations. Applied to Go — whose ``LanguageProfile`` has
``package_root.kind == "none"`` (go.mod / cmd/ / internal/ live at the repo
root, there is NO single ``src/`` source root) — that wrote ``go.mod`` / ``cmd/``
/ ``internal/`` UNDER ``src/``, breaking ``go test ./...`` from the repo root.

The fix (design ``dogfood/gpt_language_generality_design.md`` §1.2/§1.6) is
PROFILE-DRIVEN: routing resolves the project's ``LanguageProfile`` via the
registry and, when ``package_root.kind == "none"``, routes declared outputs to
the REPO ROOT (the single authority for the canonical path being
:class:`~codd.languages.path_planner.PathPlanner`) instead of forcing ``src/``.
For single-source-root languages (Python ``named_package`` / TS+node
``path_root``) the routing is BYTE-IDENTICAL to before, and a language with no
profile falls back to the exact legacy behavior.

These tests lock all three invariants.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from codd.greenfield.pipeline import (
    ImplementTaskRef,
    _output_paths_for_task,
    _root_module_output_paths,
    _route_source_into_package,
)
from codd.implementer import _parse_file_payloads
from codd.languages.path_planner import PathPlanner
from codd.languages.registry import default_registry


def _cfg(language: str, *, source_dirs=("src",), test_dirs=("tests",), name="demo-app"):
    return {
        "project": {"language": language, "name": name},
        "scan": {"source_dirs": list(source_dirs), "test_dirs": list(test_dirs)},
    }


# ── Python: BYTE-IDENTICAL to the legacy routing ──────────────────────────────


def test_python_source_routes_into_named_package_unchanged() -> None:
    """Python (``package_root.kind == named_package``) keeps the legacy routing:
    the bare ``src`` is upgraded to ``src/<canonical_package>`` and ``src`` +
    ``tests`` stay on the accept-list — byte-identical to before the fix."""
    config = _cfg("python", name="todo-cli")

    # The profile-driven short-circuit must NOT fire for a single-root language.
    assert _root_module_output_paths(config) is None

    routed = _route_source_into_package(config, ["src"])
    # Canonical package root first (project ``todo-cli`` → package ``todo_cli``),
    # then the bare source root and the test root accept-list entries.
    assert routed == ["src/todo_cli", "src", "tests"]
    # Nothing the model emits is forced off ``src/`` — the Python contract.
    assert any(p.startswith("src") for p in routed)


def test_python_storage_module_declared_output_still_under_src_package() -> None:
    """A Python declared output ``storage.py`` for package ``todo_cli`` still lands
    at ``src/todo_cli/storage.py`` once the implementer applies the routed
    accept-list — proving the byte-identical accept of the src package root."""
    config = _cfg("python", name="todo-cli")
    output_paths = _route_source_into_package(config, ["src"])

    # The implementer accepts a model file emitted under the canonical package.
    raw = "=== FILE: src/todo_cli/storage.py ===\n" "x = 1\n"
    payloads = _parse_file_payloads(raw, output_paths, "python")
    assert ("src/todo_cli/storage.py", "x = 1\n") in payloads


# ── TypeScript / node: BYTE-IDENTICAL to the legacy routing ───────────────────


def test_typescript_source_routes_under_src_unchanged() -> None:
    """TypeScript (``package_root.kind == path_root``) keeps the legacy routing:
    ``package_root == source_root == src`` so the accept-list is ``src`` +
    ``tests`` — byte-identical to before the fix. ``node`` aliases TypeScript."""
    for language in ("typescript", "node"):
        config = _cfg(language, name="todo-app")
        assert _root_module_output_paths(config) is None
        routed = _route_source_into_package(config, ["src"])
        assert routed == ["src", "tests"]
        assert all(not p.startswith("..") for p in routed)


def test_typescript_declared_output_still_under_src() -> None:
    """A TS declared output ``index.ts`` still routes under ``src/`` (the implementer
    accepts it there), unchanged by the fix."""
    config = _cfg("typescript", name="todo-app")
    output_paths = _route_source_into_package(config, ["src"])
    raw = "=== FILE: src/index.ts ===\n" "export const x = 1;\n"
    payloads = _parse_file_payloads(raw, output_paths, "typescript")
    assert ("src/index.ts", "export const x = 1;") in [
        (p, c.strip()) for p, c in payloads
    ]


# ── Go: NONE of the declared outputs may sit under src/ ───────────────────────


def test_go_routes_to_repo_root_not_src() -> None:
    """Go (``package_root.kind == none``) routes the source accept-list to the REPO
    ROOT (``.``), never under ``src/``. ``golang`` resolves the same profile."""
    for language in ("go", "golang"):
        config = _cfg(language, name="itemapi")
        # Profile-driven short-circuit fires: repo-root accept-list.
        assert _root_module_output_paths(config) == ["."]
        routed = _route_source_into_package(config, ["src"])
        assert routed == ["."]
        # HARD gate: NO routed accept-list entry imposes a ``src/`` prefix.
        assert not any(
            str(p).strip().replace("\\", "/").strip("/").startswith("src")
            for p in routed
        )


def test_go_source_task_output_paths_have_no_src_prefix() -> None:
    """End-to-end through ``_output_paths_for_task``: a Go SOURCE task with a
    declared ``internal/...`` output resolves to a repo-root accept-list with no
    ``src/`` anywhere."""
    config = _cfg("go", name="itemapi")
    task = ImplementTaskRef(
        task_id="impl_store",
        design_node="module:store",
        expected_outputs=("internal/store/store.go",),
        test_kinds=("source",),
    )
    output_paths = _output_paths_for_task(config, task)
    assert output_paths == ["."]
    assert not any(
        str(p).strip().replace("\\", "/").strip("/").startswith("src")
        for p in output_paths
    )


def test_go_declared_outputs_accepted_at_repo_root_paths() -> None:
    """With Go routed to ``["."]`` the implementer accepts the canonical Go layout
    at its repo-root paths — ``go.mod`` → ``go.mod``, ``cmd/server/main.go`` →
    ``cmd/server/main.go``, ``internal/store/store.go`` →
    ``internal/store/store.go`` — and NONE land under ``src/``."""
    config = _cfg("go", name="itemapi")
    output_paths = _route_source_into_package(config, ["src"])

    raw = (
        "=== FILE: go.mod ===\n"
        "module example.com/itemapi\n\n"
        "go 1.22\n"
        "=== FILE: cmd/server/main.go ===\n"
        "package main\n\n"
        "func main() {}\n"
        "=== FILE: internal/store/store.go ===\n"
        "package store\n\n"
        "type Store struct{}\n"
    )
    produced = {path for path, _ in _parse_file_payloads(raw, output_paths, "go")}
    assert produced == {
        "go.mod",
        "cmd/server/main.go",
        "internal/store/store.go",
    }
    assert not any(p.startswith("src/") for p in produced)


def test_go_routing_matches_path_planner_canonical_paths() -> None:
    """The repo-root paths Go routing accepts are exactly the ones
    :class:`PathPlanner` (the single declared-output authority, design §1.6)
    computes — declared output == write target, with no ``src/`` divergence."""
    profile = default_registry.resolve("go")
    planner = PathPlanner(
        profile, {"module_path": "example.com/itemapi", "package_name": "itemapi"}
    )

    assert planner.plan_output("manifest").repo_relpath == PurePosixPath("go.mod")
    assert planner.plan_output("entrypoint", name="server").repo_relpath == PurePosixPath(
        "cmd/server/main.go"
    )
    assert planner.plan_output(
        "internal_package_file", package="store", name="store"
    ).repo_relpath == PurePosixPath("internal/store/store.go")

    # And the routing for Go never re-prefixes any of those with ``src``.
    config = _cfg("go", name="itemapi")
    assert _route_source_into_package(config, ["src"]) == ["."]


# ── Fallback: a language with no profile keeps the EXACT legacy behavior ───────


def test_unknown_language_falls_back_to_legacy_routing() -> None:
    """A language with no profile (or no language configured) must NOT trigger the
    profile-driven short-circuit; routing degrades to the exact legacy path
    (returns ``explicit`` unchanged when there is no legacy LayoutProfile either)."""
    # Unknown language: no profile → short-circuit returns None.
    assert _root_module_output_paths(_cfg("rustyzzz", name="x")) is None
    # No language at all.
    assert _root_module_output_paths({"project": {"name": "x"}}) is None
    # Legacy path: no LayoutProfile builder for the unknown language ⇒ explicit
    # accept-list is returned verbatim (no src/ imposed, no crash).
    assert _route_source_into_package(_cfg("rustyzzz", name="x"), ["lib"]) == ["lib"]
