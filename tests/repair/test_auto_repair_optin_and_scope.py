"""End-to-end coverage for the auto-repair opt-in threading + the code-level
patch-scope guard (anti-false-green).

These tests deliberately do NOT mock ``_run_repair_loop`` / ``RepairLoop`` for
the e2e groups: they drive the real Click ``codd verify`` command and the real
greenfield ``_default_verify_runner`` against a real failing pytest, with a
deterministic registered repair engine. They lock:

1. CLI ``--repair-mode automatic`` with NO on-disk ``repair:`` section now
   self-heals end-to-end (the opt-in reaches the approval gate).
2. The greenfield autopilot path self-heals through the real ``RepairLoop``.
3. Plain brownfield ``codd verify --auto-repair`` stays owner-gated.
4. The scope guard rejects oracle/spec/test/gate-control edits for a
   code-addressable failure (the false-green vector), and ALLOWS a genuine
   harness-contract test fix.
5. The ``max_files_per_proposal`` valve still escalates → rejected non-interactively.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

import codd.cli as cli
from codd.cli import main
from codd.greenfield import pipeline as greenfield_pipeline
from codd.repair import engine as engine_registry
from codd.repair.engine import RepairEngine, register_repair_engine
from codd.repair.schema import (
    ApplyResult,
    FilePatch,
    RepairProposal,
    RootCauseAnalysis,
)
from codd.repair.verify_runner import run_standalone_verify


PYTEST = f"{sys.executable} -m pytest --tb=short -q -p no:cacheprovider"

#: A failing src + test. The bug RAISES inside src/calc.py so the traceback has a
#: src frame → B0 attributes an editable SOURCE target and classes it
#: runtime_exception with code_addressable=True. That matters for two reasons:
#:   (1) the repairability classifier routes an OBSERVED code-addressable failure
#:       straight to repairable (no LLM meta-classifier call → deterministic+fast);
#:   (2) the scope guard's oracle/spec/gate-control protection only engages for a
#:       code_addressable failure (the codex5 false-green vector).
#: An assertion-only failure (assert lives in the test, no src frame) would be
#: code_addressable=False and would exercise neither path.
_BUGGY_SOURCE = 'def add(a, b):\n    raise ValueError("boom")\n'
_FIXED_SOURCE = "def add(a, b):\n    return a + b\n"
_TEST = "from src.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    """Each test registers its own engines into a clean registry."""
    monkeypatch.setattr(engine_registry, "_REPAIR_ENGINES", {})
    # Force the standalone verify path (no codd-pro handler) for CLI tests.
    monkeypatch.setattr(cli, "get_command_handler", lambda name: None)


def _greenfield_repo(tmp_path: Path, *, repair: dict | None = None, source: str = _BUGGY_SOURCE) -> Path:
    """A one-commit greenfield-shaped project with a real failing pytest."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    (tmp_path / "codd").mkdir()
    config: dict = {
        "project": {"type": "generic"},
        "scan": {"source_dirs": ["src"]},
        "verify": {"test_command": PYTEST},
    }
    if repair is not None:
        config["repair"] = repair
    (tmp_path / "codd" / "codd.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text(source, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(_TEST, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "greenfield one-shot"], cwd=tmp_path, check=True)
    return tmp_path


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class _PatchEngine(RepairEngine):
    """Deterministic engine: analyze→RCA from the attributed nodes, propose a
    fixed patch list, apply by writing files. Subclasses set ``PATCHES``."""

    PATCHES: list[FilePatch] = []
    project_root: Path | None = None

    def __init__(self, project_root=None):
        self.project_root = Path(project_root) if project_root else None

    def analyze(self, failure, dag):
        return RootCauseAnalysis(
            "deterministic", list(failure.failed_nodes), "full_file_replacement", 0.9, "t"
        )

    def propose_fix(self, rca, file_contents):
        return RepairProposal(list(type(self).PATCHES), "fix", 0.9, "t", "t")

    def apply(self, proposal, *, dry_run=False):
        applied = []
        for patch in proposal.patches:
            target = self.project_root / patch.file_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(patch.content, encoding="utf-8")
            applied.append(patch.file_path)
        return ApplyResult(True, applied, [], None)


def _register_patch_engine(name: str, patches: list[FilePatch]) -> None:
    @register_repair_engine(name)
    class _Engine(_PatchEngine):
        PATCHES = list(patches)


# ── Group 1: CLI automatic, no disk repair: section ──────────────────────────

def test_cli_automatic_no_disk_repair_section_self_heals(tmp_path: Path):
    repo = _greenfield_repo(tmp_path)  # NO repair: section
    _register_patch_engine("fix_source", [FilePatch("src/calc.py", "full_file_replacement", _FIXED_SOURCE)])

    result = CliRunner().invoke(
        main,
        ["verify", "--path", str(repo), "--auto-repair", "--repair-mode", "automatic", "--engine", "fix_source"],
    )

    assert result.exit_code == 0, result.output
    assert "REPAIR_SUCCESS" in result.output
    # The opt-in reached the gate — the explicit-opt-in error must NOT appear.
    assert "require_explicit_optin" not in result.output
    # Source was patched; the failing test now passes under a fresh verify.
    assert _read(repo / "src" / "calc.py") == _FIXED_SOURCE
    assert run_standalone_verify(repo).passed is True
    # codd.yaml still has no repair: section (the per-run opt-in was in-memory only).
    assert "repair" not in yaml.safe_load(_read(repo / "codd" / "codd.yaml"))
    # The test file is unchanged.
    assert _read(repo / "tests" / "test_calc.py") == _TEST
    # History records an applied patch + a post-repair verify pass.
    sessions = sorted((repo / ".codd" / "repair_history").glob("*/attempt_0"))
    assert sessions, "expected a repair history session"
    apply_yaml = yaml.safe_load(_read(sessions[-1] / "apply_result.yaml"))
    assert apply_yaml["success"] is True
    assert "src/calc.py" in apply_yaml["applied_patches"]
    post = yaml.safe_load(_read(sessions[-1] / "post_repair_verify.yaml"))
    assert post["passed"] is True


# ── Group 2: Greenfield automatic, no disk repair: section ───────────────────

def test_greenfield_default_verify_runner_self_heals(tmp_path: Path, monkeypatch):
    repo = _greenfield_repo(tmp_path)  # NO repair: section
    _register_patch_engine("gf_fix", [FilePatch("src/calc.py", "full_file_replacement", _FIXED_SOURCE)])

    # The greenfield runner reads engine_name from the repair section; force our
    # deterministic engine via the resolved config (apply_repair_mode copy).
    # _default_verify_runner imports apply_repair_mode locally from
    # codd.repair.approval_repair, so patch it at the source module.
    import codd.repair.approval_repair as approval_repair

    real_apply = approval_repair.apply_repair_mode

    def apply_with_engine(config, mode):
        merged = real_apply(config, mode)
        merged["repair"]["engine_name"] = "gf_fix"
        return merged

    monkeypatch.setattr(approval_repair, "apply_repair_mode", apply_with_engine)

    certified = {}
    real_certify = greenfield_pipeline._certify_verify_executed

    def spy_certify(project_root, result):
        certified["executed_anything"] = getattr(result, "executed_anything", None)
        return real_certify(project_root, result)

    monkeypatch.setattr(greenfield_pipeline, "_certify_verify_executed", spy_certify)

    message = greenfield_pipeline._default_verify_runner(
        repo, ai_command=None, max_repair_attempts=3, echo=lambda _m: None
    )

    assert "verification passed after automatic repair" in message
    assert run_standalone_verify(repo).passed is True
    assert _read(repo / "src" / "calc.py") == _FIXED_SOURCE
    # The greenfield second-gate fresh verify saw real execution.
    assert certified["executed_anything"] is True


# ── Group 3: Brownfield stays owner-gated ────────────────────────────────────

def test_brownfield_plain_auto_repair_no_repair_section_is_gated(tmp_path: Path):
    repo = _greenfield_repo(tmp_path)  # NO repair: section
    _register_patch_engine("never_runs", [FilePatch("src/calc.py", "full_file_replacement", _FIXED_SOURCE)])

    result = CliRunner().invoke(
        main, ["verify", "--path", str(repo), "--auto-repair", "--engine", "never_runs"]
    )

    assert result.exit_code != 0
    assert "[repair] section is required" in result.output
    # No source change, and the repair loop never ran (no history at all).
    assert _read(repo / "src" / "calc.py") == _BUGGY_SOURCE
    history_dir = repo / ".codd" / "repair_history"
    assert not history_dir.exists() or not list(history_dir.glob("*"))


def test_brownfield_auto_mode_without_explicit_optin_reaches_gate_and_fails(tmp_path: Path):
    # Disk repair.approval_mode: auto but NO allow_auto.require_explicit_optin →
    # the per-run opt-in is NOT injected (plain --auto-repair), so the gate fires.
    repo = _greenfield_repo(tmp_path, repair={"approval_mode": "auto", "engine_name": "src_fix"})
    _register_patch_engine("src_fix", [FilePatch("src/calc.py", "full_file_replacement", _FIXED_SOURCE)])

    result = CliRunner().invoke(
        main, ["verify", "--path", str(repo), "--auto-repair"]
    )

    assert result.exit_code != 0
    assert "REPAIR_FAILED" in result.output
    # The explicit-opt-in gate is the reason; no source change.
    assert _read(repo / "src" / "calc.py") == _BUGGY_SOURCE


# ── Group 4: Scope / false-green rejects (the key ones) ──────────────────────

def _run_automatic(repo: Path, engine: str):
    return CliRunner().invoke(
        main,
        ["verify", "--path", str(repo), "--auto-repair", "--repair-mode", "automatic", "--engine", engine],
    )


def test_scope_rejects_editing_failing_test_for_assertion_failure(tmp_path: Path):
    repo = _greenfield_repo(tmp_path)
    # The model tries to rewrite the failing TEST to pass (assertion failure).
    _register_patch_engine(
        "rewrite_test",
        [FilePatch("tests/test_calc.py", "full_file_replacement",
                   "from src.calc import add\n\n\ndef test_add():\n    assert True\n")],
    )

    result = _run_automatic(repo, "rewrite_test")

    assert result.exit_code != 0
    assert "REPAIR_SUCCESS" not in result.output
    # NO files changed.
    assert _read(repo / "tests" / "test_calc.py") == _TEST
    assert _read(repo / "src" / "calc.py") == _BUGGY_SOURCE
    assert run_standalone_verify(repo).passed is False


def test_scope_rejects_editing_codd_yaml_to_weaken_verification(tmp_path: Path):
    repo = _greenfield_repo(tmp_path)
    # The model tries to weaken the oracle: edit codd.yaml to opt out of tests.
    weakened = yaml.safe_dump(
        {"project": {"type": "generic"}, "scan": {"source_dirs": ["src"]},
         "verify": {"allow_structural_only": True}}
    )
    _register_patch_engine(
        "weaken_oracle",
        [FilePatch("codd/codd.yaml", "full_file_replacement", weakened)],
    )

    result = _run_automatic(repo, "weaken_oracle")

    assert result.exit_code != 0
    assert "REPAIR_SUCCESS" not in result.output
    # codd.yaml unchanged (still has the real test command, no opt-out).
    config = yaml.safe_load(_read(repo / "codd" / "codd.yaml"))
    assert config.get("verify", {}).get("test_command") == PYTEST
    assert "allow_structural_only" not in config.get("verify", {})
    assert _read(repo / "src" / "calc.py") == _BUGGY_SOURCE


def test_scope_rejects_editing_spec_doc_for_code_addressable_failure(tmp_path: Path):
    repo = _greenfield_repo(tmp_path)
    (repo / "docs" / "infra").mkdir(parents=True)
    (repo / "docs" / "infra" / "ci.md").write_text("# CI governance\nrequire: real_add\n", encoding="utf-8")
    # The codex5 vector: a doc-only "repair" that rewrites the spec the test is
    # derived from, for a code-addressable governance failure.
    _register_patch_engine(
        "edit_spec",
        [FilePatch("docs/infra/ci.md", "full_file_replacement", "# CI governance\nrequire: nothing\n")],
    )

    result = _run_automatic(repo, "edit_spec")

    assert result.exit_code != 0
    assert "REPAIR_SUCCESS" not in result.output
    # Spec doc unchanged; source unchanged.
    assert "real_add" in _read(repo / "docs" / "infra" / "ci.md")
    assert _read(repo / "src" / "calc.py") == _BUGGY_SOURCE


def test_scope_allows_harness_contract_scaffold_fix(tmp_path: Path):
    """POSITIVE case: a genuinely broken generated scaffold TEST (collection error
    from a SyntaxError, no resolvable source culprit) IS attributed editable by B0
    (harness_contract_violation) and the scope guard lets auto-repair patch it."""
    repo = _greenfield_repo(tmp_path, source=_FIXED_SOURCE)  # source is fine
    # An invalid generated scaffold test makes collection fail with a SyntaxError
    # and NO source frame → B0 classifies harness_contract_violation, and marks the
    # scaffold test itself editable (the one class where a test IS the defect).
    scaffold = repo / "tests" / "test_scaffold.py"
    scaffold.write_text("def test_scaffold(:\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "broken scaffold"], cwd=repo, check=True)

    # Sanity: verify fails and the attribution marks the harness contract.
    pre = run_standalone_verify(repo)
    assert pre.passed is False
    assert pre.failure.failure_class == "harness_contract_violation"
    assert "tests/test_scaffold.py" in pre.failure.failed_nodes

    _register_patch_engine(
        "fix_scaffold",
        [FilePatch("tests/test_scaffold.py", "full_file_replacement",
                   "def test_scaffold():\n    assert True\n")],
    )

    result = _run_automatic(repo, "fix_scaffold")

    assert result.exit_code == 0, result.output
    assert "REPAIR_SUCCESS" in result.output
    assert _read(scaffold) == "def test_scaffold():\n    assert True\n"
    assert run_standalone_verify(repo).passed is True


# ── Group 5: Max-files valve still escalates → rejected non-interactively ─────

def test_max_files_valve_escalates_and_is_rejected_non_interactively(tmp_path: Path):
    repo = _greenfield_repo(tmp_path)
    # Two in-scope source files, but max_files_per_proposal=1 via the resolved
    # config → escalation to required approval → no TTY → rejected.
    (repo / "src" / "extra.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "extra src"], cwd=repo, check=True)
    repo_yaml = yaml.safe_load(_read(repo / "codd" / "codd.yaml"))
    repo_yaml["repair"] = {"allow_auto": {"max_files_per_proposal": 1}, "engine_name": "two_files"}
    (repo / "codd" / "codd.yaml").write_text(yaml.safe_dump(repo_yaml), encoding="utf-8")

    _register_patch_engine(
        "two_files",
        [
            FilePatch("src/calc.py", "full_file_replacement", _FIXED_SOURCE),
            FilePatch("src/extra.py", "full_file_replacement", "x = 2\n"),
        ],
    )

    with pytest.warns(RuntimeWarning, match="max_files_per_proposal"):
        result = _run_automatic(repo, "two_files")

    assert result.exit_code != 0
    assert "REPAIR_SUCCESS" not in result.output
    # Escalated to required approval and rejected (non-interactive) → no apply.
    assert _read(repo / "src" / "calc.py") == _BUGGY_SOURCE
    assert _read(repo / "src" / "extra.py") == "x = 1\n"


# ── Unit coverage for the scope guard's role-aware classification ─────────────

from codd.repair.auto_scope_guard import evaluate_auto_patch_scope  # noqa: E402
from codd.repair.schema import VerificationFailureReport  # noqa: E402


def _scope(patch_path: str, *, failure_class: str, code_addressable: bool, allowlist: list[str]):
    failure = VerificationFailureReport(
        "test_command", list(allowlist), ["x"], {}, "t",
        failure_class=failure_class, code_addressable=code_addressable,
    )
    rca = RootCauseAnalysis("c", list(allowlist), "full_file_replacement", 0.9, "t")
    proposal = RepairProposal([FilePatch(patch_path, "full_file_replacement", "x\n")], "r", 0.9, "t", "t")
    return evaluate_auto_patch_scope(proposal, failure, rca, project_root="/proj")


_CODE_ADDR = {"failure_class": "runtime_exception", "code_addressable": True, "allowlist": ["src/calc.py"]}


@pytest.mark.parametrize(
    "path",
    [
        "codd/codd.yaml",
        "pytest.ini",
        "pyproject.toml",
        "package.json",
        "tests/conftest.py",
        ".github/workflows/ci.yml",
        ".gitlab-ci.yml",
        "docs/infra/ci.md",
        "design/architecture.md",
        "requirements/spec.rst",
        "tox.ini",
        "setup.cfg",
        "playwright.config.ts",
    ],
)
def test_oracle_and_gate_control_paths_rejected_for_code_addressable(path: str):
    decision = _scope(path, **_CODE_ADDR)
    assert decision.allowed is False
    assert decision.escalate is True
    assert path in decision.offending_paths


def test_in_allowlist_source_is_allowed_for_code_addressable():
    assert _scope("src/calc.py", **_CODE_ADDR).allowed is True


def test_source_outside_allowlist_rejected_when_targets_resolved():
    # The primary failure resolved a concrete path set → containment is enforced.
    assert _scope("src/other.py", **_CODE_ADDR).allowed is False


def test_test_file_rejected_for_assertion_failure():
    decision = _scope(
        "tests/test_calc.py",
        failure_class="assertion_failure",
        code_addressable=True,
        allowlist=["src/calc.py"],
    )
    assert decision.allowed is False


def test_e2e_ts_test_file_rejected_for_assertion_failure():
    # ANTI-FALSE-GREEN: a ``.e2e.ts`` file is a TEST file and must stay read-only
    # for an assertion failure (auto-repair must not rewrite a generated e2e test
    # into passing). Even outside a ``tests/`` dir the ``.e2e.ts`` suffix marks it.
    decision = _scope(
        "src/tempconv_conversion.e2e.ts",
        failure_class="assertion_failure",
        code_addressable=True,
        allowlist=["src/index.ts"],
    )
    assert decision.allowed is False


def test_named_scaffold_allowed_for_harness_contract():
    decision = _scope(
        "tests/test_scaffold.py",
        failure_class="harness_contract_violation",
        code_addressable=True,
        allowlist=["tests/test_scaffold.py"],
    )
    assert decision.allowed is True


def test_unnamed_test_rejected_even_for_harness_contract():
    decision = _scope(
        "tests/test_other.py",
        failure_class="harness_contract_violation",
        code_addressable=True,
        allowlist=["tests/test_scaffold.py"],
    )
    assert decision.allowed is False


def test_structural_failure_allows_source_and_doc_drift():
    # A structural DAG failure (no code_addressable, logical node ids) keeps the
    # historical behaviour: source/doc drift repair is legitimate and allowed.
    # (Oracle protection is gated on code_addressable; tests stay protected.)
    structural = {"failure_class": "", "code_addressable": False, "allowlist": ["impl:main"]}
    assert _scope("src/app.py", **structural).allowed is True
    assert _scope("docs/design/login.md", **structural).allowed is True


def test_structural_failure_still_blocks_test_rewrite():
    structural = {"failure_class": "", "code_addressable": False, "allowlist": ["impl:main"]}
    assert _scope("tests/test_app.py", **structural).allowed is False
