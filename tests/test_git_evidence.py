"""H2: git history as testimony evidence (blame-driven).

Covers, against REAL temporary git repositories (git init in tmp_path):
  * blame-driven survival filtering — only commits whose changes survive into
    HEAD attach; a fully superseded commit is excluded by git's own data
    structure, not by model judgment,
  * file:line locators restricting attachment to a line range,
  * the noise filter (wip/fix/update tokens, merges, squash residue, threshold),
  * conventional-commit parsing (type/scope/description),
  * the structural testimony constants (kind=testimony, band=amber),
  * supersession chains (revert + substantial-rewrite decision trails),
  * graceful degradation (non-repo dir, failing git → empty, never raising),
  * the injectable run_git layer (pure-logic tests without a repository).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from codd.git_evidence import (
    GitTestimony,
    SupersessionChain,
    collect_git_testimony,
    detect_supersession_chains,
    is_informative_message,
    parse_conventional,
    _parse_locator,
)


# ---------------------------------------------------------------------------
# Real-repo helpers (deterministic author/committer + dates)
# ---------------------------------------------------------------------------
def _git_env(date: str) -> dict[str, str]:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "Restorer",
        "GIT_AUTHOR_EMAIL": "restorer@example.com",
        "GIT_COMMITTER_NAME": "Restorer",
        "GIT_COMMITTER_EMAIL": "restorer@example.com",
        "GIT_AUTHOR_DATE": f"{date}T00:00:00 +0000",
        "GIT_COMMITTER_DATE": f"{date}T00:00:00 +0000",
    }


def _run(repo: Path, *args: str, date: str = "2024-01-01") -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        env=_git_env(date),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _commit_all(repo: Path, message: str, date: str) -> str:
    """Stage everything, commit with a pinned date, return the short sha."""
    _run(repo, "add", "-A", date=date)
    _run(repo, "commit", "-q", "-m", message, date=date)
    return _run(repo, "rev-parse", "--short", "HEAD", date=date)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init", "-q", "-b", "main")
    return repo


# ---------------------------------------------------------------------------
# 1. Blame-driven survival: superseded commits are EXCLUDED structurally
# ---------------------------------------------------------------------------
def test_superseded_commit_is_excluded_surviving_commit_attaches(repo: Path) -> None:
    """The core temporality guarantee: a commit whose changes were fully
    replaced never blame-attaches to HEAD lines, so its (possibly stale)
    message never becomes testimony."""
    src = repo / "src"
    src.mkdir()
    target = src / "policy.py"
    target.write_text(
        "def allow_guest_checkout():\n    return True\n", encoding="utf-8"
    )
    superseded = _commit_all(
        repo, "feat(policy): allow guest checkout to reduce signup friction", "2024-01-05"
    )
    # Full rewrite: no line of the first commit survives.
    target.write_text(
        "def require_account_for_checkout():\n    return False\n", encoding="utf-8"
    )
    surviving = _commit_all(
        repo, "feat(policy): require an account for checkout after fraud reports", "2024-03-10"
    )

    testimony = collect_git_testimony(repo, ["src/policy.py"])
    shas = {t.commit for t in testimony}

    assert surviving in shas
    assert superseded not in shas  # excluded by git blame, not by judgment

    entry = next(t for t in testimony if t.commit == surviving)
    assert entry.subject.startswith("feat(policy): require an account")
    assert "2024-03-10" in entry.survival_note
    assert "still present at HEAD" in entry.survival_note


def test_partially_surviving_commit_still_attaches(repo: Path) -> None:
    """If SOME lines of an older commit survive, it remains admissible."""
    f = repo / "rates.cfg"
    f.write_text("base = 10\nbonus = 2\n", encoding="utf-8")
    first = _commit_all(repo, "feat(rates): introduce base and bonus rates", "2024-01-02")
    f.write_text("base = 10\nbonus = 5\n", encoding="utf-8")  # only line 2 changed
    second = _commit_all(repo, "feat(rates): raise bonus rate after pilot feedback", "2024-02-02")

    shas = {t.commit for t in collect_git_testimony(repo, ["rates.cfg"])}
    assert shas == {first, second}


# ---------------------------------------------------------------------------
# 2. file:line locators
# ---------------------------------------------------------------------------
def test_line_range_locator_restricts_attachment(repo: Path) -> None:
    f = repo / "config.ini"
    f.write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")
    first = _commit_all(repo, "feat(config): introduce alpha and beta tuning knobs", "2024-01-02")
    f.write_text("alpha = 1\nbeta = 2\ngamma = 3\ndelta = 4\n", encoding="utf-8")
    second = _commit_all(repo, "feat(config): add gamma and delta for the new pipeline", "2024-02-02")

    only_first = collect_git_testimony(repo, ["config.ini:1-2"])
    assert {t.commit for t in only_first} == {first}

    whole_file = collect_git_testimony(repo, ["config.ini"])
    assert {t.commit for t in whole_file} == {first, second}


@pytest.mark.parametrize(
    ("locator", "expected"),
    [
        ("src/a.py", ("src/a.py", None, None)),
        ("src/a.py:10", ("src/a.py", 10, 10)),
        ("src/a.py:10-20", ("src/a.py", 10, 20)),
        ("src/a.py:20-10", ("src/a.py", 10, 20)),  # normalized
        ("src/a.py::test_x", ("src/a.py", None, None)),
        ("k8s/dep.yaml::Deployment::api", ("k8s/dep.yaml", None, None)),
        ("", ("", None, None)),
    ],
)
def test_parse_locator_shapes(locator: str, expected: tuple) -> None:
    assert _parse_locator(locator) == expected


# ---------------------------------------------------------------------------
# 3. Noise filter
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("subject", "body", "expected"),
    [
        ("wip", "", False),
        ("fix", "", False),
        ("update", "", False),
        ("fix bug", "", False),
        ("minor cleanup", "", False),
        ("more changes and stuff", "", False),
        ("Merge branch 'main' into feature/x", "", False),
        ("Merge pull request #42 from org/branch", "", False),
        ("fixup! feat: add parser", "", False),
        ("squash! something here", "", False),
        ("x", "", False),  # below the meaningful-chars threshold
        ("fix: typo", "", False),  # conventional prefix stripped → noise word
        ("", "", False),
        ("feat(api): add rate limiting to login", "", True),
        ("fix(parser): handle CRLF line endings from legacy editors", "", True),
        ("Increase retry budget because upstream flakes during deploys", "", True),
        # An uninformative subject is rescued by an informative body.
        ("wip", "switching tenants to per-schema isolation before the cutover", True),
    ],
)
def test_is_informative_message(subject: str, body: str, expected: bool) -> None:
    assert is_informative_message(subject, body) is expected


def test_noise_only_history_yields_no_testimony(repo: Path) -> None:
    f = repo / "notes.txt"
    f.write_text("scratch\n", encoding="utf-8")
    _commit_all(repo, "wip", "2024-01-03")

    assert collect_git_testimony(repo, ["notes.txt"]) == []


# ---------------------------------------------------------------------------
# 4. Conventional-commit parsing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        (
            "feat(api): add rate limiting",
            {"type": "feat", "scope": "api", "description": "add rate limiting"},
        ),
        ("fix: handle empty payloads", {"type": "fix", "description": "handle empty payloads"}),
        ("FEAT(Core)!: breaking rework", {"type": "feat", "scope": "Core", "description": "breaking rework"}),
        ("just a plain message", None),
        ("", None),
    ],
)
def test_parse_conventional(subject: str, expected: dict | None) -> None:
    assert parse_conventional(subject) == expected


def test_testimony_carries_conventional_and_constants(repo: Path) -> None:
    src = repo / "src"
    src.mkdir()
    (src / "billing.py").write_text("TAX_RATE = 0.08\n", encoding="utf-8")
    _run(repo, "add", "-A", date="2024-01-05")
    _run(
        repo, "commit", "-q",
        "-m", "fix(billing): raise tax rate to 8 percent",
        "-m", "Regulation effective 2024 requires the higher rate.",
        date="2024-01-05",
    )

    testimony = collect_git_testimony(repo, ["src/billing.py"])
    assert len(testimony) == 1
    t = testimony[0]
    assert isinstance(t, GitTestimony)
    assert t.kind == "testimony"
    assert t.band == "amber"  # structural cap — never green
    assert t.corroborated is True
    assert t.locator == "src/billing.py"
    assert t.date.startswith("2024-01-05")
    assert t.conventional == {
        "type": "fix",
        "scope": "billing",
        "description": "raise tax rate to 8 percent",
    }
    assert "Regulation effective 2024" in t.body_excerpt


# ---------------------------------------------------------------------------
# 5. Bounds + dedup
# ---------------------------------------------------------------------------
def test_max_commits_per_locator_respected(repo: Path) -> None:
    f = repo / "story.txt"
    f.write_text("one line from the first commit\n", encoding="utf-8")
    _commit_all(repo, "feat(story): open with the first narrative line", "2024-01-01")
    f.write_text(
        "one line from the first commit\nsecond line for more detail here\n",
        encoding="utf-8",
    )
    _commit_all(repo, "feat(story): add a second descriptive line", "2024-02-01")
    f.write_text(
        "one line from the first commit\nsecond line for more detail here\nthird line closes it\n",
        encoding="utf-8",
    )
    _commit_all(repo, "feat(story): close the narrative with a third line", "2024-03-01")

    everything = collect_git_testimony(repo, ["story.txt"])
    assert len(everything) == 3

    capped = collect_git_testimony(repo, ["story.txt"], max_commits_per_locator=1)
    assert len(capped) == 1


def test_commits_deduplicated_across_locators(repo: Path) -> None:
    src = repo / "src"
    src.mkdir()
    (src / "a.py").write_text("alpha = 1\n", encoding="utf-8")
    (src / "b.py").write_text("beta = 2\n", encoding="utf-8")
    sha = _commit_all(repo, "feat(core): introduce alpha and beta modules together", "2024-01-09")

    testimony = collect_git_testimony(repo, ["src/a.py", "src/b.py"])
    assert [t.commit for t in testimony] == [sha]  # once, not per locator


# ---------------------------------------------------------------------------
# 6. Supersession chains (rejected-alternatives evidence)
# ---------------------------------------------------------------------------
def test_revert_chain_detected(repo: Path) -> None:
    src = repo / "src"
    src.mkdir()
    cache = src / "cache.py"
    cache.write_text("def compute():\n    return 1\n", encoding="utf-8")
    _commit_all(repo, "feat(cache): lazy computation on first access", "2024-01-10")
    cache.write_text(
        "PRECOMPUTED = {}\n\ndef compute():\n    return PRECOMPUTED.get(1, 1)\n",
        encoding="utf-8",
    )
    _commit_all(repo, "feat(cache): eagerly precompute results at startup", "2024-02-10")
    _run(repo, "revert", "--no-edit", "HEAD", date="2024-03-10")

    chains = detect_supersession_chains(repo, ["src/cache.py"])
    assert len(chains) == 1
    chain = chains[0]
    assert isinstance(chain, SupersessionChain)
    assert chain.file == "src/cache.py"
    assert "revert" in chain.note.lower()
    assert "rejected" in chain.note
    subjects = [subject for _sha, _date, subject in chain.commits]
    assert any(subject.startswith('Revert "') for subject in subjects)


def test_substantial_rewrite_chain_detected_chronologically(repo: Path) -> None:
    src = repo / "src"
    src.mkdir()
    engine = src / "engine.py"
    v1 = "\n".join(f"def step_{i}():\n    return {i}" for i in range(12)) + "\n"
    engine.write_text(v1, encoding="utf-8")
    first = _commit_all(
        repo, "feat(engine): batch pipeline with twelve sequential steps", "2024-01-04"
    )
    v2 = "\n".join(f"async def stage_{i}():\n    return {i * 2}" for i in range(12)) + "\n"
    engine.write_text(v2, encoding="utf-8")
    second = _commit_all(
        repo, "refactor(engine): replace batch pipeline with async stages", "2024-04-04"
    )

    chains = detect_supersession_chains(repo, ["src/engine.py"])
    assert [c.file for c in chains] == ["src/engine.py"]
    chain = chains[0]
    # Chronological decision trail: original first, replacement second.
    assert [sha for sha, _date, _subject in chain.commits] == [first, second]
    assert [date for _sha, date, _subject in chain.commits] == ["2024-01-04", "2024-04-04"]
    assert "rejected" in chain.note
    assert "testimony" in chain.note


def test_no_chain_for_single_commit_or_noise_history(repo: Path) -> None:
    src = repo / "src"
    src.mkdir()
    (src / "single.py").write_text("VALUE = 1\n", encoding="utf-8")
    _commit_all(repo, "feat(single): introduce the value constant module", "2024-01-02")

    noisy = src / "noisy.py"
    noisy.write_text("\n".join(f"a{i} = {i}" for i in range(12)) + "\n", encoding="utf-8")
    _commit_all(repo, "wip", "2024-02-02")
    noisy.write_text("\n".join(f"b{i} = {i}" for i in range(12)) + "\n", encoding="utf-8")
    _commit_all(repo, "update stuff", "2024-03-02")

    assert detect_supersession_chains(repo, ["src/single.py"]) == []
    assert detect_supersession_chains(repo, ["src/noisy.py"]) == []


# ---------------------------------------------------------------------------
# 7. Graceful degradation
# ---------------------------------------------------------------------------
def test_non_repo_dir_returns_empty(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("x = 1\n", encoding="utf-8")

    assert collect_git_testimony(plain, ["a.py"]) == []
    assert detect_supersession_chains(plain, ["a.py"]) == []


def test_empty_locators_return_empty(repo: Path) -> None:
    assert collect_git_testimony(repo, []) == []
    assert detect_supersession_chains(repo, []) == []


def test_failing_run_git_degrades_to_empty(tmp_path: Path) -> None:
    def boom(args: list[str], cwd: Path) -> str:
        raise RuntimeError("git exploded")

    assert collect_git_testimony(tmp_path, ["a.py"], run_git=boom) == []
    assert detect_supersession_chains(tmp_path, ["a.py"], run_git=boom) == []


def test_untracked_file_yields_no_testimony(repo: Path) -> None:
    (repo / "tracked.txt").write_text("present\n", encoding="utf-8")
    _commit_all(repo, "feat(repo): seed the repository with a tracked file", "2024-01-02")
    (repo / "untracked.txt").write_text("never committed\n", encoding="utf-8")

    assert collect_git_testimony(repo, ["untracked.txt"]) == []


# ---------------------------------------------------------------------------
# 8. Injectable run_git (pure logic, no repository)
# ---------------------------------------------------------------------------
_FAKE_SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"


def _fake_run_git(args: list[str], cwd: Path) -> str:
    if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
        return "true\n"
    if args[0] == "blame":
        return (
            f"{_FAKE_SHA} 1 1 1\n"
            "author T\n"
            "author-time 1700000000\n"
            "author-tz +0000\n"
            "summary feat(api): add rate limiting to login\n"
            "filename api.py\n"
            "\tcode line\n"
        )
    if args[0] == "show":
        return (
            "a1b2c3d\x012023-11-14T00:00:00+00:00\x01"
            "feat(api): add rate limiting to login\x01"
            "Brute-force pressure on the login endpoint.\x02\n\napi.py\n"
        )
    return ""


def test_collect_with_injected_run_git(tmp_path: Path) -> None:
    testimony = collect_git_testimony(tmp_path, ["api.py"], run_git=_fake_run_git)
    assert len(testimony) == 1
    t = testimony[0]
    assert t.commit == "a1b2c3d"
    assert t.subject == "feat(api): add rate limiting to login"
    assert t.body_excerpt == "Brute-force pressure on the login endpoint."
    assert t.corroborated is True
    assert t.band == "amber"
    assert "1 line(s) still present at HEAD" in t.survival_note


def test_file_level_fallback_marks_unverified_survival(tmp_path: Path) -> None:
    """When blame yields nothing, the log --follow fallback attaches with an
    explicit 'not verified by blame' survival note and computed corroboration."""

    def fallback_run_git(args: list[str], cwd: Path) -> str:
        if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
            return "true\n"
        if args[0] == "blame":
            return ""  # blame unavailable for this path
        if args[0] == "log":
            return f"{_FAKE_SHA}\n"
        if args[0] == "show":
            return (
                "a1b2c3d\x012023-11-14T00:00:00+00:00\x01"
                "feat(docs): document the retry policy decision\x01\x02\n\nother.py\n"
            )
        return ""

    testimony = collect_git_testimony(tmp_path, ["api.py"], run_git=fallback_run_git)
    assert len(testimony) == 1
    t = testimony[0]
    assert "not verified by blame" in t.survival_note
    assert t.corroborated is False  # commit touched other.py, not api.py
