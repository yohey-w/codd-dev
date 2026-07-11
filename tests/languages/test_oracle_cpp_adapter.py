"""Unit tests for ``CppToolchainOracleAdapter`` (Contract Kernel oracle dispatch §5)
— the anti-false-green normalization core + the system-vs-first-party header
heuristic, driven by SYNTHETIC g++/cmake output (no real g++/cmake needed).

Modeled on ``tests/languages/test_oracle_go_adapter.py``. RED-BEFORE-GREEN: each
case asserts the EXACT verdict the C++ analogue of Go's per-line benign accounting +
third-party (here: system-header) tolerance must produce.
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
from codd.languages.adapters.oracle_cpp import CppToolchainOracleAdapter
from codd.languages.profile import (
    CommandSpec,
    Identity,
    ImplementOracleProfileSpec,
    ImplementOracleStepSpec,
    LanguageProfile,
    LayoutSpec,
)


def _ctx(tmp_path: Path, *, cmakelists: bool = True, source: bool = True) -> OracleContext:
    """An OracleContext whose module_root is ``tmp_path`` (optionally) scoped.

    ``cmakelists`` writes a CMakeLists.txt at the root; ``source`` writes a .cpp file
    — both default True (a fully-scoped module). Set either False to exercise
    certify_scope's fail-closed paths.
    """
    if cmakelists:
        (tmp_path / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.20)\nproject(app LANGUAGES CXX)\n",
            encoding="utf-8",
        )
    if source:
        (tmp_path / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    layout = LayoutSpec(repo_root=".", module_root=".", manifest_root=".")
    profile = LanguageProfile(
        identity=Identity(id="cpp", display_name="C++", aliases=("c++", "cxx")),
        layout=layout,
        commands=MappingProxyType(
            {
                "configure": CommandSpec(id="configure", argv=("cmake", "-S", ".", "-B", "build")),
                "build": CommandSpec(id="build", argv=("cmake", "--build", "build")),
            }
        ),
        implement_oracle=ImplementOracleProfileSpec(
            kind="composite",
            adapter="cpp-toolchain",
            steps=(
                ImplementOracleStepSpec(command="configure"),
                ImplementOracleStepSpec(command="build"),
            ),
        ),
    )
    return OracleContext(
        project_root=tmp_path,
        layout_profile=layout,
        language_profile=profile,
        oracle=profile.implement_oracle,
        config=None,
    )


def _norm(ctx: OracleContext, *, command_id: str = "build", returncode: int, stdout: str = "", stderr: str = ""):
    return CppToolchainOracleAdapter().normalize_command_result(
        ctx,
        command_id=command_id,
        command=ctx.language_profile.commands[command_id],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


# ── certify_scope ─────────────────────────────────────────────────────────────


def test_certify_scope_no_cmakelists_raises(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, cmakelists=False, source=True)
    with pytest.raises(OracleScopeError) as excinfo:
        CppToolchainOracleAdapter().certify_scope(ctx)
    assert "CMakeLists.txt" in str(excinfo.value)


def test_certify_scope_cmakelists_but_no_source_raises(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, cmakelists=True, source=False)
    with pytest.raises(OracleScopeError) as excinfo:
        CppToolchainOracleAdapter().certify_scope(ctx)
    assert "no C/C++ source" in str(excinfo.value)


def test_certify_scope_both_present_returns_string(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, cmakelists=True, source=True)
    detail = CppToolchainOracleAdapter().certify_scope(ctx)
    assert isinstance(detail, str)
    assert "certified" in detail


def test_certify_scope_header_only_module_is_certified(tmp_path: Path) -> None:
    """A header-only library (only a .hpp, no .cpp) is still real, certifiable source."""
    (tmp_path / "CMakeLists.txt").write_text("project(h)\n", encoding="utf-8")
    (tmp_path / "lib.hpp").write_text("#pragma once\nint f();\n", encoding="utf-8")
    layout = LayoutSpec(repo_root=".", module_root=".", manifest_root=".")
    profile = LanguageProfile(
        identity=Identity(id="cpp", display_name="C++"),
        layout=layout,
        commands=MappingProxyType({"build": CommandSpec(id="build", argv=("cmake", "--build", "build"))}),
        implement_oracle=ImplementOracleProfileSpec(
            kind="composite", adapter="cpp-toolchain", steps=(ImplementOracleStepSpec(command="build"),)
        ),
    )
    ctx = OracleContext(
        project_root=tmp_path, layout_profile=layout, language_profile=profile,
        oracle=profile.implement_oracle, config=None,
    )
    assert "certified" in CppToolchainOracleAdapter().certify_scope(ctx)


# ── normalize_command_result ──────────────────────────────────────────────────


def test_exit_zero_is_clean_no_findings(tmp_path: Path) -> None:
    out = (
        "[ 50%] Building CXX object CMakeFiles/app.dir/main.cpp.o\n"
        "[100%] Built target app\n"
    )
    obs = _norm(_ctx(tmp_path), returncode=0, stdout=out)
    assert obs.is_clean is True
    assert obs.findings == ()


def test_undeclared_symbol_is_red_missing_symbol(tmp_path: Path) -> None:
    out = "main.cpp:5:3: error: 'foo' was not declared in this scope\n"
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is False
    codes = {f.code for f in obs.findings}
    cats = {f.category for f in obs.findings}
    assert "CPP_UNDECLARED" in codes, obs.findings
    assert EVIDENCE_MISSING_SYMBOL in cats, obs.findings


def test_first_party_header_not_found_is_red_module_resolution(tmp_path: Path) -> None:
    """A first-party-looking header missing → EVIDENCE_MODULE_RESOLUTION (RED)."""
    out = "main.cpp:1:10: fatal error: myheader.h: No such file or directory\n"
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is False
    assert any(
        f.code == "CPP_HEADER_NOT_FOUND" and f.category == EVIDENCE_MODULE_RESOLUTION
        for f in obs.findings
    ), obs.findings


def test_first_party_relative_path_header_is_red(tmp_path: Path) -> None:
    """A relative-path include (``foo/bar.h``) is first-party → RED even if it had no extension std-name collision."""
    out = 'main.cpp:2:10: fatal error: foo/bar.h: No such file or directory\n'
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is False
    assert any(f.code == "CPP_HEADER_NOT_FOUND" for f in obs.findings), obs.findings


def test_system_header_not_found_is_tolerated_env(tmp_path: Path) -> None:
    """A recognized STD header (``vector``) missing on a nonzero exit → TOLERATED (env).

    The tricky case (mirror of Go tolerating an uninstalled third-party module):
    when the ONLY diagnostic is a known-std header not found and everything else is
    noise, is_clean is True (benign/env) and findings is EMPTY.
    """
    out = (
        "[ 50%] Building CXX object CMakeFiles/app.dir/main.cpp.o\n"
        "main.cpp:1:10: fatal error: vector: No such file or directory\n"
        "    1 | #include <vector>\n"
        "      |          ^~~~~~~~\n"
        "compilation terminated.\n"
    )
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is True, obs
    assert obs.findings == ()


def test_system_header_clang_phrasing_is_tolerated_env(tmp_path: Path) -> None:
    """The clang ``'<name>' file not found`` phrasing for a std header is also tolerated."""
    out = "main.cpp:1:10: fatal error: 'iostream' file not found\n"
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is True, obs
    assert obs.findings == ()


def test_first_party_header_clang_phrasing_is_red(tmp_path: Path) -> None:
    """clang ``'myheader.h' file not found`` for a first-party header is RED."""
    out = "main.cpp:1:10: fatal error: 'myheader.h' file not found\n"
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is False
    assert any(f.code == "CPP_HEADER_NOT_FOUND" for f in obs.findings), obs.findings


def test_generic_positioned_error_is_other(tmp_path: Path) -> None:
    """A generic positioned compile error → EVIDENCE_OTHER / CPP_COMPILE_ERROR."""
    out = "main.cpp:7:14: error: invalid conversion from 'int' to 'char*'\n"
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is False
    assert any(
        f.code == "CPP_COMPILE_ERROR" and f.category == EVIDENCE_OTHER for f in obs.findings
    ), obs.findings


def test_no_column_diagnostic_is_parsed(tmp_path: Path) -> None:
    """The ``file:line: error:`` shape (no column) is tolerated by the diag regex."""
    out = "main.cpp:9: error: 'bar' was not declared in this scope\n"
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is False
    assert any(f.code == "CPP_UNDECLARED" for f in obs.findings), obs.findings


def test_absolute_path_diagnostic_is_parsed(tmp_path: Path) -> None:
    """A leading absolute path on the diagnostic is tolerated."""
    out = "/work/proj/src/widget.cpp:12:5: error: 'Gadget' was not declared in this scope\n"
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is False
    assert any(f.code == "CPP_UNDECLARED" for f in obs.findings), obs.findings


def test_cmake_error_at_first_party_file_is_red(tmp_path: Path) -> None:
    """A ``CMake Error at CMakeLists.txt:NN (add_executable):`` block → coherence RED."""
    out = (
        "CMake Error at CMakeLists.txt:8 (add_executable):\n"
        "  Cannot find source file:\n"
        "    nonexistent.cpp\n"
    )
    obs = _norm(_ctx(tmp_path), command_id="configure", returncode=1, stderr=out)
    assert obs.is_clean is False
    assert any(f.code == "CPP_CMAKE_ERROR" for f in obs.findings), obs.findings


def test_warning_only_nonzero_is_not_swallowed_but_warning_is_noise(tmp_path: Path) -> None:
    """A warning is not a failure: a nonzero exit whose ONLY content is a warning +
    noise is benign (the warning never becomes a finding)."""
    out = (
        "main.cpp:3:9: warning: unused variable 'x' [-Wunused-variable]\n"
        "[100%] Built target app\n"
    )
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is True, obs
    assert obs.findings == ()


def test_opaque_nonzero_non_diagnostic_is_not_clean_empty_findings(tmp_path: Path) -> None:
    """A nonzero exit whose residual is NEITHER noise NOR a parseable diagnostic →
    is_clean=False with EMPTY findings (executor synthesizes an opaque
    environment_build_error RED — never a benign pass).
    """
    obs = _norm(_ctx(tmp_path), returncode=1, stderr="ninja: error: loading 'build.ninja': No such file\n")
    assert obs.is_clean is False
    assert obs.findings == ()
    assert obs.detail  # a human reason is always populated for a non-clean observation


def test_regression_first_party_diagnostic_amid_progress_and_caret_is_not_swallowed(
    tmp_path: Path,
) -> None:
    """A REAL first-party diagnostic co-occurring with cmake progress / note: / caret
    noise must RED — never swallowed by the noise filters.

    The C++ analogue of Go's ``test_first_party_miss_red_even_with_fail_envelope``:
    the surrounding ``In file included from`` / ``note:`` / ``^~~~`` / build-percent
    lines are context/noise, but the positioned ``error: 'Helper' was not declared``
    in the middle is a genuine coherence failure and must surface as RED.
    """
    out = (
        "[ 50%] Building CXX object CMakeFiles/app.dir/main.cpp.o\n"
        "In file included from main.cpp:2:\n"
        "main.cpp:6:3: error: 'Helper' was not declared in this scope\n"
        "    6 |   Helper h;\n"
        "      |   ^~~~~~\n"
        "main.cpp:6:3: note: suggested alternative: 'helper'\n"
        "gmake[2]: *** [CMakeFiles/app.dir/build.make:76: main.cpp.o] Error 1\n"
        "gmake[1]: *** [CMakeFiles/Makefile2:83: all] Error 2\n"
        "gmake: *** [Makefile:91: all] Error 2\n"
    )
    obs = _norm(_ctx(tmp_path), returncode=2, stderr=out)
    assert obs.is_clean is False, obs
    assert any(f.code == "CPP_UNDECLARED" for f in obs.findings), obs.findings


def test_regression_system_header_with_first_party_error_still_reds(tmp_path: Path) -> None:
    """A tolerated system header AND a real first-party error in the same output → RED.

    The system-header tolerance must NEVER rescue a co-occurring real diagnostic
    (anti-false-green): even though ``<vector>`` not found would be tolerated alone,
    the positioned ``error: 'Widget' was not declared`` is a finding → RED.
    """
    out = (
        "main.cpp:1:10: fatal error: vector: No such file or directory\n"
        "main.cpp:8:3: error: 'Widget' was not declared in this scope\n"
    )
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is False
    assert any(f.code == "CPP_UNDECLARED" for f in obs.findings), obs.findings


# ── linker (ld) diagnostics — undefined reference → missing_symbol ────────────
# Regression family for the cpp2 exprcalc greenfield dogfood (2026-07-11): the
# whole link failure collapsed to an opaque environment_build_error and the
# repair loop aborted, because no regex recognized GNU ld's diagnostic shapes.


def test_linker_undefined_reference_is_red_missing_symbol(tmp_path: Path) -> None:
    """GNU ld ``undefined reference to `sym'`` lines → EVIDENCE_MISSING_SYMBOL
    findings whose messages carry the SYMBOL identity (the repair feedback
    channel), never an opaque env RED."""
    out = (
        "[ 85%] Linking CXX executable exprcalc_tests\n"
        "/usr/bin/ld: ast_test.cpp:(.text+0x40): undefined reference to "
        "`exprcalc::LiteralNode::value() const'\n"
        "/usr/bin/ld: ast_test.cpp:(.text+0x275): undefined reference to "
        "`exprcalc::BinaryNode::op() const'\n"
        "ast_introspection_test.cpp:(.text+0x1aa): undefined reference to "
        "`exprcalc::BinaryNode::op() const'\n"
        "collect2: error: ld returned 1 exit status\n"
        "gmake[2]: *** [CMakeFiles/exprcalc_tests.dir/build.make:120: exprcalc_tests] Error 1\n"
    )
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is False
    assert obs.findings, "ld undefined-references must parse into findings, not opaque env"
    assert all(f.category == EVIDENCE_MISSING_SYMBOL for f in obs.findings), obs.findings
    assert all(f.code == "CPP_LD_UNDEFINED_REFERENCE" for f in obs.findings), obs.findings
    msgs = " | ".join(f.message for f in obs.findings)
    assert "exprcalc::LiteralNode::value() const" in msgs
    assert "exprcalc::BinaryNode::op() const" in msgs
    # distinct (TU, symbol) pairs are all kept: (ast_test, value), (ast_test, op),
    # (ast_introspection_test, op)
    assert len(obs.findings) == 3, obs.findings


def test_linker_same_symbol_same_tu_is_deduped(tmp_path: Path) -> None:
    """The SAME symbol missing from the SAME TU at different offsets is ONE finding
    (ld repeats it per call site; per-site duplicates add no repair signal)."""
    out = (
        "/usr/bin/ld: a_test.cpp:(.text+0x1): undefined reference to `foo()'\n"
        "/usr/bin/ld: a_test.cpp:(.text+0x2): undefined reference to `foo()'\n"
        "collect2: error: ld returned 1 exit status\n"
    )
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is False
    assert len(obs.findings) == 1, obs.findings
    assert obs.findings[0].code == "CPP_LD_UNDEFINED_REFERENCE"


def test_linker_in_function_context_form_is_parsed(tmp_path: Path) -> None:
    """The two-line ld form (``…o: in function `main':`` then the positioned
    undefined-reference) parses the reference and treats the in-function line as
    context, not an unaccounted opaque line."""
    out = (
        "/usr/bin/ld: CMakeFiles/app.dir/main.cpp.o: in function `main':\n"
        "main.cpp:(.text+0x12): undefined reference to `foo()'\n"
        "collect2: error: ld returned 1 exit status\n"
    )
    obs = _norm(_ctx(tmp_path), returncode=1, stderr=out)
    assert obs.is_clean is False
    assert any(
        f.code == "CPP_LD_UNDEFINED_REFERENCE" and "foo()" in f.message for f in obs.findings
    ), obs.findings


def test_collect2_epilog_alone_is_not_benign(tmp_path: Path) -> None:
    """Anti-false-green guard: ``collect2: error: ld returned 1 exit status`` with NO
    accompanying diagnostic stays an opaque RED (is_clean=False, empty findings →
    executor synthesizes environment_build_error) — never a benign pass."""
    obs = _norm(
        _ctx(tmp_path), returncode=1, stderr="collect2: error: ld returned 1 exit status\n"
    )
    assert obs.is_clean is False
    assert obs.findings == ()
