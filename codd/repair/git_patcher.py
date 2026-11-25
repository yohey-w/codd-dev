"""Apply repair patches using git-compatible patch formats."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
from typing import Any, Callable

from codd.repair.schema import ApplyResult, FilePatch


RunCommand = Callable[..., subprocess.CompletedProcess[Any]]


@dataclass
class GitPatcher:
    """Validate and apply repair patches under a project root."""

    runner: RunCommand = subprocess.run

    def validate(self, patch: FilePatch, project_root: Path) -> bool:
        """Return whether a patch can be applied without changing files."""

        return self.validate_result(patch, project_root).success

    def validate_result(self, patch: FilePatch, project_root: Path) -> ApplyResult:
        """Return dry-run validation details without changing files."""

        try:
            root = Path(project_root).resolve()
            _resolve_target(root, patch.file_path)
        except ValueError as exc:
            return ApplyResult(False, [], [patch.file_path], str(exc))

        if patch.patch_mode == "full_file_replacement":
            return ApplyResult(True, [], [], None)

        if not patch.content.strip():
            return ApplyResult(False, [], [patch.file_path], "patch content is empty")

        result = self._git_apply(root, patch.content, "--check")
        if result.returncode == 0:
            return ApplyResult(True, [], [], None)
        return ApplyResult(False, [], [patch.file_path], _completed_error(result))

    def apply(self, patch: FilePatch, project_root: Path, *, dry_run: bool = False) -> ApplyResult:
        """Apply or preview one repair patch."""

        root = Path(project_root).resolve()
        try:
            target = _resolve_target(root, patch.file_path)
        except ValueError as exc:
            return ApplyResult(False, [], [patch.file_path], str(exc))

        if patch.patch_mode == "full_file_replacement":
            return self._apply_full_replacement(patch, target, dry_run=dry_run)

        validation = self.validate_result(patch, root)
        if not validation.success:
            return validation
        if dry_run:
            return ApplyResult(True, [], [], None)

        # --3way implies --index. Bypass it only for a matching untracked diff.
        apply_args = ()
        if self._is_git_worktree(root) and not self._all_paths_untracked(root, patch):
            apply_args = ("--3way",)

        first = self._git_apply(root, patch.content, *apply_args)
        if first.returncode == 0:
            return ApplyResult(True, [patch.file_path], [], None)

        second = self._git_apply(root, patch.content, *apply_args)
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

    def _is_git_worktree(self, root: Path) -> bool:
        """Return whether ``root`` is inside a git worktree.

        Fail-safe: any failure (non-zero exit or exception) is treated as
        "not a git worktree" so that a plain ``git apply`` fallback is used.
        """

        try:
            result = self.runner(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=str(root),
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception:
            return False
        return result.returncode == 0 and (result.stdout or "").strip() == "true"

    def _all_paths_untracked(self, root: Path, patch: FilePatch) -> bool:
        try:
            content = patch.content
            paths = self._numstat_paths(root, content, "--check")
            if not paths:
                return False

            # Git can silently skip patches outside the current directory.
            # Count every record at the repository root before trusting this list.
            repository_root = self._repository_root(root)
            if repository_root is None:
                return False
            # Use Git's repository-relative name, including for symlink entries.
            declared = root.relative_to(repository_root) / patch.file_path
            if repository_root != root and len(self._numstat_paths(repository_root, content)) != len(paths):
                return False

            # Numstat reports the destination of renames and copies. Reverse
            # the diff to get its source paths as well.
            reverse_paths = self._numstat_paths(root, content, "--reverse")
            if len(reverse_paths) != len(paths):
                return False
            touched = set(paths) | set(reverse_paths)

            if os.fsencode(declared.as_posix()) not in touched:
                return False

            ancestors = set()
            for path in touched:
                parts = path.split(b"/")
                ancestors.update(b"/".join(parts[:i]) for i in range(1, len(parts)))
            related = touched | ancestors

            # A tracked ancestor can be a gitlink. Literal pathspecs also
            # return descendants, so distinguish them from tracked siblings.
            pathspecs = [b":(top,literal)" + path for path in sorted(related)]
            indexed = self.runner(
                ["git", "--no-literal-pathspecs", "ls-files", "--full-name", "-z", "--", *pathspecs],
                cwd=str(root),
                capture_output=True,
                check=False,
            )
            tracked = indexed.stdout
            if indexed.returncode != 0 or not isinstance(tracked, bytes):
                return False
            if not tracked:
                return True
            if not tracked.endswith(b"\0"):
                return False
            ancestor_prefixes = tuple(path + b"/" for path in ancestors)
            touched_prefixes = tuple(path + b"/" for path in touched)
            # Only paths found through one of the ancestor pathspecs can be unrelated.
            return all(
                _is_git_relative_path(path)
                and path.startswith(ancestor_prefixes)
                and path not in related
                and not path.startswith(touched_prefixes)
                for path in tracked[:-1].split(b"\0")
            )
        except Exception:
            # Match the original indexed apply when a probe cannot be trusted.
            return False

    def _repository_root(self, root: Path) -> Path | None:
        result = self.runner(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(root),
            capture_output=True,
            check=False,
        )
        output = result.stdout
        if (
            result.returncode != 0
            or not isinstance(output, bytes)
            or not output.endswith(b"\n")
            or b"\0" in output
        ):
            return None
        path = Path(os.fsdecode(output[:-1]))
        return path if path.is_absolute() else None

    def _numstat_paths(self, root: Path, content: str, *args: str) -> list[bytes]:
        # --numstat without --apply is read-only. Keep its NUL-delimited paths
        # as bytes so a filename's literal whitespace survives subprocess I/O.
        result = self.runner(
            ["git", "apply", "--numstat", "-z", *args],
            cwd=str(root),
            input=content.encode("utf-8"),
            capture_output=True,
            check=False,
        )
        output = result.stdout
        if result.returncode != 0 or not isinstance(output, bytes) or not output.endswith(b"\0"):
            return []

        paths = []
        for record in output[:-1].split(b"\0"):
            fields = record.split(b"\t", 2)
            if len(fields) != 3:
                return []
            added, removed, path = fields
            if not (added == removed == b"-" or (added.isdigit() and removed.isdigit())):
                return []
            if not _is_git_relative_path(path):
                return []
            paths.append(path)
        return paths

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


def _is_git_relative_path(path: bytes) -> bool:
    return all(part not in (b"", b".", b"..") for part in path.split(b"/"))


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
