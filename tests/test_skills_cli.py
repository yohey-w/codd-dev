from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from codd.cli import main


def _prepare_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _write_skill(root: Path, name: str = "codd-evolve") -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    (skill / "README.md").write_text(f"{name}\n", encoding="utf-8")
    return skill


def _invoke(args: list[str]):
    return CliRunner().invoke(main, args)


def test_t01_install_target_both_symlink_creates_claude_and_codex(tmp_path, monkeypatch):
    home = _prepare_home(tmp_path, monkeypatch)
    source = _write_skill(tmp_path / "sources")

    result = _invoke(["skills", "install", "codd-evolve", "--target", "both", "--mode", "symlink", "--dir", str(source)])

    assert result.exit_code == 0, result.output
    claude = home / ".claude" / "skills" / "codd-evolve"
    codex = home / ".agents" / "skills" / "codd-evolve"
    assert claude.is_symlink()
    assert codex.is_symlink()
    assert claude.resolve() == source.resolve()
    assert codex.resolve() == source.resolve()


def test_t02_install_target_claude_creates_only_claude_symlink(tmp_path, monkeypatch):
    home = _prepare_home(tmp_path, monkeypatch)
    source = _write_skill(tmp_path / "sources")

    result = _invoke(
        ["skills", "install", "codd-evolve", "--target", "claude", "--mode", "symlink", "--dir", str(source)]
    )

    assert result.exit_code == 0, result.output
    assert (home / ".claude" / "skills" / "codd-evolve").is_symlink()
    assert not (home / ".agents" / "skills" / "codd-evolve").exists()


def test_t03_install_target_codex_creates_only_codex_symlink(tmp_path, monkeypatch):
    home = _prepare_home(tmp_path, monkeypatch)
    source = _write_skill(tmp_path / "sources")

    result = _invoke(
        ["skills", "install", "codd-evolve", "--target", "codex", "--mode", "symlink", "--dir", str(source)]
    )

    assert result.exit_code == 0, result.output
    assert not (home / ".claude" / "skills" / "codd-evolve").exists()
    assert (home / ".agents" / "skills" / "codd-evolve").is_symlink()


def test_t04_install_mode_copy_duplicates_files(tmp_path, monkeypatch):
    home = _prepare_home(tmp_path, monkeypatch)
    source = _write_skill(tmp_path / "sources")

    result = _invoke(["skills", "install", "codd-evolve", "--target", "claude", "--mode", "copy", "--dir", str(source)])

    dest = home / ".claude" / "skills" / "codd-evolve"
    assert result.exit_code == 0, result.output
    assert dest.is_dir()
    assert not dest.is_symlink()
    assert (dest / "SKILL.md").read_text(encoding="utf-8") == "# codd-evolve\n"
    assert (dest / ".codd-skill-install.json").is_file()


def test_t05_install_same_source_twice_is_idempotent(tmp_path, monkeypatch):
    _prepare_home(tmp_path, monkeypatch)
    source = _write_skill(tmp_path / "sources")

    first = _invoke(["skills", "install", "codd-evolve", "--target", "claude", "--dir", str(source)])
    second = _invoke(["skills", "install", "codd-evolve", "--target", "claude", "--dir", str(source)])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "Already installed claude (user)" in second.output


def test_t06_existing_different_symlink_without_force_fails(tmp_path, monkeypatch):
    home = _prepare_home(tmp_path, monkeypatch)
    source = _write_skill(tmp_path / "sources")
    other = _write_skill(tmp_path / "other")
    dest = home / ".claude" / "skills" / "codd-evolve"
    dest.parent.mkdir(parents=True)
    dest.symlink_to(other, target_is_directory=True)

    result = _invoke(["skills", "install", "codd-evolve", "--target", "claude", "--dir", str(source)])

    assert result.exit_code == 2
    assert "different symlink target" in result.output
    assert dest.resolve() == other.resolve()


def test_t07_existing_different_symlink_with_force_backs_up_and_replaces(tmp_path, monkeypatch):
    home = _prepare_home(tmp_path, monkeypatch)
    source = _write_skill(tmp_path / "sources")
    other = _write_skill(tmp_path / "other")
    dest = home / ".claude" / "skills" / "codd-evolve"
    dest.parent.mkdir(parents=True)
    dest.symlink_to(other, target_is_directory=True)

    result = _invoke(["skills", "install", "codd-evolve", "--target", "claude", "--force", "--dir", str(source)])

    assert result.exit_code == 0, result.output
    assert dest.resolve() == source.resolve()
    backups = list(dest.parent.glob("codd-evolve.bak.*"))
    assert len(backups) == 1
    assert backups[0].is_symlink()
    assert backups[0].resolve() == other.resolve()


def test_t08_existing_real_directory_without_force_fails(tmp_path, monkeypatch):
    home = _prepare_home(tmp_path, monkeypatch)
    source = _write_skill(tmp_path / "sources")
    dest = home / ".claude" / "skills" / "codd-evolve"
    dest.mkdir(parents=True)

    result = _invoke(["skills", "install", "codd-evolve", "--target", "claude", "--dir", str(source)])

    assert result.exit_code == 2
    assert "Destination already exists" in result.output
    assert dest.is_dir()
    assert not dest.is_symlink()


def test_t09_install_dir_uses_external_skill_source(tmp_path, monkeypatch):
    home = _prepare_home(tmp_path, monkeypatch)
    source = _write_skill(tmp_path / "external", "custom-skill")

    result = _invoke(["skills", "install", "custom-skill", "--target", "codex", "--dir", str(source)])

    dest = home / ".agents" / "skills" / "custom-skill"
    assert result.exit_code == 0, result.output
    assert dest.is_symlink()
    assert dest.resolve() == source.resolve()


def test_t10_install_repo_scope_uses_current_directory(tmp_path, monkeypatch):
    _prepare_home(tmp_path, monkeypatch)
    source = _write_skill(tmp_path / "sources")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)

    result = _invoke(["skills", "install", "codd-evolve", "--scope", "repo", "--target", "both", "--dir", str(source)])

    assert result.exit_code == 0, result.output
    assert (project / ".claude" / "skills" / "codd-evolve").is_symlink()
    assert (project / ".agents" / "skills" / "codd-evolve").is_symlink()


def test_t11_list_text_groups_installed_skills_by_target_and_scope(tmp_path, monkeypatch):
    _prepare_home(tmp_path, monkeypatch)
    source = _write_skill(tmp_path / "sources")
    install = _invoke(["skills", "install", "codd-evolve", "--target", "both", "--dir", str(source)])
    assert install.exit_code == 0, install.output

    result = _invoke(["skills", "list"])

    assert result.exit_code == 0, result.output
    assert "claude (user):" in result.output
    assert "codex (user):" in result.output
    assert "codd-evolve" in result.output
    assert "[symlink]" in result.output


def test_t12_list_json_returns_structured_records(tmp_path, monkeypatch):
    _prepare_home(tmp_path, monkeypatch)
    source = _write_skill(tmp_path / "sources")
    install = _invoke(["skills", "install", "codd-evolve", "--target", "codex", "--dir", str(source)])
    assert install.exit_code == 0, install.output

    result = _invoke(["skills", "list", "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    records = payload["codex"]["user"]
    assert records[0]["name"] == "codd-evolve"
    assert records[0]["kind"] == "symlink"
    assert records[0]["target"] == str(source.resolve())
    assert payload["claude"]["user"] == []


def test_t13_remove_deletes_symlink_and_keep_backup_preserves_bak(tmp_path, monkeypatch):
    home = _prepare_home(tmp_path, monkeypatch)
    source = _write_skill(tmp_path / "sources")
    install = _invoke(["skills", "install", "codd-evolve", "--target", "claude", "--dir", str(source)])
    assert install.exit_code == 0, install.output
    dest = home / ".claude" / "skills" / "codd-evolve"

    result = _invoke(["skills", "remove", "codd-evolve", "--target", "claude", "--keep-backup"])

    assert result.exit_code == 0, result.output
    assert not dest.exists()
    assert not dest.is_symlink()
    backups = list(dest.parent.glob("codd-evolve.bak.*"))
    assert len(backups) == 1
    assert backups[0].is_symlink()
    assert backups[0].resolve() == source.resolve()


def test_t14_remove_unknown_skill_exits_non_zero(tmp_path, monkeypatch):
    _prepare_home(tmp_path, monkeypatch)

    result = _invoke(["skills", "remove", "missing-skill"])

    assert result.exit_code != 0
    assert "Skill not installed: missing-skill" in result.output
