"""Tests for the bundled C# language profile (``profiles/csharp.yaml``).

Asserts the profile loads, is auto-discovered + resolvable by id AND alias via the
:class:`LanguageRegistry`, declares the expected source/test sets + ``package_root``
kind, and wires its implement-oracle (composite / ``dotnet-toolchain`` / step
``build``) + verify report adapter (``dotnet-trx``). Mirrors the assertion patterns in
``tests/languages/test_language_profiles.py`` (the Go/TS/Python profile tests).

NOTE: this does NOT assert the language CONTRACT is complete — the ``dotnet-trx``
runner-report adapter + the ``dotnet-toolchain`` oracle adapter are registered
centrally by the parent, not in this task.
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


# ── discovery / resolution ────────────────────────────────────────────────────


def test_csharp_is_discovered(registry: LanguageRegistry) -> None:
    assert "csharp" in set(registry.ids())


def test_resolve_by_id(registry: LanguageRegistry) -> None:
    profile = registry.resolve("csharp")
    assert isinstance(profile, LanguageProfile)
    assert profile.identity.id == "csharp"


@pytest.mark.parametrize("alias", ["dotnet", "c#", "C#", "cs", "  DotNet  "])
def test_resolve_by_alias(registry: LanguageRegistry, alias: str) -> None:
    """Case-insensitive + whitespace-tolerant alias resolution (registry behavior)."""
    profile = registry.resolve(alias)
    assert profile.identity.id == "csharp"


# ── identity / layout ──────────────────────────────────────────────────────────


def test_csharp_identity(registry: LanguageRegistry) -> None:
    cs = registry.resolve("csharp")
    assert cs.identity.display_name == "C#"
    assert cs.identity.strictness == "strict"
    assert ".cs" in cs.identity.file_extensions
    assert "dotnet" in cs.identity.aliases


def test_csharp_module_root_is_dot(registry: LanguageRegistry) -> None:
    cs = registry.resolve("csharp")
    assert cs.layout.module_root == "."


def test_csharp_package_root_is_none(registry: LanguageRegistry) -> None:
    """C# has NO single package root in the Go sense → kind 'none'."""
    cs = registry.resolve("csharp")
    assert isinstance(cs.layout.package_root, PackageRoot)
    assert cs.layout.package_root.kind == "none"
    assert cs.layout.package_root.path is None


def test_csharp_source_and_test_sets_shape(registry: LanguageRegistry) -> None:
    cs = registry.resolve("csharp")

    src_by_id = {s.id: s for s in cs.layout.source_sets}
    assert set(src_by_id) == {"src"}
    assert src_by_id["src"].root == "src"
    assert src_by_id["src"].file_globs == ("src/**/*.cs",)

    test_by_id = {t.id: t for t in cs.layout.test_sets}
    assert set(test_by_id) == {"tests"}
    assert test_by_id["tests"].root == "tests"
    assert test_by_id["tests"].file_globs == ("tests/**/*.cs",)


def test_csharp_does_not_borrow_go_no_src_invariant(registry: LanguageRegistry) -> None:
    """The Go-specific no-src/ invariant must NOT leak into the C# profile.

    A `src` root is conventional in C#; the profile must NOT declare
    forbidden_generated_prefixes ["src/"].
    """
    cs = registry.resolve("csharp")
    forbidden = cs.extra.get("path_rules", {}).get("forbidden_generated_prefixes", [])
    assert "src/" not in list(forbidden)


# ── toolchain / commands ───────────────────────────────────────────────────────


def test_csharp_toolchain_manifest(registry: LanguageRegistry) -> None:
    cs = registry.resolve("csharp")
    assert cs.toolchain is not None
    assert cs.toolchain.manifest.format == "msbuild-csproj"
    assert cs.toolchain.manifest.required is True


def test_csharp_commands_use_dotnet(registry: LanguageRegistry) -> None:
    cs = registry.resolve("csharp")
    for name in ("build", "verify"):
        cmd = cs.commands[name]
        assert isinstance(cmd, CommandSpec)
        assert cmd.argv[0] == "dotnet"
    assert cs.commands["build"].argv == ("dotnet", "build", "-c", "Release")
    # the oracle's build step opts OUT of the install preflight (dotnet restores itself)
    assert cs.commands["build"].requires_materialized_deps is False


# ── implement-oracle + verify report wiring ────────────────────────────────────


def test_csharp_implement_oracle(registry: LanguageRegistry) -> None:
    cs = registry.resolve("csharp")
    assert cs.implement_oracle is not None
    assert cs.implement_oracle.kind == "composite"
    assert cs.implement_oracle.adapter == "dotnet-toolchain"
    assert [s.command for s in cs.implement_oracle.steps] == ["build"]


def test_csharp_verify_report_adapter_is_dotnet_trx(registry: LanguageRegistry) -> None:
    cs = registry.resolve("csharp")
    # command-level report block
    verify_cmd = cs.commands["verify"]
    assert verify_cmd.report is not None
    assert verify_cmd.report.adapter == "dotnet-trx"
    assert verify_cmd.report.format == "dotnet-trx"
    # top-level verify block
    assert cs.verify is not None
    assert cs.verify.report is not None
    assert cs.verify.report.adapter == "dotnet-trx"
    # tests block runner_report_adapter
    assert cs.tests is not None
    assert cs.tests.runner_report_adapter == "dotnet-trx"


# ── bundled-file sanity ────────────────────────────────────────────────────────


def test_csharp_profile_loads_from_disk() -> None:
    profile = load_language_profile(PROFILES_DIR / "csharp.yaml")
    assert isinstance(profile, LanguageProfile)
    assert profile.identity.id == "csharp"
    assert profile.layout.source_sets  # declares >=1 source set
