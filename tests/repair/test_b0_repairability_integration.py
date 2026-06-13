"""B0 integration — a test_command failure now (a) gets a non-empty failed_nodes
from the verify runner, (b) routes to REPAIRABLE in the repairability classifier
even when ``git diff baseline..HEAD`` is empty (the greenfield single-commit
path), and (c) still fails honestly when unfixable (anti-false-green)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

from codd.dag import DAG
from codd.repair import verify_runner as verify_runner_module
from codd.repair.loop import (
    RepairLoop,
    RepairLoopConfig,
    _violations_from_verify_result,
)
from codd.repair.repairability_classifier import RepairabilityClassifier
from codd.repair.schema import (
    ApplyResult,
    FilePatch,
    RepairProposal,
    RootCauseAnalysis,
    VerificationFailureReport,
)
from codd.repair.engine import RepairEngine, register_repair_engine, _REPAIR_ENGINES
from codd.repair.verify_runner import VerifyRunner, run_standalone_verify


PYTEST = f"{sys.executable} -m pytest --tb=short -q -p no:cacheprovider"


class _CheckResult:
    def __init__(self) -> None:
        self.check_name = "node_completeness"
        self.severity = "red"
        self.passed = True
        self.message = ""


def _patch_dag_green(monkeypatch) -> None:
    monkeypatch.setattr(verify_runner_module, "load_dag_settings", lambda root, s: s)
    monkeypatch.setattr(verify_runner_module, "build_dag", lambda root, s: DAG())
    monkeypatch.setattr(verify_runner_module, "run_checks", lambda *a, **k: [])


def _greenfield_repo(tmp_path: Path, *, source: str, test: str) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    (tmp_path / "codd").mkdir()
    (tmp_path / "codd" / "codd.yaml").write_text(
        yaml.safe_dump(
            {"project": {"type": "generic"}, "scan": {"source_dirs": ["src"]}, "verify": {"test_command": PYTEST}}
        ),
        encoding="utf-8",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text(source, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(test, encoding="utf-8")
    # ONE commit: source + tests together — the greenfield shape.
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "greenfield one-shot"], cwd=tmp_path, check=True)
    return tmp_path


def _head(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()


def test_verify_runner_failing_test_now_populates_failed_nodes(tmp_path, monkeypatch) -> None:
    _patch_dag_green(monkeypatch)
    # Bug raised IN the source → the traceback has a src/calc.py frame, so B0
    # attributes an editable SOURCE target (the real greenfield shape).
    repo = _greenfield_repo(
        tmp_path,
        source="def add(a, b):\n    raise ValueError('boom')\n",  # bug in source
        test="from src.calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    )
    result = VerifyRunner(repo, yaml.safe_load((repo / "codd" / "codd.yaml").read_text())).run()

    assert result.passed is False
    # The previously-empty failed_nodes is now populated (the core B0 fix), with
    # the SOURCE module as the editable target.
    assert "src/calc.py" in result.failure.failed_nodes
    assert result.failure.failure_class == "runtime_exception"
    assert result.failure.code_addressable is True
    failure = next(f for f in result.failures if f.check_name == "test_command")
    assert "src/calc.py" in failure.details["failed_nodes"]
    # the failing test is recorded as read-only evidence, NOT an editable target
    assert "tests/test_calc.py" in failure.details.get("evidence_nodes", [])


def test_observed_test_failure_routes_to_repairable_with_empty_diff(tmp_path, monkeypatch) -> None:
    """The greenfield dead-end: baseline==HEAD => empty diff. B0 still routes
    the observed code failure to REPAIRABLE instead of unrepairable."""
    _patch_dag_green(monkeypatch)
    repo = _greenfield_repo(
        tmp_path,
        source="def add(a, b):\n    raise ValueError('boom')\n",
        test="from src.calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    )
    result = VerifyRunner(repo, yaml.safe_load((repo / "codd" / "codd.yaml").read_text())).run()
    violations = _violations_from_verify_result(result, fallback=result.failure)

    classifier = RepairabilityClassifier(llm=None, repo_path=repo)
    # baseline == HEAD => `git diff HEAD..HEAD` is empty.
    classification = classifier.classify(violations, baseline_ref=_head(repo))

    assert len(classification.repairable) == 1
    assert classification.unrepairable == []
    assert classification.pre_existing == []


def test_environment_failure_is_not_force_routed(tmp_path) -> None:
    repo = tmp_path
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)

    env_failure = VerificationFailureReport(
        "test_command",
        ["tests/test_x.py"],
        ["env err"],
        {},
        "t",
        failure_class="environment_build_error",
        code_addressable=False,
    )
    classifier = RepairabilityClassifier(llm=None, repo_path=repo)
    classification = classifier.classify([env_failure], baseline_ref=_head(repo))

    # Not force-routed (code_addressable False); with no LLM it stays unrepairable.
    assert classification.repairable == []
    assert classification.unrepairable == [env_failure]


def test_unfixable_failure_still_fails_honestly_after_engine_engages(tmp_path, monkeypatch) -> None:
    """Anti-false-green: B0 makes the failure reach the engine, but if the engine
    cannot fix it, verify STILL fails — no fake green, no test rewriting."""
    _patch_dag_green(monkeypatch)
    repo = _greenfield_repo(
        tmp_path,
        source="def add(a, b):\n    raise ValueError('boom')\n",
        test="from src.calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    )
    result = run_standalone_verify(repo)
    assert result.passed is False

    engaged = {"analyze": 0, "propose": 0}

    if "stub_noop" not in _REPAIR_ENGINES:

        @register_repair_engine("stub_noop")
        class _StubNoopEngine(RepairEngine):
            def __init__(self, project_root=None):
                self.project_root = project_root

            def analyze(self, failure, dag):
                engaged["analyze"] += 1
                return RootCauseAnalysis("stub", list(failure.failed_nodes), "full_file_replacement", 0.5, "t")

            def propose_fix(self, rca, file_contents):
                engaged["propose"] += 1
                # An INEFFECTIVE patch: keeps the bug. Must not yield a green.
                return RepairProposal(
                    [FilePatch("src/calc.py", "full_file_replacement", "def add(a, b):\n    raise ValueError('boom')\n")],
                    "noop",
                    0.5,
                    "t",
                    "t",
                )

            def apply(self, proposal, dry_run=False):
                (repo / "src" / "calc.py").write_text(proposal.patches[0].content, encoding="utf-8")
                return ApplyResult(True, ["src/calc.py"], [], None)

    config = RepairLoopConfig(
        max_attempts=2,
        approval_mode="auto",
        engine_name="stub_noop",
        llm_client=object(),
        repo_path=repo,
    )
    outcome = RepairLoop(config, repo).run(
        result.failure,
        DAG(),
        verify_callable=lambda: run_standalone_verify(repo),
        initial_verify_result=result,
    )

    # B0 made the failure addressable: the engine actually ran.
    assert engaged["analyze"] >= 1
    # ...but the unfixable failure does NOT become a success.
    assert outcome.status != "REPAIR_SUCCESS"
    assert run_standalone_verify(repo).passed is False
