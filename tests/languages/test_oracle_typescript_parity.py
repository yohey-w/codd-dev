"""Category-parity oracle for the TypeScript ``typescript-tsc`` contract switch
(Contract Kernel oracle dispatch §7 — the LAST per-language oracle switch).

STEP 0 characterization: these pin the CURRENT (pre-switch) TS oracle behaviour
through the STABLE public surface (``certify_oracle_scope`` / ``normalize_oracle_
output`` / ``run_implement_oracle_gate``) so they pass GREEN against today's code
AND stay green after the tsc tool-semantics relocate into
``codd.languages.adapters.oracle_typescript.TypeScriptTscOracleAdapter`` and TS
routes through the generic ``run_command_sequence`` contract path. The semantics
they lock are the category-parity oracle for the refactor:

  * coherent TS project (tsconfig present, tsc clean) → passed=True.
  * TS2305 (missing exported member) → RED, missing_symbol category.
  * TS2307 (cannot find module) → RED, module_resolution category.
  * TS18003 / "No inputs were found" → RED even on rc 0 (the load-bearing
    false-green guard: tsc can exit 0 yet typecheck nothing).
  * missing tsconfig.json → RED (scope cannot be certified).
  * test root excluded from tsconfig scope → RED.
  * a node-install FAILURE → environment_build_error RED (no retry).

The tsc-running tests are driven by a FAKE ``subprocess.run`` (monkeypatched) so
no real ``tsc``/``npm`` is needed — exactly the testability the legacy TS path had
(the legacy ``_run_oracle_command`` / ``_run_node_install`` both spawn through
``subprocess.run``; the contract path spawns tsc through
``codd.languages.oracle_executor.subprocess.run`` and the install through the
gate's ``subprocess.run``). The REAL-tsc integration coverage stays in
``tests/test_implement_oracle.py`` (guarded on a real npm install).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codd.implement_oracle import (
    EVIDENCE_ENVIRONMENT_BUILD,
    EVIDENCE_MISSING_SYMBOL,
    EVIDENCE_MODULE_RESOLUTION,
    OracleScopeError,
    certify_oracle_scope,
    normalize_oracle_output,
    run_implement_oracle_gate,
)
from codd.project_types import resolve_layout_profile


def _ts_profile(name: str = "demo-cli"):
    profile = resolve_layout_profile(language="typescript", project_name=name)
    assert profile is not None and profile.implement_oracle is not None
    return profile


def _scaffold_ts(root: Path, *, include: list[str] | None = None) -> None:
    """A minimal scaffolded TS project (package.json + tsconfig + one src + test)."""
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(
        '{"name": "demo-cli", "version": "0.0.0", "private": true, "type": "module"}\n',
        encoding="utf-8",
    )
    inc = include if include is not None else ["src/**/*", "tests/**/*"]
    import json as _json

    (root / "tsconfig.json").write_text(
        _json.dumps({"compilerOptions": {"noEmit": True}, "include": inc}) + "\n",
        encoding="utf-8",
    )
    (root / "src" / "index.ts").write_text("export const x = 1;\n", encoding="utf-8")
    (root / "tests" / "index.test.ts").write_text(
        "export const y = 1;\n", encoding="utf-8"
    )


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_subprocess(
    monkeypatch: pytest.MonkeyPatch, *, install_rc: int, tsc_rc: int, tsc_out: str
) -> dict:
    """Monkeypatch BOTH spawn seams (gate install + executor/legacy tsc).

    The install command is run via the gate's ``subprocess.run`` (shell=True, an
    ``npm ci``-shaped command). The tsc command is run via the generic executor's
    ``subprocess.run`` (shell=False argv) on the contract path, or via the gate's
    ``subprocess.run`` (shell=True ``npx … tsc``) on the legacy path. We classify by
    argv/command content so the same fake serves both pre- and post-switch.
    """
    calls: dict[str, int] = {"install": 0, "tsc": 0}

    def _is_tsc(text: str) -> bool:
        # The oracle command is ``npx --no-install tsc --noEmit`` (legacy, shell str)
        # or ``["npx", "--no-install", "tsc", "--noEmit"]`` (contract, argv). It
        # contains the literal token ``tsc`` — checked FIRST so the ``--no-install``
        # substring never misclassifies it as the install command.
        return "tsc" in text

    def _is_install(text: str) -> bool:
        # The install command is ``npm ci`` / ``npm install`` (no ``tsc`` token).
        return "npm" in text and ("ci" in text.split() or "install" in text.split())

    def fake_run(cmd, *args, **kwargs):  # noqa: ANN001
        text = cmd if isinstance(cmd, str) else " ".join(cmd)
        if _is_tsc(text):
            calls["tsc"] += 1
            return _FakeCompleted(tsc_rc, stdout=tsc_out, stderr="")
        if _is_install(text):
            calls["install"] += 1
            return _FakeCompleted(install_rc, stdout="", stderr="install log")
        return _FakeCompleted(0)

    import codd.implement_oracle as gate_mod

    monkeypatch.setattr(gate_mod.subprocess, "run", fake_run)
    # The contract path spawns tsc through the generic executor; patch it too (no-op
    # pre-switch, load-bearing post-switch).
    try:
        import codd.languages.oracle_executor as exec_mod

        monkeypatch.setattr(exec_mod.subprocess, "run", fake_run)
    except Exception:  # noqa: BLE001 — module always present; defensive only.
        pass
    return calls


# ════════════════════════════════════════════════════════════
# Profile wiring (the modeled oracle declaration the switch resolves through)
# ════════════════════════════════════════════════════════════

def test_typescript_profile_declares_command_oracle_via_registry() -> None:
    from codd.languages import default_registry

    p = default_registry.resolve("typescript")
    assert p.implement_oracle is not None
    assert p.implement_oracle.kind == "command"
    assert p.implement_oracle.adapter == "typescript-tsc"
    assert p.implement_oracle.command == "typecheck"
    assert p.commands["typecheck"].argv == ("npx", "--no-install", "tsc", "--noEmit")


# ════════════════════════════════════════════════════════════
# Category parity (normalization) — the stable public normalizer
# ════════════════════════════════════════════════════════════

def test_parity_ts2305_is_missing_symbol(tmp_path: Path) -> None:
    profile = _ts_profile()
    out = "src/index.ts(1,15): error TS2305: Module './cli.js' has no exported member 'runCli'.\n"
    findings, paths = normalize_oracle_output(
        out, command="tsc --noEmit", project_root=tmp_path, profile=profile
    )
    by_code = {f.code: f.category for f in findings}
    assert by_code["TS2305"] == EVIDENCE_MISSING_SYMBOL
    assert any(p.endswith("index.ts") for p in paths)


def test_parity_ts2307_is_module_resolution(tmp_path: Path) -> None:
    profile = _ts_profile()
    out = "src/index.ts(2,22): error TS2307: Cannot find module './nope.js' or its types.\n"
    findings, _paths = normalize_oracle_output(
        out, command="tsc --noEmit", project_root=tmp_path, profile=profile
    )
    by_code = {f.code: f.category for f in findings}
    assert by_code["TS2307"] == EVIDENCE_MODULE_RESOLUTION


def test_parity_ts18003_is_environment_build_error(tmp_path: Path) -> None:
    profile = _ts_profile()
    out = "error TS18003: No inputs were found in config file 'tsconfig.json'.\n"
    findings, _paths = normalize_oracle_output(
        out, command="tsc --noEmit", project_root=tmp_path, profile=profile
    )
    assert any(f.category == EVIDENCE_ENVIRONMENT_BUILD for f in findings)


# ════════════════════════════════════════════════════════════
# Scope certification parity (the stable public certifier)
# ════════════════════════════════════════════════════════════

def test_parity_scope_certifies_when_src_and_tests_covered(tmp_path: Path) -> None:
    profile = _ts_profile()
    _scaffold_ts(tmp_path, include=["src/**/*", "tests/**/*"])
    assert "certified" in certify_oracle_scope(tmp_path, profile, profile.implement_oracle)


def test_parity_scope_red_when_no_tsconfig(tmp_path: Path) -> None:
    profile = _ts_profile()
    with pytest.raises(OracleScopeError):
        certify_oracle_scope(tmp_path, profile, profile.implement_oracle)


def test_parity_scope_red_when_tests_excluded(tmp_path: Path) -> None:
    profile = _ts_profile()
    _scaffold_ts(tmp_path, include=["src/**/*"])  # tests NOT covered
    with pytest.raises(OracleScopeError):
        certify_oracle_scope(tmp_path, profile, profile.implement_oracle)


# ════════════════════════════════════════════════════════════
# Full-gate parity (install preflight + certify + run), tsc/install FAKED
# ════════════════════════════════════════════════════════════

def test_parity_gate_passes_on_clean_tsc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Install OK + tsc exit 0 (no TS18003) → passed=True (a TRUE green)."""
    _scaffold_ts(tmp_path)
    calls = _fake_subprocess(monkeypatch, install_rc=0, tsc_rc=0, tsc_out="")
    result = run_implement_oracle_gate(
        tmp_path, language="typescript", project_name="demo-cli", config={}, echo=lambda _m: None
    )
    assert result.executed is True
    assert result.passed is True, [(f.category, f.code) for f in result.findings]
    assert calls["install"] >= 1  # the node install preflight ran
    assert calls["tsc"] >= 1  # tsc ran


def test_parity_gate_red_on_ts2305(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Install OK + tsc TS2305 → RED, missing_symbol category, no retry callback."""
    _scaffold_ts(tmp_path)
    out = "src/index.ts(1,15): error TS2305: Module './cli.js' has no exported member 'runCli'.\n"
    _fake_subprocess(monkeypatch, install_rc=0, tsc_rc=2, tsc_out=out)
    result = run_implement_oracle_gate(
        tmp_path, language="typescript", project_name="demo-cli", config={}, rerun=None, echo=lambda _m: None
    )
    assert result.executed is True
    assert result.passed is False
    assert result.category_counts().get(EVIDENCE_MISSING_SYMBOL, 0) >= 1, result.category_counts()


def test_parity_gate_red_on_ts18003_even_when_rc_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE false-green guard: tsc exit 0 but TS18003 'No inputs' → NOT a pass."""
    _scaffold_ts(tmp_path)
    out = "error TS18003: No inputs were found in config file 'tsconfig.json'.\n"
    _fake_subprocess(monkeypatch, install_rc=0, tsc_rc=0, tsc_out=out)
    result = run_implement_oracle_gate(
        tmp_path, language="typescript", project_name="demo-cli", config={}, rerun=None, echo=lambda _m: None
    )
    assert result.executed is True
    assert result.passed is False, "tsc exited 0 but typechecked nothing — must be RED"
    assert any(f.category == EVIDENCE_ENVIRONMENT_BUILD for f in result.findings)


def test_parity_gate_red_on_install_failure_no_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A node-install FAILURE → environment_build_error RED, executed, never retried."""
    _scaffold_ts(tmp_path)
    rerun_calls = {"n": 0}

    def _rerun(_feedback, *_a):  # noqa: ANN001
        rerun_calls["n"] += 1

    calls = _fake_subprocess(monkeypatch, install_rc=1, tsc_rc=0, tsc_out="")
    result = run_implement_oracle_gate(
        tmp_path, language="typescript", project_name="demo-cli", config={}, rerun=_rerun, echo=lambda _m: None
    )
    assert result.executed is True
    assert result.passed is False
    assert all(f.category == EVIDENCE_ENVIRONMENT_BUILD for f in result.findings), result.findings
    assert calls["tsc"] == 0  # install failed → tsc never ran
    assert rerun_calls["n"] == 0  # an env failure is not handed to the SUT to "fix"


def test_parity_gate_red_on_uncertifiable_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Install OK but tsconfig excludes the test tree → OracleScopeError (HARD FAIL)."""
    _scaffold_ts(tmp_path, include=["src/**/*"])  # tests excluded
    _fake_subprocess(monkeypatch, install_rc=0, tsc_rc=0, tsc_out="")
    with pytest.raises(OracleScopeError):
        run_implement_oracle_gate(
            tmp_path, language="typescript", project_name="demo-cli", config={}, echo=lambda _m: None
        )
