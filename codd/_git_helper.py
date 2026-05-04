"""Small git helpers shared by CoDD command modules."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _diff_files(base_ref: str, *, cwd: Path, paths: list[str] | None = None) -> str:
    """Return a unified git diff against base_ref, optionally limited to paths."""
    command = [
        "git",
        "-c",
        "core.quotePath=false",
        "diff",
        "--unified=200",
        base_ref,
    ]
    if paths:
        command.extend(["--", *paths])

    try:
        result = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return ""

    if result.returncode != 0:
        return ""
    return result.stdout


def _resolve_base_ref(base_ref: str | None, *, cwd: Path) -> str:
    """Return a valid base ref, defaulting to HEAD~1."""
    resolved = base_ref or "HEAD~1"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", resolved],
            cwd=str(cwd),
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise ValueError("Cannot resolve git ref because git is not available") from exc

    if result.returncode != 0:
        detail = result.stderr.strip() or resolved
        raise ValueError(f"Cannot resolve git ref {resolved}: {detail}")
    return resolved
