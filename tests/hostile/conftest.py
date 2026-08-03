"""Shared machinery for the CoDD hostile-repository regression suite.

Every test here assembles a fresh "hostile project" in a temp directory, runs
CoDD against it, and asserts that a *real-world dirtiness* was survived or
detected. Nothing is checked in as a fixture repository: a frozen fixture repo
drifts away from what `codd init` actually produces and then quietly stops
testing anything.

Two rules this file exists to enforce:

1. **No silent skips.** A missing prerequisite (node/npx, git, the `codd`
   console script) is a hard, loud failure with the remedy in the message.
   A suite whose green contains skips has not verified what it claims to.
2. **Every protection test carries its own negative control.** Asserting only
   "the canon survived prettier" passes just as well when prettier would never
   have touched the file. The paired assertion — remove the guard, re-run, the
   bytes DO change — is what makes the green load-bearing.
"""

from __future__ import annotations

import functools
import hashlib
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner, Result

import codd
from codd.cli import main


# ──────────────────────────────────────────────────────────────────────
# The hostile requirements document
# ──────────────────────────────────────────────────────────────────────
# Deliberately RAGGED table pipes. This is the shape of the field incident
# (F-1): `npx prettier --write .` re-aligned 440 lines of a requirements
# table. Meaning unchanged, review blind, every check green, byte-identity
# with the human-approved original destroyed. If this fixture were already
# prettier-formatted, the formatter tests would pass vacuously.
HOSTILE_REQUIREMENTS = """# Hostile Sample — Requirements

## 機能要件

| ID | 要件 | 優先度 |
|---|---|---|
| FR-1 | ユーザーはメールアドレスとパスワードでログインできる | 高 |
| FR-2 | ユーザーはログアウトできる | 中 |
| FR-3 | 管理者は利用者一覧を閲覧できる | 低 |

## 非機能要件

| ID | 要件 |
|---|---|
| NFR-1 | ログイン応答は 1 秒以内 |
"""

# A section heading that `requirement_reconciliation.sections` can match, used
# by the double-counting test to count units the way CoDD counts them.
REQUIREMENTS_SECTION = "機能要件"


# ──────────────────────────────────────────────────────────────────────
# Environment / prerequisites
# ──────────────────────────────────────────────────────────────────────

def _require(binary: str, why: str) -> str:
    path = shutil.which(binary)
    if path is None:
        pytest.fail(
            f"`{binary}` is not on PATH, and this suite must not skip.\n"
            f"  needed for: {why}\n"
            f"  fix: install it (CI: actions/setup-node@v4 for node/npx) and re-run.",
            pytrace=False,
        )
    return path


# git, isolated from the developer's global config: a global `core.hooksPath`
# or `init.templateDir` would otherwise decide whether CoDD's pre-commit hook
# runs at all, and the hook tests would be measuring the machine, not CoDD.
GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
}
GIT_IDENTITY = [
    "-c", "user.name=CoDD Hostile Suite",
    "-c", "user.email=hostile@example.invalid",
    "-c", "commit.gpgsign=false",
]


@functools.lru_cache(maxsize=1)
def assert_cli_matches_import() -> None:
    """The `codd` the hook execs must be the `codd` these tests import.

    H3/H4 assert through a real `git commit`, and the pre-commit hook runs
    `exec codd hooks run-pre-commit` — a console script resolved from PATH. The
    other tests import `codd` in-process. A stale global/pipx `codd` earlier on
    PATH would make those two different builds, and the hook tests would pass or
    fail for a reason that has nothing to do with the tree under test.

    That is the same "two paths to the same thing quietly disagree" failure this
    whole suite exists to catch, so it is checked rather than assumed. `codd
    version` prints a bare semver and does not discriminate between two installs
    of the same version, so the console script's own interpreter is asked where
    it imports `codd` from.

    Reach, stated honestly: this catches the realistic case — a `codd` from a
    different venv/pipx, i.e. a different interpreter resolving a different
    package (verified: a shebang pointing at a codd-less venv fails here). It
    would NOT catch a hand-written wrapper that rewrites `sys.path` inside
    itself, because no CoDD command exposes its own install path to ask.
    """
    script = _require("codd", "the pre-commit hook execs the `codd` console script")
    try:
        shebang = Path(script).read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError) as exc:  # a compiled launcher, or unreadable
        pytest.fail(
            f"cannot read the shebang of the `codd` console script at {script} "
            f"({exc}); unable to prove the hook runs the build under test.",
            pytrace=False,
        )
    if not shebang.startswith("#!"):
        pytest.fail(
            f"`codd` at {script} is not a script with a shebang, so the build it "
            "imports cannot be verified against the one these tests import.",
            pytrace=False,
        )
    interpreter = shlex.split(shebang[2:])
    probe = subprocess.run(
        [*interpreter, "-c", "import codd; print(codd.__file__)"],
        env=GIT_ENV, capture_output=True, text=True, timeout=120,
    )
    cli_codd = probe.stdout.strip()
    mine = str(Path(codd.__file__).resolve())
    if not cli_codd or Path(cli_codd).resolve() != Path(mine):
        pytest.fail(
            "the `codd` on PATH is a DIFFERENT build from the one under test — "
            "the pre-commit tests would be measuring something else.\n"
            f"  console script : {script}\n"
            f"  it imports     : {cli_codd or probe.stderr.strip()}\n"
            f"  tests import   : {mine}\n"
            "  fix: `pip install -e .` from the repository under test, or put "
            "its console script first on PATH.",
            pytrace=False,
        )


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """The CoDD repository under test.

    Derived from the imported `codd` package, so this resolves correctly both
    while the suite lives outside CoDD (editable install) and after it is moved
    into `codd-dev/tests/hostile/`. `CODD_REPO_ROOT` overrides for a
    non-editable install where the package is inside site-packages.
    """
    override = os.environ.get("CODD_REPO_ROOT")
    if override:
        root = Path(override).resolve()
    else:
        root = Path(codd.__file__).resolve().parent.parent
    if not (root / "RUNBOOK.md").exists():
        pytest.fail(
            f"Cannot locate the CoDD repository root (looked at {root}).\n"
            f"  Set CODD_REPO_ROOT to the checkout that contains RUNBOOK.md.",
            pytrace=False,
        )
    return root


@pytest.fixture(scope="session")
def canon_mechanism(tmp_path_factory) -> bool:
    """Behavioural probe: does this CoDD build ship the canon integrity ledger?

    Probed by BEHAVIOUR (does `codd init` leave a ledger and does `codd canon
    status` exist), not by importing a module name — the mechanism landed as an
    untracked working-tree file while this suite was written, and a name probe
    would report on the wrong thing after any refactor.

    Both branches of every canon-gated test assert something. Absent → the
    `.prettierignore` layer alone is pinned. Present → the tool-independent
    tripwire is pinned as well.
    """
    root = tmp_path_factory.mktemp("canon_probe") / "proj"
    root.mkdir()
    spec = root.parent / "spec.md"
    spec.write_text(HOSTILE_REQUIREMENTS, encoding="utf-8")
    result = run_codd(
        "init", "canonprobe", "--language", "typescript",
        "--dest", str(root), "--requirements", str(spec),
        "--no-suggest-lexicons",
    )
    if result.exit_code != 0:
        pytest.fail(f"canon probe: `codd init` failed\n{result.output}", pytrace=False)
    ledger = root / "codd" / "canon.lock"
    status = run_codd("canon", "status", "--path", str(root))
    return ledger.exists() and status.exit_code in (0, 1) and "Ledger" in status.output


# ──────────────────────────────────────────────────────────────────────
# Running CoDD
# ──────────────────────────────────────────────────────────────────────

def run_codd(*args: object) -> Result:
    """Invoke the CoDD CLI in-process (fast, and reports the full output)."""
    return CliRunner().invoke(main, [str(a) for a in args])


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prettier_command() -> list[str]:
    """How to invoke the real formatter on this machine.

    A `prettier` on PATH (CI: `npm install --global prettier`) is used directly.
    Otherwise `npx --no-install`, which resolves a project-local or cached copy
    and — crucially — refuses to reach the network, so a missing prettier fails
    fast and loudly instead of hanging or silently pulling an unpinned version.
    """
    direct = shutil.which("prettier")
    if direct:
        return [direct]
    _require(
        "npx",
        "running the real `prettier --write .` against a hostile project. "
        "Install prettier (`npm install --global prettier`) or provide node/npx",
    )
    return ["npx", "--no-install", "prettier"]


def run_prettier(cwd: Path) -> subprocess.CompletedProcess:
    """Run the real formatter — this is the point of the exercise.

    A simulated re-aligner would not consult `.prettierignore`, which is
    precisely the thing under test. Prettier resolves `.prettierignore`
    relative to its working directory, so it must run from the project root.
    """
    run = subprocess.run(
        [*prettier_command(), "--write", "."],
        cwd=str(cwd), capture_output=True, text=True, timeout=180,
    )
    if run.returncode != 0 and "could not determine executable" in (run.stderr or ""):
        pytest.fail(
            "prettier is not available and this suite must not skip.\n"
            "  fix: `npm install --global prettier` (CI: add a step for it).\n"
            f"  npx said: {run.stderr.strip()}",
            pytrace=False,
        )
    return run


# ──────────────────────────────────────────────────────────────────────
# The hostile project
# ──────────────────────────────────────────────────────────────────────

@dataclass
class HostileProject:
    """A throwaway project assembled to be as dirty as a real one."""

    root: Path
    init_output: str = ""
    init_exit_code: int = -1
    _spec: Path | None = field(default=None, repr=False)

    # -- construction ------------------------------------------------
    def git_init(self) -> "HostileProject":
        _require("git", "building a real repository so the pre-commit hook can run")
        self._git("init", "-q", ".")
        return self

    def codd_init(self, *extra: str, requirements: bool = True) -> "HostileProject":
        args = [
            "init", "hostile", "--language", "typescript",
            "--dest", str(self.root), "--no-suggest-lexicons", *extra,
        ]
        if requirements:
            assert self._spec is not None
            args += ["--requirements", str(self._spec)]
        result = run_codd(*args)
        self.init_output = result.output
        self.init_exit_code = result.exit_code
        return self

    # -- files -------------------------------------------------------
    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8")

    def sha(self, rel: str) -> str:
        return sha256(self.root / rel)

    def exists(self, rel: str) -> bool:
        return (self.root / rel).exists()

    def config(self) -> dict:
        return yaml.safe_load(self.read("codd/codd.yaml")) or {}

    def config_text(self) -> str:
        # The template's guidance lives in COMMENTS; yaml.safe_load throws them
        # away, so template assertions must read the raw text.
        return self.read("codd/codd.yaml")

    # -- git ---------------------------------------------------------
    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=str(self.root), env=GIT_ENV,
            capture_output=True, text=True, timeout=180,
        )

    def stage_all(self) -> None:
        self._git("add", "-A")

    def commit(self, message: str = "hostile commit") -> subprocess.CompletedProcess:
        """Attempt a real commit — the pre-commit hook runs for real."""
        self.stage_all()
        return self._git(*GIT_IDENTITY, "commit", "-m", message)

    # -- codd --------------------------------------------------------
    def codd(self, *args: object) -> Result:
        return run_codd(*args, "--path", str(self.root))

    def install_hook(self) -> Result:
        # Cheap, cached, and the one guard the suite needs against itself:
        # the hook shells out, these tests import.
        assert_cli_matches_import()
        result = self.codd("hooks", "install")
        assert result.exit_code == 0, f"`codd hooks install` failed:\n{result.output}"
        assert (self.root / ".git" / "hooks" / "pre-commit").exists()
        return result


@pytest.fixture(scope="session")
def requirements_section() -> str:
    """The heading `requirement_reconciliation.sections` must match to see units."""
    return REQUIREMENTS_SECTION


@pytest.fixture
def prettier():
    """Callable fixture: `prettier(project)` runs the real formatter in its root.

    Exposed as a fixture rather than a bare import so the tests never do
    `import conftest` — that breaks the moment this directory becomes a package
    inside `codd-dev/tests/hostile/`.
    """
    def _run(project: "HostileProject") -> subprocess.CompletedProcess:
        run = run_prettier(project.root)
        assert run.returncode == 0, (
            f"prettier itself failed (not a CoDD finding):\n{run.stdout}\n{run.stderr}"
        )
        return run

    return _run


@pytest.fixture
def make_project(tmp_path):
    """Factory: `make_project(git=True, codd_init=True, seed={...})`.

    `seed` writes files BEFORE `codd init` runs — that is how the "user already
    had one" cases (an existing .gitignore, a scaffolder's output) are staged.
    """
    counter = {"n": 0}

    def _make(*, git: bool = False, codd_init: bool = True,
              seed: dict[str, str] | None = None, init_args: tuple[str, ...] = (),
              requirements: bool = True) -> HostileProject:
        counter["n"] += 1
        root = tmp_path / f"proj{counter['n']}"
        root.mkdir()
        spec = tmp_path / f"spec{counter['n']}.md"
        spec.write_text(HOSTILE_REQUIREMENTS, encoding="utf-8")
        project = HostileProject(root=root, _spec=spec)
        if git:
            project.git_init()
        for rel, text in (seed or {}).items():
            project.write(rel, text)
        if codd_init:
            project.codd_init(*init_args, requirements=requirements)
            assert project.init_exit_code == 0, (
                f"`codd init` failed:\n{project.init_output}"
            )
        return project

    return _make
