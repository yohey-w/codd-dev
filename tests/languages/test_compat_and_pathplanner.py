"""Golden / snapshot tests for the compat shim + PathPlanner (Phase 1+2).

Two guarantees, per ``dogfood/gpt_language_generality_design.md`` §4:

1. **No-regression snapshot** (§4 Phase 1): for python & typescript, the
   ``LayoutProfile`` *topology triple* derived by
   :func:`codd.languages.compat.layout_profile_from_language_profile` is
   **byte-identical** to what the REAL builders
   (:func:`codd.project_types._python_layout_profile` /
   :func:`codd.project_types._typescript_layout_profile`) produce for the same
   inputs. We import and call the real functions — no hand-copied expectations.

2. **The C-Go fix** (§4 Phase 2 / §5.3): :class:`codd.languages.path_planner.PathPlanner`
   plans ``go.mod``, ``cmd/server/main.go`` and ``internal/<pkg>/<file>.go`` for
   Go, and **NO planned path contains ``src/``**. For python/typescript the
   planner's source/test paths still match their existing ``src/`` layout (no
   change).
"""

from __future__ import annotations

import pytest

from codd.languages import (
    PathPlanError,
    PathPlanner,
    layout_profile_from_language_profile,
)
from codd.languages.compat import UnsupportedLayoutShape
from codd.languages.registry import LanguageRegistry
from codd.project_types import (
    _python_layout_profile,
    _typescript_layout_profile,
    normalize_package_name,
)


@pytest.fixture(scope="module")
def registry() -> LanguageRegistry:
    return LanguageRegistry()


# ---------------------------------------------------------------------------
# 1. compat shim: byte-identical topology vs the REAL builders
# ---------------------------------------------------------------------------

#: The fields the compat shim is the authority for (the topology triple +
#: identity). These are what must be byte-identical to the legacy builders.
_LAYOUT_FIELDS = ("language", "package_name", "source_root", "package_root", "test_root")


def _assert_triple_identical(shim, real) -> None:
    for fld in _LAYOUT_FIELDS:
        assert getattr(shim, fld) == getattr(real, fld), (
            f"compat shim drifted from the real builder on {fld!r}: "
            f"{getattr(shim, fld)!r} != {getattr(real, fld)!r}"
        )


@pytest.mark.parametrize(
    "project_name",
    ["todo-cli", "calc-lib", "2048 Game", "Already_Good", "x"],
)
def test_python_compat_is_byte_identical(
    registry: LanguageRegistry, project_name: str
) -> None:
    """Python: shim triple == ``_python_layout_profile`` triple, for many names."""
    profile = registry.resolve("python")
    # The legacy builder resolves the canonical package name itself; with no
    # config/project_root that is normalize_package_name(project_name).
    package_name = normalize_package_name(project_name)

    real = _python_layout_profile(
        project_name=project_name, source_dirs=None, test_dirs=None
    )
    shim = layout_profile_from_language_profile(profile, package_name=package_name)

    _assert_triple_identical(shim, real)
    # Spell out the expected concrete values too (defends the parametrization).
    assert shim.language == "python"
    assert shim.source_root == "src"
    assert shim.package_root == f"src/{package_name}"
    assert shim.test_root == "tests"


@pytest.mark.parametrize(
    "project_name",
    ["todo-cli", "calc-lib", "Some App", "x"],
)
def test_typescript_compat_is_byte_identical(
    registry: LanguageRegistry, project_name: str
) -> None:
    """TypeScript: shim triple == ``_typescript_layout_profile`` triple."""
    profile = registry.resolve("typescript")
    package_name = normalize_package_name(project_name)

    real = _typescript_layout_profile(
        project_name=project_name, source_dirs=None, test_dirs=None
    )
    shim = layout_profile_from_language_profile(profile, package_name=package_name)

    _assert_triple_identical(shim, real)
    assert shim.language == "typescript"
    assert shim.source_root == "src"
    # TS: package_root == source_root (path-relative layout, no named pkg subdir).
    assert shim.package_root == "src"
    assert shim.test_root == "tests"


def test_compat_typescript_package_root_equals_source_root(
    registry: LanguageRegistry,
) -> None:
    """Guard the TS-specific invariant the builder encodes (package_root==source_root)."""
    real = _typescript_layout_profile(
        project_name="demo", source_dirs=None, test_dirs=None
    )
    assert real.package_root == real.source_root  # builder contract
    shim = layout_profile_from_language_profile(
        registry.resolve("typescript"), package_name=normalize_package_name("demo")
    )
    assert shim.package_root == shim.source_root == real.source_root


def test_compat_go_has_no_single_root_raises(registry: LanguageRegistry) -> None:
    """Go (package_root.kind == none) must NOT yield a legacy source_root."""
    go = registry.resolve("go")
    with pytest.raises(UnsupportedLayoutShape):
        layout_profile_from_language_profile(go, package_name="itemapi")


# ---------------------------------------------------------------------------
# 2. PathPlanner: Go lands at repo root, NEVER under src/  (the C-Go fix)
# ---------------------------------------------------------------------------


def test_pathplanner_go_manifest_is_go_mod(registry: LanguageRegistry) -> None:
    planner = PathPlanner(registry.resolve("go"), {"module_path": "example.com/itemapi"})
    plan = planner.plan_output("manifest")
    assert plan.posix == "go.mod"
    assert plan.owner == "harness"


def test_pathplanner_go_entrypoint_is_cmd_server_main(
    registry: LanguageRegistry,
) -> None:
    planner = PathPlanner(registry.resolve("go"), {"module_path": "example.com/itemapi"})
    plan = planner.plan_output("entrypoint", name="server")
    assert plan.posix == "cmd/server/main.go"
    assert not plan.posix.startswith("src/")
    assert plan.source_set == "commands"
    assert plan.owner == "sut"


def test_pathplanner_go_internal_package_file(registry: LanguageRegistry) -> None:
    planner = PathPlanner(registry.resolve("go"))
    plan = planner.plan_output("internal_package_file", package="server", file="handler")
    assert plan.posix == "internal/server/handler.go"
    assert not plan.posix.startswith("src/")
    assert plan.source_set == "internal"


def test_pathplanner_go_no_planned_path_starts_with_src(
    registry: LanguageRegistry,
) -> None:
    """The load-bearing C-Go invariant: NOTHING Go plans may sit under src/."""
    planner = PathPlanner(registry.resolve("go"), {"module_path": "example.com/itemapi"})
    planned = [
        planner.plan_output("manifest"),
        planner.plan_output("entrypoint", name="server"),
        planner.plan_output("entrypoint", name="worker"),
        planner.plan_output("internal_package_file", package="store", file="db"),
        planner.plan_output("internal_package_file", package="api", file="routes"),
        planner.plan_output("colocated_test_file", package_dir="internal/store", name="db"),
        planner.plan_output("e2e_test_file", name="smoke"),
    ]
    for plan in planned:
        p = plan.posix
        assert not (p == "src" or p.startswith("src/") or "/src/" in p), (
            f"Go PathPlanner leaked a src/ path: {p}"
        )
    # spot-check a couple of the templated ones resolved fully (no leftover {..})
    by_role = {pl.role: pl.posix for pl in planned}
    assert by_role["colocated_test_file"] == "internal/store/db_test.go"
    assert by_role["e2e_test_file"] == "tests/e2e/smoke_test.go"


def test_pathplanner_go_forbidden_prefix_is_enforced(
    registry: LanguageRegistry,
) -> None:
    """Even a hand-forced src/ path is rejected by the planner's invariant."""
    planner = PathPlanner(registry.resolve("go"))
    # An internal file whose package tries to climb into src/ must be canonical-safe;
    # but a direct attempt to plan a src-rooted template is impossible via roles —
    # assert the guard catches a crafted forbidden path through _finalize.
    with pytest.raises(PathPlanError):
        planner._finalize(  # noqa: SLF001 - testing the invariant directly
            role="x", rel="src/cmd/server/main.go", owner="sut",
            source_set=None, test_set=None,
        )


# ---------------------------------------------------------------------------
# 2b. PathPlanner: python / typescript still match their src/ layout (no change)
# ---------------------------------------------------------------------------


def test_pathplanner_python_module_keeps_src_package_layout(
    registry: LanguageRegistry,
) -> None:
    planner = PathPlanner(registry.resolve("python"), {"package_name": "todo_cli"})
    plan = planner.plan_output("package_module", file="storage.py")
    assert plan.posix == "src/todo_cli/storage.py"
    assert plan.source_set == "package"


def test_pathplanner_python_test_path(registry: LanguageRegistry) -> None:
    planner = PathPlanner(registry.resolve("python"), {"package_name": "todo_cli"})
    plan = planner.plan_output("test", file="test_storage.py")
    assert plan.posix == "tests/test_storage.py"
    assert plan.test_set == "tests"


def test_pathplanner_typescript_module_keeps_src_layout(
    registry: LanguageRegistry,
) -> None:
    planner = PathPlanner(registry.resolve("typescript"))
    plan = planner.plan_output("package_module", file="index.ts")
    assert plan.posix == "src/index.ts"
    assert plan.source_set == "src"


def test_pathplanner_typescript_manifest(registry: LanguageRegistry) -> None:
    planner = PathPlanner(registry.resolve("typescript"))
    plan = planner.plan_output("manifest")
    assert plan.posix == "package.json"
    assert plan.owner == "harness"


def test_pathplanner_unknown_role_raises(registry: LanguageRegistry) -> None:
    planner = PathPlanner(registry.resolve("python"))
    with pytest.raises(PathPlanError):
        planner.plan_output("totally_made_up_role")


def test_pathplanner_unresolved_placeholder_raises(
    registry: LanguageRegistry,
) -> None:
    """A template needing {command_name} with no name= is a hard error, not a leftover."""
    planner = PathPlanner(registry.resolve("go"))
    with pytest.raises(PathPlanError):
        planner.plan_output("entrypoint")  # no name=
