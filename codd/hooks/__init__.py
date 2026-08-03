"""Git hook helpers for CoDD pre-commit enforcement."""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

import yaml

from codd.config import find_codd_dir
from codd.discovery import matches_exclude_pattern, scan_exclude_patterns
from codd.scanner import _extract_frontmatter
from codd.validator import run_validate


HOOK_SOURCE = Path(__file__).parent / "pre-commit"


def install_pre_commit_hook(project_root: Path) -> tuple[Path, bool]:
    """Install the packaged pre-commit hook into a Git repository."""
    codd_dir = find_codd_dir(project_root)
    if codd_dir is None:
        raise FileNotFoundError("CoDD config dir not found (looked for codd/ and .codd/)")

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
    codd_dir = find_codd_dir(project_root)
    if codd_dir is None:
        print("ERROR: CoDD config dir not found (looked for codd/ and .codd/).")
        return 1

    config_path = codd_dir / "codd.yaml"
    if not config_path.exists():
        print(f"ERROR: {config_path} not found.")
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

    if _canon_drift_blocks_commit(project_root, config):
        return 1

    return run_validate(project_root, codd_dir)


def _canon_drift_blocks_commit(project_root: Path, config: dict) -> bool:
    """Reject a commit whose canon documents no longer match ``canon.lock``.

    This is the commit-time half of the canon tripwire (``codd dag verify`` is
    the other). It catches the case the ledger exists for: a formatter rewrote a
    requirements table — no meaning changed, so review would pass it — and the
    corrupted bytes are about to enter history.

    Deliberate scoping decisions:

    * **Only when a ledger exists.** A project that has not adopted the
      mechanism must not suddenly be unable to commit after a CoDD upgrade. The
      adoption prompt lives in ``codd dag verify`` (amber), not here, so it is
      not printed on every single commit.
    * **Only altered/missing blocks.** An untracked in-scope document is
      reported as a note and lets the commit through: adding a file is already
      visible in ``git status``, and blocking it would stop the very commit that
      introduces a project's first requirements document.
    * **Working tree, not the index.** ``prettier --write .`` damages the
      working tree wholesale while the user commits something else entirely; a
      gate that only looked at staged paths would wave that through and the
      corruption would ride in on a later commit. The cost is that an
      in-progress edit to a canon document blocks unrelated commits — which is
      the intended reading of "canon changes are deliberate", and the message
      names both ways out.
    * **Never raises.** Any unexpected failure inside the tripwire must not make
      the repository uncommittable; it degrades to "not blocking" and the
      ``dag verify`` path still reports.
    """
    try:
        from codd.canon import evaluate_canon

        status = evaluate_canon(project_root, config)
    except Exception:  # a broken tripwire must not brick `git commit`
        return False

    if not status.enabled or not status.ledger_present or status.ledger_unreadable:
        return False

    if status.untracked and not status.has_drift:
        print(
            f"NOTE: {len(status.untracked)} canon document(s) are not in "
            f"{status.ledger_path}: {', '.join(status.untracked[:5])}"
            f"{' ...' if len(status.untracked) > 5 else ''}. "
            "Run 'codd canon accept --for <work reference>' to protect them."
        )
        return False

    if not status.has_drift:
        return False

    print("ERROR: canon changed outside the accept path.")
    for relative_path in status.modified:
        print(f"  ALTERED  {relative_path}")
    for relative_path in status.missing:
        print(f"  MISSING  {relative_path}")
    print(
        "A requirement unit is a Markdown table row, so a formatter that only "
        "re-aligns table pipes passes review while breaking byte-identity with "
        "the approved original. Check the diff (git diff -- <path>):"
    )
    print(
        "  intended change   -> codd canon accept --for <work reference>   "
        "(then commit the updated ledger)"
    )
    print("  unintended change -> git checkout -- <path>")
    return True


def _get_staged_markdown_files(project_root: Path, config: dict) -> list[Path]:
    result = subprocess.run(
        ["git", "-c", "core.quotePath=false", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        cwd=project_root,
        capture_output=True,
        text=True, encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git diff --cached failed")

    doc_dirs = ((config.get("scan") or {}).get("doc_dirs") or [])
    # `scan.exclude` means "not CoDD's business". The hook used to ignore it,
    # so a path the project had explicitly excluded from scanning still had to
    # carry CoDD frontmatter or the commit was rejected — the gate contradicted
    # the config. This matters most for reference material parked under a
    # doc_dir (a received customer spec, a vendored upstream document): it is
    # foreign text that will never have frontmatter, and excluding it was the
    # documented way to say so.
    exclude_patterns = scan_exclude_patterns(config)
    staged_docs: list[Path] = []

    for entry in result.stdout.splitlines():
        relative_path = entry.strip()
        if not relative_path.endswith(".md"):
            continue
        if not _is_in_doc_dirs(relative_path, doc_dirs):
            continue
        if any(
            matches_exclude_pattern(relative_path, pattern)
            for pattern in exclude_patterns
        ):
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
