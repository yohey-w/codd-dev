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

from pathlib import Path

import pytest

from codd.coverage_execution_coherence import coherence_gate_applies
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
from codd.project_types import resolve_layout_profile, scaffold_layout
from codd.vb_marker_authenticity import CppTestBlockProfile


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


# ── greenfield ② OPT-IN synthesis + scaffold ──────────────────────────────────
#
# This file (as of the greenfield_synthesis opt-in) previously covered ONLY the
# raw YAML shape — nothing about the synthesizer/gate/scaffold machinery the
# opt-in key actually activates. Mirrors the vertical-slice pattern used for
# java.yaml / csharp.yaml (tests/test_java_greenfield_vertical_slice.py,
# tests/test_csharp_greenfield_vertical_slice.py), scoped down to a focused set
# of assertions for the C++ increment rather than a full new vertical-slice file.


def test_cpp_greenfield_synthesis_opted_in() -> None:
    """cpp.yaml declares greenfield_synthesis: true — the SAME data-driven opt-in
    key java.yaml (commit 2736959) and csharp.yaml use; the language-free core
    authorizes the generic LayoutProfile synthesizer + generic-template scaffolder
    by the PRESENCE of this key alone, never a language-name branch."""
    cpp = load_language_profile(PROFILES_DIR / "cpp.yaml")
    assert cpp.raw.get("greenfield_synthesis") is True


def test_cpp_coherence_gate_applies_after_opt_in() -> None:
    """resolve_layout_profile('cpp') now synthesizes a real LayoutProfile (was
    None before the opt-in, a strict NO-OP), and the coverage-execution-coherence
    gate — previously a silent NO-OP for C++ — now applies. This is the load-
    bearing empirical claim the greenfield_synthesis opt-in makes for every
    downstream anti-false-green gate (coverage-execution-coherence here; VB
    marker-authenticity via test_block_profile below)."""
    profile = resolve_layout_profile(language="cpp", project_name="todo-cli")
    assert profile is not None
    assert coherence_gate_applies(profile) is True


def test_cpp_test_block_profile_resolves_to_cpp_parser() -> None:
    """LayoutProfile.test_block_profile() resolves to the ALREADY-EXISTING
    CppTestBlockProfile (codd/vb_marker_authenticity.py) for a synthesized cpp
    layout profile — no new adapter class was needed, only the opt-in flag.
    Without this wiring the VB marker-authenticity gate would silently degrade to
    its language-agnostic stage-1 (orphan-marker) check only, for every C++
    project (the same silent-degrade risk java's analogous test guards against)."""
    profile = resolve_layout_profile(language="cpp", project_name="todo-cli")
    assert profile is not None
    assert isinstance(profile.test_block_profile(), CppTestBlockProfile)


def test_cpp_scaffolds_cmakelists_with_defaults_substituted(tmp_path: Path) -> None:
    """scaffold_layout writes the SINGLE CMakeLists.txt at the repo root from
    cpp.yaml's template — cpp's package_root.kind: none (like Java) means there is
    no C#-style nested lib/test project split — create-only / idempotent,
    substituting {package_name} + scaffold.defaults (cpp_standard,
    googletest_version). Mirrors test_java_scaffolds_pom_with_defaults_substituted."""
    profile = resolve_layout_profile(language="cpp", project_name="todo-cli")
    assert profile is not None

    result = scaffold_layout(tmp_path, profile)
    cmakelists = tmp_path / "CMakeLists.txt"
    assert cmakelists.is_file()
    assert {"CMakeLists.txt"} <= set(result.created)

    body = cmakelists.read_text(encoding="utf-8")

    # scaffold.defaults + the resolved package_name substituted.
    assert "project(todo_cli LANGUAGES CXX)" in body
    assert "set(CMAKE_CXX_STANDARD 20)" in body  # cpp_standard default
    assert "GIT_TAG v1.17.0" in body  # googletest_version default

    # (a) no UNSUBSTITUTED template var leaked into the rendered file.
    for var in ("{package_name}", "{cpp_standard}", "{googletest_version}"):
        assert var not in body, var

    # (b) GoogleTest FetchContent wiring is present — the modern, no-system-
    # install approach; find_package(GTest) would require a pre-installed GTest
    # and silently break any environment lacking one, so it must NOT be USED (an
    # executable, non-comment line) — the template's own explanatory comment is
    # allowed to just NAME find_package(GTest) when saying why it's avoided, so
    # the check below is line-based and skips comment lines, not a raw substring
    # match on the whole rendered file.
    assert "include(FetchContent)" in body
    assert "FetchContent_Declare(" in body
    assert "FetchContent_MakeAvailable(googletest)" in body
    executable_lines = [
        line for line in body.splitlines() if line.strip() and not line.strip().startswith("#")
    ]
    assert not any("find_package(GTest" in line for line in executable_lines)
    assert "include(GoogleTest)" in body
    assert "gtest_discover_tests(" in body

    # idempotent: a second call creates nothing, skips the existing file (the
    # SAME create-only/non-clobber contract java/csharp scaffolding relies on).
    result2 = scaffold_layout(tmp_path, profile)
    assert result2.created == ()
    assert "CMakeLists.txt" in result2.skipped

    # non-clobber: an authored file is left byte-for-byte.
    cmakelists.write_text("AUTHORED", encoding="utf-8")
    scaffold_layout(tmp_path, profile)
    assert cmakelists.read_text(encoding="utf-8") == "AUTHORED"


def test_cpp_googletest_is_test_scope_only_no_leak_into_library_link(
    tmp_path: Path,
) -> None:
    """ANTI-FALSE-GREEN (C++'s analogue of
    test_java_pom_test_dependency_is_scoped_test_no_leak_into_compile_runtime):
    GoogleTest must be a link requirement of the TEST EXECUTABLE ONLY, never of
    the library target — C++ has no C#-style physical lib/test project split, so
    purity here is enforced by the LINK GRAPH itself (mirrors Maven's
    <scope>test</scope> exclusion for JUnit5)."""
    profile = resolve_layout_profile(language="cpp", project_name="todo-cli")
    assert profile is not None
    scaffold_layout(tmp_path, profile)
    body = (tmp_path / "CMakeLists.txt").read_text(encoding="utf-8")

    # Isolate the library target's OWN declaration block and prove nothing in it
    # references GTest/gtest.
    lib_start = body.index("add_library(")
    lib_end = body.index("endif()", lib_start)
    lib_block = body[lib_start:lib_end]
    assert "GTest" not in lib_block
    assert "gtest" not in lib_block

    # The test executable, in contrast, DOES link GTest::gtest_main.
    assert "add_executable(todo_cli_tests" in body
    exe_start = body.index("add_executable(todo_cli_tests")
    exe_block = body[exe_start:]
    assert "target_link_libraries(todo_cli_tests PRIVATE GTest::gtest_main)" in exe_block


def test_cpp_verify_argv_output_junit_path_matches_report_path(
    registry: LanguageRegistry,
) -> None:
    """Regression test for a pre-existing bug found (by an empirical dry run with
    real ctest 3.28.3) and fixed alongside the greenfield_synthesis opt-in:
    ``ctest --test-dir build`` chdirs INTO build/ before resolving ITS OWN
    relative-path flags, so --output-junit's value must be relative to build/
    (bare "ctest-junit.xml"), NOT prefixed with "build/" again — a "build/"
    prefix there silently writes the report to build/build/ctest-junit.xml, which
    report.path (resolved relative to {module_root}) never finds, so every C++
    verify campaign would CampaignError("no report") regardless of whether the
    tests themselves passed."""
    cpp = registry.resolve("cpp")
    assert cpp.commands["verify"].argv == (
        "ctest", "--test-dir", "build", "--output-junit", "ctest-junit.xml",
    )
    assert cpp.commands["verify"].report is not None
    assert cpp.commands["verify"].report.path == "build/ctest-junit.xml"


def test_cpp_scaffold_test_target_has_repo_root_include_dir(tmp_path: Path) -> None:
    """The TEST target declares the repo root as a PRIVATE include dir, so BOTH
    intra-tree quoted-include conventions resolve — file-relative
    (``"helpers/x.h"``) via the compiler's local lookup AND root-relative
    (``"tests/e2e/helpers/x.h"``). Independently-generated test files legitimately
    disagree on the convention (cpp4 exprcalc dogfood, 2026-07-11: 3 files used
    one form, 2 the other → module_resolution_error) — the scaffolded build makes
    the ambiguity CLASS moot instead of policing the model's choice. PRIVATE: the
    repo root must never leak into the library's usage requirements."""
    profile = resolve_layout_profile(language="cpp", project_name="todo-cli")
    assert profile is not None
    scaffold_layout(tmp_path, profile)
    body = (tmp_path / "CMakeLists.txt").read_text(encoding="utf-8")
    assert (
        'target_include_directories(todo_cli_tests PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}")'
        in body
    ), body
