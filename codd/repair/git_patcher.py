"""Apply repair patches using git-compatible patch formats."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Callable

from codd.repair.schema import ApplyResult, FilePatch


RunCommand = Callable[..., subprocess.CompletedProcess[str]]


@dataclass
class GitPatcher:
    """Validate and apply repair patches under a project root."""

    runner: RunCommand = subprocess.run

    def validate(self, patch: FilePatch, project_root: Path) -> bool:
        """Return whether a patch can be applied without changing files."""

        try:
            root = Path(project_root).resolve()
            _resolve_target(root, patch.file_path)
        except ValueError:
            return False

        if patch.patch_mode == "full_file_replacement":
            return True

        if not patch.content.strip():
            return False

        result = self._git_apply(root, patch.content, "--check")
        return result.returncode == 0

    def apply(self, patch: FilePatch, project_root: Path, *, dry_run: bool = False) -> ApplyResult:
        """Apply or preview one repair patch."""

        root = Path(project_root).resolve()
        try:
            target = _resolve_target(root, patch.file_path)
        except ValueError as exc:
            return ApplyResult(False, [], [patch.file_path], str(exc))

        if patch.patch_mode == "full_file_replacement":
            return self._apply_full_replacement(patch, target, dry_run=dry_run)

        if not self.validate(patch, root):
            return ApplyResult(
                False,
                [],
                [patch.file_path],
                "unified diff failed git apply --check; full_file_replacement fallback required",
            )
        if dry_run:
            return ApplyResult(True, [], [], None)

        first = self._git_apply(root, patch.content, "--3way")
        if first.returncode == 0:
            return ApplyResult(True, [patch.file_path], [], None)

        second = self._git_apply(root, patch.content, "--3way")
        if second.returncode == 0:
            return ApplyResult(True, [patch.file_path], [], None)

        return ApplyResult(
            False,
            [],
            [patch.file_path],
            f"{_completed_error(first)}\nretry: {_completed_error(second)}",
        )

    def _apply_full_replacement(self, patch: FilePatch, target: Path, *, dry_run: bool) -> ApplyResult:
        if dry_run:
            return ApplyResult(True, [], [], None)

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(patch.content, encoding="utf-8")
        except OSError as exc:
            return ApplyResult(False, [], [patch.file_path], str(exc))
        return ApplyResult(True, [patch.file_path], [], None)

    def _git_apply(self, root: Path, content: str, *args: str) -> subprocess.CompletedProcess[str]:
        return self.runner(
            ["git", "apply", *args],
            cwd=str(root),
            input=content,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )


def _resolve_target(root: Path, file_path: str) -> Path:
    relative = Path(file_path)
    if relative.is_absolute() or not str(file_path).strip():
        raise ValueError("patch file_path must be a non-empty relative path")
    if any(part == ".." for part in relative.parts):
        raise ValueError("patch file_path must stay within project root")

    target = (root / relative).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("patch file_path must stay within project root") from exc
    return target


def _completed_error(result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or "").strip() or (result.stdout or "").strip()
    return detail or f"git apply exited with {result.returncode}"


__all__ = ["GitPatcher"]
