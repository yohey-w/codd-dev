"""Unit tests for ``GoToolchainOracleAdapter.normalize_command_result`` (Contract
Kernel oracle dispatch §5) — the anti-false-green normalization core, driven by
SYNTHETIC tool output (no real ``go`` needed).

These pin the per-line benign accounting + third-party tolerance + the ``go test``
run-summary envelope filter that the contract switch introduced (``typecheck`` =
``go test -run ^$ ./...`` emits ``FAIL\\t<pkg> [build/setup failed]`` + a trailing
bare ``FAIL`` envelope that ``go build``/``go vet`` never produced — it must be
treated as noise WITHOUT ever hiding a real positioned diagnostic).
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

from codd.languages.adapters.implement_oracle import OracleContext
from codd.languages.adapters.oracle_go import GoToolchainOracleAdapter
from codd.languages.profile import (
    CommandSpec,
    Identity,
    ImplementOracleProfileSpec,
    ImplementOracleStepSpec,
    LanguageProfile,
    LayoutSpec,
)


def _ctx(tmp_path: Path, *, module: str = "example.com/m") -> OracleContext:
    """An OracleContext whose module_root is ``tmp_path`` with a go.mod (module path)."""
    (tmp_path / "go.mod").write_text(f"module {module}\n\ngo 1.26\n", encoding="utf-8")
    layout = LayoutSpec(repo_root=".", module_root=".", manifest_root=".")
    profile = LanguageProfile(
        identity=Identity(id="go", display_name="Go", aliases=("golang",)),
        layout=layout,
        commands=MappingProxyType(
            {"typecheck": CommandSpec(id="typecheck", argv=("go", "test", "-run", "^$", "./..."))}
        ),
        implement_oracle=ImplementOracleProfileSpec(
            kind="composite",
            adapter="go-toolchain",
            steps=(ImplementOracleStepSpec(command="typecheck"),),
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
    return GoToolchainOracleAdapter().normalize_command_result(
        ctx,
        command_id="typecheck",
        command=ctx.language_profile.commands["typecheck"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_exit_zero_is_clean_no_findings(tmp_path: Path) -> None:
    obs = _norm(_ctx(tmp_path), returncode=0, stdout="ok  \texample.com/m\t0.1s\n")
    assert obs.is_clean is True
    assert obs.findings == ()


def test_undefined_symbol_is_red_with_finding(tmp_path: Path) -> None:
    out = "# example.com/m\n./lib.go:3:9: undefined: missingHelper\n"
    obs = _norm(_ctx(tmp_path), returncode=2, stderr=out)
    assert obs.is_clean is False
    assert any(f.code == "GO_UNDEFINED" for f in obs.findings), obs.findings


def test_third_party_with_go_test_fail_envelope_is_clean(tmp_path: Path) -> None:
    """An uninstalled THIRD-PARTY import + go test's ``FAIL`` envelope → CLEAN.

    The positioned ``cannot find module providing package github.com/...`` line is
    tolerated (third-party, env state); the ``# pkg`` header, ``FAIL\\t<pkg> [setup
    failed]`` and trailing bare ``FAIL`` are run-summary noise. NOTHING is
    unaccounted-for → benign → is_clean=True (the false-RED the switch must avoid).
    """
    out = (
        "# example.com/m\n"
        "lib.go:3:8: cannot find module providing package github.com/some/nonexistent/pkg: "
        "import lookup disabled by -mod=readonly\n"
        "FAIL\texample.com/m [setup failed]\n"
        "FAIL\n"
    )
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is True, obs
    assert obs.findings == ()


def test_third_party_no_required_module_phrasing_is_clean(tmp_path: Path) -> None:
    """The alternate ``no required module provides package`` phrasing is also tolerated."""
    out = (
        "# example.com/m\n"
        "lib.go:3:8: no required module provides package github.com/some/nonexistent/pkg: "
        "import lookup disabled by -mod=readonly\n"
        "FAIL\texample.com/m [setup failed]\nFAIL\n"
    )
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is True, obs


def test_first_party_miss_red_even_with_fail_envelope(tmp_path: Path) -> None:
    """A missing FIRST-PARTY package REDs even though the FAIL envelope is filtered.

    Proves the summary filter never SWALLOWS a real positioned diagnostic: the
    first-party ``cannot find module providing package example.com/m/internal/...``
    line is classified RED; the FAIL envelope around it is noise.
    """
    out = (
        "# example.com/m/cmd/app\n"
        "cmd/app/main.go:3:8: cannot find module providing package "
        "example.com/m/internal/missing: import lookup disabled by -mod=readonly\n"
        "FAIL\texample.com/m/cmd/app [setup failed]\nFAIL\n"
    )
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is False
    assert any(f.code == "GO_PACKAGE_NOT_FOUND" for f in obs.findings), obs.findings


def test_positioned_diagnostic_in_ok_named_package_is_red_not_filtered(tmp_path: Path) -> None:
    """A compile error whose PATH starts with a status-token word must RED, not filter.

    Regression for a false-GREEN: ``go test -run ^$ ./...`` emits sub-package
    diagnostics WITHOUT a ``./`` prefix — for a package dir literally named ``ok``
    the line is ``ok/ok.go:2:19: undefined: undefinedSymbol`` (empirically captured).
    A greedy summary regex (``^(?:ok|FAIL|...)\\b.*$``) treats that real positioned
    diagnostic as a run-summary line → filtered → benign → false-GREEN. The summary
    filter must require whitespace after the status token (which ``ok/`` lacks), so
    the diagnostic reaches the classifier → GO_UNDEFINED → RED.
    """
    out = (
        "# example.com/m/ok\n"
        "ok/ok.go:2:19: undefined: undefinedSymbol\n"
        "FAIL\texample.com/m/ok [build failed]\nFAIL\n"
    )
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is False, obs
    assert any(f.code == "GO_UNDEFINED" for f in obs.findings), obs.findings


def test_opaque_nonzero_non_envelope_is_not_clean_empty_findings(tmp_path: Path) -> None:
    """A nonzero exit whose residual is NEITHER noise/envelope NOR a parseable
    diagnostic → is_clean=False with EMPTY findings (the executor then synthesizes an
    opaque environment_build_error RED — never a benign pass).
    """
    obs = _norm(_ctx(tmp_path), returncode=1, stderr="go: some totally unexpected toolchain explosion\n")
    assert obs.is_clean is False
    assert obs.findings == ()
    assert obs.detail  # a human reason is always populated for a non-clean observation


def test_bare_fail_alone_does_not_falsely_pass_without_tolerated_reason(tmp_path: Path) -> None:
    """A bare ``FAIL`` envelope alongside an UNACCOUNTED real error line stays RED.

    The summary filter accounts for ``FAIL``/``ok`` lines, but a co-occurring real
    error line (no position, not third-party) is still unaccounted-for → not benign
    → is_clean=False (anti-false-green: the envelope never rescues a real failure).
    """
    out = "build constraints exclude all Go files in /x\nFAIL\texample.com/m [build failed]\nFAIL\n"
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is False
    assert obs.findings == ()  # opaque → executor escalates to environment_build_error
