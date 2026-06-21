"""Contract Kernel §2 — generic oracle command-sequence executor anti-false-green matrix.

Exercises :func:`codd.languages.oracle_executor.run_command_sequence` across every
branch DETERMINISTICALLY, the same way ``test_verify_executor`` does:

* a FAKE :class:`~codd.languages.adapters.implement_oracle.ImplementOracleAdapter`
  returns a crafted :class:`OracleStepObservation` per command (so the per-step
  verdict is fully controlled), and records which commands it normalized (so a test
  can prove a step was NEVER spawned); and
* tiny ``python -c`` fixture commands exit with a chosen code (or are deliberately
  un-spawnable) so spawn / exit / cwd are controlled too.

THE INVARIANT UNDER TEST: a not-clean signal ALWAYS beats a clean-looking result,
and an opaque nonzero step (adapter returns no findings) is an
``environment_build_error`` RED — never a benign pass. The executor is UNCONNECTED
to the live gate; these fixtures are its only driver.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

import pytest

from codd.implement_oracle_types import (
    EVIDENCE_ENVIRONMENT_BUILD,
    EVIDENCE_MISSING_SYMBOL,
    ImplementOracleFinding,
)
from codd.languages.adapters.implement_oracle import (
    OracleContext,
    OracleStepObservation,
)
from codd.languages.oracle_executor import run_command_sequence
from codd.languages.profile import (
    CommandSpec,
    Identity,
    ImplementOracleProfileSpec,
    ImplementOracleStepSpec,
    LanguageProfile,
    LayoutSpec,
)


# ── fixtures: a controllable fake adapter + a profile/context builder ──────────


@dataclass
class _FakeOracleAdapter:
    """An oracle adapter whose per-step observation is fully controlled by the test.

    ``observations`` maps a command id → the :class:`OracleStepObservation` to
    return for it (default: a clean observation). ``normalized`` records, in order,
    every ``command_id`` the executor handed to :meth:`normalize_command_result` —
    so a test can assert a step was NEVER reached (e.g. an unsubstituted-placeholder
    step that must not spawn). ``scope_detail`` is what :meth:`certify_scope` returns.
    """

    observations: dict[str, OracleStepObservation] = field(default_factory=dict)
    normalized: list[str] = field(default_factory=list)
    scope_detail: str = "scope certified (fake)"

    def certify_scope(self, ctx: OracleContext) -> str:
        return self.scope_detail

    def normalize_command_result(
        self,
        ctx: OracleContext,
        *,
        command_id: str,
        command: CommandSpec,
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> OracleStepObservation:
        self.normalized.append(command_id)
        if command_id in self.observations:
            return self.observations[command_id]
        # Default: clean iff the process exited 0 (a sane stand-in for a real adapter).
        return OracleStepObservation(is_clean=(returncode == 0))


def _py(code: str) -> tuple[str, ...]:
    """A fixture command: run this python ``code`` with the current interpreter."""
    return (sys.executable, "-c", code)


_EXIT_0 = _py("import sys; sys.exit(0)")
_EXIT_1 = _py("import sys; sys.stderr.write('boom\\n'); sys.exit(1)")


def _command(
    command_id: str,
    argv: tuple[str, ...],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    mutates: tuple[str, ...] = (),
) -> CommandSpec:
    return CommandSpec(
        id=command_id,
        argv=argv,
        cwd=cwd,
        env=MappingProxyType(dict(env or {})),
        mutates=mutates,
    )


def _context(
    project_root: Path,
    commands: dict[str, CommandSpec],
    *,
    module_root: str = ".",
    config: dict | None = None,
) -> OracleContext:
    """Build an OracleContext over a minimal LanguageProfile carrying ``commands``."""
    layout = LayoutSpec(repo_root=".", module_root=module_root, manifest_root=".")
    profile = LanguageProfile(
        identity=Identity(id="testlang", display_name="Test Lang"),
        layout=layout,
        commands=MappingProxyType(dict(commands)),
        implement_oracle=ImplementOracleProfileSpec(
            kind="composite",
            adapter="fake",
            steps=tuple(ImplementOracleStepSpec(command=c) for c in commands),
        ),
    )
    return OracleContext(
        project_root=project_root,
        layout_profile=layout,
        language_profile=profile,
        oracle=profile.implement_oracle,
        config=config,
    )


# ── the matrix ─────────────────────────────────────────────────────────────


def test_clean_multi_step_passes(tmp_path: Path) -> None:
    cmds = {"typecheck": _command("typecheck", _EXIT_0), "vet": _command("vet", _EXIT_0)}
    ctx = _context(tmp_path, cmds)
    adapter = _FakeOracleAdapter()
    result = run_command_sequence(ctx, ["typecheck", "vet"], adapter)
    assert result.passed is True
    assert result.executed is True
    assert result.findings == []
    # both steps were normalized (every step required + executed).
    assert adapter.normalized == ["typecheck", "vet"]


def test_step_nonzero_with_adapter_findings_is_red_with_those_findings(tmp_path: Path) -> None:
    cmds = {"typecheck": _command("typecheck", _EXIT_1)}
    ctx = _context(tmp_path, cmds)
    finding = ImplementOracleFinding(
        category=EVIDENCE_MISSING_SYMBOL, code="TS2305", message="no exported member 'X'", path="a.ts"
    )
    adapter = _FakeOracleAdapter(
        observations={
            "typecheck": OracleStepObservation(
                is_clean=False, findings=(finding,), failed_paths=("a.ts",), detail="missing symbol"
            )
        }
    )
    result = run_command_sequence(ctx, ["typecheck"], adapter)
    assert result.passed is False
    assert finding in result.findings
    assert result.failed_paths == ["a.ts"]
    # the adapter's real finding is surfaced — NOT replaced by an opaque env error.
    assert all(f.category != EVIDENCE_ENVIRONMENT_BUILD for f in result.findings)


def test_step_nonzero_with_no_findings_is_environment_build_error_red(tmp_path: Path) -> None:
    cmds = {"typecheck": _command("typecheck", _EXIT_1)}
    ctx = _context(tmp_path, cmds)
    # Adapter says "not clean" but offers NO diagnostic — the opaque-failure case.
    adapter = _FakeOracleAdapter(
        observations={"typecheck": OracleStepObservation(is_clean=False, findings=())}
    )
    result = run_command_sequence(ctx, ["typecheck"], adapter)
    assert result.passed is False
    assert len(result.findings) == 1
    assert result.findings[0].category == EVIDENCE_ENVIRONMENT_BUILD
    assert "environment_build_error" in result.findings[0].code


def test_unsubstituted_placeholder_cwd_is_red_and_never_spawns(tmp_path: Path) -> None:
    # cwd carries a placeholder the layout does NOT resolve ({nonexistent_root}).
    cmds = {
        "typecheck": _command("typecheck", _EXIT_0, cwd="{nonexistent_root}"),
    }
    ctx = _context(tmp_path, cmds)
    adapter = _FakeOracleAdapter()
    result = run_command_sequence(ctx, ["typecheck"], adapter)
    assert result.passed is False
    assert any(
        f.code == "oracle_unsubstituted_placeholder" and f.category == EVIDENCE_ENVIRONMENT_BUILD
        for f in result.findings
    )
    # CRITICAL anti-false-green: the step was NEVER spawned (so never normalized) —
    # we did not run in a literal "{nonexistent_root}" directory.
    assert adapter.normalized == []


def test_substituted_placeholder_cwd_runs_in_resolved_dir(tmp_path: Path) -> None:
    # {module_root} IS resolvable (the layout sets it to "svc"); the dir exists and the
    # command runs there — proving substitution works (the placeholder guard only reds
    # UNresolved tokens, never a legitimately-substituted one).
    (tmp_path / "svc").mkdir()
    cmds = {
        "typecheck": _command(
            "typecheck",
            _py(
                "import os,sys; "
                "open(os.path.join(os.getcwd(),'ran.txt'),'w').close(); "
                "sys.exit(0)"
            ),
            cwd="{module_root}",
        ),
    }
    ctx = _context(tmp_path, cmds, module_root="svc")
    adapter = _FakeOracleAdapter()
    result = run_command_sequence(ctx, ["typecheck"], adapter)
    assert result.passed is True
    assert adapter.normalized == ["typecheck"]
    # the marker landed in <project>/svc, NOT <project>/{module_root}.
    assert (tmp_path / "svc" / "ran.txt").is_file()
    assert not (tmp_path / "{module_root}").exists()


def test_unsubstituted_placeholder_in_env_is_red(tmp_path: Path) -> None:
    cmds = {
        "typecheck": _command("typecheck", _EXIT_0, env={"OUT_DIR": "{unresolved_dir}"}),
    }
    ctx = _context(tmp_path, cmds)
    adapter = _FakeOracleAdapter()
    result = run_command_sequence(ctx, ["typecheck"], adapter)
    assert result.passed is False
    assert any(f.code == "oracle_unsubstituted_placeholder" for f in result.findings)
    assert adapter.normalized == []


def test_mutating_command_step_is_rejected_red(tmp_path: Path) -> None:
    # A command that declares mutates (go mod tidy class) cannot be an oracle step.
    cmds = {
        "tidy": _command("tidy", _EXIT_0, mutates=("go.mod", "go.sum")),
    }
    ctx = _context(tmp_path, cmds)
    adapter = _FakeOracleAdapter()
    result = run_command_sequence(ctx, ["tidy"], adapter)
    assert result.passed is False
    assert any(f.code == "oracle_step_mutates" for f in result.findings)
    # rejected BEFORE spawning — a mutating command must not run as an oracle step.
    assert adapter.normalized == []


def test_spawn_failure_is_red(tmp_path: Path) -> None:
    # An argv whose program does not exist → spawn failure (FileNotFoundError).
    cmds = {
        "typecheck": _command("typecheck", ("this-binary-does-not-exist-xyz", "--noEmit")),
    }
    ctx = _context(tmp_path, cmds)
    adapter = _FakeOracleAdapter()
    result = run_command_sequence(ctx, ["typecheck"], adapter)
    assert result.passed is False
    assert any(
        f.code == "oracle_typecheck_spawn_error" and f.category == EVIDENCE_ENVIRONMENT_BUILD
        for f in result.findings
    )
    assert adapter.normalized == []  # never normalized — it never ran


def test_missing_command_id_is_red(tmp_path: Path) -> None:
    cmds = {"typecheck": _command("typecheck", _EXIT_0)}
    ctx = _context(tmp_path, cmds)
    adapter = _FakeOracleAdapter()
    # ask for a command id the profile does not declare
    result = run_command_sequence(ctx, ["typecheck", "vet"], adapter)
    assert result.passed is False
    assert any(f.code == "oracle_command_missing" for f in result.findings)
    # the present step still ran; only the missing one reds.
    assert adapter.normalized == ["typecheck"]


def test_empty_command_sequence_is_red(tmp_path: Path) -> None:
    ctx = _context(tmp_path, {"typecheck": _command("typecheck", _EXIT_0)})
    adapter = _FakeOracleAdapter()
    result = run_command_sequence(ctx, [], adapter)
    assert result.passed is False
    assert any(f.code == "oracle_no_commands" for f in result.findings)
    assert adapter.normalized == []


def test_timeout_is_red(tmp_path: Path) -> None:
    cmds = {"slow": _command("slow", _py("import time; time.sleep(5)"))}
    ctx = _context(tmp_path, cmds, config={"implement": {"oracle_timeout_seconds": 0.5}})
    adapter = _FakeOracleAdapter()
    result = run_command_sequence(ctx, ["slow"], adapter)
    assert result.passed is False
    assert any(
        f.code == "oracle_slow_timeout" and f.category == EVIDENCE_ENVIRONMENT_BUILD
        for f in result.findings
    )
    assert adapter.normalized == []  # timed out before normalization


def test_one_clean_one_dirty_step_is_red(tmp_path: Path) -> None:
    # Mixed sequence: every step required, so one dirty step reds the whole run even
    # though the other is clean.
    cmds = {
        "typecheck": _command("typecheck", _EXIT_0),
        "vet": _command("vet", _EXIT_1),
    }
    ctx = _context(tmp_path, cmds)
    finding = ImplementOracleFinding(category=EVIDENCE_MISSING_SYMBOL, code="V1", message="vet issue")
    adapter = _FakeOracleAdapter(
        observations={"vet": OracleStepObservation(is_clean=False, findings=(finding,))}
    )
    result = run_command_sequence(ctx, ["typecheck", "vet"], adapter)
    assert result.passed is False
    assert finding in result.findings
    assert adapter.normalized == ["typecheck", "vet"]  # both executed
