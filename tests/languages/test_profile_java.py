"""Tests for the Java language profile (``codd/languages/profiles/java.yaml``),
modeled on the Go cases in ``tests/languages/test_language_profiles.py``.

Asserts the profile loads, is discovered + resolvable by the registry (by id and
by alias), carries the expected source/test sets + ``pom.xml`` manifest, declares a
``composite`` implement-oracle (adapter ``java-toolchain``) with a single
``compile`` step, and wires the ``surefire-xml`` verify report adapter.

NOTE: this does NOT assert the language CONTRACT is complete — the ``surefire-xml``
runner-report adapter is registered centrally by the parent, not here.
"""

from __future__ import annotations

import pytest

from codd.languages import (
    CommandSpec,
    LanguageProfile,
    LanguageRegistry,
    PackageRoot,
    load_language_profile,
)
from codd.languages.registry import PROFILES_DIR


@pytest.fixture(scope="module")
def registry() -> LanguageRegistry:
    return LanguageRegistry()


def _java() -> LanguageProfile:
    return load_language_profile(PROFILES_DIR / "java.yaml")


# ── discovery / resolution ───────────────────────────────────────────────────


def test_java_is_discovered(registry: LanguageRegistry) -> None:
    assert "java" in set(registry.ids())


def test_java_resolves_by_id(registry: LanguageRegistry) -> None:
    profile = registry.resolve("java")
    assert isinstance(profile, LanguageProfile)
    assert profile.identity.id == "java"


def test_java_resolves_by_alias(registry: LanguageRegistry) -> None:
    profile = registry.resolve("jvm")
    assert profile.identity.id == "java"
    assert "jvm" in profile.identity.aliases


# ── identity / layout ────────────────────────────────────────────────────────


def test_java_identity(registry: LanguageRegistry) -> None:
    java = registry.resolve("java")
    assert java.identity.display_name == "Java"
    assert java.identity.strictness == "strict"
    assert ".java" in java.identity.file_extensions


def test_java_manifest_is_pom_at_module_root(registry: LanguageRegistry) -> None:
    java = registry.resolve("java")
    assert java.layout.module_root == "."
    assert java.toolchain is not None
    assert java.toolchain.manifest.path == "pom.xml"
    assert java.toolchain.manifest.format == "pom.xml"
    assert java.toolchain.manifest.required is True


def test_java_source_and_test_sets_shape(registry: LanguageRegistry) -> None:
    java = registry.resolve("java")

    src_by_id = {s.id: s for s in java.layout.source_sets}
    assert set(src_by_id) == {"main"}
    assert src_by_id["main"].root == "src/main/java"
    assert src_by_id["main"].file_globs == ("src/main/java/**/*.java",)

    test_by_id = {t.id: t for t in java.layout.test_sets}
    assert set(test_by_id) == {"unit"}
    assert test_by_id["unit"].root == "src/test/java"
    assert test_by_id["unit"].file_globs == ("src/test/java/**/*.java",)


def test_java_package_root_is_none(registry: LanguageRegistry) -> None:
    java = registry.resolve("java")
    assert isinstance(java.layout.package_root, PackageRoot)
    assert java.layout.package_root.kind == "none"
    assert java.layout.package_root.path is None


# ── implement oracle (Contract Kernel §1) ────────────────────────────────────


def test_java_implement_oracle_is_composite_compile(registry: LanguageRegistry) -> None:
    java = registry.resolve("java")
    assert java.implement_oracle is not None
    assert java.implement_oracle.kind == "composite"
    assert java.implement_oracle.adapter == "java-toolchain"
    steps = [s.command for s in java.implement_oracle.steps]
    assert steps == ["compile"]
    # The referenced command id exists (the loader enforces this; assert anyway).
    assert "compile" in java.commands


def test_java_compile_command_shape(registry: LanguageRegistry) -> None:
    java = registry.resolve("java")
    compile_cmd = java.commands["compile"]
    assert isinstance(compile_cmd, CommandSpec)
    assert compile_cmd.argv == ("mvn", "-q", "-e", "compile")
    assert compile_cmd.cwd == "{module_root}"
    # The oracle command opts OUT of the install preflight (like Go's typecheck).
    assert compile_cmd.requires_materialized_deps is False


# ── verify report adapter ────────────────────────────────────────────────────


def test_java_verify_report_adapter_is_surefire(registry: LanguageRegistry) -> None:
    java = registry.resolve("java")
    assert java.commands["verify"].argv == ("mvn", "-q", "test")
    assert java.commands["verify"].report is not None
    assert java.commands["verify"].report.adapter == "surefire-xml"
    # the top-level verify block + tests block agree on the adapter id
    assert java.verify is not None
    assert java.verify.report is not None
    assert java.verify.report.adapter == "surefire-xml"
    assert java.tests is not None
    assert java.tests.runner_report_adapter == "surefire-xml"


# ── round-trip / loadability ─────────────────────────────────────────────────


def test_java_profile_loads_standalone() -> None:
    java = _java()
    assert java.identity.id == "java"
    assert java.layout.source_sets  # >=1 source set
    assert java.implement_oracle is not None
