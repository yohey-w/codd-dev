"""Tests for the C++ language profile (cpp.yaml), modeled on
``tests/languages/test_language_profiles.py``.

Asserts the profile loads, is auto-discovered + resolvable by id and alias, and
carries the load-bearing shape: include/+src/ source sets, tests/ test set, the
header/source file extensions, a composite ``configure``+``build`` implement-oracle
resolved by ``cpp-toolchain``, and a ctest verify whose report adapter is
``ctest-junit``. (It does NOT assert the language CONTRACT is complete — the
``ctest-junit`` / ``cpp-toolchain`` adapters are registered centrally by the
parent, not in this task.)
"""

from __future__ import annotations

import pytest

from codd.languages import (
    CommandSpec,
    LanguageProfile,
    LanguageRegistry,
    PackageRoot,
    SourceSet,
    TestSet,
    load_language_profile,
)
from codd.languages.registry import PROFILES_DIR


@pytest.fixture(scope="module")
def registry() -> LanguageRegistry:
    return LanguageRegistry()


# ── discovery / resolution ────────────────────────────────────────────────────


def test_cpp_profile_is_discovered(registry: LanguageRegistry) -> None:
    assert "cpp" in set(registry.ids())


def test_cpp_resolves_by_id(registry: LanguageRegistry) -> None:
    profile = registry.resolve("cpp")
    assert isinstance(profile, LanguageProfile)
    assert profile.identity.id == "cpp"


@pytest.mark.parametrize("alias", ["c++", "cxx", "cplusplus", "C++", "  Cxx  "])
def test_cpp_resolves_by_alias(registry: LanguageRegistry, alias: str) -> None:
    profile = registry.resolve(alias)
    assert profile.identity.id == "cpp"


# ── identity / strictness / extensions ────────────────────────────────────────


def test_cpp_is_strict(registry: LanguageRegistry) -> None:
    cpp = registry.resolve("cpp")
    assert cpp.identity.strictness == "strict"
    assert "c++" in cpp.identity.aliases


def test_cpp_file_extensions(registry: LanguageRegistry) -> None:
    cpp = registry.resolve("cpp")
    exts = set(cpp.identity.file_extensions)
    # both header and source variants must be present
    assert {".h", ".hpp", ".hh"} <= exts, exts
    assert {".cc", ".cpp", ".cxx"} <= exts, exts


# ── layout: include/ + src/ source sets, tests/ test set ──────────────────────


def test_cpp_module_root_and_manifest(registry: LanguageRegistry) -> None:
    cpp = registry.resolve("cpp")
    assert cpp.layout.module_root == "."
    assert cpp.toolchain is not None
    assert cpp.toolchain.manifest.path == "CMakeLists.txt"
    assert cpp.toolchain.manifest.format == "cmake"
    assert cpp.toolchain.manifest.required is True


def test_cpp_source_and_test_sets_shape(registry: LanguageRegistry) -> None:
    cpp = registry.resolve("cpp")

    src_by_id = {s.id: s for s in cpp.layout.source_sets}
    assert set(src_by_id) == {"src", "include"}
    assert src_by_id["src"].root == "src"
    assert "src/**/*.cpp" in src_by_id["src"].file_globs
    assert src_by_id["include"].root == "include"
    assert any(g.startswith("include/") for g in src_by_id["include"].file_globs)

    test_by_id = {t.id: t for t in cpp.layout.test_sets}
    assert set(test_by_id) == {"tests"}
    assert test_by_id["tests"].root == "tests"
    assert "tests/**/*.cpp" in test_by_id["tests"].file_globs


def test_cpp_package_root_is_none(registry: LanguageRegistry) -> None:
    cpp = registry.resolve("cpp")
    assert isinstance(cpp.layout.package_root, PackageRoot)
    assert cpp.layout.package_root.kind == "none"
    assert cpp.layout.package_root.path is None


def test_cpp_uses_src_layout_without_forbidden_prefix(registry: LanguageRegistry) -> None:
    """C++ legitimately uses src/ — the no-src/ invariant is GO-SPECIFIC.

    So cpp.yaml must NOT carry forbidden_generated_prefixes: ["src/"] (that would
    forbid its own source set).
    """
    cpp = registry.resolve("cpp")
    forbidden = list(
        cpp.extra.get("path_rules", {}).get("forbidden_generated_prefixes", [])
    )
    assert "src/" not in forbidden, forbidden
    # and src IS a declared source root
    assert "src" in {s.root for s in cpp.layout.source_sets}


# ── commands + implement-oracle ───────────────────────────────────────────────


def test_cpp_commands_use_cmake_ctest(registry: LanguageRegistry) -> None:
    cpp = registry.resolve("cpp")
    assert cpp.commands["configure"].argv[0] == "cmake"
    assert cpp.commands["build"].argv[:2] == ("cmake", "--build")
    assert cpp.commands["verify"].argv[0] == "ctest"
    # the oracle commands opt OUT of the materialize preflight
    assert cpp.commands["configure"].requires_materialized_deps is False
    assert cpp.commands["build"].requires_materialized_deps is False


def test_cpp_implement_oracle_is_composite_configure_then_build(registry: LanguageRegistry) -> None:
    cpp = registry.resolve("cpp")
    assert cpp.implement_oracle is not None
    assert cpp.implement_oracle.kind == "composite"
    assert cpp.implement_oracle.adapter == "cpp-toolchain"
    assert [s.command for s in cpp.implement_oracle.steps] == ["configure", "build"]


def test_cpp_verify_report_adapter_is_ctest_junit(registry: LanguageRegistry) -> None:
    cpp = registry.resolve("cpp")
    # the verify COMMAND's report
    assert cpp.commands["verify"].report is not None
    assert cpp.commands["verify"].report.adapter == "ctest-junit"
    assert cpp.commands["verify"].report.format == "ctest-junit"
    # the top-level verify: block's report
    assert cpp.verify is not None
    assert cpp.verify.report is not None
    assert cpp.verify.report.adapter == "ctest-junit"
    # the tests: runner_report_adapter
    assert cpp.tests is not None
    assert cpp.tests.runner_report_adapter == "ctest-junit"


# ── model round-trip via the loader ───────────────────────────────────────────


def test_cpp_loader_produces_expected_dataclass_shapes() -> None:
    profile = load_language_profile(PROFILES_DIR / "cpp.yaml")
    assert isinstance(profile, LanguageProfile)
    assert all(isinstance(s, SourceSet) for s in profile.layout.source_sets)
    assert all(isinstance(t, TestSet) for t in profile.layout.test_sets)
    assert all(isinstance(c, CommandSpec) for c in profile.commands.values())
    assert not hasattr(profile.layout, "source_root")


def test_cpp_extra_preserves_path_rules() -> None:
    cpp = load_language_profile(PROFILES_DIR / "cpp.yaml")
    assert "path_rules" in cpp.extra
    # implement_oracle is a first-class field, NOT left in .extra
    assert "implement_oracle" not in cpp.extra
    assert cpp.raw["id"] == "cpp"
