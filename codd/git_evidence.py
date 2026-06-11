"""Git history as *testimony* evidence for brownfield restoration.

The git evidence unit is the (diff, message, timestamp) tuple. The DIFF is a
deterministic FACT; the MESSAGE is **testimony** — a claim by the commit author
about the *why*. This module makes testimony admissible under strict,
structural rules (not LLM judgment):

* **Blame-driven temporality** — we never ingest the commit log as a bag of
  messages. Each restoration evidence locator (a file, or ``file:line``) is
  resolved through ``git blame --line-porcelain``: ONLY the commits whose
  changes SURVIVE into HEAD attach as evidence. A superseded commit never
  blame-attaches to surviving lines, so stale intent is excluded by git's own
  data structure.
* **Amber cap** — every :class:`GitTestimony` carries ``kind="testimony"`` and
  ``band="amber"`` as constants. Testimony can corroborate an amber statement
  or supply a ``candidate_answer`` for an open question; it can NEVER produce a
  green statement on its own (the restoration prompt enforces this downstream).
* **Noise filter** — :func:`is_informative_message` discards messages below an
  information threshold (wip/fix/update-class tokens, merge auto-messages,
  squash/fixup residue, < ~10 meaningful chars). Discarded testimony leaves the
  open questions blank: absence of evidence stays absent.
* **Supersession chains** — :func:`detect_supersession_chains` surfaces files
  whose ``git log --follow`` history shows the implementation being replaced
  or reverted over time. The chain is deterministic evidence that *an
  alternative existed and was rejected* (the replaced implementation is fact;
  the WHY of the rejection is testimony, amber).
* **Staleness surfaced, not decayed** — no decay function. Each testimony
  carries its author date and a ``survival_note`` ("N line(s) still present at
  HEAD since <date>"). Visible staleness, no invisible weighting.

Everything degrades gracefully: not a git repo / git missing / shallow or
broken history → empty results, never an exception. The git I/O is isolated
behind an injectable ``run_git`` callable so the logic is testable without a
repository.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Constants of the testimony evidence class (structural, not configurable):
# testimony is a CLAIM about intent and is therefore capped at amber.
KIND_TESTIMONY = "testimony"
TESTIMONY_BAND = "amber"

# Bounds (keep total subprocess calls predictable).
DEFAULT_MAX_LOCATORS = 50
DEFAULT_MAX_COMMITS_PER_LOCATOR = 3
_MAX_TOTAL_COMMITS = 60
_MAX_BODY_EXCERPT_CHARS = 400
_MAX_LOG_FALLBACK_COMMITS = 5
_MAX_CHAIN_LOG_COMMITS = 30
_MAX_CHAIN_COMMITS_SHOWN = 5
_GIT_TIMEOUT_SECONDS = 20

# A per-file change is "substantial" (for supersession detection) when at least
# this many lines were added+deleted in that commit. Binary changes count.
_SUBSTANTIAL_CHANGE_LINES = 10

# Minimum count of meaningful (non-whitespace) characters in a message for it
# to clear the information threshold.
_MIN_MEANINGFUL_CHARS = 10

RunGit = Callable[[list[str], Path], str]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class GitTestimony:
    """One admissible piece of git-history testimony, anchored to a locator.

    ``kind``/``band`` are constants: testimony is never fact and never green.
    """

    locator: str
    commit: str  # short sha
    date: str  # ISO-8601 author date
    subject: str
    body_excerpt: str = ""
    conventional: dict[str, str] | None = None  # {type, scope?, description}
    corroborated: bool = True
    kind: str = KIND_TESTIMONY
    band: str = TESTIMONY_BAND
    survival_note: str = ""


@dataclass
class SupersessionChain:
    """A deterministic decision trail: an alternative existed and was rejected.

    ``commits`` is chronological (oldest first): each later commit replaced or
    reverted what came before. The replaced implementation is FACT; the why of
    the rejection is testimony (amber).
    """

    file: str
    commits: list[tuple[str, str, str]] = field(default_factory=list)  # (sha, date, subject)
    note: str = ""


# ---------------------------------------------------------------------------
# Git subprocess layer (thin, injectable)
# ---------------------------------------------------------------------------
def _default_run_git(args: list[str], cwd: Path) -> str:
    """Run a read-only git command; return stdout, or '' on ANY failure."""
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotePath=false", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


def _is_git_repo(project_root: Path, run_git: RunGit) -> bool:
    try:
        out = run_git(["rev-parse", "--is-inside-work-tree"], project_root)
    except Exception:
        return False
    return out.strip() == "true"


# ---------------------------------------------------------------------------
# Noise filter + conventional-commit parsing (pure logic)
# ---------------------------------------------------------------------------
_CONVENTIONAL_RE = re.compile(
    r"^(?P<type>feat|fix|chore|docs|style|refactor|perf|test|build|ci|revert)"
    r"(?:\((?P<scope>[^)]*)\))?!?:\s*(?P<description>.+)$",
    re.IGNORECASE,
)

_MERGE_PREFIXES = (
    "merge branch",
    "merge pull request",
    "merge remote-tracking",
    "merge tag",
    "merged in",
)

_SQUASH_PREFIXES = ("squash!", "fixup!", "amend!")

# Vocabulary of zero-information tokens. A message whose meaningful words are
# ALL drawn from this set carries no recoverable intent ("wip", "fix bug",
# "minor cleanup", …). Deliberately general — no domain words.
_NOISE_TOKENS = frozenset(
    {
        "wip", "fix", "fixes", "fixed", "fixing", "bug", "bugs", "bugfix",
        "hotfix", "patch", "update", "updates", "updated", "updating",
        "upgrade", "bump", "cleanup", "clean", "up", "minor", "tweak",
        "tweaks", "typo", "typos", "lint", "linting", "format", "formatting",
        "style", "refactor", "refactoring", "rework", "change", "changes",
        "misc", "stuff", "things", "temp", "tmp", "test", "tests", "testing",
        "ci", "build", "chore", "chores", "more", "small", "quick", "oops",
        "todo", "review", "comments", "comment", "polish", "tidy", "rename",
        "renames", "remove", "removed", "add", "added", "adds", "edit",
        "edits", "save", "sync", "merge", "rebase", "version", "release",
        "initial", "commit", "first", "again", "stash", "checkpoint", "work",
        "progress", "and", "a", "an", "the", "some", "of", "to", "in", "on",
        "it",
    }
)


def parse_conventional(subject: str) -> dict[str, str] | None:
    """Parse a conventional-commit subject into {type, scope?, description}."""
    if not isinstance(subject, str):
        return None
    match = _CONVENTIONAL_RE.match(subject.strip())
    if not match:
        return None
    parsed: dict[str, str] = {
        "type": match.group("type").lower(),
        "description": match.group("description").strip(),
    }
    scope = match.group("scope")
    if scope:
        parsed["scope"] = scope.strip()
    return parsed


def is_informative_message(subject: str, body: str = "") -> bool:
    """Information threshold for commit-message testimony.

    Rejects: empty subjects, merge auto-messages, squash/fixup residue,
    messages whose meaningful content is shorter than ~10 chars, and messages
    composed solely of zero-information tokens (wip / fix / update class).
    A conventional-commit prefix is stripped before measuring, so a bare
    ``fix: typo`` is still noise while ``fix(parser): handle CRLF`` is not.
    """

    cleaned = (subject or "").strip()
    if not cleaned:
        return False

    lowered = cleaned.lower()
    if any(lowered.startswith(prefix) for prefix in _MERGE_PREFIXES):
        return False
    if any(lowered.startswith(prefix) for prefix in _SQUASH_PREFIXES):
        return False
    if lowered.startswith("squashed commit"):
        return False

    conventional = parse_conventional(cleaned)
    core = conventional["description"] if conventional else cleaned
    if conventional and conventional.get("scope"):
        # A scope is information: it names where the change applies.
        core = f"{conventional['scope']} {core}"
    body_text = (body or "").strip()
    text = f"{core} {body_text}".strip()

    meaningful = re.sub(r"\s+", "", text)
    if len(meaningful) < _MIN_MEANINGFUL_CHARS:
        return False

    words = [w for w in re.split(r"[^0-9A-Za-z]+", text.lower()) if w]
    if words and all(w in _NOISE_TOKENS for w in words):
        return False
    return True


# ---------------------------------------------------------------------------
# Locator parsing
# ---------------------------------------------------------------------------
_LINE_RANGE_RE = re.compile(r"^(?P<path>.+?):(?P<start>\d+)(?:-(?P<end>\d+))?$")


def _parse_locator(locator: str) -> tuple[str, int | None, int | None]:
    """Split a locator into (file_path, start_line, end_line).

    Accepts ``path``, ``path:12``, ``path:12-40``; tolerates the other
    provenance shapes (``file::test_name``, ``file::Kind::name``) by keeping
    only the file part.
    """

    raw = (locator or "").strip()
    if not raw:
        return "", None, None
    file_part = raw.split("::", 1)[0].strip()
    match = _LINE_RANGE_RE.match(file_part)
    if match:
        start = int(match.group("start"))
        end = int(match.group("end")) if match.group("end") else start
        if end < start:
            start, end = end, start
        return match.group("path"), start, end
    return file_part, None, None


# ---------------------------------------------------------------------------
# Blame-driven collection
# ---------------------------------------------------------------------------
_BLAME_HEADER_RE = re.compile(r"^(?P<sha>[0-9a-f]{40}) \d+ \d+")


def _blame_surviving_commits(
    file_path: str,
    start: int | None,
    end: int | None,
    project_root: Path,
    run_git: RunGit,
) -> dict[str, int]:
    """Map full sha → number of surviving lines, via ``git blame``.

    Only commits that blame-attach to lines present at HEAD appear here —
    this IS the survival filter. Uncommitted lines (all-zero sha) are skipped.
    """

    args = ["blame", "--line-porcelain"]
    if start is not None and end is not None:
        args.extend(["-L", f"{start},{end}"])
    args.extend(["--", file_path])
    out = run_git(args, project_root)
    if not out:
        return {}

    counts: dict[str, int] = {}
    for line in out.splitlines():
        match = _BLAME_HEADER_RE.match(line)
        if not match:
            continue
        sha = match.group("sha")
        if set(sha) == {"0"}:  # uncommitted working-tree lines: not history
            continue
        counts[sha] = counts.get(sha, 0) + 1
    return counts


def _log_follow_commits(
    file_path: str,
    project_root: Path,
    run_git: RunGit,
    limit: int = _MAX_LOG_FALLBACK_COMMITS,
) -> list[str]:
    """File-level fallback: full shas of the most recent commits touching the file."""
    out = run_git(
        ["log", "--follow", "-n", str(limit), "--format=%H", "--", file_path],
        project_root,
    )
    return [line.strip() for line in out.splitlines() if line.strip()]


def _commit_details(
    sha: str, project_root: Path, run_git: RunGit
) -> tuple[str, str, str, str, list[str]] | None:
    """Fetch (short_sha, iso_date, subject, body, touched_files) for a commit."""
    out = run_git(
        [
            "show",
            "--name-only",
            "--format=%h\x01%aI\x01%s\x01%b\x02",
            sha,
        ],
        project_root,
    )
    if not out or "\x02" not in out:
        return None
    header, _, files_part = out.partition("\x02")
    fields = header.split("\x01")
    if len(fields) < 4:
        return None
    short_sha, date, subject = fields[0].strip(), fields[1].strip(), fields[2]
    body = "\x01".join(fields[3:])
    touched = [line.strip() for line in files_part.splitlines() if line.strip()]
    return short_sha, date, subject.strip(), body.strip(), touched


def _excerpt(body: str) -> str:
    collapsed = re.sub(r"\s+", " ", (body or "").strip())
    if len(collapsed) <= _MAX_BODY_EXCERPT_CHARS:
        return collapsed
    return collapsed[: _MAX_BODY_EXCERPT_CHARS - 1].rstrip() + "…"


def collect_git_testimony(
    project_root: Path,
    locators: list[str],
    *,
    max_commits_per_locator: int = DEFAULT_MAX_COMMITS_PER_LOCATOR,
    max_locators: int = DEFAULT_MAX_LOCATORS,
    run_git: RunGit | None = None,
) -> list[GitTestimony]:
    """Collect blame-anchored commit-message testimony for evidence locators.

    For each locator (``file`` or ``file:line[-line]``), ``git blame`` resolves
    the commits whose changes survive into HEAD; only those attach. A plain
    ``git log --follow`` per file is the fallback when blame yields nothing
    (then ``corroborated`` is computed from the commit's touched files and the
    survival note says line survival was not verified). Non-informative
    messages are discarded. Commits are deduplicated across locators. Returns
    ``[]`` for non-repos, missing git, or any failure.
    """

    runner = run_git or _default_run_git
    try:
        if not locators or not _is_git_repo(project_root, runner):
            return []

        results: list[GitTestimony] = []
        seen_shas: set[str] = set()
        detail_cache: dict[str, tuple[str, str, str, str, list[str]] | None] = {}

        for locator in locators[:max_locators]:
            if len(seen_shas) >= _MAX_TOTAL_COMMITS:
                break
            file_path, start, end = _parse_locator(locator)
            if not file_path:
                continue

            survival = _blame_surviving_commits(file_path, start, end, project_root, runner)
            blame_hit = bool(survival)
            if blame_hit:
                # Most-surviving-lines first; sha as deterministic tiebreak.
                ranked = sorted(survival.items(), key=lambda kv: (-kv[1], kv[0]))
                candidates = [sha for sha, _ in ranked]
            else:
                candidates = _log_follow_commits(file_path, project_root, runner)

            attached = 0
            for sha in candidates:
                if attached >= max_commits_per_locator:
                    break
                if sha in seen_shas:
                    attached += 1  # already attached elsewhere; still occupies the slot
                    continue
                if sha not in detail_cache:
                    detail_cache[sha] = _commit_details(sha, project_root, runner)
                details = detail_cache[sha]
                if details is None:
                    continue
                short_sha, date, subject, body, touched = details
                if not is_informative_message(subject, body):
                    continue
                day = date[:10] if date else "unknown date"
                if blame_hit:
                    lines = survival.get(sha, 0)
                    corroborated = True  # by construction: blame attaches only surviving changes
                    survival_note = (
                        f"introduced {day}, {lines} line(s) still present at HEAD"
                    )
                else:
                    corroborated = file_path in touched
                    survival_note = (
                        f"authored {day}; file-level history match "
                        "(line survival not verified by blame)"
                    )
                results.append(
                    GitTestimony(
                        locator=locator,
                        commit=short_sha,
                        date=date,
                        subject=subject,
                        body_excerpt=_excerpt(body),
                        conventional=parse_conventional(subject),
                        corroborated=corroborated,
                        survival_note=survival_note,
                    )
                )
                seen_shas.add(sha)
                attached += 1
                if len(seen_shas) >= _MAX_TOTAL_COMMITS:
                    break
        return results
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Supersession chains (rejected-alternatives evidence)
# ---------------------------------------------------------------------------
_REVERT_SUBJECT_RE = re.compile(r"^revert\b|^revert:", re.IGNORECASE)


def detect_supersession_chains(
    project_root: Path,
    file_paths: list[str],
    *,
    run_git: RunGit | None = None,
    max_files: int = 20,
) -> list[SupersessionChain]:
    """Detect files whose history is a decision trail of replaced alternatives.

    A chain is emitted when ``git log --follow`` for a file shows revert
    commits, or >= 2 distinct non-noise commits each changing a substantial
    number of lines (the earlier implementation was replaced — a deterministic
    in-repo trace that an alternative existed and was rejected). Best-effort
    and bounded; returns ``[]`` on any failure.
    """

    runner = run_git or _default_run_git
    try:
        if not file_paths or not _is_git_repo(project_root, runner):
            return []

        chains: list[SupersessionChain] = []
        for file_path in file_paths[:max_files]:
            records = _file_history(file_path, project_root, runner)
            if len(records) < 2:
                continue

            reverts = [r for r in records if _REVERT_SUBJECT_RE.match(r["subject"])]
            substantial = [
                r
                for r in records
                if r["churn"] >= _SUBSTANTIAL_CHANGE_LINES
                and is_informative_message(r["subject"])
            ]

            if not reverts and len(substantial) < 2:
                continue

            relevant = reverts + [r for r in substantial if r not in reverts]
            # Chronological (oldest first) decision trail, bounded.
            relevant.sort(key=lambda r: r["order"], reverse=True)
            shown = relevant[-_MAX_CHAIN_COMMITS_SHOWN:]
            commits = [(r["sha"], r["date"], r["subject"]) for r in shown]

            if reverts:
                note = (
                    "history contains revert commit(s): an implemented alternative "
                    "was explicitly rejected (the reverted implementation is fact; "
                    "the WHY of the rejection is testimony)"
                )
            else:
                note = (
                    f"implementation replaced over time across {len(substantial)} "
                    "substantial commits: earlier version(s) are rejected "
                    "alternatives (fact); the rationale for replacement is testimony"
                )
            chains.append(SupersessionChain(file=file_path, commits=commits, note=note))
        return chains
    except Exception:
        return []


def _file_history(
    file_path: str, project_root: Path, run_git: RunGit
) -> list[dict]:
    """Per-commit history records for a file: sha/date/subject + line churn."""
    out = run_git(
        [
            "log",
            "--follow",
            "--numstat",
            "-n",
            str(_MAX_CHAIN_LOG_COMMITS),
            "--format=\x02%h\x01%aI\x01%s",
            "--",
            file_path,
        ],
        project_root,
    )
    if not out:
        return []

    records: list[dict] = []
    for order, chunk in enumerate(out.split("\x02")):
        chunk = chunk.strip("\n")
        if not chunk.strip():
            continue
        lines = chunk.splitlines()
        fields = lines[0].split("\x01")
        if len(fields) < 3:
            continue
        sha, date, subject = fields[0].strip(), fields[1].strip(), fields[2].strip()
        churn = 0
        for numstat in lines[1:]:
            parts = numstat.split("\t")
            if len(parts) < 3:
                continue
            added, deleted = parts[0].strip(), parts[1].strip()
            if added == "-" or deleted == "-":
                churn += _SUBSTANTIAL_CHANGE_LINES  # binary change: treat as substantial
                continue
            try:
                churn += int(added) + int(deleted)
            except ValueError:
                continue
        # git log emits newest first; keep that index so callers can re-sort
        # chronologically (higher order == older).
        records.append(
            {"sha": sha, "date": date[:10], "subject": subject, "churn": churn, "order": order}
        )
    return records
