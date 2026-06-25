"""Unit tests for ``JavaToolchainOracleAdapter`` (Contract Kernel oracle dispatch §5)
— the anti-false-green normalization core, driven by SYNTHETIC tool output (no real
``mvn``/``javac`` needed). Modeled on ``tests/languages/test_oracle_go_adapter.py``.

These pin: fail-closed scope certification (no pom.xml / no .java is a HARD FAIL),
the javac/maven diagnostic classification (cannot-find-symbol → missing_symbol,
package-does-not-exist → module_resolution, other → type_error), and the per-line
benign accounting that lets maven banner/summary noise pass on a nonzero exit
WITHOUT ever swallowing a real positioned diagnostic.
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
from codd.languages.adapters.oracle_java import JavaToolchainOracleAdapter
from codd.languages.profile import (
    CommandSpec,
    Identity,
    ImplementOracleProfileSpec,
    ImplementOracleStepSpec,
    LanguageProfile,
    LayoutSpec,
)


def _ctx(tmp_path: Path, *, with_pom: bool = True, with_java: bool = True) -> OracleContext:
    """An OracleContext whose module_root is ``tmp_path``.

    ``with_pom`` writes a pom.xml at the root; ``with_java`` writes one .java under
    ``src/main/java`` — the two scope-certification preconditions, toggled per test.
    """
    if with_pom:
        (tmp_path / "pom.xml").write_text(
            "<project><modelVersion>4.0.0</modelVersion></project>\n", encoding="utf-8"
        )
    if with_java:
        src = tmp_path / "src" / "main" / "java" / "com" / "example"
        src.mkdir(parents=True, exist_ok=True)
        (src / "App.java").write_text(
            "package com.example;\npublic class App {}\n", encoding="utf-8"
        )
    layout = LayoutSpec(repo_root=".", module_root=".", manifest_root=".")
    profile = LanguageProfile(
        identity=Identity(id="java", display_name="Java", aliases=("jvm",)),
        layout=layout,
        commands=MappingProxyType(
            {"compile": CommandSpec(id="compile", argv=("mvn", "-q", "-e", "compile"))}
        ),
        implement_oracle=ImplementOracleProfileSpec(
            kind="composite",
            adapter="java-toolchain",
            steps=(ImplementOracleStepSpec(command="compile"),),
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
    return JavaToolchainOracleAdapter().normalize_command_result(
        ctx,
        command_id="compile",
        command=ctx.language_profile.commands["compile"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


# ── certify_scope: fail-closed (anti-false-green) ────────────────────────────


def test_certify_scope_without_pom_raises(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, with_pom=False, with_java=True)
    with pytest.raises(OracleScopeError) as exc:
        JavaToolchainOracleAdapter().certify_scope(ctx)
    assert "pom.xml" in str(exc.value)


def test_certify_scope_without_any_java_raises(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, with_pom=True, with_java=False)
    with pytest.raises(OracleScopeError) as exc:
        JavaToolchainOracleAdapter().certify_scope(ctx)
    assert ".java" in str(exc.value)


def test_certify_scope_ignores_target_dir_java(tmp_path: Path) -> None:
    """A .java that exists ONLY under target/ (a generated/build artifact) is NOT
    source proof — scope is still uncertifiable (mirrors Go's .git/vendor skip)."""
    (tmp_path / "pom.xml").write_text("<project/>\n", encoding="utf-8")
    gen = tmp_path / "target" / "generated-sources"
    gen.mkdir(parents=True)
    (gen / "Generated.java").write_text("class Generated {}\n", encoding="utf-8")
    layout = LayoutSpec(repo_root=".", module_root=".", manifest_root=".")
    profile = LanguageProfile(
        identity=Identity(id="java", display_name="Java"),
        layout=layout,
        commands=MappingProxyType(
            {"compile": CommandSpec(id="compile", argv=("mvn", "compile"))}
        ),
        implement_oracle=ImplementOracleProfileSpec(
            kind="composite",
            adapter="java-toolchain",
            steps=(ImplementOracleStepSpec(command="compile"),),
        ),
    )
    ctx = OracleContext(
        project_root=tmp_path,
        layout_profile=layout,
        language_profile=profile,
        oracle=profile.implement_oracle,
    )
    with pytest.raises(OracleScopeError):
        JavaToolchainOracleAdapter().certify_scope(ctx)


def test_certify_scope_with_pom_and_java_succeeds(tmp_path: Path) -> None:
    detail = JavaToolchainOracleAdapter().certify_scope(_ctx(tmp_path))
    assert isinstance(detail, str) and detail  # non-empty certification string
    assert "certified" in detail


# ── normalize_command_result: pass / red / opaque ───────────────────────────


def test_exit_zero_is_clean_no_findings(tmp_path: Path) -> None:
    """A passing build (rc 0, maven BUILD SUCCESS banner) → clean, no findings."""
    out = (
        "[INFO] Scanning for projects...\n"
        "[INFO] BUILD SUCCESS\n"
        "[INFO] ------------------------------------------------------------------------\n"
    )
    obs = _norm(_ctx(tmp_path), returncode=0, stdout=out)
    assert obs.is_clean is True
    assert obs.findings == ()


def test_cannot_find_symbol_is_red_with_finding(tmp_path: Path) -> None:
    """``Foo.java:7: error: cannot find symbol`` (rc 1) → missing_symbol RED."""
    out = (
        "[INFO] BUILD FAILURE\n"
        "Foo.java:7: error: cannot find symbol\n"
        "  symbol:   variable missingHelper\n"
    )
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is False
    assert any(
        f.code == "JAVA_CANNOT_FIND_SYMBOL" and f.category == EVIDENCE_MISSING_SYMBOL
        for f in obs.findings
    ), obs.findings


def test_package_does_not_exist_is_module_resolution(tmp_path: Path) -> None:
    """``package com.x does not exist`` (rc 1) → module_resolution_error RED."""
    out = "Bar.java:3: error: package com.x does not exist\nimport com.x.Thing;\n"
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is False
    assert any(
        f.code == "JAVA_PACKAGE_DOES_NOT_EXIST" and f.category == EVIDENCE_MODULE_RESOLUTION
        for f in obs.findings
    ), obs.findings


def test_generic_positioned_error_is_other(tmp_path: Path) -> None:
    """A generic positioned ``error:`` (incompatible types) → type_error (other)."""
    out = "Baz.java:12: error: incompatible types: String cannot be converted to int\n"
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is False
    assert any(
        f.code == "JAVA_COMPILE_ERROR" and f.category == EVIDENCE_OTHER
        for f in obs.findings
    ), obs.findings


def test_maven_bracket_form_diagnostic_is_red(tmp_path: Path) -> None:
    """The maven-compiler bracket form ``File.java:[LINE,COL] msg`` is classified.

    Maven sometimes prefixes ``[ERROR] `` and uses ``[7,15]`` instead of ``:7:``;
    the adapter must still recognize+classify it (here a cannot-find-symbol)."""
    out = (
        "[ERROR] COMPILATION ERROR :\n"
        "[ERROR] /abs/src/main/java/com/example/App.java:[7,15] cannot find symbol\n"
        "[ERROR] BUILD FAILURE\n"
    )
    obs = _norm(_ctx(tmp_path), returncode=1, stdout=out)
    assert obs.is_clean is False
    assert any(f.code == "JAVA_CANNOT_FIND_SYMBOL" for f in obs.findings), obs.findings


def test_opaque_nonzero_non_noise_is_not_clean_empty_findings(tmp_path: Path) -> None:
    """A nonzero exit whose residual is NEITHER maven noise NOR a parseable
    diagnostic → is_clean=False with EMPTY findings (the executor then synthesizes an
    opaque environment_build_error RED — never a benign pass)."""
    obs = _norm(
        _ctx(tmp_path),
        returncode=1,
        stderr="Cannot execute mojo: some totally unexpected maven plugin explosion\n",
    )
    assert obs.is_clean is False
    assert obs.findings == ()
    assert obs.detail  # a human reason is always populated for a non-clean observation


def test_pure_maven_noise_nonzero_is_benign_clean(tmp_path: Path) -> None:
    """A nonzero exit whose lines are ALL recognizable maven banner/summary/[ERROR]-
    echo noise (no positioned diagnostic) → benign → clean (parallels Go's
    third-party-tolerant benign accounting; here it is pure env/summary noise)."""
    out = (
        "[INFO] Scanning for projects...\n"
        "[INFO] ------------------------------------------------------------------------\n"
        "[INFO] BUILD FAILURE\n"
        "[ERROR] Failed to execute goal on project demo: a transient resolution hiccup\n"
        "[ERROR] -> [Help 1]\n"
        "[ERROR] Re-run Maven using the -X switch to enable full debug logging.\n"
        "[ERROR] For more information about the errors and possible solutions, please read:\n"
    )
    obs = _norm(_ctx(tmp_path), returncode=1, stdout=out)
    assert obs.is_clean is True, obs
    assert obs.findings == ()


def test_positioned_diagnostic_with_maven_noise_is_red_not_swallowed(tmp_path: Path) -> None:
    """REGRESSION (anti-false-green): a real positioned diagnostic co-occurring with
    maven banner/summary noise is NOT swallowed by the noise filter → still RED.

    Parallels Go's ``test_positioned_diagnostic_in_ok_named_package_is_red_not_filtered``:
    even when surrounded by ``[INFO] BUILD FAILURE`` / ``[ERROR] -> [Help 1]`` /
    ``[ERROR] COMPILATION ERROR`` echo noise, the ``File.java:LINE: error: cannot find
    symbol`` line must reach the classifier (JAVA_CANNOT_FIND_SYMBOL), never be
    accounted as benign noise → false-GREEN.
    """
    out = (
        "[INFO] Scanning for projects...\n"
        "[INFO] --- maven-compiler-plugin:3.11.0:compile (default-compile) ---\n"
        "[INFO] -------------------------------------------------------------\n"
        "[ERROR] COMPILATION ERROR :\n"
        "[ERROR] -------------------------------------------------------------\n"
        "src/main/java/com/example/App.java:9: error: cannot find symbol\n"
        "        return helper();\n"
        "               ^\n"
        "  symbol:   method helper()\n"
        "[INFO] BUILD FAILURE\n"
        "[ERROR] -> [Help 1]\n"
        "[ERROR] Re-run Maven using the -X switch to enable full debug logging.\n"
    )
    obs = _norm(_ctx(tmp_path), returncode=1, stdout=out)
    assert obs.is_clean is False, obs
    assert any(f.code == "JAVA_CANNOT_FIND_SYMBOL" for f in obs.findings), obs.findings


def test_benign_accounting_is_not_blanket_no_findings(tmp_path: Path) -> None:
    """A nonzero exit with a single UNACCOUNTED real error line (no position, not a
    maven banner) stays RED even amid summary noise — the summary never rescues a
    real failure (anti-false-green: benign accounting is per-line, not blanket)."""
    out = (
        "java.lang.OutOfMemoryError: Java heap space during annotation processing\n"
        "[INFO] BUILD FAILURE\n"
    )
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is False
    assert obs.findings == ()  # opaque → executor escalates to environment_build_error
