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
    # argv form (design A) with the --logger completion that emits the TRX.
    assert campaign.command_argv == ("dotnet", "test", "--logger", "trx;LogFileName=test.trx")
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


# ── (4) generic-template scaffolder ─────────────────────────────────────────────


def test_csharp_scaffolds_csproj_from_template(tmp_path: Path) -> None:
    """scaffold_layout writes the .csproj from the YAML template (create-only / idempotent
    / non-clobber), substituting {package_name} + scaffold.defaults (target_framework)."""
    profile = resolve_layout_profile(language="csharp", project_name="todo-cli")
    assert profile is not None

    result = scaffold_layout(tmp_path, profile)
    assert "todo_cli.csproj" in result.created
    csproj = tmp_path / "todo_cli.csproj"
    assert csproj.is_file()
    text = csproj.read_text(encoding="utf-8")
    assert "<TargetFramework>net8.0</TargetFramework>" in text  # defaults substituted
    assert "{" not in text and "}" not in text  # no unsubstituted placeholder leaked

    # idempotent: a second call creates nothing, skips the existing file.
    result2 = scaffold_layout(tmp_path, profile)
    assert result2.created == ()
    assert "todo_cli.csproj" in result2.skipped

    # non-clobber: an authored file is left byte-for-byte.
    csproj.write_text("AUTHORED", encoding="utf-8")
    scaffold_layout(tmp_path, profile)
    assert csproj.read_text(encoding="utf-8") == "AUTHORED"


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


def test_csharp_harness_owned_scaffold_paths_includes_csproj() -> None:
    """The orphan-gate / write-fence must recognise the scaffolded .csproj as harness-
    owned (not an unowned orphan)."""
    profile = resolve_layout_profile(language="csharp", project_name="todo-cli")
    assert profile is not None
    owned = profile.harness_owned_scaffold_paths()
    assert "todo_cli.csproj" in owned


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
