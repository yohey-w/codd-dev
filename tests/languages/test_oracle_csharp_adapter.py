"""Unit tests for ``DotnetToolchainOracleAdapter`` (Contract Kernel oracle dispatch
§5) — the anti-false-green normalization core + scope certification, driven by
SYNTHETIC ``dotnet build`` output (no real ``dotnet`` needed).

Modeled on ``tests/languages/test_oracle_go_adapter.py``. RED-BEFORE-GREEN: each
case asserts the specific verdict the adapter MUST produce, including that a real
``error CS####`` line co-occurring with MSBuild banner/summary noise is NEVER
swallowed by the benign filter (the false-green the noise filter must avoid) and
that a ``warning CS####`` on a clean exit stays clean (a warning is not an error).
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest

from codd.implement_oracle_types import (
    EVIDENCE_MISSING_SYMBOL,
    EVIDENCE_MODULE_RESOLUTION,
    EVIDENCE_OTHER,
    OracleScopeError,
)
from codd.languages.adapters.implement_oracle import OracleContext
from codd.languages.adapters.oracle_csharp import DotnetToolchainOracleAdapter
from codd.languages.profile import (
    CommandSpec,
    Identity,
    ImplementOracleProfileSpec,
    ImplementOracleStepSpec,
    LanguageProfile,
    LayoutSpec,
)


def _ctx(
    tmp_path: Path, *, project: bool = True, source: bool = True
) -> OracleContext:
    """An OracleContext whose module_root is ``tmp_path``.

    ``project`` writes a ``.csproj``; ``source`` writes a ``.cs`` file. Both default
    True (a certifiable scope); a test flips one to False to exercise certify_scope.
    """
    if project:
        (tmp_path / "App.csproj").write_text(
            '<Project Sdk="Microsoft.NET.Sdk"></Project>\n', encoding="utf-8"
        )
    if source:
        src = tmp_path / "src"
        src.mkdir(exist_ok=True)
        (src / "Program.cs").write_text(
            "namespace App;\npublic class Program { }\n", encoding="utf-8"
        )
    layout = LayoutSpec(repo_root=".", module_root=".", manifest_root=".")
    profile = LanguageProfile(
        identity=Identity(id="csharp", display_name="C#", aliases=("dotnet", "cs")),
        layout=layout,
        commands=MappingProxyType(
            {"build": CommandSpec(id="build", argv=("dotnet", "build", "-c", "Release"))}
        ),
        implement_oracle=ImplementOracleProfileSpec(
            kind="composite",
            adapter="dotnet-toolchain",
            steps=(ImplementOracleStepSpec(command="build"),),
        ),
    )
    return OracleContext(
        project_root=tmp_path,
        layout_profile=layout,
        language_profile=profile,
        oracle=profile.implement_oracle,
        config=None,
    )


def _norm(ctx: OracleContext, *, returncode: int, stdout: str = "", stderr: str = ""):
    return DotnetToolchainOracleAdapter().normalize_command_result(
        ctx,
        command_id="build",
        command=ctx.language_profile.commands["build"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


# ── certify_scope ────────────────────────────────────────────────────────────


def test_certify_scope_no_project_file_raises(tmp_path: Path) -> None:
    """No .csproj/.sln/.slnx → hard fail (a green over a project-less scope is false)."""
    ctx = _ctx(tmp_path, project=False, source=True)
    with pytest.raises(OracleScopeError):
        DotnetToolchainOracleAdapter().certify_scope(ctx)


def test_certify_scope_no_cs_source_raises(tmp_path: Path) -> None:
    """Project file present but NO .cs source → hard fail (empty scope proves nothing)."""
    ctx = _ctx(tmp_path, project=True, source=False)
    with pytest.raises(OracleScopeError):
        DotnetToolchainOracleAdapter().certify_scope(ctx)


def test_certify_scope_with_project_and_source_returns_string(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, project=True, source=True)
    detail = DotnetToolchainOracleAdapter().certify_scope(ctx)
    assert isinstance(detail, str)
    assert detail  # a non-empty certification detail


def test_certify_scope_accepts_sln_without_csproj(tmp_path: Path) -> None:
    """A .sln (no .csproj) also satisfies the project-file requirement."""
    (tmp_path / "App.sln").write_text("Microsoft Visual Studio Solution File\n", encoding="utf-8")
    (tmp_path / "Program.cs").write_text("class P {}\n", encoding="utf-8")
    ctx = _ctx(tmp_path, project=False, source=False)
    detail = DotnetToolchainOracleAdapter().certify_scope(ctx)
    assert isinstance(detail, str) and detail


def test_certify_scope_ignores_cs_under_bin_obj(tmp_path: Path) -> None:
    """A .cs file ONLY under bin/ or obj/ does not count as real source → hard fail."""
    (tmp_path / "App.csproj").write_text("<Project/>\n", encoding="utf-8")
    gen = tmp_path / "obj" / "Debug"
    gen.mkdir(parents=True)
    (gen / "App.AssemblyInfo.cs").write_text("// generated\n", encoding="utf-8")
    ctx = _ctx(tmp_path, project=False, source=False)
    with pytest.raises(OracleScopeError):
        DotnetToolchainOracleAdapter().certify_scope(ctx)


# ── normalize_command_result: clean ──────────────────────────────────────────


def test_exit_zero_is_clean_no_findings(tmp_path: Path) -> None:
    out = (
        "Determining projects to restore...\n"
        "  Restored /work/App.csproj (in 120 ms).\n"
        "  App -> /work/bin/Release/net8.0/App.dll\n"
        "Build succeeded.\n"
        "    0 Warning(s)\n"
        "    0 Error(s)\n"
        "Time Elapsed 00:00:01.23\n"
    )
    obs = _norm(_ctx(tmp_path), returncode=0, stdout=out)
    assert obs.is_clean is True
    assert obs.findings == ()


def test_warning_only_on_zero_exit_stays_clean(tmp_path: Path) -> None:
    """A ``warning CS####`` on a zero exit is NOT a finding (a warning != an error)."""
    out = (
        "  App -> /work/bin/Release/net8.0/App.dll\n"
        "Program.cs(7,13): warning CS0168: The variable 'x' is declared but never used "
        "[/work/App.csproj]\n"
        "Build succeeded.\n"
        "    1 Warning(s)\n"
        "    0 Error(s)\n"
    )
    obs = _norm(_ctx(tmp_path), returncode=0, stdout=out)
    assert obs.is_clean is True
    assert obs.findings == ()


# ── normalize_command_result: classified findings (via the CS-code DATA dict) ─


def test_cs0246_is_module_resolution_red(tmp_path: Path) -> None:
    out = (
        "  App -> /work/bin/Release/net8.0/App.dll\n"
        "Foo.cs(10,9): error CS0246: The type or namespace name 'Bar' could not be "
        "found (are you missing a using directive or an assembly reference?) "
        "[/work/App.csproj]\n"
        "Build FAILED.\n"
        "    0 Warning(s)\n"
        "    1 Error(s)\n"
    )
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is False
    assert any(
        f.category == EVIDENCE_MODULE_RESOLUTION and f.code == "CS0246"
        for f in obs.findings
    ), obs.findings


def test_cs0234_is_module_resolution_red(tmp_path: Path) -> None:
    out = (
        "Foo.cs(3,18): error CS0234: The type or namespace name 'Widgets' does not "
        "exist in the namespace 'App' (are you missing an assembly reference?)\n"
        "Build FAILED.\n"
    )
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is False
    assert any(
        f.category == EVIDENCE_MODULE_RESOLUTION and f.code == "CS0234"
        for f in obs.findings
    ), obs.findings


def test_cs0103_is_missing_symbol_red(tmp_path: Path) -> None:
    out = (
        "Foo.cs(12,5): error CS0103: The name 'x' does not exist in the current "
        "context [/work/App.csproj]\n"
        "Build FAILED.\n"
    )
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is False
    assert any(
        f.category == EVIDENCE_MISSING_SYMBOL and f.code == "CS0103"
        for f in obs.findings
    ), obs.findings


def test_cs1061_is_missing_symbol_red(tmp_path: Path) -> None:
    out = (
        "Foo.cs(8,16): error CS1061: 'Widget' does not contain a definition for "
        "'Frobnicate' and no accessible extension method 'Frobnicate' accepting a "
        "first argument of type 'Widget' could be found\n"
    )
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is False
    assert any(
        f.category == EVIDENCE_MISSING_SYMBOL and f.code == "CS1061"
        for f in obs.findings
    ), obs.findings


def test_generic_cs_error_is_other_red(tmp_path: Path) -> None:
    """An ``error CS####`` not in the DATA dict → EVIDENCE_OTHER, code = the CS number."""
    out = (
        "Foo.cs(4,5): error CS0029: Cannot implicitly convert type 'string' to 'int' "
        "[/work/App.csproj]\n"
        "Build FAILED.\n"
    )
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is False
    assert any(
        f.category == EVIDENCE_OTHER and f.code == "CS0029" for f in obs.findings
    ), obs.findings


def test_error_without_position_is_classified(tmp_path: Path) -> None:
    """A position-less ``error CS####:`` is still classified (path=None)."""
    out = "error CS5001: Program does not contain a static 'Main' method suitable for an entry point\n"
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is False
    assert any(f.code == "CS5001" for f in obs.findings), obs.findings


# ── normalize_command_result: anti-false-green (opaque + regression) ──────────


def test_opaque_nonzero_non_noise_is_not_clean_empty_findings(tmp_path: Path) -> None:
    """A nonzero exit whose residual is NEITHER noise NOR a parseable diagnostic →
    is_clean=False with EMPTY findings (the executor then synthesizes an opaque
    environment_build_error RED — never a benign pass).
    """
    obs = _norm(
        _ctx(tmp_path),
        returncode=1,
        stderr="A fatal error was encountered. The library 'hostpolicy.dll' was not found.\n",
    )
    assert obs.is_clean is False
    assert obs.findings == ()
    assert obs.detail  # a human reason is always populated for a non-clean observation


def test_real_error_with_banner_noise_is_red_not_swallowed(tmp_path: Path) -> None:
    """REGRESSION: a real ``error CS####`` line surrounded by MSBuild banner/summary
    noise must RED — the benign noise filter must NEVER swallow a real diagnostic.
    """
    out = (
        "Determining projects to restore...\n"
        "  Restored /work/App.csproj (in 90 ms).\n"
        "MSBuild version 17.8.3+195e7f5a3 for .NET\n"
        "Foo.cs(10,9): error CS0246: The type or namespace name 'Bar' could not be found "
        "[/work/App.csproj]\n"
        "Build FAILED.\n"
        "    0 Warning(s)\n"
        "    1 Error(s)\n"
        "Time Elapsed 00:00:00.98\n"
    )
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is False, obs
    assert any(f.code == "CS0246" for f in obs.findings), obs.findings


def test_build_failed_envelope_alone_with_unaccounted_line_stays_red(tmp_path: Path) -> None:
    """A ``Build FAILED.`` envelope co-occurring with an UNACCOUNTED real error line
    (no CS code, not noise) stays RED with EMPTY findings (opaque → executor escalates).
    """
    out = (
        "CSC : error : Metadata file '/work/lib/Missing.dll' could not be found\n"
        "Build FAILED.\n"
        "    1 Error(s)\n"
    )
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is False
    # "error : ..." has no CS#### code, so it is not a classified finding; it is also
    # not recognizable noise → opaque RED (anti-false-green), not a benign pass.
    assert obs.findings == ()
    assert obs.detail
