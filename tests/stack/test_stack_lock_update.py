"""Generic recovery and anti-gaming tests for versioned stack-lock updates."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys

from click.testing import CliRunner
import pytest

import codd.cli as cli_module
from codd.cli import main
from codd.languages.loader import LanguageProfileError, load_language_profile
from codd.languages.profile import CommandSpec, Identity, LanguageProfile, LayoutSpec, SourceSet
from codd.profile_version import validated_profile_version
from codd.stack.compose import ResolvedStackContract, compose
from codd.stack.loader import StackProfileError, load_framework_profile
from codd.stack.lock import (
    LOCK_OK,
    StackLockUpdateError,
    build_lock,
    commit_stack_lock_update,
    dump_lock,
    enforce_stack_lock,
    parse_lock,
    plan_stack_lock_update,
    stack_lock_path,
    verify_lock,
)


def _contract(
    version: str,
    marker: str,
    *,
    command_exit: int | None = None,
) -> ResolvedStackContract:
    """Compose one language-neutral synthetic layer with deterministic raw data."""
    commands = {}
    if command_exit is not None:
        commands["typecheck"] = CommandSpec(
            id="typecheck",
            argv=(
                sys.executable,
                "-c",
                f"raise SystemExit({command_exit})",
            ),
        )
    profile = LanguageProfile(
        identity=Identity(
            id="synthetic",
            display_name="Synthetic",
            profile_version=version,
        ),
        layout=LayoutSpec(
            source_sets=(SourceSet(id="source", root="source"),),
        ),
        commands=commands,
        raw={
            "id": "synthetic",
            "profile_version": version,
            "marker": marker,
            "commands": {
                key: {"argv": list(value.argv)} for key, value in commands.items()
            },
        },
    )
    return compose(profile)


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    codd_dir = project / "codd"
    codd_dir.mkdir(parents=True)
    (codd_dir / "codd.yaml").write_text(
        "project:\n  name: sample\n  language: synthetic\n"
        "stack:\n  language: synthetic\n",
        encoding="utf-8",
    )
    return project


def test_profile_upgrade_plan_preserves_metadata_and_binds_raw_digest() -> None:
    old_contract = _contract("0.1.0", "old")
    new_contract = _contract("0.2.0", "new")
    old_lock = build_lock(
        old_contract,
        adapter_digests={"adapter": "sha256:adapter"},
        permissions={"mode": "strict"},
    )

    plan = plan_stack_lock_update(new_contract, old_lock)

    assert [(u.kind, u.id, u.old_version, u.new_version) for u in plan.layer_updates] == [
        ("language", "synthetic", "0.1.0", "0.2.0")
    ]
    assert plan.candidate.adapter_digests == old_lock.adapter_digests
    assert plan.candidate.permissions == old_lock.permissions
    assert verify_lock(new_contract, plan.candidate) == (True, [])

    # The resolved contract hash is identical for two raw-only edits at the same
    # new version, but acceptance must still bind the raw layer digest in the lock.
    alternate = plan_stack_lock_update(_contract("0.2.0", "other raw data"), old_lock)
    assert alternate.candidate.resolved_contract_digest == plan.candidate.resolved_contract_digest
    assert alternate.candidate.layers[0].digest != plan.candidate.layers[0].digest
    assert alternate.candidate_fingerprint != plan.candidate_fingerprint


def test_same_version_profile_edit_remains_red_and_not_update_eligible() -> None:
    locked_contract = _contract("0.1.0", "locked")
    edited_contract = _contract("0.1.0", "edited without version bump")
    lock = build_lock(locked_contract)

    ok, diffs = verify_lock(edited_contract, lock)
    assert not ok
    assert any("digest changed" in diff for diff in diffs)
    with pytest.raises(StackLockUpdateError, match="same-version profile drift remains RED"):
        plan_stack_lock_update(edited_contract, lock)


def test_profile_version_rollback_is_refused() -> None:
    newer_lock = build_lock(_contract("2.0.0", "newer"))
    with pytest.raises(StackLockUpdateError, match="rollback/non-upgrade"):
        plan_stack_lock_update(_contract("1.9.9", "older"), newer_lock)


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda lock: replace(lock, schema_version=99), "schema_version"),
        (
            lambda lock: replace(
                lock,
                layers=(replace(lock.layers[0], digest=""),),
            ),
            "digest missing",
        ),
        (
            lambda lock: replace(
                lock,
                layers=(lock.layers[0], lock.layers[0]),
            ),
            "duplicate layer ids",
        ),
        (
            lambda lock: replace(
                lock,
                layers=(replace(lock.layers[0], kind="different-kind"),),
            ),
            "kind",
        ),
    ],
)
def test_malformed_lock_shapes_fail_closed(mutate, expected: str) -> None:  # noqa: ANN001
    contract = _contract("0.1.0", "locked")
    ok, diffs = verify_lock(contract, mutate(build_lock(contract)))
    assert not ok
    assert any(expected in diff for diff in diffs)


def test_missing_lock_schema_is_not_defaulted_to_current() -> None:
    contract = _contract("0.1.0", "locked")
    lock_text = dump_lock(build_lock(contract)).replace("schema_version: 1\n", "")

    ok, diffs = verify_lock(contract, parse_lock(lock_text))

    assert not ok
    assert any("schema_version" in diff for diff in diffs)


def test_empty_digests_on_both_sides_are_red() -> None:
    contract = _contract("0.1.0", "locked")
    contract = replace(
        contract,
        layers=(replace(contract.layers[0], digest=""),),
    )
    built = build_lock(contract)
    lock = replace(
        built,
        layers=(replace(built.layers[0], digest=""),),
    )

    ok, diffs = verify_lock(contract, lock)

    assert not ok
    assert "layer 'synthetic' digest missing from lock" in diffs
    assert "layer 'synthetic' digest missing from resolved contract" in diffs
    with pytest.raises(StackLockUpdateError, match="lacks a profile digest"):
        plan_stack_lock_update(_contract("0.2.0", "new"), lock)


def test_atomic_commit_refuses_stale_lock_without_overwrite(tmp_path: Path) -> None:
    project = _project(tmp_path)
    old_lock = build_lock(_contract("0.1.0", "old"))
    plan = plan_stack_lock_update(_contract("0.2.0", "new"), old_lock)
    path = stack_lock_path(project)
    expected = dump_lock(old_lock)
    path.write_text(expected, encoding="utf-8")
    concurrent = expected + "# concurrent review edit\n"
    path.write_text(concurrent, encoding="utf-8")

    with pytest.raises(StackLockUpdateError, match="changed after.*planned"):
        commit_stack_lock_update(plan, project, expected_lock_text=expected)
    assert path.read_text(encoding="utf-8") == concurrent


def test_cli_dry_run_is_read_only_and_prints_complete_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    old_contract = _contract("0.1.0", "old")
    new_contract = _contract("0.2.0", "new")
    old_text = dump_lock(build_lock(old_contract))
    stack_lock_path(project).write_text(old_text, encoding="utf-8")
    plan = plan_stack_lock_update(new_contract, parse_lock(old_text))

    import codd.stack.project as stack_project

    monkeypatch.setattr(stack_project, "resolve_project_stack", lambda _root: new_contract)
    result = CliRunner().invoke(
        main,
        [
            "stack",
            "update-lock",
            "--path",
            str(project),
            "--reason",
            "reviewed upstream profile revision",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert plan.candidate_fingerprint in result.output
    assert stack_lock_path(project).read_text(encoding="utf-8") == old_text


def test_cli_proof_failure_leaves_existing_lock_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    old_contract = _contract("0.1.0", "old")
    new_contract = _contract("0.2.0", "new")
    old_text = dump_lock(build_lock(old_contract))
    stack_lock_path(project).write_text(old_text, encoding="utf-8")
    plan = plan_stack_lock_update(new_contract, parse_lock(old_text))

    import codd.stack.project as stack_project

    monkeypatch.setattr(stack_project, "resolve_project_stack", lambda _root: new_contract)

    def fail_proof(_contract, _root) -> None:  # noqa: ANN001
        raise StackLockUpdateError("seeded proof failure")

    monkeypatch.setattr(cli_module, "_prove_stack_lock_update", fail_proof)
    result = CliRunner().invoke(
        main,
        [
            "stack",
            "update-lock",
            "--path",
            str(project),
            "--reason",
            "reviewed upstream profile revision",
            "--accept",
            plan.candidate_fingerprint,
        ],
    )

    assert result.exit_code == 1
    assert "seeded proof failure" in result.output
    assert stack_lock_path(project).read_text(encoding="utf-8") == old_text


def test_cli_success_updates_only_after_accepted_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    old_contract = _contract("0.1.0", "old")
    new_contract = _contract("0.2.0", "new")
    old_text = dump_lock(build_lock(old_contract))
    stack_lock_path(project).write_text(old_text, encoding="utf-8")
    plan = plan_stack_lock_update(new_contract, parse_lock(old_text))

    import codd.stack.project as stack_project

    monkeypatch.setattr(stack_project, "resolve_project_stack", lambda _root: new_contract)
    proof_calls: list[object] = []
    monkeypatch.setattr(
        cli_module,
        "_prove_stack_lock_update",
        lambda contract, _root: proof_calls.append(contract),
    )
    result = CliRunner().invoke(
        main,
        [
            "stack",
            "update-lock",
            "--path",
            str(project),
            "--reason",
            "reviewed upstream profile revision",
            "--accept",
            plan.candidate_fingerprint,
        ],
    )

    assert result.exit_code == 0, result.output
    assert proof_calls == [new_contract]
    assert parse_lock(stack_lock_path(project).read_text(encoding="utf-8")) == plan.candidate
    gate = enforce_stack_lock(new_contract, project)
    assert gate.status == LOCK_OK and not gate.red


def test_real_candidate_command_failure_blocks_update_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    # Failure happens in the real composed-command materialization before the
    # ordinary verifier can be consulted.
    monkeypatch.setattr(
        cli_module,
        "_run_verify_once",
        lambda **_kwargs: pytest.fail("ordinary verify must not run after command RED"),
    )
    with pytest.raises(StackLockUpdateError, match="command proof failed"):
        cli_module._prove_stack_lock_update(
            _contract("0.2.0", "candidate", command_exit=7),
            project,
        )


def test_real_candidate_command_and_obligation_proof_can_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    monkeypatch.setattr(
        cli_module,
        "_run_verify_once",
        lambda **_kwargs: cli_module._CliVerificationResult(passed=True, exit_code=0),
    )
    monkeypatch.setattr(cli_module, "_emit_verify_summary", lambda _result: None)
    monkeypatch.setattr(
        cli_module,
        "_enforce_stage_contract_gate",
        lambda *_args, **_kwargs: None,
    )

    cli_module._prove_stack_lock_update(
        _contract("0.2.0", "candidate", command_exit=0),
        project,
    )


def test_language_and_stack_profile_yaml_require_explicit_stable_versions(
    tmp_path: Path,
) -> None:
    language = tmp_path / "language.yaml"
    language.write_text(
        "id: synthetic\ndisplay_name: Synthetic\nlayout:\n  source_sets: []\n",
        encoding="utf-8",
    )
    with pytest.raises(LanguageProfileError, match="profile_version"):
        load_language_profile(language)

    framework = tmp_path / "framework.yaml"
    framework.write_text("id: synthetic\nkind: framework\n", encoding="utf-8")
    with pytest.raises(StackProfileError, match="profile_version"):
        load_framework_profile(framework)

    language.write_text(
        "id: synthetic\nprofile_version: latest\ndisplay_name: Synthetic\n"
        "layout:\n  source_sets: []\n",
        encoding="utf-8",
    )
    with pytest.raises(LanguageProfileError, match="MAJOR.MINOR.PATCH"):
        load_language_profile(language)


def test_bundled_profiles_expose_explicit_current_version() -> None:
    from codd.languages.registry import default_registry as languages
    from codd.stack.registry import default_addon_registry, default_framework_registry

    profiles = list(languages.all_profiles())
    profiles.extend(default_framework_registry.all_profiles())
    profiles.extend(default_addon_registry.all_profiles())

    assert profiles
    for profile in profiles:
        version = profile.identity.profile_version
        assert profile.raw["profile_version"] == version
        assert validated_profile_version(version) == version
