"""Tests for the implement-time native-oracle gate (A-core anti-false-green).

The gate moves a compiler-class coherence oracle (TypeScript = ``tsc --noEmit``)
from the verify stage EARLIER into the greenfield IMPLEMENT stage — while the SUT
can still freely edit ALL files — so cross-artifact symbol/module incoherence
(``src/index.ts`` importing a ``runCli`` that ``./cli`` never exports; a test
importing ``repoRoot`` when the helper exports ``projectRoot`` → TS2305/2724/
2459) is made coherent BEFORE verify, where auto-repair is scope-blocked from
rewriting test files.

Two layers of coverage:

1. **Pure-unit** (always run; no toolchain): evidence normalization (tsc codes →
   language-neutral categories), scope CERTIFICATION (a green oracle over an
   uncovered test tree is a HARD FAIL, not a silent pass), and the profile-driven
   NO-OP for stacks without a declared oracle (Python today) + the opt-out.

2. **REAL tsc integration** (guarded on ``npm`` + a successful install): a TS
   fixture with a DELIBERATE symbol mismatch → assert the gate CATCHES it by
   running REAL ``tsc`` (NOT a mock/string-match — the ca3dfc7 regression shipped
   an invalid flag precisely because its test only string-matched the command and
   never ran the real tool) → fix the symbols → assert it PASSES; plus the bounded
   retry-with-feedback loop and the honest failure when uncurable.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from codd.implement_oracle import (
    EVIDENCE_CATEGORIES,
    EVIDENCE_ENVIRONMENT_BUILD,
    EVIDENCE_MISSING_SYMBOL,
    EVIDENCE_MODULE_RESOLUTION,
    OracleScopeError,
    certify_oracle_scope,
    normalize_oracle_output,
    resolve_implement_oracle,
    run_implement_oracle_gate,
)
from codd.project_types import (
    ImplementOracleSpec,
    OracleScopeSpec,
    resolve_layout_profile,
)


def _ts_profile(name: str = "demo-cli"):
    profile = resolve_layout_profile(language="typescript", project_name=name)
    assert profile is not None and profile.implement_oracle is not None
    return profile


# ════════════════════════════════════════════════════════════
# Profile wiring
# ════════════════════════════════════════════════════════════

def test_typescript_profile_declares_tsc_oracle() -> None:
    profile = _ts_profile()
    spec = profile.implement_oracle
    assert isinstance(spec, ImplementOracleSpec)
    assert spec.command == "npx --no-install tsc --noEmit"
    assert spec.kind == "compiler"
    assert spec.requires_node_install is True
    assert spec.scope.require_source_root is True
    assert spec.scope.require_test_root is True
    # The oracle is serialized for diagnostics / doctor surfaces.
    assert profile.to_dict()["implement_oracle"]["command"] == spec.command


def test_python_profile_has_no_oracle_yet() -> None:
    """DEFERRED: Python's composite oracle is a separate task — gate is a no-op."""
    profile = resolve_layout_profile(language="python", project_name="calc-lib")
    assert profile is not None
    assert profile.implement_oracle is None


def test_resolve_implement_oracle_none_for_python(tmp_path: Path) -> None:
    assert (
        resolve_implement_oracle(
            tmp_path, language="python", project_name="calc-lib", config={}
        )
        is None
    )


def test_resolve_implement_oracle_none_when_opted_out(tmp_path: Path) -> None:
    assert (
        resolve_implement_oracle(
            tmp_path,
            language="typescript",
            project_name="demo-cli",
            config={"implement": {"implement_oracle": False}},
        )
        is None
    )


def test_resolve_implement_oracle_present_for_typescript(tmp_path: Path) -> None:
    resolved = resolve_implement_oracle(
        tmp_path, language="typescript", project_name="demo-cli", config={}
    )
    assert resolved is not None
    _profile, spec = resolved
    assert spec.command == "npx --no-install tsc --noEmit"


# ════════════════════════════════════════════════════════════
# Evidence normalization (tsc codes → language-neutral categories)
# ════════════════════════════════════════════════════════════

def test_normalize_categorizes_missing_symbol_and_module_resolution(tmp_path: Path) -> None:
    profile = _ts_profile()
    output = (
        "src/index.ts(1,15): error TS2305: Module './cli.js' has no exported member 'runCli'.\n"
        "src/index.ts(2,22): error TS2307: Cannot find module './nope.js' or its types.\n"
        "tests/helpers/io.ts(3,1): error TS2552: Cannot find name 'repoRoot'. Did you mean 'projectRoot'?\n"
    )
    findings, paths = normalize_oracle_output(
        output, command="npx tsc --noEmit", project_root=tmp_path, profile=profile
    )
    by_code = {f.code: f.category for f in findings}
    assert by_code["TS2305"] == EVIDENCE_MISSING_SYMBOL
    assert by_code["TS2307"] == EVIDENCE_MODULE_RESOLUTION
    assert by_code["TS2552"] == EVIDENCE_MISSING_SYMBOL
    # Attribution resolves both the source AND the test/helper file.
    assert "src/index.ts" in paths
    assert "tests/helpers/io.ts" in paths


def test_normalize_unmapped_code_is_type_error_not_dropped(tmp_path: Path) -> None:
    profile = _ts_profile()
    output = "src/x.ts(1,1): error TS2322: Type 'string' is not assignable to type 'number'.\n"
    findings, _paths = normalize_oracle_output(
        output, command="npx tsc --noEmit", project_root=tmp_path, profile=profile
    )
    assert len(findings) == 1
    assert findings[0].category == "type_error"
    assert findings[0].category in EVIDENCE_CATEGORIES


def test_normalize_no_inputs_is_environment_build_error(tmp_path: Path) -> None:
    profile = _ts_profile()
    output = "error TS18003: No inputs were found in config file 'tsconfig.json'.\n"
    findings, _paths = normalize_oracle_output(
        output, command="npx tsc --noEmit", project_root=tmp_path, profile=profile
    )
    assert any(f.category == EVIDENCE_ENVIRONMENT_BUILD for f in findings)


# ════════════════════════════════════════════════════════════
# Scope certification (anti-false-green: oracle must SEE src + tests)
# ════════════════════════════════════════════════════════════

def _write_tsconfig(root: Path, include: list[str]) -> None:
    root.joinpath("tsconfig.json").write_text(
        json.dumps({"compilerOptions": {"noEmit": True}, "include": include}),
        encoding="utf-8",
    )


def test_certify_scope_passes_for_recursive_src_and_tests(tmp_path: Path) -> None:
    profile = _ts_profile()
    _write_tsconfig(tmp_path, ["src/**/*", "tests/**/*"])
    detail = certify_oracle_scope(tmp_path, profile, profile.implement_oracle)
    assert "certified" in detail


def test_certify_scope_passes_for_catch_all_glob(tmp_path: Path) -> None:
    profile = _ts_profile()
    _write_tsconfig(tmp_path, ["**/*"])
    assert "certified" in certify_oracle_scope(tmp_path, profile, profile.implement_oracle)


def test_certify_scope_hard_fails_when_tests_excluded(tmp_path: Path) -> None:
    profile = _ts_profile()
    _write_tsconfig(tmp_path, ["src/**/*"])  # tests NOT covered
    with pytest.raises(OracleScopeError) as exc:
        certify_oracle_scope(tmp_path, profile, profile.implement_oracle)
    assert "tests" in str(exc.value)


def test_certify_scope_hard_fails_for_single_level_glob(tmp_path: Path) -> None:
    """``src/*`` / ``tests/*`` does NOT reach nested e2e/helpers → not certifiable."""
    profile = _ts_profile()
    _write_tsconfig(tmp_path, ["src/*", "tests/*"])
    with pytest.raises(OracleScopeError):
        certify_oracle_scope(tmp_path, profile, profile.implement_oracle)


def test_certify_scope_hard_fails_when_no_tsconfig(tmp_path: Path) -> None:
    profile = _ts_profile()
    with pytest.raises(OracleScopeError) as exc:
        certify_oracle_scope(tmp_path, profile, profile.implement_oracle)
    assert "tsconfig" in str(exc.value)


def test_certify_scope_hard_fails_when_no_include_declared(tmp_path: Path) -> None:
    profile = _ts_profile()
    tmp_path.joinpath("tsconfig.json").write_text(
        json.dumps({"compilerOptions": {"noEmit": True}}), encoding="utf-8"
    )
    with pytest.raises(OracleScopeError):
        certify_oracle_scope(tmp_path, profile, profile.implement_oracle)


def test_certify_scope_parses_jsonc_with_comments(tmp_path: Path) -> None:
    profile = _ts_profile()
    tmp_path.joinpath("tsconfig.json").write_text(
        '{\n  // scaffolded\n  "compilerOptions": { "noEmit": true },\n'
        '  "include": ["src/**/*", "tests/**/*"] /* covers all */\n}\n',
        encoding="utf-8",
    )
    assert "certified" in certify_oracle_scope(tmp_path, profile, profile.implement_oracle)


def test_certify_scope_respects_relaxed_test_requirement(tmp_path: Path) -> None:
    """A spec that does not require the test root certifies on src-only include."""
    profile = _ts_profile()
    spec = ImplementOracleSpec(
        command="npx --no-install tsc --noEmit",
        scope=OracleScopeSpec(require_source_root=True, require_test_root=False),
    )
    _write_tsconfig(tmp_path, ["src/**/*"])
    assert "certified" in certify_oracle_scope(tmp_path, profile, spec)


# ════════════════════════════════════════════════════════════
# Gate NO-OP behavior (no toolchain needed)
# ════════════════════════════════════════════════════════════

def test_gate_is_noop_for_python(tmp_path: Path) -> None:
    result = run_implement_oracle_gate(
        tmp_path, language="python", project_name="calc-lib", config={}, echo=lambda _m: None
    )
    assert result.passed is True
    assert result.executed is False


def test_gate_is_noop_when_opted_out(tmp_path: Path) -> None:
    result = run_implement_oracle_gate(
        tmp_path,
        language="typescript",
        project_name="demo-cli",
        config={"implement": {"implement_oracle": False}},
        echo=lambda _m: None,
    )
    assert result.passed is True
    assert result.executed is False


# ════════════════════════════════════════════════════════════
# REAL tsc integration (guarded — runs the actual TypeScript compiler)
# ════════════════════════════════════════════════════════════

def _npm() -> str | None:
    return shutil.which("npm")


def _install_typescript(project: Path) -> bool:
    """Install a real TypeScript into ``project``; False if unavailable/offline.

    This is the load-bearing anti-mock guarantee: the integration tests below run
    the REAL ``tsc`` binary, so a bad oracle command (cf. the ca3dfc7 ``--include``
    regression) would actually fail here instead of passing a string match.

    MUST be called AFTER the project's ``package.json`` exists so ``npm install``
    records the dependency INTO it (the gate then re-runs ``npm ci`` against the
    coherent lockfile/package.json pair; a mismatch would otherwise wipe
    ``node_modules`` and surface as an honest environment error).
    """
    npm = _npm()
    if npm is None:
        return False
    assert (project / "package.json").is_file(), "write package.json before installing"
    try:
        completed = subprocess.run(
            [npm, "install", "--no-audit", "--no-fund", "--save-dev", "typescript@5"],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and (project / "node_modules" / "typescript").is_dir()


def _ts_fixture(root: Path, *, coherent: bool) -> None:
    """A minimal TS CLI project; ``coherent=False`` plants the symbol mismatch.

    Incoherent form reproduces BOTH classes from the task: a src↔src missing
    export (``src/index.ts`` imports ``runCli`` that ``src/cli.ts`` never exports)
    AND a test↔helper symbol mismatch (the test imports ``repoRoot`` but the
    helper exports ``projectRoot``).
    """
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "helpers").mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(
        json.dumps(
            {"name": "demo-cli", "version": "0.0.0", "private": True, "type": "module"}
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "tsconfig.json").write_text(
        json.dumps(
            {
                "compilerOptions": {
                    "module": "NodeNext",
                    "moduleResolution": "NodeNext",
                    "strict": True,
                    "noEmit": True,
                },
                "include": ["src/**/*", "tests/**/*"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    if coherent:
        (root / "src" / "cli.ts").write_text(
            "export function run(): number { return 0; }\n"
            "export function runCli(): number { return run(); }\n",
            encoding="utf-8",
        )
        (root / "tests" / "helpers" / "io.ts").write_text(
            "export function projectRoot(): string { return '.'; }\n"
            "export function repoRoot(): string { return projectRoot(); }\n",
            encoding="utf-8",
        )
    else:
        # cli.ts exports run only — index.ts imports a non-existent runCli (TS2305).
        (root / "src" / "cli.ts").write_text(
            "export function run(): number { return 0; }\n", encoding="utf-8"
        )
        # helper exports projectRoot — test imports a non-existent repoRoot (TS2305).
        (root / "tests" / "helpers" / "io.ts").write_text(
            "export function projectRoot(): string { return '.'; }\n", encoding="utf-8"
        )
    (root / "src" / "index.ts").write_text(
        'import { run, runCli } from "./cli.js";\nexport { run, runCli };\n', encoding="utf-8"
    )
    (root / "tests" / "cli.test.ts").write_text(
        'import { repoRoot } from "./helpers/io.js";\nexport const x = repoRoot();\n',
        encoding="utf-8",
    )


def _prepared_ts_project(tmp_path: Path, *, coherent: bool) -> Path:
    """A TS fixture with REAL typescript installed, or skip if unavailable.

    Order matters: write the fixture (incl. ``package.json``) FIRST, THEN install
    typescript so the dependency lands in the project's own ``package.json`` and
    the lockfile/package.json pair the gate's ``npm ci`` re-validates is coherent.
    """
    project = tmp_path / "proj"
    project.mkdir()
    if _npm() is None:
        pytest.skip("npm not available (no node) — real-tsc test skipped")
    _ts_fixture(project, coherent=coherent)
    if not _install_typescript(project):
        pytest.skip("TypeScript install unavailable (offline?) — real-tsc test skipped")
    return project


def test_real_tsc_gate_catches_symbol_mismatch(tmp_path: Path) -> None:
    """REAL ``tsc``: an incoherent build is REJECTED with the right categories."""
    ts_project = _prepared_ts_project(tmp_path, coherent=False)
    result = run_implement_oracle_gate(
        ts_project,
        language="typescript",
        project_name="demo-cli",
        config={},
        rerun=None,  # no fix-up: assert the gate catches the planted mismatch
        echo=lambda _m: None,
    )
    assert result.executed is True
    assert result.passed is False
    # Both planted mismatches are missing-symbol class (TS2305).
    counts = result.category_counts()
    assert counts.get(EVIDENCE_MISSING_SYMBOL, 0) >= 2, counts
    # Attribution names the real offending files (src AND test).
    assert any(p.endswith("index.ts") for p in result.failed_paths), result.failed_paths
    assert any("cli.test.ts" in p for p in result.failed_paths), result.failed_paths


def test_real_tsc_gate_passes_on_coherent_build(tmp_path: Path) -> None:
    """REAL ``tsc``: a coherent build PASSES (no false-RED)."""
    ts_project = _prepared_ts_project(tmp_path, coherent=True)
    result = run_implement_oracle_gate(
        ts_project, language="typescript", project_name="demo-cli", config={}, echo=lambda _m: None
    )
    assert result.executed is True
    assert result.passed is True


def test_real_tsc_gate_retries_then_passes_after_fix(tmp_path: Path) -> None:
    """REAL ``tsc`` + bounded feedback loop: incoherent → rerun fixes → PASS."""
    ts_project = _prepared_ts_project(tmp_path, coherent=False)
    calls = {"n": 0}

    def rerun(feedback: str) -> None:
        calls["n"] += 1
        assert "tsc --noEmit" in feedback  # the SUT-facing message names the oracle
        # The retry "regenerates" coherent source + helper.
        (ts_project / "src" / "cli.ts").write_text(
            "export function run(): number { return 0; }\n"
            "export function runCli(): number { return run(); }\n",
            encoding="utf-8",
        )
        (ts_project / "tests" / "helpers" / "io.ts").write_text(
            "export function projectRoot(): string { return '.'; }\n"
            "export function repoRoot(): string { return projectRoot(); }\n",
            encoding="utf-8",
        )

    result = run_implement_oracle_gate(
        ts_project,
        language="typescript",
        project_name="demo-cli",
        config={"implement": {"oracle_max_attempts": 3}},
        rerun=rerun,
        echo=lambda _m: None,
    )
    assert calls["n"] == 1, "exactly one corrective retry should have been needed"
    assert result.passed is True


def test_real_tsc_gate_honest_failure_when_uncurable(tmp_path: Path) -> None:
    """REAL ``tsc``: a rerun that never fixes → honest fail after bounded retries."""
    ts_project = _prepared_ts_project(tmp_path, coherent=False)
    calls = {"n": 0}

    def rerun_noop(_feedback: str) -> None:
        calls["n"] += 1  # never fixes

    result = run_implement_oracle_gate(
        ts_project,
        language="typescript",
        project_name="demo-cli",
        config={"implement": {"oracle_max_attempts": 3}},
        rerun=rerun_noop,
        echo=lambda _m: None,
    )
    assert result.passed is False
    assert calls["n"] == 2, "3 total attempts = 2 corrective retries"
    assert result.category_counts().get(EVIDENCE_MISSING_SYMBOL, 0) >= 1


def test_real_tsc_gate_hard_fails_uncertifiable_scope(tmp_path: Path) -> None:
    """REAL toolchain present, but tsconfig excludes tests → OracleScopeError.

    Even with a build that would typecheck clean over src, an oracle that cannot
    SEE the test tree is a hard fail — proving the scope gate fires before trust.
    """
    ts_project = _prepared_ts_project(tmp_path, coherent=True)
    # Narrow the scaffolded tsconfig so tests are NOT in scope.
    (ts_project / "tsconfig.json").write_text(
        json.dumps({"compilerOptions": {"noEmit": True}, "include": ["src/**/*"]}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(OracleScopeError):
        run_implement_oracle_gate(
            ts_project, language="typescript", project_name="demo-cli", config={}, echo=lambda _m: None
        )


# ════════════════════════════════════════════════════════════
# REAL tsc + SCOPED rerun (the localized-rerun design, end-to-end)
# ════════════════════════════════════════════════════════════


class _ScopeTask:
    """An ImplementTaskRef-shaped stand-in for the scope index."""

    def __init__(self, task_id: str, output_paths) -> None:
        self.task_id = task_id
        self.output_paths = tuple(output_paths)


def _scope_index_for_fixture(project: Path):
    """File-level tasks so importer (index.ts) and exporter (cli.ts) own SEPARATE
    tasks — the only way to prove the scope includes BOTH ends, not just one."""
    from codd.implement_oracle_scope import build_path_owner_index

    tasks = [
        _ScopeTask("index_task", ["src/index.ts"]),
        _ScopeTask("cli_task", ["src/cli.ts"]),
        _ScopeTask("test_task", ["tests/cli.test.ts"]),
        _ScopeTask("helper_task", ["tests/helpers/io.ts"]),
    ]
    return build_path_owner_index(tasks, project_root=project)


def test_real_tsc_scoped_rerun_targets_both_ends(tmp_path: Path) -> None:
    """REAL ``tsc`` + scoped rerun: the scope includes BOTH the importer's task
    AND the exporter's task (the fix may belong to either) — not all tasks, not
    only the importer. The corrective rerun then fixes both and the gate PASSES.
    """
    from codd.implement_oracle_scope import SCOPE_NARROW

    ts_project = _prepared_ts_project(tmp_path, coherent=False)
    index = _scope_index_for_fixture(ts_project)
    captured_scopes: list = []

    def rerun(feedback: str, scope=None) -> None:
        captured_scopes.append(scope)
        # Fix BOTH ends so the next real tsc run is clean.
        (ts_project / "src" / "cli.ts").write_text(
            "export function run(): number { return 0; }\n"
            "export function runCli(): number { return run(); }\n",
            encoding="utf-8",
        )
        (ts_project / "tests" / "helpers" / "io.ts").write_text(
            "export function projectRoot(): string { return '.'; }\n"
            "export function repoRoot(): string { return projectRoot(); }\n",
            encoding="utf-8",
        )

    result = run_implement_oracle_gate(
        ts_project,
        language="typescript",
        project_name="demo-cli",
        config={"implement": {"oracle_max_attempts": 3}},
        rerun=rerun,
        echo=lambda _m: None,
        scope_index=index,
    )
    assert result.passed is True
    assert len(captured_scopes) == 1, "one corrective scoped rerun"
    scope = captured_scopes[0]
    assert scope is not None and not scope.is_broad(), "rerun must be SCOPED, not broad"
    assert scope.rung == SCOPE_NARROW
    task_set = set(scope.task_ids)
    # src↔src edge: importer index_task AND exporter cli_task.
    assert "index_task" in task_set, task_set
    assert "cli_task" in task_set, "exporter task must be in scope (naive-targeted is invalid)"
    # test↔helper edge: importer test_task AND exporter helper_task.
    assert "test_task" in task_set, task_set
    assert "helper_task" in task_set, task_set
    # The write-fence allowed BOTH edge files (the scope is genuinely localized).
    allowed = set(scope.allowed_paths)
    assert "src/cli.ts" in allowed and "src/index.ts" in allowed
