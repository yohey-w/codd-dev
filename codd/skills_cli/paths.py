"""Path helpers for Claude Code and Codex CLI skill installs."""

from __future__ import annotations

from pathlib import Path


def claude_user_skills_dir() -> Path:
    """Return the Claude Code user skills directory."""
    return Path("~/.claude/skills").expanduser()


def codex_user_skills_dir() -> Path:
    """Return the Codex CLI user skills directory."""
    return Path("~/.agents/skills").expanduser()


def claude_repo_skills_dir(cwd: Path | str | None = None) -> Path:
    """Return the repository-local Claude Code skills directory."""
    return _repo_base(cwd) / ".claude" / "skills"


def codex_repo_skills_dir(cwd: Path | str | None = None) -> Path:
    """Return the repository-local Codex CLI skills directory."""
    return _repo_base(cwd) / ".agents" / "skills"


def skills_dir(target: str, scope: str, cwd: Path | str | None = None) -> Path:
    """Resolve a skills directory for target/scope."""
    if target == "claude" and scope == "user":
        return claude_user_skills_dir()
    if target == "codex" and scope == "user":
        return codex_user_skills_dir()
    if target == "claude" and scope == "repo":
        return claude_repo_skills_dir(cwd)
    if target == "codex" and scope == "repo":
        return codex_repo_skills_dir(cwd)
    raise ValueError(f"unsupported skills target/scope: {target}/{scope}")


def expand_targets(target: str) -> tuple[str, ...]:
    """Expand a CLI target choice into concrete targets."""
    if target == "both":
        return ("claude", "codex")
    if target in {"claude", "codex"}:
        return (target,)
    raise ValueError(f"unsupported skills target: {target}")


def expand_scopes(scope: str) -> tuple[str, ...]:
    """Expand a CLI scope choice into concrete scopes."""
    if scope == "all":
        return ("user", "repo")
    if scope in {"user", "repo"}:
        return (scope,)
    raise ValueError(f"unsupported skills scope: {scope}")


def _repo_base(cwd: Path | str | None) -> Path:
    return Path.cwd().resolve() if cwd is None else Path(cwd).expanduser().resolve()
