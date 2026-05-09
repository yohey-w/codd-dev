from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil

from click.testing import CliRunner
import pytest
import yaml

import codd.cli as cli
from codd.cli import main
from codd.dag import DAG
from codd.repair import engine as engine_registry
from codd.repair.engine import RepairEngine, register_repair_engine
from codd.repair.git_patcher import GitPatcher
from codd.repair.schema import (
    ApplyResult,
    FilePatch,
    RepairProposal,
    RootCauseAnalysis,
    VerificationFailureReport,
)
from codd.repair.verify_runner import VerificationResult, VerifyRunner, run_standalone_verify


SKELETON = Path(__file__).with_name("standalone_repair_skeleton")
STATUS_FILE = "src/status_service.py"


@dataclass
class _Outcome:
    status: str
    history_session_dir: Path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    target = tmp_path / "project"
    shutil.copytree(SKELETON, target)
    return target


@pytest.fixture(autouse=True)
def isolated_repair_registry(monkeypatch):
    """Snapshot + restore the repair engine registry around each test.

    cmd_460: in CI (Python 3.11) the previous shallow `monkeypatch.setattr`
    sometimes left a registered scripted engine visible to a later test, which
    routed the repair loop down the unrepairable path before it could try
    `--max-attempts`. The explicit teardown that follows guarantees a fresh
    registry for every test regardless of pytest's reuse heuristics.
    """

    snapshot = dict(engine_registry._REPAIR_ENGINES)
    monkeypatch.setattr(engine_registry, "_REPAIR_ENGINES", dict(snapshot))
    yield
    engine_registry._REPAIR_ENGINES.clear()
    engine_registry._REPAIR_ENGINES.update(snapshot)


def _status_patch() -> FilePatch:
    return FilePatch(
        STATUS_FILE,
        "full_file_replacement",
        "def status() -> str:\n    return 'ok'\n",
    )


def _failure(message: str = "missing implementation") -> VerificationFailureReport:
    return VerificationFailureReport(
        check_name="verify",
        failed_nodes=[STATUS_FILE],
        error_messages=[message],
        dag_snapshot={"nodes": [], "edges": []},
        timestamp="2026-05-06T00:00:00Z",
    )


def _register_status_engine(name: str, *, apply_success: bool = True) -> type[RepairEngine]:
    class ScriptedStatusRepairEngine(RepairEngine):
        analyzed: list[VerificationFailureReport] = []
        proposed_from: list[dict[str, str]] = []

        def __init__(self, project_root: Path | None = None):
            self.project_root = Path(project_root) if project_root is not None else None

        def analyze(self, failure: VerificationFailureReport, dag: DAG) -> RootCauseAnalysis:
            type(self).analyzed.append(failure)
            return RootCauseAnalysis(
                probable_cause="planned output file is absent",
                affected_nodes=[STATUS_FILE],
                repair_strategy="full_file_replacement",
                confidence=0.9,
                analysis_timestamp="2026-05-06T00:00:01Z",
            )

        def propose_fix(self, rca: RootCauseAnalysis, file_contents: dict[str, str]) -> RepairProposal:
            type(self).proposed_from.append(file_contents)
            return RepairProposal(
                [_status_patch()],
                "create the missing status service",
                0.9,
                "2026-05-06T00:00:02Z",
                rca.analysis_timestamp,
            )

        def apply(self, proposal: RepairProposal, *, dry_run: bool = False) -> ApplyResult:
            if self.project_root is None:
                return ApplyResult(False, [], [STATUS_FILE], "project_root missing")
            if not apply_success:
                return ApplyResult(False, [], [STATUS_FILE], "scripted apply failure")
            return GitPatcher().apply(proposal.patches[0], self.project_root, dry_run=dry_run)

    return register_repair_engine(name)(ScriptedStatusRepairEngine)


def test_skeleton_is_generic_and_has_no_domain_specific_paths():
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in SKELETON.rglob("*")
        if path.is_file() and path.suffix in {".md", ".py", ".yaml"}
    )

    assert "standalone-repair-skeleton" in text
    assert "osato" not in text.lower()
    assert "/home/" not in text


def test_standalone_skeleton_initially_reports_missing_output(project: Path):
    result = run_standalone_verify(project)

    assert result.passed is False
    assert STATUS_FILE in result.failure.failed_nodes


def test_standalone_skeleton_passes_after_missing_output_exists(project: Path):
    target = project / STATUS_FILE
    target.parent.mkdir(parents=True)
    target.write_text("def status() -> str:\n    return 'ok'\n", encoding="utf-8")

    result = run_standalone_verify(project)

    assert result.passed is True
    assert result.failures == []


def test_auto_repair_uses_standalone_path_even_when_pro_handler_exists(project: Path, monkeypatch):
    calls: list[Path] = []
    monkeypatch.setattr(cli, "get_command_handler", lambda name: object())
    monkeypatch.setattr(
        "codd.repair.verify_runner.run_standalone_verify",
        lambda project_root: calls.append(project_root) or VerificationResult(True),
    )
    monkeypatch.setattr(cli, "_run_pro_command", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("pro called")))

    result = CliRunner().invoke(main, ["verify", "--path", str(project), "--auto-repair"])

    assert result.exit_code == 0
    assert calls == [project.resolve()]


def test_verify_without_auto_repair_preserves_pro_command_behavior(project: Path, monkeypatch):
    captured = {}

    def run_pro(name, **kwargs):
        captured["name"] = name
        captured["kwargs"] = kwargs

    monkeypatch.setattr(cli, "_run_pro_command", run_pro)

    result = CliRunner().invoke(main, ["verify", "--path", str(project)])

    assert result.exit_code == 0
    assert captured == {"name": "verify", "kwargs": {"path": str(project), "sprint": None}}


def test_missing_expected_proof_break_warns_and_skips(project: Path, monkeypatch):
    monkeypatch.setattr(
        "codd.repair.verify_runner.build_dag",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("docs/design/feature_design.md does not contain the expected proof break")),
    )

    with pytest.warns(RuntimeWarning, match="expected proof break missing"):
        result = VerifyRunner(project, {"project": {"type": "generic"}}).run()

    assert result.passed is True
    assert result.failures == []
    assert result.warnings == ["expected proof break missing; skipped proof-break verification"]


def test_auto_repair_cli_echoes_proof_break_warning(project: Path, monkeypatch):
    monkeypatch.setattr(
        "codd.repair.verify_runner.run_standalone_verify",
        lambda project_root: VerificationResult(True, warnings=["expected proof break missing; skipped proof-break verification"]),
    )

    result = CliRunner().invoke(main, ["verify", "--path", str(project), "--auto-repair"])

    assert result.exit_code == 0
    assert "WARNING: expected proof break missing" in result.output


def test_repair_loop_reaches_success_on_standalone_skeleton(project: Path):
    _register_status_engine("scripted-success")

    result = CliRunner().invoke(
        main,
        ["verify", "--path", str(project), "--auto-repair", "--engine", "scripted-success"],
    )

    assert result.exit_code == 0
    assert "Repair outcome: REPAIR_SUCCESS" in result.output
    assert (project / STATUS_FILE).read_text(encoding="utf-8").startswith("def status")


def test_repair_loop_returns_exhausted_after_max_attempts(project: Path):
    _register_status_engine("scripted-noop", apply_success=False)

    result = CliRunner().invoke(
        main,
        ["verify", "--path", str(project), "--auto-repair", "--engine", "scripted-noop", "--max-attempts", "2"],
    )

    assert result.exit_code == 2
    assert "Repair outcome: MAX_ATTEMPTS_REACHED" in result.output
    assert not (project / STATUS_FILE).exists()


def test_repair_loop_passes_standalone_file_contents_to_engine(project: Path):
    engine_cls = _register_status_engine("scripted-file-context")

    result = CliRunner().invoke(
        main,
        ["verify", "--path", str(project), "--auto-repair", "--engine", "scripted-file-context"],
    )

    assert result.exit_code == 0
    assert engine_cls.proposed_from[0] == {}
    assert engine_cls.analyzed[0].failed_nodes


def test_auto_repair_verify_callable_reruns_standalone_verify(project: Path, monkeypatch):
    outcomes = [VerificationResult(False, failure=_failure()), VerificationResult(True)]
    monkeypatch.setattr("codd.repair.verify_runner.run_standalone_verify", lambda project_root: outcomes.pop(0))
    monkeypatch.setattr(
        cli,
        "_run_repair_loop",
        lambda project_root, failure, **kwargs: (
            kwargs["verify_callable"](),
            _Outcome("REPAIR_SUCCESS", project / ".codd" / "repair_history" / "verify-callable"),
        )[1],
    )

    result = CliRunner().invoke(main, ["verify", "--path", str(project), "--auto-repair"])

    assert result.exit_code == 0
    assert outcomes == []


def test_auto_repair_warning_result_does_not_launch_repair_loop(project: Path, monkeypatch):
    called = False
    monkeypatch.setattr(
        "codd.repair.verify_runner.run_standalone_verify",
        lambda project_root: VerificationResult(True, warnings=["expected proof break missing; skipped proof-break verification"]),
    )

    def run_loop(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(cli, "_run_repair_loop", run_loop)

    result = CliRunner().invoke(main, ["verify", "--path", str(project), "--auto-repair"])

    assert result.exit_code == 0
    assert called is False
