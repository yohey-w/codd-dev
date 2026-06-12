#!/usr/bin/env python3
"""D7 — chaos / interruption: kill the greenfield pipeline, then resume.

Reuses the vendor-neutral stub-AI pattern from tests/greenfield/conftest.py: the
pipeline is driven by dependency-injected stage runners (no real LLM at all), so
this is free + deterministic. For each kill point we:

  1. run the pipeline with one stage scripted to fail (the "kill"),
  2. assert it checkpoints a FAILED status,
  3. resume from the checkpoint,
  4. assert it converges (success), does NOT re-run already-checkpointed stages
     (option/checkpoint restoration), and DOES re-run the unit that failed.

A deviation at any step is a finding: the resume/idempotency contract broke.
No LLM.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from _common import AxisResult, Finding, ensure_repo_on_path

ensure_repo_on_path()

# Early / mid / late kill points across the stage graph.
KILL_POINTS = ["generate:1", "generate:2", "implement:docs/design/core_design.md"]
# Linear stage order used to compute which stages must survive a resume.
STAGE_ORDER = ["init", "plan", "generate:1", "generate:2", "implement:docs/design/core_design.md"]


def _silent(_msg: str) -> None:
    """Swallow the pipeline's per-stage progress chatter; keep the axis report clean."""


def _runners(calls: list[str], *, fail_on: str | None = None) -> dict:
    """Dependency-injected pipeline runners that record calls; one may fail."""
    from codd.greenfield.pipeline import ImplementTaskRef, StageError

    def init_runner(project_root, **kw):
        calls.append("init")

    def elicit_runner(project_root, **kw):
        calls.append("elicit")
        return "findings=0"

    def plan_runner(project_root, **kw):
        calls.append("plan")
        return 2

    def wave_lister(project_root):
        return [1, 2]

    def generate_wave_runner(project_root, wave, **kw):
        unit = f"generate:{wave}"
        calls.append(unit)
        if fail_on == unit:
            raise ValueError(f"scripted chaos failure at {unit}")
        return "1 generated, 0 skipped"

    def task_lister(project_root):
        return [ImplementTaskRef(task_id="docs/design/core_design.md",
                                 design_node="docs/design/core_design.md")]

    def implement_task_runner(project_root, task, **kw):
        unit = f"implement:{task.task_id}"
        calls.append(unit)
        if fail_on == unit:
            raise StageError(f"scripted chaos failure at {unit}")
        return "1 file(s) generated"

    def verify_runner(project_root, **kw):
        calls.append("verify")
        return "verification passed"

    def propagate_runner(project_root, **kw):
        calls.append("propagate")
        return "committed=0"

    def check_runner(project_root):
        calls.append("check")
        return "health check passed"

    return {
        "init_runner": init_runner,
        "elicit_runner": elicit_runner,
        "plan_runner": plan_runner,
        "wave_lister": wave_lister,
        "generate_wave_runner": generate_wave_runner,
        "task_lister": task_lister,
        "implement_task_runner": implement_task_runner,
        "verify_runner": verify_runner,
        "propagate_runner": propagate_runner,
        "check_runner": check_runner,
    }


def _must_survive(kill_point: str) -> set[str]:
    """Stages strictly before the kill point: they must NOT re-run on resume."""
    idx = STAGE_ORDER.index(kill_point)
    return set(STAGE_ORDER[:idx])


def run() -> AxisResult:
    result = AxisResult(axis="D7")

    from codd.greenfield.pipeline import GreenfieldPipeline
    from tests.greenfield.conftest import make_stub_project

    converged = 0
    for kp in KILL_POINTS:
        with tempfile.TemporaryDirectory(prefix="codd-d7.") as td:
            project = make_stub_project(Path(td), "stub-ai --print")

            # 1+2. kill: one stage fails; expect a checkpointed FAILED run.
            calls1: list[str] = []
            r1 = GreenfieldPipeline(echo=_silent, **_runners(calls1, fail_on=kp)).run(project)
            if getattr(r1, "status", None) != "failed":
                result.findings.append(
                    Finding("D7", "kill did not yield a checkpointed failure",
                            f"status={getattr(r1, 'status', None)}", subject=kp))
                continue

            # 3. resume from the checkpoint.
            calls2: list[str] = []
            r2 = GreenfieldPipeline(echo=_silent, **_runners(calls2)).run(project, resume=True)
            if getattr(r2, "status", None) != "success":
                result.findings.append(
                    Finding("D7", "resume after kill did not converge",
                            f"status={getattr(r2, 'status', None)}", subject=kp))
                continue

            # 4a. checkpointed stages must NOT be re-run.
            re_ran = _must_survive(kp) & set(calls2)
            if re_ran:
                result.findings.append(
                    Finding("D7", "resume re-ran already-checkpointed stages (lost restoration)",
                            f"re-ran {sorted(re_ran)}", subject=kp))
            # 4b. the failed unit MUST be re-run.
            if kp not in calls2:
                result.findings.append(
                    Finding("D7", "resume skipped the unit that had failed",
                            f"{kp} absent from resume calls", subject=kp))
            converged += 1

    result.stats["kill_points"] = KILL_POINTS
    result.summary = (
        f"{converged}/{len(KILL_POINTS)} kill→resume cycles converged; "
        f"{len(result.findings)} finding(s)"
    )
    return result


def main() -> int:
    result = run()
    result.print_report()
    return 1 if result.findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
