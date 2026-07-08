"""Regression tests for applying repair patches in greenfield (non-git) workspaces.

Greenfield workspaces are not git repositories, so ``git apply --3way`` (which
needs the git object database for a 3-way merge) fails with
``error: '--3way' outside a repository`` and every verify-repair bounces. The
patcher must fall back to a plain ``git apply`` when the root is not a git
worktree, while still using ``--3way`` inside a real repository.
"""

from __future__ import annotations

import subprocess

from codd.repair.git_patcher import GitPatcher
from codd.repair.schema import FilePatch


def _valid_diff(old: str = "one", new: str = "two") -> str:
    return (
        "diff --git a/sample.txt b/sample.txt\n"
        "--- a/sample.txt\n"
        "+++ b/sample.txt\n"
        "@@ -1 +1 @@\n"
        f"-{old}\n"
        f"+{new}\n"
    )


def _init_repo(tmp_path):
    root = tmp_path
    (root / "sample.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True)
    subprocess.run(["git", "add", "sample.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
    return root


def test_apply_unified_diff_succeeds_in_non_git_workspace(tmp_path):
    """Greenfield (non-git) workspace: a clean unified diff must still apply."""

    root = tmp_path
    (root / "sample.txt").write_text("one\n", encoding="utf-8")
    # Deliberately NOT a git repository — this is the greenfield case.

    result = GitPatcher().apply(FilePatch("sample.txt", "unified_diff", _valid_diff()), root)

    assert result.success is True
    assert (root / "sample.txt").read_text(encoding="utf-8") == "two\n"


def test_apply_unified_diff_still_succeeds_in_git_repo(tmp_path):
    """Real git repository: 3-way apply must keep working (no regression)."""

    root = _init_repo(tmp_path)

    result = GitPatcher().apply(FilePatch("sample.txt", "unified_diff", _valid_diff()), root)

    assert result.success is True
    assert (root / "sample.txt").read_text(encoding="utf-8") == "two\n"


def _make_runner(is_worktree: bool):
    """Build a fake runner that reports worktree status and succeeds otherwise."""

    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(command)
        if "rev-parse" in command:
            return subprocess.CompletedProcess(
                command,
                0 if is_worktree else 128,
                "true\n" if is_worktree else "",
                "" if is_worktree else "fatal: not a git repository",
            )
        # --check validation and both apply attempts succeed cleanly.
        return subprocess.CompletedProcess(command, 0, "", "")

    return runner, calls


def _apply_argvs(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if "apply" in c and "--check" not in c]


def test_apply_uses_three_way_inside_git_worktree(tmp_path):
    """Inside a git worktree the apply invocations must carry ``--3way``."""

    (tmp_path / "sample.txt").write_text("one\n", encoding="utf-8")
    runner, calls = _make_runner(is_worktree=True)

    result = GitPatcher(runner=runner).apply(
        FilePatch("sample.txt", "unified_diff", _valid_diff()), tmp_path
    )

    assert result.success is True
    apply_argvs = _apply_argvs(calls)
    assert apply_argvs, "expected at least one git apply invocation"
    assert all("--3way" in argv for argv in apply_argvs)


def test_apply_omits_three_way_outside_git_worktree(tmp_path):
    """Outside a git worktree the apply invocations must omit ``--3way``."""

    (tmp_path / "sample.txt").write_text("one\n", encoding="utf-8")
    runner, calls = _make_runner(is_worktree=False)

    result = GitPatcher(runner=runner).apply(
        FilePatch("sample.txt", "unified_diff", _valid_diff()), tmp_path
    )

    assert result.success is True
    apply_argvs = _apply_argvs(calls)
    assert apply_argvs, "expected at least one git apply invocation"
    assert all("--3way" not in argv for argv in apply_argvs)
