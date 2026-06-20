"""Certification fixtures for the Go COMPOSITE implement-time oracle.

Before this oracle, the greenfield implement gate NO-OPed for Go
(``resolve_layout_profile('go')`` returns None — Go has no single ``source_root``,
so the legacy ``LayoutProfile``/compat shim deliberately refuses to build one), so
a Go package that did NOT compile sailed through implement UNCHECKED — a
false-green. The Go oracle closes that hole with a compiler-class composite, run
from the module root (design: ``dogfood/gpt_language_generality_design.md``
§1.4–1.5; commands from ``codd/languages/profiles/go.yaml``):

  1. **go build ./...** — compile + import resolution across the whole module.
  2. **go vet ./...**   — the full typechecker (catches ``undefined: X``) PLUS
     suspicious-construct analysis (e.g. a ``Printf`` format/arg-type mismatch).

It is reachable WITHOUT a legacy ``LayoutProfile``: ``resolve_implement_oracle``
SYNTHESIZES the (profile, spec) from the declarative ``go`` profile in
``codd.languages.registry`` and the SAME ``language``/``kind`` dispatch the Python
composite uses routes it to the Go executor (one entry, mirroring TS — no
``if language=='go'`` scattered in the gate).

ANTI-FALSE-GREEN is the cardinal rule and is asserted REAL (not mocked — these run
the REAL ``go`` toolchain at ``/home/tono/go-sdk/go/bin``):

  * a clean Go module → PASS (modulo tolerated VCS-stamping noise on a main pkg).
  * an undefined symbol / a missing FIRST-PARTY import → RED with a finding.
  * an uninstalled THIRD-PARTY import (``-mod=readonly`` cannot fetch it at
    implement time) → NOT a false-RED (env concern, tolerated) — mirroring the
    Python oracle's "first-party provably absent → fail; third-party → never fail".

These tests SKIP cleanly if ``go`` is unavailable, but ``go`` IS installed in the
target env (go-sdk on PATH), so they run REAL ``go build``/``go vet``.
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
    certify_go_oracle_scope,
    resolve_implement_oracle,
    run_implement_oracle_gate,
)
from codd.project_types import (
    ImplementOracleSpec,
    LayoutProfile,
    OracleScopeSpec,
    resolve_layout_profile,
)


# ─────────────────────────────────────────────────────────────
# Toolchain guard — prefer the real ``go`` (go-sdk bin), skip cleanly if absent.
# ─────────────────────────────────────────────────────────────

_GO_SDK_BIN = "/home/tono/go-sdk/go/bin"


@pytest.fixture(autouse=True)
def _go_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the go-sdk bin is on PATH + a writable GOCACHE for the real ``go`` run.

    The gate runs ``go build``/``go vet`` via ``subprocess.run(env=os.environ + go
    env)``, so ``go`` must be discoverable on PATH. We prepend the known go-sdk bin
    (the target env) and point GOCACHE at a temp dir so a sandboxed CI HOME does
    not break the build cache.
    """
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
    """A minimal Go module (go.mod at the module root)."""
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


# ════════════════════════════════════════════════════════════
# Profile / dispatch wiring (pure-unit — no toolchain needed)
# ════════════════════════════════════════════════════════════


def test_go_has_no_legacy_layout_profile() -> None:
    """``resolve_layout_profile('go')`` is None — Go has no single source_root.

    This is the whole reason the oracle is synthesized from the registry instead of
    a legacy builder; the rest of the wiring depends on it.
    """
    assert resolve_layout_profile(language="go", project_name="m") is None


def test_resolve_implement_oracle_synthesizes_go_composite() -> None:
    """``resolve_implement_oracle('go')`` synthesizes a composite spec (no None NO-OP).

    The language→oracle map entry: even without a legacy LayoutProfile, Go resolves
    to a ``kind="composite"`` spec on a ``language="go"`` profile, so the gate runs
    instead of silently NO-OPing (the false-green this whole change closes).
    """
    resolved = resolve_implement_oracle(Path("/tmp"), language="go", project_name="m")
    assert resolved is not None, "Go must resolve to a real oracle, not a NO-OP"
    profile, spec = resolved
    assert profile.language == "go"
    assert spec.kind == "composite"
    # Module root is carried for the go commands' cwd (go.yaml module_root ".").
    assert profile.source_root in (".", "")


def test_go_oracle_respects_opt_out() -> None:
    """``implement.implement_oracle: false`` opts Go out (a deliberate NO-OP)."""
    resolved = resolve_implement_oracle(
        Path("/tmp"),
        language="go",
        project_name="m",
        config={"implement": {"implement_oracle": False}},
    )
    assert resolved is None


def test_go_alias_golang_resolves() -> None:
    """The ``golang`` alias resolves the same composite oracle as ``go``."""
    resolved = resolve_implement_oracle(Path("/tmp"), language="golang", project_name="m")
    assert resolved is not None and resolved[0].language == "go"


# ════════════════════════════════════════════════════════════
# Scope certification (anti-false-green: empty module proves nothing)
# ════════════════════════════════════════════════════════════


def _go_profile() -> LayoutProfile:
    return LayoutProfile(
        language="go",
        package_name="m",
        source_root=".",
        package_root=".",
        test_root=".",
        implement_oracle=ImplementOracleSpec(
            command="go-composite",
            kind="composite",
            scope=OracleScopeSpec(require_source_root=True, require_test_root=False),
            requires_node_install=False,
        ),
    )


def test_certify_fails_on_missing_go_mod(tmp_path: Path) -> None:
    """No go.mod at the module root → HARD FAIL (not a module build)."""
    _write(tmp_path, "lib.go", "package m\n")
    profile = _go_profile()
    with pytest.raises(OracleScopeError):
        certify_go_oracle_scope(tmp_path, profile, profile.implement_oracle)


def test_certify_fails_on_empty_module(tmp_path: Path) -> None:
    """go.mod present but ZERO .go files → HARD FAIL (a green build proves nothing)."""
    _scaffold(tmp_path)
    profile = _go_profile()
    with pytest.raises(OracleScopeError):
        certify_go_oracle_scope(tmp_path, profile, profile.implement_oracle)


def test_certify_passes_with_go_mod_and_source(tmp_path: Path) -> None:
    """go.mod + ≥1 .go file → certified (the whole-module ./... scope is covered)."""
    _scaffold(tmp_path)
    _write(tmp_path, "lib.go", "package m\n\nfunc Add(a, b int) int { return a + b }\n")
    profile = _go_profile()
    detail = certify_go_oracle_scope(tmp_path, profile, profile.implement_oracle)
    assert "certified" in detail


# ════════════════════════════════════════════════════════════
# REAL go-toolchain integration — the anti-false-green core.
# ════════════════════════════════════════════════════════════


def test_clean_library_module_passes(tmp_path: Path) -> None:
    """A clean Go library module (source + colocated test) → PASS."""
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


def test_clean_main_package_passes_despite_vcs_noise(tmp_path: Path) -> None:
    """A clean ``main`` package → PASS (the build's VCS-stamping noise is filtered).

    ``go build`` stamps VCS info for a main package and fails with ``error
    obtaining VCS status`` when there is no usable git repo (e.g. a tmp dir). That
    is an ENVIRONMENT artifact, never a code-coherence signal — it must NOT RED a
    clean module (``go vet`` does not stamp VCS, so it stays the clean authority).
    """
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


def test_undefined_symbol_is_red(tmp_path: Path) -> None:
    """A reference to an undefined symbol → RED (missing_symbol)."""
    _require_go()
    _scaffold(tmp_path)
    _write(tmp_path, "lib.go", "package m\n\nfunc Use() int { return missingHelper() }\n")

    result = _run(tmp_path)

    assert result.passed is False, "an undefined symbol must RED"
    assert result.category_counts().get(EVIDENCE_MISSING_SYMBOL, 0) >= 1
    assert "GO_UNDEFINED" in _codes(result)


def test_missing_first_party_import_is_red(tmp_path: Path) -> None:
    """An import of a FIRST-PARTY package that does not exist → RED (module_resolution).

    The Go analogue of the Python keystone (``from .missing import X``) / TS2307:
    ``cmd/app`` imports ``<module>/internal/missing`` which no source provides.
    Because it is module-path-prefixed (first-party), it is RED — NOT tolerated.
    """
    _require_go()
    _scaffold(tmp_path, module="example.com/m")
    _write(
        tmp_path,
        "cmd/app/main.go",
        'package main\n\nimport "example.com/m/internal/missing"\n\n'
        "func main() { missing.Run() }\n",
    )

    result = _run(tmp_path)

    assert result.passed is False, "a missing first-party import must RED"
    assert result.category_counts().get(EVIDENCE_MODULE_RESOLUTION, 0) >= 1
    assert "GO_PACKAGE_NOT_FOUND" in _codes(result)


def test_uninstalled_third_party_import_is_not_a_false_red(tmp_path: Path) -> None:
    """An uninstalled THIRD-PARTY import → NOT a false-RED (env concern, tolerated).

    Under ``-mod=readonly`` Go cannot fetch a third-party dep at implement time and
    emits the SAME "cannot find module providing package" it would for a missing
    first-party package. The oracle classifies by module path: a NON-module-prefixed
    import (``github.com/...``) is an uninstalled dependency (environment), so it is
    TOLERATED — mirroring the Python oracle's third-party tolerance. A clean module
    that only "fails" on an uninstalled third-party import must still PASS.
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


def test_first_party_missing_red_while_third_party_tolerated(tmp_path: Path) -> None:
    """A module mixing BOTH: the first-party miss REDs; the third-party is tolerated.

    Proves the classification is per-import (not all-or-nothing): the SAME run sees
    an uninstalled third-party import (tolerated) and a missing first-party package
    (RED) — only the first-party one yields a finding.
    """
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
    # The first-party miss is reported; the third-party one is not.
    assert any(
        f.category == EVIDENCE_MODULE_RESOLUTION and "internal/missing" in f.message
        for f in result.findings
    ), result.findings
    assert not any("nonexistent/pkg" in f.message for f in result.findings), (
        f"the uninstalled third-party import must be tolerated, not reported: {result.findings}"
    )


def test_go_vet_catches_suspicious_construct(tmp_path: Path) -> None:
    """A ``go vet``-only finding (Printf format/arg mismatch) → RED (other).

    Proves ``go vet`` runs and contributes findings beyond pure compile errors —
    a string passed to ``%d`` compiles fine but vet flags it.
    """
    _require_go()
    _scaffold(tmp_path)
    _write(
        tmp_path,
        "lib.go",
        'package m\n\nimport "fmt"\n\n'
        'func Use() {\n\tname := "x"\n\tfmt.Printf("%d\\n", name)\n}\n',
    )

    result = _run(tmp_path)

    assert result.passed is False, "go vet must catch the Printf format mismatch"
    assert result.category_counts().get(EVIDENCE_OTHER, 0) >= 1
    assert any("[vet]" in f.message for f in result.findings), result.findings
