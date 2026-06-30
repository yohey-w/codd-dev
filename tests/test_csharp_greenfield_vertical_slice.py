"""Greenfield ② vertical slice for the first non-Python stack: C# (csharp).

RED-BEFORE-GREEN. Before this increment ``resolve_layout_profile('csharp')`` returned
``None`` (no legacy builder) so every greenfield deep gate (coverage-execution-campaign,
import-coherence, scaffold) was a strict NO-OP for C#. This module pins the OPT-IN,
language-free synthesis that de-NO-OPs the C# path WITHOUT a per-language code branch:

  (b) the generic LayoutProfile *synthesizer* (``resolve_layout_profile`` fallback),
  (c) the generic-template *scaffolder* (``scaffold_layout`` 3rd branch),
      + ``harness_owned_scaffold_paths``取り込み,
      + ``run_verify_campaign`` argv-form generalization,

all gated by a single data-driven opt-in key (``greenfield_synthesis: true``) added ONLY
to csharp.yaml. Go (whose ``scaffold.adapter`` is generic-template too but which does NOT
opt in) MUST stay a strict NO-OP — the opt-in exclusion is asserted here too.

These are unit/integration tests; a real ``codd greenfield --language csharp`` AI run is a
SEPARATE next step (the argv campaign is exercised with a python stub that emits a TRX, no
``dotnet`` needed).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from codd.coverage_execution_coherence import (
    coherence_gate_applies,
    resolve_runner_report_adapter,
    run_verify_campaign,
    supported_runner_report_formats,
)
from codd.implement_oracle import resolve_implement_oracle
from codd.import_coherence import check_import_coherence
from codd.languages.adapters.runner_report import DotnetTrxReportAdapter
from codd.languages.profile import (
    DependencyIntegrityFile,
    ManifestSpec,
    ToolchainSpec,
)
from codd.project_types import (
    LayoutProfile,
    VerifyCampaignSpec,
    _synthesize_toolchain_dependencies,
    resolve_layout_profile,
    scaffold_layout,
    synthesize_implement_oracle_spec,
)

_NS = "http://microsoft.com/schemas/VisualStudio/TeamTest/2010"


# ── (1) generic LayoutProfile synthesizer ───────────────────────────────────────


def test_csharp_synthesizes_layout_profile() -> None:
    """resolve_layout_profile('csharp') synthesizes a profile (was None → NO-OP).

    Fields follow the design: source_root/test_root from the YAML layout sets;
    package_root == source_root for a ``kind: none`` layout; requires_*_init=False and a
    non-"package_absolute" policy (so the Python import-coherence checks stay NO-OPs);
    the implement-oracle + verify-campaign are SET (not None).
    """
    profile = resolve_layout_profile(language="csharp", project_name="todo-cli")
    assert profile is not None, "csharp layout profile must be synthesized, not None"
    assert profile.language == "csharp"
    assert profile.source_root == "src"
    assert profile.test_root == "tests"
    # kind: none → no named-package subdir.
    assert profile.package_root == "src"
    assert profile.requires_package_init is False
    assert profile.requires_test_init is False
    assert profile.test_import_policy != "package_absolute"
    assert profile.install_mode == "none"
    # the implement-oracle + verify-campaign are wired (not the silent NO-OP None).
    assert profile.implement_oracle is not None
    assert profile.verify_campaign is not None


def test_csharp_synthesis_is_alias_resolvable() -> None:
    """The opt-in resolves through the registry alias set too (``dotnet``/``c#``)."""
    for name in ("dotnet", "c#", "CS"):
        assert resolve_layout_profile(language=name, project_name="x") is not None, name


# ── (2) oracle non-regression (must NOT become None) ────────────────────────────


def test_csharp_implement_oracle_not_none_no_regression(tmp_path: Path) -> None:
    """resolve_implement_oracle('csharp') stays non-None after the layout profile exists.

    Before, csharp reached the oracle via ``_resolve_registry_oracle`` (legacy profile
    None). Now it reaches it via the synthesized profile's ``implement_oracle``. Both MUST
    yield the SAME composite/``csharp-composite`` spec (the shared-helper drift guard).
    """
    resolved = resolve_implement_oracle(tmp_path, language="csharp", project_name="todo")
    assert resolved is not None, "the csharp implement-oracle must not regress to None"
    _profile, spec = resolved
    assert spec.kind == "composite"
    assert spec.command == "csharp-composite"


def test_shared_oracle_spec_helper_matches_registry_path() -> None:
    """The shared helper produces the SAME spec the registry path used (drift guard)."""
    from codd.languages import default_registry

    lang_profile = default_registry.resolve("csharp")
    spec = synthesize_implement_oracle_spec(lang_profile)
    assert spec is not None
    assert (spec.kind, spec.command) == ("composite", "csharp-composite")
    assert spec.scope.require_source_root is True
    assert spec.scope.require_test_root is False
    assert spec.requires_node_install is False


# ── (3) dotnet-trx runner-report adapter resolution (pin) ───────────────────────


def test_dotnet_trx_runner_report_adapter_resolves() -> None:
    assert isinstance(resolve_runner_report_adapter("dotnet-trx"), DotnetTrxReportAdapter)
    assert "dotnet-trx" in set(supported_runner_report_formats())


# ── coverage-execution-coherence gate APPLIES for csharp (NO-OP脱却) ─────────────


def test_csharp_coverage_gate_applies_not_noop() -> None:
    """The coverage-execution coherence gate is APPLICABLE for the synthesized csharp
    profile (it was a strict NO-OP before — no campaign → not applicable), and its
    runner-report adapter resolves to the dotnet-trx parser."""
    profile = resolve_layout_profile(language="csharp", project_name="todo-cli")
    assert profile is not None
    assert coherence_gate_applies(profile) is True
    assert isinstance(profile.runner_report_adapter(), DotnetTrxReportAdapter)
    campaign = profile.verify_campaign
    assert campaign is not None
    assert campaign.report_format == "dotnet-trx"
    # argv form (design A): --logger emits the TRX; --results-directory lands the SINGLE
    # .trx at the ROOT-level TestResults/ where run_verify_campaign reads it. (A solution
    # `dotnet test` otherwise writes a per-test-project TestResults/ — empirically the TRX
    # would land at tests/<pkg>.Tests/TestResults/, NOT the declared report_relpath.)
    assert campaign.command_argv == (
        "dotnet", "test", "--logger", "trx;LogFileName=test.trx",
        "--results-directory", "TestResults",
    )
    assert campaign.report_relpath == "TestResults/test.trx"


# ── lock = honest NO-OP (data-driven) ───────────────────────────────────────────


def test_csharp_toolchain_dependencies_is_none() -> None:
    """C# declares no lockfile → toolchain_dependencies None (honest NO-OP, Python-like)."""
    profile = resolve_layout_profile(language="csharp", project_name="todo-cli")
    assert profile is not None
    assert profile.toolchain_dependencies is None


def test_synthesize_toolchain_dependencies_lock_branch_is_data_driven() -> None:
    """A stack that DOES declare a ``kind: lock`` integrity file synthesizes a profile —
    proving the None for csharp is data-driven (lock absence), not a language-name branch."""
    toolchain = ToolchainSpec(
        manifest=ManifestSpec(path="Cargo.toml", format="cargo"),
        dependency_integrity_files=(DependencyIntegrityFile(path="Cargo.lock", kind="lock"),),
        package_manager={
            "reconcile_command": {"argv": ["cargo", "generate-lockfile"]},
            "materialize_command": {"argv": ["cargo", "fetch"]},
        },
    )
    synthetic = SimpleNamespace(toolchain=toolchain)
    result = _synthesize_toolchain_dependencies(synthetic)
    assert result is not None
    assert result.manifest_filename == "Cargo.toml"
    assert result.lock_filenames == ("Cargo.lock",)
    assert result.lock_refresh_command == "cargo generate-lockfile"
    # ecosystem-specific npm digest inputs default EMPTY (no npm literals leaked).
    assert result.config_filenames == ()
    assert result.package_manager_version_command is None


# ── (4) generic-template scaffolder → IDIOMATIC C# multi-project (Option C) ──────


def test_csharp_scaffolds_idiomatic_multi_project(tmp_path: Path) -> None:
    """scaffold_layout writes the IDIOMATIC C# topology from the YAML templates: a 0-pkg
    LIBRARY project under ``src/{pkg}/``, a SEPARATE xunit TEST project under
    ``tests/{pkg}.Tests/`` (ProjectReference back to the lib), and a root ``.sln`` that
    ties them — create-only / idempotent / non-clobber, substituting {package_name} +
    scaffold.defaults (target_framework, package versions)."""
    profile = resolve_layout_profile(language="csharp", project_name="todo-cli")
    assert profile is not None

    result = scaffold_layout(tmp_path, profile)
    lib = tmp_path / "src" / "todo_cli" / "todo_cli.csproj"
    test = tmp_path / "tests" / "todo_cli.Tests" / "todo_cli.Tests.csproj"
    sln = tmp_path / "todo_cli.sln"
    assert lib.is_file() and test.is_file() and sln.is_file()
    assert {
        "src/todo_cli/todo_cli.csproj",
        "tests/todo_cli.Tests/todo_cli.Tests.csproj",
        "todo_cli.sln",
    } <= set(result.created)

    # scaffold.defaults substituted; no UNSUBSTITUTED template var leaked. (The .sln
    # legitimately contains GUID braces, so we assert on the actual {var} tokens — never
    # on the mere presence of a brace, which would false-RED on a valid GUID.)
    assert "<TargetFramework>net8.0</TargetFramework>" in lib.read_text(encoding="utf-8")
    for f in (lib, test, sln):
        body = f.read_text(encoding="utf-8")
        for var in (
            "{package_name}", "{target_framework}", "{test_sdk_version}",
            "{xunit_version}", "{xunit_runner_version}",
        ):
            assert var not in body, (f.name, var)

    # idempotent: a second call creates nothing, skips the existing files.
    result2 = scaffold_layout(tmp_path, profile)
    assert result2.created == ()
    assert "src/todo_cli/todo_cli.csproj" in result2.skipped

    # non-clobber: an authored file is left byte-for-byte.
    lib.write_text("AUTHORED", encoding="utf-8")
    scaffold_layout(tmp_path, profile)
    assert lib.read_text(encoding="utf-8") == "AUTHORED"


def test_csharp_library_deliverable_is_pure_no_test_packages(tmp_path: Path) -> None:
    """ANTI-FALSE-GREEN (the load-bearing point of Option C): the LIBRARY project — the
    actual deliverable — carries ZERO third-party/test PackageReferences. The single-
    project model (one .csproj that builds+tests green but bakes xunit into the shipped
    assembly's deps.json) is a FALSE GREEN; this pins the STRUCTURAL purity that forbids it.
    A regression that re-collapses the test packages into the lib .csproj turns this RED."""
    profile = resolve_layout_profile(language="csharp", project_name="todo-cli")
    assert profile is not None
    scaffold_layout(tmp_path, profile)

    lib_text = (tmp_path / "src" / "todo_cli" / "todo_cli.csproj").read_text(encoding="utf-8")
    assert "PackageReference" not in lib_text, "lib deliverable must have NO PackageReference"
    assert "xunit" not in lib_text.lower(), "lib deliverable must not reference xunit"
    assert "Test.Sdk" not in lib_text, "lib deliverable must not reference the test SDK"

    # the test deps + the source<-test linkage live ONLY in the separate test project.
    test_text = (
        tmp_path / "tests" / "todo_cli.Tests" / "todo_cli.Tests.csproj"
    ).read_text(encoding="utf-8")
    assert "xunit" in test_text.lower()
    assert "Microsoft.NET.Test.Sdk" in test_text
    assert "xunit.runner.visualstudio" in test_text
    assert '<ProjectReference Include="../../src/todo_cli/todo_cli.csproj" />' in test_text


def test_csharp_solution_references_both_projects(tmp_path: Path) -> None:
    """The root .sln ties the lib + test projects together so a single root ``dotnet test``
    resolves the whole surface. GUID braces survive the REPLACE-based substitution
    (``str.format`` would raise on them) — the reason the scaffolder is replace-based."""
    profile = resolve_layout_profile(language="csharp", project_name="todo-cli")
    assert profile is not None
    scaffold_layout(tmp_path, profile)
    sln = (tmp_path / "todo_cli.sln").read_text(encoding="utf-8")
    assert "src/todo_cli/todo_cli.csproj" in sln
    assert "tests/todo_cli.Tests/todo_cli.Tests.csproj" in sln
    # the .NET-SDK project-type GUID is intact and exactly the two projects are declared.
    assert "{9A19103F-16F7-4668-BE54-9A1E7A4F7556}" in sln
    assert sln.count("Project(") == 2


# ── (5) import-coherence does NOT false-RED for csharp ──────────────────────────


def test_csharp_import_coherence_no_false_red(tmp_path: Path) -> None:
    """A realistic C# project passes import-coherence (the Python missing-__init__ /
    bare-basename / manifest checks must be strict NO-OPs for a kind:none layout)."""
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "Calculator.cs").write_text(
        "namespace App;\npublic class Calculator { public int Add(int a, int b) => a + b; }\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "CalculatorTests.cs").write_text(
        "using Xunit;\nusing App;\npublic class CalculatorTests "
        "{ [Fact] public void Adds() { Assert.Equal(3, new Calculator().Add(1, 2)); } }\n",
        encoding="utf-8",
    )
    (tmp_path / "todo_cli.csproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"></Project>\n', encoding="utf-8"
    )
    result = check_import_coherence(tmp_path, language="csharp", project_name="todo-cli")
    assert result.passed is True, [f.kind for f in result.findings]
    assert result.findings == []


# ── (6) harness_owned_scaffold_paths includes the csproj ────────────────────────


def test_csharp_harness_owned_scaffold_paths_includes_all_three() -> None:
    """The orphan-gate / write-fence must recognise ALL THREE scaffolded files (lib csproj,
    test csproj, .sln) as harness-owned — never unowned orphans, never reverted by the
    scoped-rerun write-fence."""
    profile = resolve_layout_profile(language="csharp", project_name="todo-cli")
    assert profile is not None
    owned = profile.harness_owned_scaffold_paths()
    assert "src/todo_cli/todo_cli.csproj" in owned
    assert "tests/todo_cli.Tests/todo_cli.Tests.csproj" in owned
    assert "todo_cli.sln" in owned


# ── (7) Go exclusion: opt-out stays a strict NO-OP ──────────────────────────────


def test_go_excluded_resolve_layout_profile_still_none() -> None:
    """Go does NOT opt in → resolve_layout_profile('go') stays None (no synthesis)."""
    assert resolve_layout_profile(language="go", project_name="m") is None
    assert resolve_layout_profile(language="golang", project_name="m") is None


def test_go_excluded_scaffold_is_noop(tmp_path: Path) -> None:
    """test_unknown_stack_is_noop preserved: a Go LayoutProfile scaffolds NOTHING (its
    scaffold.adapter is generic-template too, but Go has no opt-in key)."""
    go_profile = LayoutProfile(
        language="go",
        package_name="x",
        source_root="src",
        package_root="src/x",
        test_root="tests",
    )
    result = scaffold_layout(tmp_path, go_profile)
    assert result.created == ()
    assert list(tmp_path.iterdir()) == []


# ── run_verify_campaign argv-form generalization (design A) ──────────────────────


def _minimal_trx() -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<TestRun xmlns="{_NS}">\n'
        f"  <Results>\n"
        f'    <UnitTestResult testId="g1" testName="CalculatorTests.Adds" outcome="Passed" />\n'
        f"  </Results>\n"
        f"  <TestDefinitions>\n"
        f'    <UnitTest id="g1">\n'
        f'      <TestMethod className="App.Tests.CalculatorTests" name="Adds" />\n'
        f"    </UnitTest>\n"
        f"  </TestDefinitions>\n"
        f"</TestRun>\n"
    )


def test_run_verify_campaign_argv_form_parses_single_file_trx(tmp_path: Path) -> None:
    """run_verify_campaign runs an ARGV campaign (shell=False) and parses the single-file
    TRX it writes. No real ``dotnet``: a python stub emits the TRX at the report path,
    standing in for ``dotnet test --logger "trx;LogFileName=test.trx"``."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "CalculatorTests.cs").write_text("// test\n", encoding="utf-8")

    emitter = tmp_path / "emit_trx.py"
    emitter.write_text(
        "import os, pathlib\n"
        "os.makedirs('TestResults', exist_ok=True)\n"
        "pathlib.Path('TestResults/test.trx').write_text(" + repr(_minimal_trx()) + ")\n",
        encoding="utf-8",
    )

    campaign = VerifyCampaignSpec(
        report_relpath="TestResults/test.trx",
        report_format="dotnet-trx",
        command_argv=("python3", str(emitter)),
    )
    profile = LayoutProfile(
        language="csharp",
        package_name="todo_cli",
        source_root="src",
        package_root="src",
        test_root="tests",
        verify_campaign=campaign,
    )
    run = run_verify_campaign(tmp_path, profile, echo=lambda _m: None)
    assert run.execution.total_cases >= 1
    assert "tests/CalculatorTests.cs" in run.execution.executed_passed_files


def test_verify_campaign_rejects_no_command() -> None:
    """A campaign with neither a shell template nor an argv is invalid (anti-false-green:
    a campaign with no command cannot run)."""
    with pytest.raises(ValueError):
        VerifyCampaignSpec(report_relpath=".codd/x", report_format="dotnet-trx")


def test_verify_campaign_argv_substitution_is_per_element() -> None:
    """resolve_argv substitutes {test_root}/{report} per element; an element with shell
    metacharacters but no placeholder (trx;...) is passed VERBATIM."""
    campaign = VerifyCampaignSpec(
        report_relpath="r.trx",
        report_format="dotnet-trx",
        command_argv=("dotnet", "test", "{test_root}", "--logger", "trx;LogFileName=test.trx"),
    )
    argv = campaign.resolve_argv(test_root="tests", report_path="r.trx")
    assert argv == ("dotnet", "test", "tests", "--logger", "trx;LogFileName=test.trx")
