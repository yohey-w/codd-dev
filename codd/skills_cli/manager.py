"""Install, list, and remove CoDD skills for supported agents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import shutil
from typing import Any

import click

from codd.skills_cli.discovery import SkillNotFoundError, find_skill_source
from codd.skills_cli.paths import expand_scopes, expand_targets, skills_dir

_MARKER_FILENAME = ".codd-skill-install.json"


class SkillsCLIError(click.ClickException):
    """CLI-facing skills error with the requested collision exit code."""

    exit_code = 2


@dataclass(frozen=True)
class InstallResult:
    skill_name: str
    target: str
    scope: str
    path: Path
    source: Path
    mode: str
    action: str
    backup_path: Path | None = None
    warning: str | None = None


@dataclass(frozen=True)
class SkillRecord:
    name: str
    target: str
    scope: str
    path: Path
    kind: str
    target_path: str | None = None


@dataclass(frozen=True)
class RemoveResult:
    skill_name: str
    target: str
    scope: str
    path: Path
    backup_path: Path | None = None


class SkillManager:
    """Manage skill installation state for Claude Code and Codex CLI."""

    def __init__(self, cwd: Path | str | None = None):
        self.cwd = Path.cwd().resolve() if cwd is None else Path(cwd).expanduser().resolve()

    def install(
        self,
        skill_name: str,
        target: str = "both",
        scope: str = "user",
        mode: str = "symlink",
        force: bool = False,
        skill_dir: str | Path | None = None,
        *,
        emit: bool = True,
    ) -> list[InstallResult]:
        """Install a skill to one or more target directories."""
        source = self._find_source(skill_name, skill_dir)
        results: list[InstallResult] = []
        for target_name in expand_targets(target):
            dest = skills_dir(target_name, scope, cwd=self.cwd) / skill_name
            results.append(self._install_one(skill_name, target_name, scope, source, dest, mode, force))
        if emit:
            _echo_install_results(results)
        return results

    def list_skills(
        self,
        target: str = "both",
        scope: str = "all",
        fmt: str = "text",
        *,
        emit: bool = True,
    ) -> list[SkillRecord]:
        """List installed skills grouped by target and scope."""
        targets = expand_targets(target)
        scopes = expand_scopes(scope)
        records: list[SkillRecord] = []
        for target_name in targets:
            for scope_name in scopes:
                root = skills_dir(target_name, scope_name, cwd=self.cwd)
                records.extend(_records_in_dir(root, target_name, scope_name))
        if emit:
            if fmt == "json":
                click.echo(json.dumps(_records_to_payload(records, targets, scopes), indent=2))
            else:
                _echo_list_records(records, targets, scopes)
        return records

    def remove(
        self,
        skill_name: str,
        target: str = "both",
        scope: str = "user",
        keep_backup: bool = False,
        *,
        emit: bool = True,
    ) -> list[RemoveResult]:
        """Remove a skill from one or more target directories."""
        results: list[RemoveResult] = []
        missing: list[tuple[str, str, Path]] = []
        for target_name in expand_targets(target):
            dest = skills_dir(target_name, scope, cwd=self.cwd) / skill_name
            if not dest.exists() and not dest.is_symlink():
                missing.append((target_name, scope, dest))
                continue

            _assert_safe_to_remove(dest)
            backup_path = _rename_to_backup(dest) if keep_backup else None
            if backup_path is None:
                _delete_installed_destination(dest)
            results.append(RemoveResult(skill_name, target_name, scope, dest, backup_path))

        if not results:
            raise SkillsCLIError(f"Skill not installed: {skill_name}")
        if emit:
            _echo_remove_results(results, missing)
        return results

    def _find_source(self, skill_name: str, skill_dir: str | Path | None) -> Path:
        try:
            return find_skill_source(skill_name, skill_dir=skill_dir)
        except SkillNotFoundError as exc:
            raise SkillsCLIError(str(exc)) from exc

    def _install_one(
        self,
        skill_name: str,
        target: str,
        scope: str,
        source: Path,
        dest: Path,
        mode: str,
        force: bool,
    ) -> InstallResult:
        dest.parent.mkdir(parents=True, exist_ok=True)
        action, backup_path = _prepare_destination(dest, source, force)
        if action == "already_installed":
            return InstallResult(skill_name, target, scope, dest, source, mode, action)

        warning: str | None = None
        mode_used = mode
        if mode == "symlink":
            try:
                dest.symlink_to(source, target_is_directory=source.is_dir())
            except OSError as exc:
                _copy_source(source, dest)
                mode_used = "copy"
                warning = f"symlink failed ({exc}); copied instead"
        elif mode == "copy":
            _copy_source(source, dest)
        else:
            raise SkillsCLIError(f"Unsupported install mode: {mode}")

        if mode_used == "copy" and dest.is_dir():
            _write_marker(dest, skill_name, source, target, scope, mode_used)
        return InstallResult(skill_name, target, scope, dest, source, mode_used, action, backup_path, warning)


def install(
    skill_name: str,
    target: str = "both",
    scope: str = "user",
    mode: str = "symlink",
    force: bool = False,
    skill_dir: str | Path | None = None,
) -> list[InstallResult]:
    """CLI adapter for installing skills."""
    return SkillManager().install(skill_name, target, scope, mode, force, skill_dir)


def list_skills(target: str = "both", scope: str = "all", fmt: str = "text") -> list[SkillRecord]:
    """CLI adapter for listing skills."""
    return SkillManager().list_skills(target, scope, fmt)


def remove(skill_name: str, target: str = "both", scope: str = "user", keep_backup: bool = False) -> list[RemoveResult]:
    """CLI adapter for removing skills."""
    return SkillManager().remove(skill_name, target, scope, keep_backup)


def _prepare_destination(dest: Path, source: Path, force: bool) -> tuple[str, Path | None]:
    if dest.is_symlink():
        if _same_symlink_target(dest, source):
            return "already_installed", None
        if not force:
            raise SkillsCLIError(f"Destination already exists with a different symlink target: {dest}")
        return "installed", _rename_to_backup(dest)

    if dest.exists():
        if not force:
            raise SkillsCLIError(f"Destination already exists: {dest}")
        return "installed", _rename_to_backup(dest)

    return "installed", None


def _same_symlink_target(dest: Path, source: Path) -> bool:
    try:
        return dest.resolve(strict=True) == source.resolve(strict=True)
    except OSError:
        return False


def _rename_to_backup(path: Path) -> Path:
    backup = _backup_path(path)
    path.rename(backup)
    return backup


def _backup_path(path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    base = path.with_name(f"{path.name}.bak.{timestamp}")
    candidate = base
    counter = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = path.with_name(f"{base.name}.{counter}")
        counter += 1
    return candidate


def _copy_source(source: Path, dest: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        return
    shutil.copy2(source, dest)


def _write_marker(dest: Path, skill_name: str, source: Path, target: str, scope: str, mode: str) -> None:
    payload = {
        "manager": "codd skills",
        "skill_name": skill_name,
        "source": str(source),
        "target": target,
        "scope": scope,
        "mode": mode,
        "installed_at": datetime.now().isoformat(timespec="seconds"),
    }
    (dest / _MARKER_FILENAME).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _records_in_dir(root: Path, target: str, scope: str) -> list[SkillRecord]:
    if not root.is_dir():
        return []
    records: list[SkillRecord] = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if path.name.startswith(".") or ".bak." in path.name:
            continue
        if not _is_installed_skill_path(path):
            continue
        records.append(
            SkillRecord(
                name=path.name,
                target=target,
                scope=scope,
                path=path,
                kind=_path_kind(path),
                target_path=str(path.resolve(strict=False)) if path.is_symlink() else None,
            )
        )
    return records


def _is_installed_skill_path(path: Path) -> bool:
    return path.is_dir() and (path / "SKILL.md").is_file()


def _path_kind(path: Path) -> str:
    if path.is_symlink():
        return "symlink"
    if path.is_dir() and (path / _MARKER_FILENAME).is_file():
        return "copy"
    if path.is_dir():
        return "directory"
    return "file"


def _records_to_payload(records: list[SkillRecord], targets: tuple[str, ...], scopes: tuple[str, ...]) -> dict[str, Any]:
    payload: dict[str, Any] = {target: {scope: [] for scope in scopes} for target in targets}
    for record in records:
        payload[record.target][record.scope].append(
            {
                "name": record.name,
                "path": str(record.path),
                "target": record.target_path,
                "kind": record.kind,
            }
        )
    return payload


def _assert_safe_to_remove(path: Path) -> None:
    if path.is_symlink():
        return
    if path.is_dir() and (path / _MARKER_FILENAME).is_file():
        return
    raise SkillsCLIError(f"Refusing to remove non-codd-managed path: {path}")


def _delete_installed_destination(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
        return
    if path.is_dir():
        shutil.rmtree(path)
        return
    path.unlink()


def _echo_install_results(results: list[InstallResult]) -> None:
    for result in results:
        if result.action == "already_installed":
            click.echo(f"Already installed {result.target} ({result.scope}): {result.path} -> {result.source}")
        else:
            click.echo(
                f"Installed {result.target} ({result.scope}): "
                f"{result.path} -> {result.source} [{result.mode}]"
            )
        if result.backup_path is not None:
            click.echo(f"Backup: {result.backup_path}")
        if result.warning:
            click.echo(f"Warning: {result.warning}")


def _echo_list_records(records: list[SkillRecord], targets: tuple[str, ...], scopes: tuple[str, ...]) -> None:
    grouped: dict[tuple[str, str], list[SkillRecord]] = {}
    for record in records:
        grouped.setdefault((record.target, record.scope), []).append(record)

    first = True
    for target in targets:
        for scope in scopes:
            if not first:
                click.echo("")
            first = False
            click.echo(f"{target} ({scope}):")
            group_records = grouped.get((target, scope), [])
            if not group_records:
                click.echo("  <none>")
                continue
            for record in group_records:
                arrow = f" -> {record.target_path}" if record.target_path else ""
                click.echo(f"  {record.name}  {record.path}{arrow} [{record.kind}]")


def _echo_remove_results(results: list[RemoveResult], missing: list[tuple[str, str, Path]]) -> None:
    for result in results:
        if result.backup_path is None:
            click.echo(f"Removed {result.target} ({result.scope}): {result.path}")
        else:
            click.echo(f"Removed {result.target} ({result.scope}): {result.path} (backup: {result.backup_path})")
    for target, scope, path in missing:
        click.echo(f"Not installed {target} ({scope}): {path}")
