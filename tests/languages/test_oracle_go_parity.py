"""Go implement-oracle PARITY characterization (Contract Kernel oracle dispatch, step 5).

This file PINS the observable behaviour of the live Go implement-oracle at the gate
boundary (:func:`codd.implement_oracle.run_implement_oracle_gate` with
``language="go"``) so the step-5 switch — moving Go off the hand-written
``_run_go_composite_oracle`` (``go build`` + ``go vet``) onto the Contract-Kernel
contract path (``run_command_sequence`` over the profile's ``typecheck`` + ``vet``
commands, driven by the ``go-toolchain`` :class:`ImplementOracleAdapter`) — is
proven behaviour-preserving.

EVERYTHING here runs the REAL ``go`` toolchain (go-sdk on PATH at
``/home/tono/go-sdk/go/bin``); the cardinal rule is anti-false-green, and a real
``go`` is the only honest way to assert a non-compiling module REDs.

THE ONE INTENTIONAL BEHAVIOUR CHANGE
====================================
The old oracle ran ``go build ./...`` which does NOT compile ``*_test.go`` files,
so a module whose ONLY breakage is a test-file compile error sailed through
(false-green). The new ``typecheck`` command is ``go test -run ^$ ./...`` which
DOES compile test files. So the ``_test.go``-only case is the single fixture whose
verdict FLIPS at the switch: it is named ``..._intentional_stricter_red_after_switch``
and its expectation is updated AT the switch (see the comment on that test).

All OTHER fixtures are must-parity: their verdict is identical before and after.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from codd.implement_oracle import (
    EVIDENCE_MISSING_SYMBOL,
    EVIDENCE_MODULE_RESOLUTION,
    EVIDENCE_OTHER,
    OracleScopeError,
    run_implement_oracle_gate,
)

_GO_SDK_BIN = "/home/tono/go-sdk/go/bin"


@pytest.fixture(autouse=True)
def _go_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the go-sdk bin on PATH + a writable GOCACHE (mirrors test_go_implement_oracle)."""
    if Path(_GO_SDK_BIN).is_dir():
        monkeypatch.setenv("PATH", _GO_SDK_BIN + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("GOCACHE", os.path.join(tempfile.gettempdir(), "codd-go-oracle-cache"))


def _require_go() -> None:
    if shutil.which("go") is None:
        pytest.skip("go toolchain not available on PATH")


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _scaffold(root: Path, *, module: str = "example.com/m") -> None:
    _write(root, "go.mod", f"module {module}\n\ngo 1.26\n")


def _run(root: Path, config: dict | None = None):
    return run_implement_oracle_gate(
        root,
        language="go",
        project_name="m",
        config=config or {},
        echo=lambda _m: None,
    )


def _codes(result) -> set[str]:
    return {f.code for f in result.findings}


# ════════════════════════════════════════════════════════════════════════════
# must-parity: identical verdict before AND after the contract switch
# ════════════════════════════════════════════════════════════════════════════


def test_parity_valid_module_passes(tmp_path: Path) -> None:
    """A compiling, vet-clean module (source + colocated test) → passed=True."""
    _require_go()
    _scaffold(tmp_path)
    _write(tmp_path, "lib.go", "package m\n\nfunc Add(a, b int) int { return a + b }\n")
    _write(
        tmp_path,
        "lib_test.go",
        'package m\n\nimport "testing"\n\n'
        "func TestAdd(t *testing.T) {\n\tif Add(1, 2) != 3 {\n\t\tt.Fatal(\"bad\")\n\t}\n}\n",
    )

    result = _run(tmp_path)

    assert result.executed is True
    assert result.passed is True, f"a clean module must PASS: {result.findings}"
    assert result.findings == []


def test_parity_undefined_symbol_is_red(tmp_path: Path) -> None:
    """A source undefined symbol → RED (missing_symbol, GO_UNDEFINED)."""
    _require_go()
    _scaffold(tmp_path)
    _write(tmp_path, "lib.go", "package m\n\nfunc Use() int { return missingHelper() }\n")

    result = _run(tmp_path)

    assert result.passed is False
    assert result.category_counts().get(EVIDENCE_MISSING_SYMBOL, 0) >= 1
    assert "GO_UNDEFINED" in _codes(result)


def test_parity_missing_first_party_import_is_red(tmp_path: Path) -> None:
    """A missing FIRST-PARTY package/import → RED (module_resolution)."""
    _require_go()
    _scaffold(tmp_path, module="example.com/m")
    _write(
        tmp_path,
        "cmd/app/main.go",
        'package main\n\nimport "example.com/m/internal/missing"\n\n'
        "func main() { missing.Run() }\n",
    )

    result = _run(tmp_path)

    assert result.passed is False
    assert result.category_counts().get(EVIDENCE_MODULE_RESOLUTION, 0) >= 1
    assert "GO_PACKAGE_NOT_FOUND" in _codes(result)


def test_parity_vet_diagnostic_is_red(tmp_path: Path) -> None:
    """A ``go vet`` diagnostic (Printf format mismatch) → RED (type_error/other)."""
    _require_go()
    _scaffold(tmp_path)
    _write(
        tmp_path,
        "lib.go",
        'package m\n\nimport "fmt"\n\n'
        'func Use() {\n\tname := "x"\n\tfmt.Printf("%d\\n", name)\n}\n',
    )

    result = _run(tmp_path)

    assert result.passed is False
    assert result.category_counts().get(EVIDENCE_OTHER, 0) >= 1
    assert any("[vet]" in f.message for f in result.findings), result.findings


def test_parity_uninstalled_third_party_is_tolerated(tmp_path: Path) -> None:
    """An uninstalled THIRD-PARTY import under -mod=readonly → TOLERATED (passed=True).

    This is environment state (the dep is simply not downloaded at implement time),
    NOT code incoherence — it MUST stay tolerated after the switch. The per-line
    benign accounting in the adapter is what preserves this.
    """
    _require_go()
    _scaffold(tmp_path, module="example.com/m")
    _write(
        tmp_path,
        "lib.go",
        'package m\n\nimport "github.com/some/nonexistent/pkg"\n\nfunc Use() { pkg.Do() }\n',
    )

    result = _run(tmp_path)

    assert result.passed is True, (
        f"an uninstalled third-party import must NOT false-RED: {result.findings}"
    )


def test_parity_first_party_red_while_third_party_tolerated(tmp_path: Path) -> None:
    """Mixed: first-party miss REDs, the third-party import is tolerated (per-import)."""
    _require_go()
    _scaffold(tmp_path, module="example.com/m")
    _write(
        tmp_path,
        "internal/svc/svc.go",
        'package svc\n\nimport (\n\t"github.com/some/nonexistent/pkg"\n'
        '\t"example.com/m/internal/missing"\n)\n\n'
        "func Run() { pkg.Do(); missing.Go() }\n",
    )

    result = _run(tmp_path)

    assert result.passed is False
    assert any(
        f.category == EVIDENCE_MODULE_RESOLUTION and "internal/missing" in f.message
        for f in result.findings
    ), result.findings
    assert not any("nonexistent/pkg" in f.message for f in result.findings), (
        f"the third-party import must be tolerated, not reported: {result.findings}"
    )


def test_parity_main_package_passes_despite_vcs_noise(tmp_path: Path) -> None:
    """A clean ``main`` package → PASS (build VCS-stamp noise is filtered)."""
    _require_go()
    _scaffold(tmp_path)
    _write(
        tmp_path,
        "main.go",
        'package main\n\nimport "fmt"\n\n'
        "func add(a, b int) int { return a + b }\n\nfunc main() { fmt.Println(add(1, 2)) }\n",
    )

    result = _run(tmp_path)

    assert result.passed is True, (
        f"a clean main package must PASS (VCS-stamp noise filtered): {result.findings}"
    )


def test_parity_go_mod_missing_raises_scope_error(tmp_path: Path) -> None:
    """No go.mod at the module root → OracleScopeError (RED, never a silent pass).

    The certifier hard-fails before any command runs: without go.mod the first-party
    boundary cannot be established, so a green result would prove nothing.
    """
    _require_go()
    _write(tmp_path, "lib.go", "package m\n")
    with pytest.raises(OracleScopeError):
        _run(tmp_path)


def test_parity_go_off_path_is_environment_red(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A spawn failure (go off PATH) → environment_build_error RED, NOT a pass.

    Anti-false-green: a missing toolchain is an honest opaque environment failure;
    it must never read as green.
    """
    _scaffold(tmp_path)
    _write(tmp_path, "lib.go", "package m\n\nfunc Add(a, b int) int { return a + b }\n")
    # Hide go: empty PATH so the spawn fails (FileNotFoundError → environment RED).
    monkeypatch.setenv("PATH", "")
    result = _run(tmp_path)
    assert result.passed is False, "go off PATH must RED, never pass"
    assert any(f.category == "environment_build_error" for f in result.findings), result.findings


# ════════════════════════════════════════════════════════════════════════════
# the ``_test.go``-compile-error case (the task's "intentional stricter-RED" axis)
#
# CHARACTERIZATION FINDING (recorded against today's code, before the switch):
# the verdict does NOT flip. The old oracle's composite is ``go build ./...`` +
# ``go vet ./...`` — and although ``go build`` ignores ``*_test.go``, ``go vet``
# DOES compile test files (verified directly: ``go vet`` exits 1 with
# ``vet: ./lib_test.go:..: undefined: ..`` for both an internal ``package m`` and an
# external ``package m_test`` test file). So the OLD path ALREADY catches a
# ``_test.go``-only compile error VIA vet → RED today. After the switch the NEW
# typecheck (``go test -run ^$ ./...``) ALSO compiles tests → still RED. Both
# before and after: RED. Hence this is a MUST-PARITY fixture, not a flip.
#
# The task brief hypothesized the old build+vet would PASS a _test.go-only break
# (a false-green that the typecheck switch would newly catch); that hypothesis is
# FALSE for this codebase because the old composite already includes ``go vet``,
# which compiles tests. The switch's real value is that test-compile catching now
# belongs to ``typecheck`` (a first-class, scoped command) instead of relying on a
# vet side effect — but the gate verdict is unchanged. (Reported to the reviewer.)
# ════════════════════════════════════════════════════════════════════════════


def test_test_go_compile_error_is_red_parity(tmp_path: Path) -> None:
    """A module whose ONLY breakage is a ``_test.go`` compile error → RED (both paths).

    RED before the switch (old ``go vet`` compiles tests) and RED after (new
    ``go test -run ^$`` typecheck compiles tests). Pinned here so the switch is
    proven to preserve the RED verdict on the test-compile axis.
    """
    _require_go()
    _scaffold(tmp_path)
    _write(tmp_path, "lib.go", "package m\n\nfunc Add(a, b int) int { return a + b }\n")
    # A test file that references an undefined symbol → only the TEST is broken.
    _write(
        tmp_path,
        "lib_test.go",
        'package m\n\nimport "testing"\n\n'
        "func TestAdd(t *testing.T) {\n\tif Add(1, 2) != totallyUndefinedTestSymbol {\n"
        "\t\tt.Fatal(\"bad\")\n\t}\n}\n",
    )

    result = _run(tmp_path)

    assert result.passed is False, (
        f"a _test.go compile error MUST be caught (RED), before and after: {result.findings}"
    )
    assert result.category_counts().get(EVIDENCE_MISSING_SYMBOL, 0) >= 1, result.findings
