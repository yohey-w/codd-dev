"""Characterization tests for the modeled ``implement_oracle`` declaration.

Contract Kernel oracle dispatch, Steps 0–1: ``implement_oracle`` is parsed out
of ``LanguageProfile.extra`` into a first-class
:class:`ImplementOracleProfileSpec` (+ :class:`ImplementOracleStepSpec`).

These tests pin TWO things:

* the three BUNDLED profiles parse into the new model with the expected
  kind / adapter / steps (and are NO LONGER left in ``.extra``);
* each fail-closed validation raises :class:`LanguageProfileError` on a
  malformed block (unknown kind, empty composite steps, a step whose command is
  not declared, kind=command with steps, a missing adapter, and the kind-misuse
  cross-checks).

No validation is weakened to make a bundled profile pass — if a bundled profile
failed validation that would be a real inconsistency, surfaced here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.languages import (
    ImplementOracleProfileSpec,
    ImplementOracleStepSpec,
    load_language_profile,
)
from codd.languages.loader import LanguageProfileError
from codd.languages.registry import PROFILES_DIR


# ---------------------------------------------------------------------------
# bundled profiles parse into the new model (Step 1)
# ---------------------------------------------------------------------------


def test_go_profile_implement_oracle_is_composite_typecheck_vet() -> None:
    profile = load_language_profile(PROFILES_DIR / "go.yaml")
    oracle = profile.implement_oracle
    assert isinstance(oracle, ImplementOracleProfileSpec)
    assert oracle.kind == "composite"
    assert oracle.adapter == "go-toolchain"
    assert oracle.command is None
    assert oracle.steps == (
        ImplementOracleStepSpec(command="typecheck"),
        ImplementOracleStepSpec(command="vet"),
    )
    # every composite step references a real command id.
    for step in oracle.steps:
        assert step.command in profile.commands


def test_typescript_profile_implement_oracle_is_command_typecheck() -> None:
    profile = load_language_profile(PROFILES_DIR / "typescript.yaml")
    oracle = profile.implement_oracle
    assert isinstance(oracle, ImplementOracleProfileSpec)
    assert oracle.kind == "command"
    assert oracle.adapter == "typescript-tsc"
    assert oracle.command == "typecheck"
    assert oracle.steps == ()
    assert oracle.command in profile.commands


def test_python_profile_implement_oracle_is_adapter() -> None:
    profile = load_language_profile(PROFILES_DIR / "python.yaml")
    oracle = profile.implement_oracle
    assert isinstance(oracle, ImplementOracleProfileSpec)
    assert oracle.kind == "adapter"
    assert oracle.adapter == "python-composite"
    assert oracle.command is None
    assert oracle.steps == ()


@pytest.mark.parametrize("name", ["go", "typescript", "python"])
def test_implement_oracle_no_longer_in_extra(name: str) -> None:
    """The migration is complete: implement_oracle is a field, not raw extra."""
    profile = load_language_profile(PROFILES_DIR / f"{name}.yaml")
    assert "implement_oracle" not in profile.extra
    assert profile.implement_oracle is not None


# ---------------------------------------------------------------------------
# fail-closed validation (Step 1)
# ---------------------------------------------------------------------------

# A minimal-but-valid profile body the malformed implement_oracle blocks attach
# to. It declares the command ids the valid composite/command kinds reference, so
# only the implement_oracle block under test is the cause of any RED.
_BASE_PROFILE = """\
id: sample
display_name: "Sample"
strictness: strict
file_extensions: [".smp"]

layout:
  source_sets:
    - id: src
      root: "src"
      file_globs: ["src/**/*.smp"]

commands:
  typecheck:
    argv: ["sample", "typecheck"]
  vet:
    argv: ["sample", "vet"]

implement_oracle:
"""


def _write_profile(tmp_path: Path, oracle_block: str) -> Path:
    body = _BASE_PROFILE + "\n".join(
        f"  {line}" if line else "" for line in oracle_block.splitlines()
    )
    p = tmp_path / "sample.yaml"
    p.write_text(body + "\n", encoding="utf-8")
    return p


def test_base_profile_with_valid_oracle_loads(tmp_path: Path) -> None:
    """Sanity: the shared base profile + a valid block loads (so the negative
    cases below isolate the malformed block, not a broken base)."""
    p = _write_profile(
        tmp_path,
        "kind: composite\nadapter: sample-toolchain\nsteps:\n  - command: typecheck\n  - command: vet",
    )
    profile = load_language_profile(p)
    assert profile.implement_oracle is not None
    assert profile.implement_oracle.kind == "composite"
    assert profile.implement_oracle.adapter == "sample-toolchain"


def test_unknown_kind_is_red(tmp_path: Path) -> None:
    p = _write_profile(tmp_path, "kind: bogus\nadapter: sample-toolchain")
    with pytest.raises(LanguageProfileError, match="unknown kind"):
        load_language_profile(p)


def test_command_kind_with_no_command_is_red(tmp_path: Path) -> None:
    p = _write_profile(tmp_path, "kind: command\nadapter: sample-toolchain")
    with pytest.raises(LanguageProfileError, match="requires a 'command'"):
        load_language_profile(p)


def test_command_kind_with_steps_is_red(tmp_path: Path) -> None:
    p = _write_profile(
        tmp_path,
        "kind: command\nadapter: sample-toolchain\ncommand: typecheck\n"
        "steps:\n  - command: vet",
    )
    with pytest.raises(LanguageProfileError, match="must not declare 'steps'"):
        load_language_profile(p)


def test_composite_kind_with_empty_steps_is_red(tmp_path: Path) -> None:
    p = _write_profile(tmp_path, "kind: composite\nadapter: sample-toolchain\nsteps: []")
    with pytest.raises(LanguageProfileError, match="non-empty 'steps'"):
        load_language_profile(p)


def test_composite_kind_with_command_is_red(tmp_path: Path) -> None:
    p = _write_profile(
        tmp_path,
        "kind: composite\nadapter: sample-toolchain\ncommand: typecheck\n"
        "steps:\n  - command: vet",
    )
    with pytest.raises(LanguageProfileError, match="must not declare 'command'"):
        load_language_profile(p)


def test_composite_step_missing_command_ref_is_red(tmp_path: Path) -> None:
    p = _write_profile(
        tmp_path,
        "kind: composite\nadapter: sample-toolchain\nsteps:\n  - command: nonexistent",
    )
    with pytest.raises(LanguageProfileError, match="not declared in 'commands'"):
        load_language_profile(p)


def test_command_kind_missing_command_ref_is_red(tmp_path: Path) -> None:
    p = _write_profile(tmp_path, "kind: command\nadapter: sample-toolchain\ncommand: nonexistent")
    with pytest.raises(LanguageProfileError, match="not declared in 'commands'"):
        load_language_profile(p)


def test_adapter_kind_with_command_is_red(tmp_path: Path) -> None:
    p = _write_profile(tmp_path, "kind: adapter\nadapter: sample-composite\ncommand: typecheck")
    with pytest.raises(LanguageProfileError, match="must not declare 'command'"):
        load_language_profile(p)


def test_adapter_kind_with_steps_is_red(tmp_path: Path) -> None:
    p = _write_profile(
        tmp_path,
        "kind: adapter\nadapter: sample-composite\nsteps:\n  - command: typecheck",
    )
    with pytest.raises(LanguageProfileError, match="must not declare 'steps'"):
        load_language_profile(p)


@pytest.mark.parametrize(
    "block",
    [
        "kind: command\ncommand: typecheck",
        "kind: composite\nsteps:\n  - command: typecheck",
        "kind: adapter",
    ],
)
def test_missing_adapter_is_red(tmp_path: Path, block: str) -> None:
    p = _write_profile(tmp_path, block)
    with pytest.raises(LanguageProfileError, match="missing required key 'adapter'"):
        load_language_profile(p)
