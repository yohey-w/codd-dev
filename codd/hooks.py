"""Git hook helpers for CoDD pre-commit enforcement."""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

import yaml

from codd.scanner import _extract_frontmatter
from codd.validator import run_validate


HOOK_SOURCE = Path(__file__).parent.parent / "hooks" / "pre-commit"


def install_pre_commit_hook(project_root: Path) -> tuple[Path, bool]:
    """Install the packaged pre-commit hook into a Git repository."""
    config_path = project_root / "codd" / "codd.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"{config_path} not found")

    git_dir = project_root / ".git"
    if not git_dir.exists():
        raise FileNotFoundError(f"{git_dir} not found")

    if not HOOK_SOURCE.exists():
        raise FileNotFoundError(f"{HOOK_SOURCE} not found")

    destination = git_dir / "hooks" / "pre-commit"
    source = HOOK_SOURCE.resolve()
    source.chmod(source.stat().st_mode | 0o111)

    if destination.is_symlink():
        if destination.resolve() == source:
            return destination, False
        raise FileExistsError(f"{destination} already exists and points to {destination.resolve()}")

    if destination.exists():
        raise FileExistsError(f"{destination} already exists")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.symlink_to(source)
    return destination, True


def run_pre_commit(project_root: Path) -> int:
    """Validate staged CoDD documents before commit."""
    config_path = project_root / "codd" / "codd.yaml"
    if not config_path.exists():
        print("ERROR: codd/codd.yaml not found.")
        return 1

    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    try:
        staged_docs = _get_staged_markdown_files(project_root, config)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1

    for relative_path in staged_docs:
        if _extract_frontmatter(project_root / relative_path) is not None:
            continue
        print(f"ERROR: {relative_path} is missing CoDD YAML frontmatter")
        return 1

    return run_validate(project_root, project_root / "codd")


def _get_staged_markdown_files(project_root: Path, config: dict) -> list[Path]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git diff --cached failed")

    doc_dirs = ((config.get("scan") or {}).get("doc_dirs") or [])
    staged_docs: list[Path] = []

    for entry in result.stdout.splitlines():
        relative_path = entry.strip()
        if not relative_path.endswith(".md"):
            continue
        if not _is_in_doc_dirs(relative_path, doc_dirs):
            continue
        staged_docs.append(Path(relative_path))

    return staged_docs


def _is_in_doc_dirs(relative_path: str, doc_dirs: list[str]) -> bool:
    rel = PurePosixPath(relative_path)
    for doc_dir in doc_dirs:
        base = PurePosixPath(str(doc_dir).rstrip("/"))
        try:
            rel.relative_to(base)
            return True
        except ValueError:
            continue
    return False
