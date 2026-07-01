"""Greenfield autopilot pipeline: a requirements document in, a built system out.

This module realizes CoDD's core greenfield philosophy: the user writes a
requirements document and walks away — the system builds itself. The pipeline
composes the existing stage implementations through the SAME code paths the
CLI commands use (in-process imports, not subprocesses), while keeping each
stage boundary identical to its CLI command so a shell-script composition of
the CLI is a faithful equivalent.

Stage sequence (CLI equivalents in parentheses):

  1. ``init``       (``codd init NAME --language LANG [--requirements FILE]``)
                    Skipped when a CoDD config dir already exists.
  2. ``elicit``     (``codd elicit`` + ``codd elicit apply``) — advisory and
                    NEVER blocking: findings are applied automatically; any
                    failure degrades to a warning and the pipeline continues.
  3. ``plan``       (``codd plan --init --force``) — writes ``wave_config``.
  4. ``generate``   (``codd generate --wave N`` for every wave, in order;
                    the ``--all-waves`` CLI flag is the shell equivalent).
  5. ``implement``  (per task: ``codd implement plan --task T`` →
                    ``codd implement steps --task T --approve --all`` →
                    ``codd implement run --task T``, + the verifiable-behavior
                    coverage gate). Tasks are enumerated deterministically
                    (see :func:`codd.implementer.list_implement_tasks`); when
                    none exist, tasks are first derived from the design docs
                    and auto-approved (``codd plan derive`` + ``plan approve``).
  6. ``verify``     (``codd verify --auto-repair --max-attempts N``) — repair
                    approval runs in AUTOMATIC mode by default in autopilot:
                    the run itself is the explicit opt-in
                    (``repair.allow_auto.require_explicit_optin``). Proposals
                    exceeding ``repair.allow_auto.max_files_per_proposal``
                    still escalate to required approval as a safety valve and
                    are rejected in unattended runs.
  7. ``propagate``  (``codd propagate --verify`` then ``--commit``) —
                    advisory on a fresh build: "nothing to propagate" (no git
                    repo / no changed files / no verify state) degrades to a
                    warning instead of failing the autopilot.
  8. ``check``      (``codd check``) — the final health gate.

Session checkpoint schema (``.codd/greenfield_session.yaml``) — the source of
truth for ``codd greenfield --resume``. Written after every completed unit::

    version: 1
    created_at: "2026-01-01T00:00:00Z"     # ISO-8601 UTC
    updated_at: "2026-01-01T00:05:00Z"
    options:                                # resolved run options
      project_name: my-app
      language: python
      requirements: docs/spec.md            # or null
      ai_command: null                       # explicit --ai-cmd override only
      elicit: true
      max_repair_attempts: 10
      coverage_gate: true
      propagate_commit: true
      ntfy_topic: ""
    stages:                                  # one entry per stage, in order
      init:      {status: done, detail: "..."}
      elicit:    {status: warning, detail: "..."}
      plan:      {status: done, detail: "3 wave(s)"}
      generate:                              # unit-tracked stage
        status: failed
        detail: "wave 2: ..."
        units: {"1": done, "2": failed, "3": pending}
      implement:                             # unit-tracked stage
        status: pending
        units: {"docs/design/auth.md": pending}
      verify:    {status: pending}
      propagate: {status: pending}
      check:     {status: pending}
    result:
      status: failed                         # running | success | failed
      failed_stage: generate
      failed_unit: "2"
      error: "..."

Stage ``status`` values: ``pending`` (not started), ``done``, ``warning``
(advisory failure — counts as complete), ``skipped`` (not applicable — counts
as complete), ``failed``. ``--resume`` re-runs the first stage that is not
complete, skipping units already marked ``done``; stages are idempotent
(``generate`` skips existing files, implement re-runs are safe), which makes
resumption safe.

ntfy notifications (start / per-stage / failure / success) are notify-only and
never block: posting failures are swallowed, and the pipeline never waits for
a human. This is the async-HITL MVP — the human gets pinged, the build goes on.

AI-CLI agnosticism: this module never inspects or special-cases the configured
``ai_command``. Every AI invocation resolves through the project configuration
(``resolve_ai_command`` / ``SubprocessAiCommand``), so any text-in/text-out
CLI string works unchanged.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
from typing import Any

import yaml


SESSION_FILENAME = "greenfield_session.yaml"
SESSION_VERSION = 1

STAGES: tuple[str, ...] = (
    "init",
    "elicit",
    "plan",
    "generate",
    "implement",
    "verify",
    "ci_scaffold",
    "propagate",
    "check",
)

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_WARNING = "warning"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
_COMPLETE_STATUSES = {STATUS_DONE, STATUS_WARNING, STATUS_SKIPPED}

#: Built-in defaults; ``greenfield:`` in codd.yaml/defaults.yaml overrides them.
DEFAULT_OPTIONS: dict[str, Any] = {
    "elicit": True,
    "max_repair_attempts": 10,
    "coverage_gate": True,
    "ntfy_topic": "",
    "propagate_commit": True,
    "chunk_size": None,
    "timeout_per_chunk": 600,
}

RESUME_COMMAND = "codd greenfield --resume"

_INSPECT_COMMANDS: dict[str, str] = {
    "init": "codd init <name> --language <language>",
    "elicit": "codd elicit",
    "plan": "codd plan --init --force",
    "generate": "codd generate --wave {unit}",
    "implement": "codd implement run --task {unit}",
    "verify": "codd verify --auto-repair",
    "ci_scaffold": "codd greenfield --resume",
    "propagate": "codd propagate --verify",
    "check": "codd check",
}


class StageError(RuntimeError):
    """A pipeline stage (or one of its units) failed."""


@dataclass
class StageOutcome:
    name: str
    status: str = STATUS_PENDING
    detail: str = ""
    units: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": self.name, "status": self.status, "detail": self.detail}
        if self.units:
            payload["units"] = dict(self.units)
        return payload


@dataclass
class GreenfieldResult:
    project_root: Path
    status: str  # "success" | "failed" | "dry-run"
    stages: list[StageOutcome]
    session_path: Path | None = None
    failed_stage: str | None = None
    failed_unit: str | None = None
    error: str | None = None

    @property
    def inspect_command(self) -> str | None:
        if self.failed_stage is None:
            return None
        template = _INSPECT_COMMANDS.get(self.failed_stage, "codd check")
        return template.replace("{unit}", str(self.failed_unit or "<unit>"))

    @property
    def resume_command(self) -> str:
        return RESUME_COMMAND

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "project_root": self.project_root.as_posix(),
            "status": self.status,
            "stages": [stage.to_dict() for stage in self.stages],
            "session_path": self.session_path.as_posix() if self.session_path else None,
        }
        if self.status == "failed":
            payload["failure"] = {
                "stage": self.failed_stage,
                "unit": self.failed_unit,
                "error": self.error,
                "inspect_command": self.inspect_command,
                "resume_command": self.resume_command,
            }
        return payload


def format_greenfield_result(result: GreenfieldResult, format_name: str) -> str:
    if format_name == "json":
        return json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str) + "\n"
    if format_name != "text":
        raise ValueError(f"unsupported greenfield format: {format_name}")
    lines = [f"Greenfield autopilot: {result.status.upper()} ({result.project_root.as_posix()})"]
    for stage in result.stages:
        detail = f" — {stage.detail}" if stage.detail else ""
        lines.append(f"  [{stage.status:>7}] {stage.name}{detail}")
        for unit, unit_status in stage.units.items():
            lines.append(f"            - {unit}: {unit_status}")
    if result.status == "failed":
        lines.append("")
        lines.append(f"Failed stage: {result.failed_stage}" + (f" (unit: {result.failed_unit})" if result.failed_unit else ""))
        if result.error:
            lines.append(f"Error: {result.error}")
        lines.append(f"Inspect: {result.inspect_command}")
        lines.append(f"Resume:  {result.resume_command}")
    if result.session_path is not None:
        lines.append(f"Session: {result.session_path.as_posix()}")
    return "\n".join(lines) + "\n"


# ═══════════════════════════════════════════════════════════
# Session checkpoint
# ═══════════════════════════════════════════════════════════

def session_path(project_root: Path) -> Path:
    return Path(project_root) / ".codd" / SESSION_FILENAME


def load_session(project_root: Path) -> dict[str, Any] | None:
    path = session_path(project_root)
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    return payload if isinstance(payload, dict) and payload.get("stages") else None


def save_session(project_root: Path, session: dict[str, Any]) -> Path:
    session["updated_at"] = _utc_now()
    path = session_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(session, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return path


def new_session(options: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": SESSION_VERSION,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "options": dict(options),
        "stages": {name: {"status": STATUS_PENDING, "detail": ""} for name in STAGES},
        "result": {"status": "running", "failed_stage": None, "failed_unit": None, "error": None},
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ═══════════════════════════════════════════════════════════
# Task references
# ═══════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ImplementTaskRef:
    """One implement unit: a task id plus how to run it.

    ``expected_outputs`` and ``test_kinds`` carry the DECLARED intent of the
    task (verbatim from the :class:`~codd.llm.plan_deriver.DerivedTask` it came
    from) so the runner can verify that the implementer actually produced the
    intended *kind* of artifact (e.g. a test-writing task that emitted only
    application code is a task-level false-GREEN). They are empty when the task
    came from a configured ``implement_targets`` mapping (which declares no
    V-model intent).
    """

    task_id: str
    design_node: str
    output_paths: tuple[str, ...] | None = None
    source: str = "configured"
    expected_outputs: tuple[str, ...] = ()
    test_kinds: tuple[str, ...] = ()


# DI seam signatures (all keyword-overridable on the pipeline constructor).
InitRunner = Callable[..., Any]
ElicitRunner = Callable[..., str]
PlanRunner = Callable[..., int]
WaveLister = Callable[[Path], list[int]]
GenerateWaveRunner = Callable[..., str]
TaskLister = Callable[[Path], list[ImplementTaskRef]]
TaskDeriver = Callable[..., int]
ImplementTaskRunner = Callable[..., str]
VerifyRunner = Callable[..., str]
CiScaffoldRunner = Callable[..., str]
PropagateRunner = Callable[..., str]
CheckRunner = Callable[[Path], str]
Notifier = Callable[[str, str], bool]
#: Injectable seam for the v2.77c stack-command materialization executor (a slot →
#: exit-code outcome). Default (None) uses the real subprocess executor in
#: ``codd.stack.command_plan``; tests inject a recording/sentinel executor so the
#: declared stack command slots are provably invoked without real Next.js/Playwright.
StackCommandExecutor = Callable[..., Any]


class GreenfieldPipeline:
    """Run the unattended greenfield autopilot (see module docstring).

    Like :class:`codd.brownfield.pipeline.BrownfieldPipeline`, every stage is
    a DI seam: pass a runner to replace the default implementation (which
    invokes the same code paths as the corresponding CLI command).
    """

    def __init__(
        self,
        *,
        project_name: str | None = None,
        language: str | None = None,
        requirements: str | Path | None = None,
        project_type: str | None = None,
        ai_command: str | None = None,
        elicit: bool | None = None,
        max_repair_attempts: int | None = None,
        coverage_gate: bool | None = None,
        propagate_commit: bool | None = None,
        ntfy_topic: str | None = None,
        chunk_size: int | None = None,
        timeout_per_chunk: int | None = None,
        init_runner: InitRunner | None = None,
        elicit_runner: ElicitRunner | None = None,
        plan_runner: PlanRunner | None = None,
        wave_lister: WaveLister | None = None,
        generate_wave_runner: GenerateWaveRunner | None = None,
        task_lister: TaskLister | None = None,
        task_deriver: TaskDeriver | None = None,
        implement_task_runner: ImplementTaskRunner | None = None,
        verify_runner: VerifyRunner | None = None,
        ci_scaffold_runner: CiScaffoldRunner | None = None,
        propagate_runner: PropagateRunner | None = None,
        check_runner: CheckRunner | None = None,
        notifier: Notifier | None = None,
        stack_command_executor: StackCommandExecutor | None = None,
        echo: Callable[[str], None] = print,
    ) -> None:
        self.project_name = project_name
        self.language = language
        self.requirements = str(requirements) if requirements is not None else None
        self.project_type = project_type
        self.ai_command = ai_command
        self._option_overrides = {
            "elicit": elicit,
            "max_repair_attempts": max_repair_attempts,
            "coverage_gate": coverage_gate,
            "propagate_commit": propagate_commit,
            "ntfy_topic": ntfy_topic,
            "chunk_size": chunk_size,
            "timeout_per_chunk": timeout_per_chunk,
        }
        self.init_runner = init_runner
        self.elicit_runner = elicit_runner
        self.plan_runner = plan_runner
        self.wave_lister = wave_lister
        self.generate_wave_runner = generate_wave_runner
        self.task_lister = task_lister
        self.task_deriver = task_deriver
        self.implement_task_runner = implement_task_runner
        self.verify_runner = verify_runner
        self.ci_scaffold_runner = ci_scaffold_runner
        self.propagate_runner = propagate_runner
        self.check_runner = check_runner
        self.notifier = notifier
        self.stack_command_executor = stack_command_executor
        self.echo = echo

    def _restore_session_options(self, session: dict[str, Any]) -> None:
        """Adopt persisted session options where this invocation set none."""
        stored = session.get("options") or {}
        if not isinstance(stored, dict):
            return
        if self.ai_command is None and stored.get("ai_command"):
            self.ai_command = str(stored["ai_command"])
        if self.project_name is None and stored.get("project_name"):
            self.project_name = str(stored["project_name"])
        if self.language is None and stored.get("language"):
            self.language = str(stored["language"])
        if self.requirements is None and stored.get("requirements"):
            self.requirements = str(stored["requirements"])
        if self.project_type is None and stored.get("project_type"):
            self.project_type = str(stored["project_type"])
        for key, value in self._option_overrides.items():
            if value is None and stored.get(key) is not None:
                self._option_overrides[key] = stored[key]

    # ── public entry ────────────────────────────────────────

    def run(
        self,
        target_path: Path | str,
        *,
        resume: bool = False,
        dry_run: bool = False,
    ) -> GreenfieldResult:
        project_root = _resolve_project_root(target_path)
        if dry_run:
            return self._dry_run(project_root)

        session = load_session(project_root) if resume else None
        if session is not None:
            # A resumed run must continue with the SAME options the original
            # run recorded — most critically ai_command: silently falling back
            # to the project-config default mid-pipeline switches the AI model
            # between stages (found in the 2026-06-11 real-AI dogfood, where a
            # --resume without --ai-cmd flipped sonnet to the opus default).
            # Explicit CLI overrides on the resume invocation still win.
            self._restore_session_options(session)
        options = self._resolve_options(project_root)
        if session is None:
            session = new_session(
                {
                    "project_name": self.project_name,
                    "language": self.language,
                    "requirements": self.requirements,
                    "project_type": self.project_type,
                    "ai_command": self.ai_command,
                    **options,
                }
            )
        session["result"] = {"status": "running", "failed_stage": None, "failed_unit": None, "error": None}
        self._session_ref = session

        self._notify(options, f"greenfield start: {project_root.name}")

        # Stack contract intake (Contract Kernel v2.77a) — bring the project's
        # declared framework-stack contract into the LIVE run (in the run record +
        # trace) so the framework layer is consumed by production, not only tests.
        # INTAKE ONLY: no obligation is enforced here and NO verdict changes (that
        # is v2.77b-e). A project with no ``stack:`` block is byte-identical (the
        # opt-in framework layer is unused). A declared-but-broken stack is an
        # HONEST failure (anti-false-green), never a silent skip. Done once, early,
        # before the stage loop, so the hash covers the whole run. Routed through
        # _fail (like every stage) so the autopilot REPORTS the honest error via a
        # failed GreenfieldResult instead of raising to the caller. Its own
        # pseudo-stage name (not a real STAGES entry) so the failure is attributed
        # honestly to intake, not to ``init``.
        # Whether THIS run is a genuine first generation (the project is being
        # created now) vs a re-run/resume of an already-progressed project. This is
        # the ONLY context in which the stack-lock gate may bootstrap a missing lock
        # (anti-gaming). It is read from the ON-DISK session INDEPENDENTLY of the
        # ``resume`` flag — because a re-run WITHOUT ``--resume`` builds a fresh
        # in-memory ``session`` (all-pending), which must NOT be mistaken for a first
        # generation on an already-built project (GPT-consult leak: "absence of
        # session ≠ first generation"). Fail-closed: any persisted completed stage ⇒
        # not first-gen, so a deleted lock on a built project is RED, never silently
        # re-bootstrapped to green.
        on_disk = load_session(project_root)
        first_generation = on_disk is None or not any(
            rec.get("status") in _COMPLETE_STATUSES
            for rec in on_disk.get("stages", {}).values()
        )

        try:
            contract = self._intake_stack_contract(project_root, session)
        except StageError as exc:
            return self._fail(
                project_root, session, options, "stack_intake",
                {"status": STATUS_PENDING, "detail": ""}, str(exc),
            )

        # Stack lock ENFORCEMENT (Contract Kernel v2.77b) — turn the already-live
        # stack contract into a red/green GATE. For stack-declared projects
        # (contract is not None): drift / missing-in-resume = RED (anti-false-green);
        # a first-generation missing lock is bootstrapped once (never overwritten —
        # anti-gaming). For projects with NO stack block: byte-identical UNLESS a
        # committed lock still exists (a removed declaration on a still-pinned
        # project = RED, closing the "drop stack: to dodge the gate" bypass).
        # Routed through _fail like intake so the autopilot reports it honestly.
        try:
            if contract is not None:
                self._enforce_stack_lock(project_root, session, contract, first_generation)
            else:
                from codd.stack.lock import orphan_stack_lock

                orphan = orphan_stack_lock(project_root)
                if orphan is not None and orphan.red:
                    self.echo(f"[greenfield] stack lock gate: {orphan.message}")
                    raise StageError(orphan.message)
        except StageError as exc:
            return self._fail(
                project_root, session, options, "stack_lock",
                {"status": STATUS_PENDING, "detail": ""}, str(exc),
            )

        # Stack command MATERIALIZATION (Contract Kernel v2.77c) — connect the
        # composed stack commands (and thus the stack obligations) to the run's
        # ACTUAL verify/build/test command plan. Two parts (both RED-routed through
        # _fail, like intake/lock):
        #   1. CONFLICT GATE — a composition conflict (command collision / unproved
        #      replace / weakened obligation / exclusive / deny) is RED. The composer
        #      already records these (it refuses last-wins); this makes that a gate.
        #   2. PLAN + EXECUTE — build a deterministic, contract-driven command plan
        #      from contract.commands (NO framework literal) and INVOKE each slot by
        #      exit code, so a declared framework_build/e2e_test is genuinely run (not
        #      silently skipped while the language verify greens alone — the false
        #      green this step closes). Exit-code ONLY here; command AUTHENTICITY
        #      (no-op/"build":"true"/observed-no-tests) is v2.77d and the obligation-
        #      checker gate is v2.77e — both deliberately out of lane.
        # Only for stack-declared projects (contract is not None) — a non-stack run
        # never reaches here, so it is byte-identical (no plan, no execution, no new
        # trace keys).
        try:
            if contract is not None:
                self._materialize_stack_commands(project_root, session, contract)
        except StageError as exc:
            return self._fail(
                project_root, session, options, "stack_commands",
                {"status": STATUS_PENDING, "detail": ""}, str(exc),
            )

        # Stack obligation CHECKER gate (Contract Kernel v2.77e) — turn the framework/
        # addon OBLIGATIONS into a red/green gate, AFTER materialization+authenticity. The
        # composed commands now run (v2.77c) and are authentic (v2.77d); this CHECKS the
        # declared obligations (the Next.js ignoreBuildErrors guard reds a build that would
        # pass with type errors; the Playwright e2e_actually_executed obligation reds a
        # 0-test run). Anti-false-green: a missing/disabled/faulting checker or an
        # unenforceable ERROR obligation is RED, never a silent pass. Uses the ALREADY-
        # RESOLVED contract (no re-resolution from disk — avoids a TOCTOU skip) and the
        # SAME current-run evidence the authenticity layer blessed. Only for stack-declared
        # projects (contract is not None) — a non-stack run is byte-identical (no gate).
        # Routed through _fail like intake/lock/materialization.
        try:
            if contract is not None:
                self._enforce_stack_obligations(project_root, session, contract)
        except StageError as exc:
            return self._fail(
                project_root, session, options, "stack_obligations",
                {"status": STATUS_PENDING, "detail": ""}, str(exc),
            )

        runners: dict[str, Callable[[Path, dict[str, Any], dict[str, Any]], None]] = {
            "init": self._stage_init,
            "elicit": self._stage_elicit,
            "plan": self._stage_plan,
            "generate": self._stage_generate,
            "implement": self._stage_implement,
            "verify": self._stage_verify,
            "ci_scaffold": self._stage_ci_scaffold,
            "propagate": self._stage_propagate,
            "check": self._stage_check,
        }

        for index, stage_name in enumerate(STAGES, start=1):
            record = session["stages"].setdefault(stage_name, {"status": STATUS_PENDING, "detail": ""})
            if record.get("status") in _COMPLETE_STATUSES:
                continue
            record["status"] = STATUS_PENDING
            record["started_at"] = _utc_now()
            save_session(project_root, session)
            try:
                runners[stage_name](project_root, record, options)
            except StageError as exc:
                return self._fail(project_root, session, options, stage_name, record, str(exc))
            except Exception as exc:  # noqa: BLE001 — autopilot must always checkpoint + report.
                return self._fail(project_root, session, options, stage_name, record, f"{type(exc).__name__}: {exc}")
            if record.get("status") not in _COMPLETE_STATUSES:
                record["status"] = STATUS_DONE
            record["finished_at"] = _utc_now()
            save_session(project_root, session)
            self.echo(f"[greenfield] stage {stage_name}: {record['status']} {record.get('detail', '')}".rstrip())
            self._notify(
                options,
                f"greenfield {project_root.name}: {stage_name} {record['status']} ({index}/{len(STAGES)})",
            )

        session["result"]["status"] = "success"
        path = save_session(project_root, session)
        self._notify(options, f"greenfield {project_root.name}: SUCCESS — system built")
        return GreenfieldResult(
            project_root=project_root,
            status="success",
            stages=_stage_outcomes(session),
            session_path=path,
        )

    # ── failure handling ────────────────────────────────────

    def _fail(
        self,
        project_root: Path,
        session: dict[str, Any],
        options: dict[str, Any],
        stage_name: str,
        record: dict[str, Any],
        error: str,
    ) -> GreenfieldResult:
        record["status"] = STATUS_FAILED
        record["detail"] = error
        record["finished_at"] = _utc_now()
        failed_unit = _first_failed_unit(record)
        session["result"] = {
            "status": "failed",
            "failed_stage": stage_name,
            "failed_unit": failed_unit,
            "error": error,
        }
        path = save_session(project_root, session)
        result = GreenfieldResult(
            project_root=project_root,
            status="failed",
            stages=_stage_outcomes(session),
            session_path=path,
            failed_stage=stage_name,
            failed_unit=failed_unit,
            error=error,
        )
        self.echo(f"[greenfield] stage {stage_name} FAILED: {error}")
        self.echo(f"[greenfield] inspect: {result.inspect_command}")
        self.echo(f"[greenfield] resume:  {result.resume_command}")
        self._notify(options, f"greenfield {project_root.name}: FAILED at {stage_name} — {error}")
        return result

    # ── stack contract intake (Contract Kernel v2.77a, intake only) ──

    def _intake_stack_contract(self, project_root: Path, session: dict[str, Any]):
        """Resolve the project's declared stack contract into the live run record + trace.

        See the call site in :meth:`run`. INTAKE ONLY — proves the framework-stack
        contract is LIVE-consumed (it enters the run record and the trace); it does
        NOT enforce any obligation and does NOT change any pass/fail verdict
        (obligation enforcement is v2.77b-e).

        * No ``stack:`` block → no-op: nothing is added to the record and only a
          single debug ``self.echo`` is emitted, so a non-stack project (the vast
          majority) is behaviour-preserving. Returns ``None``.
        * A declared stack → its :func:`stack_contract_trace` payload (incl.
          ``stack_contract_hash``) is written to ``session["stack_contract"]`` and
          echoed to the run trace. Returns the resolved ``ResolvedStackContract`` so
          the v2.77b lock-enforcement gate (run after intake) can pin it.
        * A declared-but-BROKEN stack → :class:`StageError` (honest error), never a
          silent skip (anti-false-green / no-silent-fallback).
        """
        from codd.stack.project import stack_contract_intake, stack_contract_trace

        try:
            contract = stack_contract_intake(project_root)
        except Exception as exc:  # noqa: BLE001 — a declared-but-broken stack must fail HONESTLY.
            raise StageError(
                "stack contract intake failed: the project declares a `stack:` block in "
                f"codd.yaml but it could not be resolved ({type(exc).__name__}: {exc}). "
                "A declared-but-unresolvable stack is an honest error, never a silent skip "
                "(check the language / frameworks / addons ids against the curated stack "
                "profiles, or remove the `stack:` block to opt out of the framework layer)."
            ) from exc

        if contract is None:
            # The opt-in framework layer is unused — byte-identical behaviour.
            self.echo("[greenfield] stack contract intake: no `stack:` block (framework layer opt-out)")
            return None

        trace = stack_contract_trace(contract)
        session["stack_contract"] = dict(trace)
        self.echo(
            "[greenfield] stack contract intake: "
            f"{trace['resolved_stack_id']} "
            f"stack_contract_hash={trace['stack_contract_hash']}"
        )
        return contract

    # ── stack lock enforcement (Contract Kernel v2.77b) ──

    def _enforce_stack_lock(self, project_root: Path, session: dict[str, Any], contract, first_generation: bool) -> None:
        """Enforce the project's stack lock as a red/green gate (v2.77b).

        Only called for stack-declared projects (``contract is not None``); non-stack
        projects never reach here, so they are byte-identical (no lock gate at all).

        Design B′ (split read-only gate vs. creation-path bootstrap):

        * genuine FIRST GENERATION (``first_generation`` True — no prior completed
          stage) → :func:`bootstrap_stack_lock` writes the first lock (exclusive
          create) and immediately enforces it read-only; ``generated`` is traced.
        * otherwise (RESUME / already-progressed project) → read-only
          :func:`enforce_stack_lock`: a missing lock is RED (an already-progressed
          stack project with no committed pin is unverifiable; a deleted lock cannot
          be silently regenerated), drift is RED, a valid lock is GREEN.

        Anti-gaming (exit gate 3): the read-only gate NEVER writes/refreshes a lock;
        ``bootstrap_stack_lock`` writes only when the lock is ABSENT (exclusive
        create) on this creation path, so a drift cannot be silenced by regenerating
        the lock. See :func:`enforce_stack_lock` / :func:`bootstrap_stack_lock`.
        """
        from codd.stack.lock import bootstrap_stack_lock, enforce_stack_lock

        if first_generation:
            gate = bootstrap_stack_lock(contract, project_root)
        else:
            gate = enforce_stack_lock(contract, project_root)
        # Record the lock verdict in the run trace (observable, like the intake hash).
        record = session.get("stack_contract")
        if isinstance(record, dict):
            record["stack_lock_status"] = gate.status
            record["stack_lock_path"] = gate.lock_path
        self.echo(f"[greenfield] stack lock gate: {gate.message}")
        if gate.red:
            raise StageError(gate.message)

    # ── stack command materialization (Contract Kernel v2.77c) ──

    def _materialize_stack_commands(self, project_root: Path, session: dict[str, Any], contract) -> None:
        """Conflict-gate + materialize the composed stack commands into the run plan (v2.77c).

        See the call site in :meth:`run`. Only invoked for stack-declared projects.

        1. CONFLICT GATE — :func:`assert_stack_contract_clean` (inside
           :func:`stack_command_plan`) raises :class:`StackContractConflictError` on
           ANY composition conflict (command collision / unproved replace / weakened
           obligation / exclusive / deny). That is RED — the composer already refuses
           a silent last-wins merge by recording a ``Conflict``; this makes it a gate.
        2. MATERIALIZE — build a deterministic, contract-driven command plan and
           INVOKE each composed slot by exit code (via the injectable
           ``stack_command_executor`` seam). A failing slot raises
           :class:`StackCommandMaterializationError`.
        3. AUTHENTICITY (v2.77d) — exit 0 is necessary but NOT sufficient: each slot
           must prove it did its job for its kind (a no-op / observed-no-tests /
           missing-or-unreadable-report command is RED even on exit 0), raising
           :class:`StackCommandAuthenticityError`. All three domain errors become a
           :class:`StageError` so the autopilot reports them honestly (the run does
           NOT advance to the stage loop with a conflicted/failing/inauthentic plan).

        The materialized plan + executed slot ids are recorded in the run trace
        (observable, like the intake hash + lock verdict) so "the declared stack
        command slots were invoked AND authentic" is provable. The obligation-checker
        gate (``verify_project_stack``) is v2.77e (out of lane here).
        """
        from codd.stack.command_authenticity import StackCommandAuthenticityError
        from codd.stack.command_plan import (
            StackCommandMaterializationError,
            StackContractConflictError,
            materialize_stack_command_plan,
        )

        try:
            plan, result = materialize_stack_command_plan(
                contract, project_root, executor=self.stack_command_executor
            )
        except (
            StackContractConflictError,
            StackCommandMaterializationError,
            StackCommandAuthenticityError,
        ) as exc:
            self.echo(f"[greenfield] stack command materialization: {exc}")
            raise StageError(str(exc)) from exc

        record = session.get("stack_contract")
        if isinstance(record, dict):
            record["stack_command_plan"] = plan.to_record()
            record["stack_commands_executed"] = list(result.executed_slot_ids)
        self.echo(
            "[greenfield] stack command materialization: "
            f"{len(plan.slots)} slot(s) invoked ({', '.join(plan.command_ids)})"
        )

    # ── stack obligation checker gate (Contract Kernel v2.77e) ──

    def _enforce_stack_obligations(self, project_root: Path, session: dict[str, Any], contract) -> None:
        """Run the composed stack obligation checkers as a red/green gate (v2.77e).

        See the call site in :meth:`run`. Only invoked for stack-declared projects
        (``contract is not None``); non-stack projects never reach here, so they are
        byte-identical (no obligation gate at all).

        Uses the ALREADY-RESOLVED ``contract`` (passed in from intake/lock/
        materialization) — NOT a re-resolution from disk — so a stack file that changes
        or is deleted after materialization cannot make this gate silently skip (a
        TOCTOU false-green). :func:`enforce_stack_obligation_gate` invokes every
        obligation's registered checker with ``project_root`` plus the current-run
        evidence (the SAME e2e report the authenticity layer blessed), and raises
        :class:`StackObligationGateError` unless every ERROR obligation was genuinely
        enforced AND satisfied: a blocking violation, an unenforced ERROR obligation
        (no registered checker — unverifiable), or a checker fault (raised / None /
        malformed return) is RED. Translated to :class:`StageError` so the autopilot
        reports it honestly via ``_fail``. The per-obligation verdict counts are
        recorded in the run trace (observable, like the lock verdict + command plan).
        """
        from codd.stack.project import StackObligationGateError, enforce_stack_obligation_gate

        try:
            result = enforce_stack_obligation_gate(contract, project_root)
        except StackObligationGateError as exc:
            self.echo(f"[greenfield] stack obligation gate: {exc}")
            raise StageError(str(exc)) from exc

        record = session.get("stack_contract")
        if isinstance(record, dict) and result is not None:
            record["stack_obligations_checked"] = len(contract.obligations)
            record["stack_obligations_unenforced"] = [o.id for o in result.unenforced]
        self.echo(
            "[greenfield] stack obligation gate: "
            f"{len(contract.obligations)} obligation(s) checked — all enforced obligations satisfied"
        )

    # ── option resolution ───────────────────────────────────

    def _resolve_options(self, project_root: Path) -> dict[str, Any]:
        """Explicit constructor option > codd.yaml ``greenfield:`` > built-in default."""
        config_section: Mapping[str, Any] = {}
        try:
            from codd.config import load_project_config

            section = load_project_config(project_root).get("greenfield")
            if isinstance(section, Mapping):
                config_section = section
        except (FileNotFoundError, ValueError):
            config_section = {}

        resolved: dict[str, Any] = {}
        for key, default in DEFAULT_OPTIONS.items():
            override = self._option_overrides.get(key)
            if override is not None:
                resolved[key] = override
            elif key in config_section and config_section[key] is not None:
                resolved[key] = deepcopy(config_section[key])
            else:
                resolved[key] = deepcopy(default)
        return resolved

    # ── notifications (notify-only, never blocking) ─────────

    def _notify(self, options: dict[str, Any], message: str) -> None:
        topic = str(options.get("ntfy_topic") or "").strip()
        if not topic:
            return
        notifier = self.notifier or _default_notifier
        try:
            notifier(topic, message)
        except Exception:  # noqa: BLE001 — notifications must never block or fail the build.
            pass

    # ── stage: init ─────────────────────────────────────────

    def _stage_init(self, project_root: Path, record: dict[str, Any], options: dict[str, Any]) -> None:
        from codd.config import find_codd_dir

        existing = find_codd_dir(project_root)
        if existing is not None:
            record["status"] = STATUS_SKIPPED
            record["detail"] = f"CoDD config dir already exists: {existing.name}/"
            if self.project_type:
                # Record the project type on an already-initialized project so
                # capability resolution applies to every downstream stage.
                import codd.cli as cli_module

                cli_module._record_project_type(project_root, existing, self.project_type)
                record["detail"] += f" (project_type {self.project_type})"
            if self.requirements:
                self._run_init(project_root)  # import requirements into the existing project
                record["detail"] += " (requirements imported)"
            return
        if not self.project_name or not self.language:
            raise StageError(
                "project is not initialized and --project-name/--language were not provided; "
                "pass both (plus --requirements) or run codd init first"
            )
        self._run_init(project_root)
        record["detail"] = f"initialized {self.project_name} ({self.language})"

    def _run_init(self, project_root: Path) -> None:
        if self.init_runner is not None:
            self.init_runner(
                project_root,
                name=self.project_name,
                language=self.language,
                requirements=self.requirements,
                project_type=self.project_type,
            )
            return
        import codd.cli as cli_module

        try:
            cli_module.init.callback(
                name=self.project_name,
                project_name=None,
                language=self.language,
                dest=str(project_root),
                requirements=self.requirements,
                config_dir="codd",
                project_type=self.project_type,
                suggest_lexicons=False,
                llm_enhanced=False,
                auto_approve=True,
            )
        except SystemExit as exc:
            raise StageError(f"codd init failed (exit {exc.code})") from exc

    # ── stage: elicit (advisory, never blocking) ────────────

    def _stage_elicit(self, project_root: Path, record: dict[str, Any], options: dict[str, Any]) -> None:
        if not options.get("elicit", True):
            record["status"] = STATUS_SKIPPED
            record["detail"] = "disabled (--no-elicit / greenfield.elicit: false)"
            return
        try:
            runner = self.elicit_runner or _default_elicit_runner
            record["detail"] = str(runner(project_root, ai_command=self.ai_command))
        except Exception as exc:  # noqa: BLE001 — elicit is advisory and must never block the autopilot.
            record["status"] = STATUS_WARNING
            record["detail"] = f"elicit skipped (non-blocking): {exc}"

    # ── stage: plan ─────────────────────────────────────────

    def _stage_plan(self, project_root: Path, record: dict[str, Any], options: dict[str, Any]) -> None:
        runner = self.plan_runner or _default_plan_runner
        try:
            wave_count = int(runner(project_root, ai_command=self.ai_command, force=True))
        except (FileNotFoundError, ValueError) as exc:
            raise StageError(f"plan --init failed: {exc}") from exc
        record["detail"] = f"{wave_count} wave(s)"
        record["waves"] = wave_count
        self._enforce_greenfield_vb_plan_contract(project_root, options)

    def _enforce_greenfield_vb_plan_contract(
        self, project_root: Path, options: dict[str, Any]
    ) -> None:
        """Backstop: a greenfield run with the coverage gate ON must PLAN the canonical
        VB registry doc (``test:test-strategy`` / ``docs/test/test_strategy.md``).

        ``planner._ensure_canonical_vb_doc_planned`` force-injects it on the normal path;
        this catches a custom ``plan_runner``, a hand-written wave_config, or any future
        path that bypasses the injection. Deliberately NOT gated on
        ``project_expects_vb_registry`` — that returns False in exactly the dangerous case
        (AI omitted the canonical doc, no ``test_coverage.docs``, no file yet), which is the
        hole this closes. Without the canonical doc the VB audit has zero declarations and
        coverage trivially passes 0/0 (false-GREEN). Skipped when the coverage gate is
        explicitly OFF (owner opt-out) or an explicit ``test_coverage.docs`` is pinned."""

        from codd.config import load_project_config
        from codd.verifiable_behavior_audit import (
            coverage_gate_enabled,
            load_verifiable_behaviors,
            wave_config_plans_canonical_vb_doc,
        )

        if not bool(options.get("coverage_gate", True)):
            return
        try:
            config = load_project_config(project_root)
        except (FileNotFoundError, ValueError):
            config = {}
        if not coverage_gate_enabled(config):
            return
        section = config.get("test_coverage")
        if isinstance(section, dict) and section.get("docs"):
            return  # owner pinned an explicit VB doc — respect it
        if wave_config_plans_canonical_vb_doc(config):
            return  # the canonical VB doc is planned — SSOT will exist
        # Not planned — but VBs may ALREADY be declared in a discoverable docs/test
        # doc. The contract is "a VB SSOT must exist", satisfied by EITHER a planned
        # canonical doc OR existing declarations. Only RED when NEITHER holds (e.g. a
        # real greenfield at plan stage with no docs generated and no canonical planned).
        try:
            behaviors = load_verifiable_behaviors(project_root, config=config)
        except Exception:  # noqa: BLE001 — discovery failure ⇒ fall through to the RED below.
            behaviors = []
        if behaviors:
            return  # a VB surface already exists
        raise StageError(
            "greenfield coverage gate requires a verifiable-behavior SSOT: plan the canonical "
            "VB registry document (test:test-strategy / docs/test/test_strategy.md) in wave_config "
            "or declare VBs in a docs/test doc. Without it the declared-VB set is empty and the "
            "coverage/authenticity gate can pass WITHOUT certifying any behavior (a false-green). "
            "Disable the coverage gate explicitly if this build has no behaviors to verify."
        )

    # ── stage: generate ─────────────────────────────────────

    def _stage_generate(self, project_root: Path, record: dict[str, Any], options: dict[str, Any]) -> None:
        lister = self.wave_lister or _default_wave_lister
        waves = list(lister(project_root))
        if not waves:
            raise StageError("wave_config is empty; run codd plan --init")
        units: dict[str, str] = record.get("units") or {}
        record["units"] = {str(wave): units.get(str(wave), STATUS_PENDING) for wave in waves}

        runner = self.generate_wave_runner or _default_generate_wave_runner
        for wave in waves:
            key = str(wave)
            if record["units"][key] == STATUS_DONE:
                continue
            try:
                detail = runner(project_root, wave, ai_command=self.ai_command)
            except (FileNotFoundError, ValueError) as exc:
                record["units"][key] = STATUS_FAILED
                raise StageError(f"wave {wave}: {exc}") from exc
            record["units"][key] = STATUS_DONE
            self._checkpoint(project_root)
            self.echo(f"[greenfield] generate wave {wave}: {detail}")

        # After ALL waves are generated, enforce the canonical VB-registry
        # completeness contract (model-independence): a weak model can reference
        # VB ids in acceptance-criteria tables yet declare none in the canonical
        # registry, which honest-REDs only later at implement with a confusing
        # orphan message. Catch it HERE and drive a bounded, canonical-doc-scoped
        # repair so a coherent registry exists before implement — or fail
        # honestly at generate. Runs BEFORE implement by construction.
        self._enforce_generate_vb_registry_gate(project_root, options)
        record["detail"] = f"{len(waves)} wave(s) generated"

    def _enforce_generate_vb_registry_gate(
        self, project_root: Path, options: dict[str, Any]
    ) -> None:
        """Generate-time canonical VB-registry completeness gate + bounded repair.

        Adopts the GPT design (B as contract + A as bounded generate repair). On
        error-level :func:`validate_vb_registry_completeness` issues (empty
        canonical registry / unresolved AC references / missing canonical doc),
        re-invoke generation SCOPED TO the canonical VB doc only
        (:func:`codd.generator.regenerate_artifact`) with repair feedback, then
        re-validate. After the bounded attempts (1–2), a still-failing registry
        raises :class:`StageError` (generate honest-RED).

        The MODEL writes the declarations — this is NOT deterministic auto-derive.
        Acceptance-criteria references are used ONLY as the candidate list in the
        repair feedback, NEVER auto-inserted into the canonical table (rejecting
        design option C). Skipped for a project with no verifiable-behavior
        surface (:func:`project_expects_vb_registry`) and when the owner turned
        the coverage gate off — the registry contract tracks the coverage gate.
        """
        from codd.config import load_project_config
        from codd.verifiable_behavior_audit import (
            _CANONICAL_VB_OUTPUT_PATH,
            coverage_gate_enabled,
            project_expects_vb_registry,
            validate_vb_registry_completeness,
        )

        if not bool(options.get("coverage_gate", True)):
            return
        try:
            config = load_project_config(project_root)
        except (FileNotFoundError, ValueError):
            config = {}
        if not coverage_gate_enabled(config):
            return
        if not project_expects_vb_registry(project_root, config):
            # No VB surface planned/declared — nothing to certify. Mirrors the
            # brownfield "nothing to audit -> pass" rule; a minimal greenfield
            # project with only design docs is not forced to invent behaviors.
            return

        max_attempts = max(1, int(self._option_overrides.get("max_repair_attempts") or 0) or 2)
        max_attempts = min(max_attempts, 2)  # bounded: 1–2 attempts (design)

        # Reference the seam through the module (not a bound name) so a test's
        # monkeypatch on ``codd.generator.regenerate_artifact`` is honored.
        from codd import generator as generator_module

        issues = validate_vb_registry_completeness(project_root, config, strict=True)
        errors = [issue for issue in issues if issue.severity == "error"]
        attempt = 0
        while errors and attempt < max_attempts:
            attempt += 1
            feedback = self._build_vb_registry_repair_feedback(project_root, config, errors)
            self.echo(
                f"[greenfield] generate VB-registry gate: {len(errors)} issue(s); "
                f"re-generating {_CANONICAL_VB_OUTPUT_PATH} with repair feedback "
                f"(attempt {attempt}/{max_attempts})"
            )
            try:
                generator_module.regenerate_artifact(
                    project_root,
                    output_path=_CANONICAL_VB_OUTPUT_PATH,
                    feedback=feedback,
                    ai_command=self.ai_command,
                )
            except (FileNotFoundError, ValueError) as exc:
                # The canonical doc is not a planned artifact, or regeneration
                # failed — cannot repair. Fail honestly with the original gap.
                raise StageError(
                    f"generate VB-registry gate: canonical registry repair could not run "
                    f"({exc}); the canonical VB document ({_CANONICAL_VB_OUTPUT_PATH}) must "
                    "declare every verifiable behavior as a first-column VB-* row."
                ) from exc
            issues = validate_vb_registry_completeness(project_root, config, strict=True)
            errors = [issue for issue in issues if issue.severity == "error"]

        if errors:
            detail = "\n".join(f"  - {issue.message}" for issue in errors)
            raise StageError(
                "generate VB-registry gate FAILED: the generated canonical verifiable-behavior "
                f"registry ({_CANONICAL_VB_OUTPUT_PATH}) is still incomplete after "
                f"{attempt} repair attempt(s):\n{detail}\n"
                "Declare every real verifiable behavior exactly once as a first-column VB-* row "
                "in the canonical doc so coverage/authenticity can certify the system."
            )
        if attempt:
            self.echo(
                f"[greenfield] generate VB-registry gate: canonical registry repaired "
                f"after {attempt} attempt(s) — OK"
            )

    def _build_vb_registry_repair_feedback(
        self, project_root: Path, config: dict[str, Any], errors: list[Any]
    ) -> str:
        """Repair feedback for the canonical-doc-scoped regeneration.

        Lists the canonical-registry declaration count + the unresolved AC
        references (the CANDIDATE id list, never auto-inserted) + the repair
        rules in the shape the GPT design specified. The references are a hint
        about which behaviors likely exist — the model decides which correspond
        to real behaviors and declares them; nothing is derived deterministically.
        """
        from codd.verifiable_behavior_audit import (
            _CANONICAL_VB_OUTPUT_PATH,
            _config_without_doc_pin,
            _iter_doc_texts,
            _normalize_vb_id,
            is_canonical_vb_doc,
            parse_vb_references,
            parse_vb_table,
        )

        # Scan the WHOLE test-doc tree (not just test_coverage.docs) so the
        # candidate-reference list mirrors what validate_vb_registry_completeness
        # saw (an AC doc that references VB ids is not in test_coverage.docs).
        doc_texts = _iter_doc_texts(project_root, config=_config_without_doc_pin(config))
        canonical_count = 0
        declared: set[str] = set()
        for doc, text in doc_texts:
            if not is_canonical_vb_doc(output_path=doc):
                continue
            rows = parse_vb_table(text, source_doc=doc)
            canonical_count += len(rows)
            declared.update(_normalize_vb_id(row.vb_id) for row in rows)

        # Collect unresolved references (id -> the docs that reference it) as the
        # CANDIDATE list only — never auto-inserted into the canonical table.
        candidates: dict[str, set[str]] = {}
        for doc, text in doc_texts:
            if is_canonical_vb_doc(output_path=doc):
                continue
            for ref in parse_vb_references(text, source_doc=doc):
                if _normalize_vb_id(ref.vb_id) in declared:
                    continue
                candidates.setdefault(ref.vb_id, set()).add(doc)

        lines = [
            "The generated verifiable-behavior (VB) registry is incomplete.",
            "",
            "Canonical registry:",
            f"- {_CANONICAL_VB_OUTPUT_PATH} declares {canonical_count} canonical VB-* row(s).",
        ]
        if candidates:
            lines.append("")
            lines.append("Unresolved VB references (candidate ids — NOT yet declared canonically):")
            for vb_id in sorted(candidates):
                docs = ", ".join(sorted(candidates[vb_id]))
                lines.append(f"- {vb_id} (referenced in {docs})")
        lines.extend(
            [
                "",
                "Repair rules:",
                f"- Rewrite ONLY {_CANONICAL_VB_OUTPUT_PATH}.",
                "- Declare every real verifiable behavior exactly once as a first-column VB-* row.",
                "- Use a referenced id only when it corresponds to a real behavior derived from "
                "the requirements/design; fix a reference elsewhere only if its id is genuinely wrong.",
                "- Do NOT add or modify `codd: covers` markers.",
                "- Do NOT edit source or executable tests.",
            ]
        )
        return "\n".join(lines)

    # ── stage: implement ────────────────────────────────────

    def _stage_implement(self, project_root: Path, record: dict[str, Any], options: dict[str, Any]) -> None:
        lister = self.task_lister or _default_task_lister
        tasks = list(lister(project_root))
        if not tasks:
            deriver = self.task_deriver or _default_task_deriver
            derived = int(deriver(project_root, ai_command=self.ai_command))
            self.echo(f"[greenfield] implement: derived and auto-approved {derived} task(s) from design docs")
            tasks = list(lister(project_root))
        if not tasks:
            raise StageError(
                "no implement tasks found: declare implement.default_output_paths in codd.yaml "
                "or check that design documents support task derivation (codd plan derive)"
            )

        units: dict[str, str] = record.get("units") or {}
        record["units"] = {task.task_id: units.get(task.task_id, STATUS_PENDING) for task in tasks}

        # Scaffold the harness-owned stack TOPOLOGY before the per-task loop runs
        # any task gate / build-oracle. The per-task runner's contract check
        # (_verify_task_contract) and any compiler-class build run task-by-task,
        # INSIDE the loop below; for a MANIFEST-DRIVEN stack the build resolves its
        # compile surface from a harness-owned manifest the AI never authors (a C#
        # ``src/<Pkg>/<Pkg>.csproj`` + ``<Pkg>.sln`` — the SDK compiles ONLY what a
        # project file's implicit glob captures). Until now scaffold_layout ran only
        # at verify and implement-END, so the FIRST task's build saw NO manifest and
        # compiled zero files — an honest-looking RED with no real defect. Running
        # the scaffold here puts that topology on disk BEFORE the first build. It is
        # PROFILE-DRIVEN (dispatched through the layout-profile / scaffolder-id
        # registry — never a language-name branch) so it is the SAME idempotent,
        # non-clobbering, create-only call verify and the implement-end finalizers
        # already use: a strict no-op for a stack with no scaffolder (and for a
        # legacy Python/TS stack it merely materializes the same topology earlier —
        # a SUT/AI-authored file is preserved byte-for-byte, and verify's later
        # re-scaffold is a no-op). Advisory (self-swallowing); the verify honesty +
        # coherence gates remain the authorities on whether the build is certifiable.
        self._ensure_test_runner(project_root)

        runner = self.implement_task_runner or self._default_implement_task_runner
        for task in tasks:
            if record["units"][task.task_id] == STATUS_DONE:
                continue
            try:
                detail = runner(
                    project_root,
                    task,
                    ai_command=self.ai_command,
                    coverage_gate=bool(options.get("coverage_gate", True)),
                    chunk_size=options.get("chunk_size"),
                    timeout_per_chunk=int(options.get("timeout_per_chunk") or 600),
                )
            except StageError:
                record["units"][task.task_id] = STATUS_FAILED
                raise
            except (FileNotFoundError, ValueError) as exc:
                record["units"][task.task_id] = STATUS_FAILED
                raise StageError(f"task {task.task_id}: {exc}") from exc
            record["units"][task.task_id] = STATUS_DONE
            self._checkpoint(project_root)
            self.echo(f"[greenfield] implement {task.task_id}: {detail}")

        # Manifest↔lock coherence finalization — ONCE, at implement-end, AFTER the
        # SUT has finished authoring package.json and BEFORE any frozen install
        # (the implement-oracle's npm ci below, and verify's npm ci later). The SUT
        # may have written an OLD test-toolchain dep (``"vitest": "^1.6.0"``) while
        # the scaffold/gate install produced a lock with the LATEST resolution; a
        # frozen ``npm ci`` then hard-fails on the lock↔manifest mismatch. This
        # step RECONCILES the harness-owned toolchain dep versions back to the
        # profile (vitest/typescript/@types/node are the VERIFIER's tooling, not
        # the app's deps) and REFRESHES the lock (``npm install
        # --package-lock-only``) so the frozen install passes HONESTLY. It runs
        # BEFORE the implement-oracle so the oracle's own ``npm ci`` benefits from
        # the coherent lock too. A strict NO-OP for stacks with no toolchain
        # profile (Python today). verify's install stays FROZEN — see
        # codd.dependency_lock_coherence.
        self._finalize_dependency_lock_coherence(project_root)

        # Implement-time native-oracle gate — ONCE, after every unit is generated
        # and BEFORE the run advances to verify, while the SUT can still freely
        # edit ALL files (source AND tests). For a compiler-class stack (TS=tsc
        # --noEmit) this proves cross-artifact symbol/module coherence statically
        # — the src↔src and test↔helper mismatches (TS2305/2724/2459) that verify
        # catches TOO LATE (where auto-repair is scope-blocked from rewriting test
        # files). STAGE-level for the same forward-reference reason the VB gate is
        # (a per-unit tsc would false-fail on an import of a not-yet-generated
        # unit). On failure it normalizes diagnostics to evidence categories and
        # re-runs implementation with that feedback (bounded), so implement does
        # not "succeed" until the oracle passes — or fails HONESTLY. A NO-OP for
        # stacks with no declared oracle (Python today): see
        # codd.project_types.ImplementOracleSpec. Runs BEFORE the VB coverage gate
        # — code that does not even typecheck cannot have meaningful VB coverage.
        self._enforce_implement_oracle_gate(project_root, tasks, options)

        # Project-wide VB coverage + marker-authenticity gate — ONCE, after every
        # implement task has run and all covering tests therefore exist. Per-task
        # enforcement was disabled in _default_implement_task_runner precisely
        # because the gate is project-wide; this is where it belongs. Honors the
        # greenfield coverage_gate option (--no-coverage-gate /
        # greenfield.coverage_gate: false) — when the owner turned it off, the
        # final gate is skipped too. The rerun is wired (was dormant): an
        # uncovered VB drives a bounded, TEST-SCOPED re-implementation (source is
        # never edited by a VB rerun) with gap feedback, and the native oracle is
        # re-asserted after each test edit.
        coverage_on = bool(options.get("coverage_gate", True))
        _enforce_stage_coverage_gate(
            project_root,
            coverage_gate=coverage_on,
            echo=self.echo,
            rerun=self._make_vb_rerun_callback(project_root, tasks, options) if coverage_on else None,
            rerun_oracle=(
                (lambda: self._enforce_implement_oracle_gate(project_root, tasks, options))
                if coverage_on
                else None
            ),
            scope_resolver=self._make_vb_scope_resolver(project_root, tasks) if coverage_on else None,
            authenticity_profile=self._resolve_layout_profile(project_root) if coverage_on else None,
        )
        record["detail"] = f"{len(tasks)} task(s) implemented"

    def _finalize_dependency_lock_coherence(self, project_root: Path) -> None:
        """Reconcile harness-owned toolchain deps + refresh the lock (implement-end).

        See the call site in :meth:`_stage_implement`. Ensures the stack topology
        is scaffolded first (idempotent — the same ``_ensure_test_runner`` verify
        uses) so a ``package.json`` exists to reconcile, then runs the profile-
        driven finalization (:func:`finalize_dependency_lock_coherence`): reconcile
        the manifest's harness-owned toolchain dep versions to the profile, refresh
        the lock (``npm install --package-lock-only``), and materialize
        node_modules with the FROZEN ``npm ci`` so a same-process implement-oracle
        typecheck has its deps. A hard finalization failure (a lock refresh /
        materialize that exits non-zero or times out) is an honest
        ``environment_build_error`` raised as a :class:`StageError`. A strict
        NO-OP for a stack with no toolchain profile (Python today).
        """
        from codd.dependency_lock_coherence import (
            finalize_dependency_lock_coherence,
            resolve_toolchain_profile,
        )

        config, language, source_dirs, test_dirs = self._layout_inputs(project_root)
        project_name = self._layout_project_name(project_root, config)

        # Cheap NO-OP short-circuit: a stack with no toolchain profile (Python
        # today) needs no scaffold/echo — skip silently.
        if resolve_toolchain_profile(
            project_root,
            language=language,
            project_name=project_name,
            source_dirs=source_dirs,
            test_dirs=test_dirs,
            config=config,
        ) is None:
            return

        # The finalization reconciles the SUT's package.json; make sure the
        # scaffolded manifest (and its toolchain-script wiring) is present NOW, at
        # implement-end. Idempotent + non-clobbering, so verify's re-scaffold is a
        # no-op and a SUT-authored package.json is preserved (only its harness-
        # owned toolchain dep VERSIONS are reconciled, by the step below).
        self._ensure_test_runner(project_root)

        result = finalize_dependency_lock_coherence(
            project_root,
            language=language,
            project_name=project_name,
            source_dirs=source_dirs,
            test_dirs=test_dirs,
            config=config,
            echo=self.echo,
        )
        if not result.ok:
            raise StageError(
                "manifest↔lock coherence finalization failed at implement-end: "
                f"{result.detail}. This is an environment/toolchain failure (the "
                "lock could not be refreshed/materialized to match the reconciled "
                "manifest), not a code defect."
            )

    def _ensure_lock_freshness(self, project_root: Path) -> None:
        """Run the verify-time lock-freshness barrier (see the call in _stage_verify).

        Enforces "no frozen install runs unless the lock is fresh for the current
        manifest set" BEFORE verify's frozen installs. Ensures the scaffold is
        present (idempotent — same ``_ensure_test_runner`` verify uses) so a
        ``package.json`` exists to digest/reconcile, then runs the profile-driven
        barrier (:func:`ensure_lock_freshness_barrier`): it is a NO-OP when the
        manifest set is unchanged since the last freeze (verify's own ``npm ci``
        reproduces the lock), and re-reconciles + re-refreshes + re-validates (with
        the completeness fallback) only when a post-implement-end rerun changed the
        manifest (the observed dogfood gap). A hard barrier failure (the lock cannot satisfy
        a frozen install even after the fallback) is an honest
        ``environment_build_error`` raised as a :class:`StageError`. A strict NO-OP
        for a stack with no toolchain profile (Python today).
        """
        from codd.dependency_lock_coherence import (
            ensure_lock_freshness_barrier,
            resolve_toolchain_profile,
        )

        config, language, source_dirs, test_dirs = self._layout_inputs(project_root)
        project_name = self._layout_project_name(project_root, config)

        # Cheap NO-OP short-circuit: a stack with no toolchain profile (Python
        # today) needs no scaffold/echo.
        if resolve_toolchain_profile(
            project_root,
            language=language,
            project_name=project_name,
            source_dirs=source_dirs,
            test_dirs=test_dirs,
            config=config,
        ) is None:
            return

        # The barrier digests + reconciles the SUT's package.json; ensure the
        # scaffolded manifest is present (idempotent, non-clobbering — verify's own
        # _ensure_test_runner already ran just above, so this is a no-op in the
        # normal flow and a safety net for DI/standalone callers).
        self._ensure_test_runner(project_root)

        result = ensure_lock_freshness_barrier(
            project_root,
            language=language,
            project_name=project_name,
            source_dirs=source_dirs,
            test_dirs=test_dirs,
            config=config,
            echo=self.echo,
        )
        if not result.ok:
            raise StageError(
                "lock-freshness barrier failed before verify: "
                f"{result.detail}. This is an environment/toolchain failure (the "
                "lock could not be made to satisfy a frozen install for the current "
                "manifest set), not a code defect."
            )

    def _enforce_implement_oracle_gate(
        self,
        project_root: Path,
        tasks: list[ImplementTaskRef],
        options: dict[str, Any],
    ) -> None:
        """Run the implement-time native-oracle gate (see _stage_implement).

        Ensures the stack topology is scaffolded first (idempotent — the same
        ``_ensure_test_runner`` verify uses), so the oracle's config (tsconfig)
        exists to certify and run against AT implement-time. Then runs the gate
        with a SCOPED ``rerun(feedback, scope)`` callback: on an oracle failure
        the gate derives the both-ends-of-the-broken-edge scope and re-implements
        ONLY the owning tasks (under a write-fence), escalating
        narrow→expanded→broad only when the diagnostic signature fails to move
        (see ``codd.implement_oracle_scope`` + ``run_implement_oracle_gate``).
        ``scope is None`` ⇒ the broad fallback (re-implement every task — the
        legacy shape the VB coverage gate uses). A non-passing final result is a
        StageError; an uncertifiable oracle scope (OracleScopeError) propagates
        as a hard failure. The whole gate is a NO-OP for a stack without a
        declared implement-time oracle.
        """
        from codd.implement_oracle import (
            ORACLE_STATE_UNSUPPORTED_EXPLICIT,
            OracleScopeError,
            classify_implement_oracle_state,
            resolve_implement_oracle,
            run_implement_oracle_gate,
        )

        config, language, source_dirs, test_dirs = self._layout_inputs(project_root)
        project_name = self._layout_project_name(project_root, config)

        # NO-OP short-circuit — but the 4-state model decides WHICH "no oracle" this is
        # (Contract Kernel oracle dispatch §9). ``_stage_implement`` is strict by
        # construction: it GENERATED the code and must PROVE it. So a DECLARED-but-
        # UNSUPPORTED stack must NOT be silently skipped here — that is the very false-
        # green §9 closes. Only a NON-DECLARED stack (LEGACY_ABSENT) or an explicit
        # opt-out (OPT_OUT) is genuinely skippable (the gate's visible trace covers the
        # observability; here the short-circuit avoids needless scaffolding).
        resolved = resolve_implement_oracle(
            project_root,
            language=language,
            project_name=project_name,
            source_dirs=source_dirs,
            test_dirs=test_dirs,
            config=config,
        )
        if resolved is None:
            state = classify_implement_oracle_state(language, config, resolved=resolved)
            if state == ORACLE_STATE_UNSUPPORTED_EXPLICIT:
                # A declared stack CoDD cannot prove (no registered oracle adapter) —
                # the strict greenfield implement stage RED. Mirror the gate's verdict
                # as a StageError instead of silently advancing to verify (where the
                # incoherence would surface later, or not at all).
                raise StageError(
                    f"implement-time native-oracle gate: language {language!r} is declared "
                    "but UNSUPPORTED — no registered implement-oracle adapter, so the "
                    "generated code's cross-artifact coherence is UNPROVEN. A declared-but-"
                    "unsupported stack is RED, never a silent pass (add a LanguageProfile + "
                    "oracle adapter for this stack, or opt out via implement.implement_oracle:"
                    " false)."
                )
            # LEGACY_ABSENT / OPT_OUT — genuinely skippable (visible-trace cases). Run the
            # gate once anyway so its (now VISIBLE) NO-OP trace is emitted, then return —
            # cheap, and it keeps the observability promise (§9: NO-OP is never silent).
            run_implement_oracle_gate(
                project_root,
                language=language,
                project_name=project_name,
                source_dirs=source_dirs,
                test_dirs=test_dirs,
                config=config,
                echo=self.echo,
            )
            return
        # A supported oracle resolved (resolved is not None) — fall through to
        # certify + run it below.

        # The oracle needs the scaffolded config (tsconfig) present NOW, at
        # implement-time — verify's scaffold runs later. Idempotent + non-
        # clobbering, so verify's re-scaffold is a no-op.
        self._ensure_test_runner(project_root)

        # The path→owning-task index for the SCOPED rerun: declared task outputs
        # UNION the config-derived output paths (incl. the permissive
        # ``_output_paths_for_task`` fallback so the index knows where to LOOK).
        config_output_paths = self._resolve_oracle_config_output_paths(tasks, config)
        # HARD owner-uniqueness gate (contract artifact.owner.unique.v1): runs
        # BEFORE the index build (and OUTSIDE its best-effort except) so an
        # ambiguous-ownership topology honest-fails deterministically rather than
        # letting the index's first-owner-wins setdefault silently pick a winner.
        # It reasons over EXCLUSIVE ownership CLAIMS only — declared task outputs
        # (read internally) + config-DECLARED default_output_paths — NOT the
        # permissive fallback the index uses. Passing the fallback false-RED's a
        # NORMAL Python src-layout (source_root nests package_root) whenever ≥2
        # tasks with no declared output fall to ``_route_source_into_package``'s
        # ``src`` + ``src/<pkg>`` accept-list (a "may write here", not an exclusive
        # claim). See ``_resolve_owner_uniqueness_config_paths``.
        self._certify_output_owner_uniqueness(
            tasks, self._resolve_owner_uniqueness_config_paths(tasks, config)
        )
        scope_index = self._build_oracle_scope_index(
            project_root, tasks, config, config_output_paths=config_output_paths
        )
        manifest_paths = self._oracle_manifest_paths(project_root)

        def _rerun(feedback: str, scope: Any = None) -> None:
            self._rerun_tasks_with_feedback(project_root, tasks, feedback, options, scope=scope)

        try:
            result = run_implement_oracle_gate(
                project_root,
                language=language,
                project_name=project_name,
                source_dirs=source_dirs,
                test_dirs=test_dirs,
                config=config,
                rerun=_rerun,
                echo=self.echo,
                scope_index=scope_index,
                manifest_paths=manifest_paths,
            )
        except OracleScopeError as exc:
            raise StageError(str(exc)) from exc

        if not result.passed:
            paths = f" (files: {', '.join(result.failed_paths)})" if result.failed_paths else ""
            raise StageError(
                f"implement-time native-oracle gate failed: the generated code does not "
                f"typecheck — independently-generated artifacts disagree on the "
                f"symbols/modules they import{paths}. Evidence categories: "
                f"{result.category_counts()}. {result.detail}"
            )

    def _resolve_oracle_config_output_paths(
        self,
        tasks: list[ImplementTaskRef],
        config: dict[str, Any],
    ) -> dict[str, list[str]]:
        """The config-derived output paths per task (the owner-index input).

        Mirrors the resolution the rerun + owner-index use: a task's DECLARED
        ``output_paths`` when present, else ``_output_paths_for_task`` (the same
        config routing the rerun itself applies). Best-effort per task.
        """
        config_output_paths: dict[str, list[str]] = {}
        for task in tasks:
            try:
                config_output_paths[task.task_id] = (
                    list(task.output_paths)
                    if task.output_paths
                    else _output_paths_for_task(config, task)
                )
            except Exception:  # noqa: BLE001 — a task whose paths fail just falls to broad.
                config_output_paths[task.task_id] = list(task.output_paths or ())
        return config_output_paths

    def _resolve_owner_uniqueness_config_paths(
        self,
        tasks: list[ImplementTaskRef],
        config: dict[str, Any],
    ) -> dict[str, list[str]]:
        """Per-task EXCLUSIVE-ownership CLAIMS for the owner-uniqueness gate.

        Distinct from :meth:`_resolve_oracle_config_output_paths` (the oracle
        scope index input): the uniqueness gate must reason over what a task
        EXCLUSIVELY CLAIMS to own, not where it MAY write. The gate already reads
        each task's declared ``output_paths`` internally, so this adds only the
        config-DECLARED ``implement.default_output_paths`` / ``implement_targets``
        mapping for the task's design node. It deliberately does NOT fall back to
        ``_output_paths_for_task`` — that fallback returns the permissive
        ``source_root`` + ``package_root`` accept-list (``_route_source_into_package``),
        which is "this task MAY write under here", not an exclusive claim. Feeding
        it to the gate false-RED's a normal Python src-layout (``src`` nests
        ``src/<pkg>``) whenever ≥2 undeclared tasks share that fallback — the
        v2.41 regression this resolves. TS was unaffected only because its
        ``source_root == package_root`` (no nesting); the fix is layout-agnostic.
        """
        from codd.implementer import _configured_output_path_groups

        try:
            declared = _configured_output_path_groups(config)
        except Exception:  # noqa: BLE001 — a malformed config maps to "nothing declared".
            declared = {}
        out: dict[str, list[str]] = {}
        for task in tasks:
            design = getattr(task, "design_node", None)
            if design and design in declared:
                paths = [str(p) for p in declared[design] if str(p).strip()]
                if paths:
                    out[task.task_id] = paths
        return out

    def _certify_output_owner_uniqueness(
        self,
        tasks: list[ImplementTaskRef],
        config_output_paths: dict[str, list[str]],
    ) -> None:
        """HARD GATE (deterministic, before implement-oracle): exactly one owner.

        Contract ``artifact.owner.unique.v1`` (GPT-5.5 Pro round-2 §3.3). The
        owner index's ``setdefault`` lets the FIRST task silently win a contested
        output; this raises a :class:`StageError` instead so an ambiguous-owner
        topology (the same exact file declared by >1 task; a directory owner that
        nests a different task's exact file; two overlapping directory owners)
        fails fast and honestly — its repair scope/write-fence would otherwise be
        undecidable. Pure structural check; never weakens an existing gate.
        """
        from codd.implement_oracle_scope import (
            OwnerUniquenessError,
            validate_task_output_ownership_uniqueness,
        )

        try:
            validate_task_output_ownership_uniqueness(
                tasks, config_output_paths=config_output_paths
            )
        except OwnerUniquenessError as exc:
            raise StageError(
                "implement-oracle output-owner uniqueness gate failed — an artifact "
                "would be owned by more than one task, so a scoped rerun's "
                f"responsibility and write-fence are ambiguous. {exc}"
            ) from exc

    def _build_oracle_scope_index(
        self,
        project_root: Path,
        tasks: list[ImplementTaskRef],
        config: dict[str, Any],
        *,
        config_output_paths: dict[str, list[str]] | None = None,
    ) -> Any:
        """Build the path→owning-task index used to scope an oracle rerun.

        Unions each task's DECLARED ``output_paths`` with its CONFIG-derived
        output paths (``_output_paths_for_task`` — the same resolution the rerun
        itself uses), so a diagnostic on any owned file/dir maps back to the task
        that wrote it. A failure to build the index degrades the gate to the
        broad rerun (``None`` → ``scope_index`` unset), never aborts.
        """
        try:
            from codd.implement_oracle_scope import build_path_owner_index

            if config_output_paths is None:
                config_output_paths = self._resolve_oracle_config_output_paths(
                    tasks, config
                )
            return build_path_owner_index(
                tasks,
                project_root=project_root,
                config=config,
                config_output_paths=config_output_paths,
            )
        except Exception as exc:  # noqa: BLE001 — index build is best-effort.
            self.echo(f"[greenfield] implement-oracle: scope index unavailable ({exc}); rerun stays broad.")
            return None

    def _oracle_manifest_paths(self, project_root: Path) -> tuple[str, ...]:
        """Harness-owned shared files the write-fence permits + the orphan gate exempts.

        Two roles, one list (both legitimate for the same files):
          * **write-fence permit** — a scoped rerun may touch shared build
            manifest/config even when no task "owns" it (e.g. adding a dependency
            the fix needs).
          * **orphan-gate escape hatch** (``extra_owned``) — a scaffolded config
            is a generated artifact owned by the HARNESS contract, not a task, so
            it must not be mis-flagged as an unowned orphan.

        Sources, unioned: (1) the active stack's :class:`LayoutProfile`
        ``harness_owned_scaffold_paths`` (e.g. TS ``vitest.config.ts`` /
        ``tsconfig.json`` / ``package.json`` — the files the scaffolder creates),
        derived language-agnostically from the profile; (2) a small fallback set of
        common lockfiles a manager may have produced that the profile does not
        enumerate. Only files that actually EXIST are returned, project-relative.
        """
        candidates: list[str] = []

        def _add(rel: str) -> None:
            if rel and rel not in candidates:
                candidates.append(rel)

        # (1) Profile-declared scaffold contract (language-agnostic, single source).
        try:
            from codd.project_types import resolve_layout_profile

            config, language, source_dirs, test_dirs = self._layout_inputs(project_root)
            profile = resolve_layout_profile(
                language=language,
                project_name=self._layout_project_name(project_root, config),
                source_dirs=source_dirs,
                test_dirs=test_dirs,
                config=config,
                project_root=project_root,
            )
            if profile is not None:
                for rel in profile.harness_owned_scaffold_paths():
                    _add(rel)
        except Exception as exc:  # noqa: BLE001 — escape hatch is best-effort; fall back to the static set.
            self.echo(f"[greenfield] implement-oracle: scaffold-path contract unavailable ({exc}).")

        # (2) Lockfile fallbacks not necessarily enumerated by the profile.
        for name in ("package.json", "tsconfig.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"):
            _add(name)

        present: list[str] = []
        for name in candidates:
            if (project_root / name).is_file():
                present.append(name)
        return tuple(present)

    def _rerun_tasks_with_feedback(
        self,
        project_root: Path,
        tasks: list[ImplementTaskRef],
        feedback: str,
        options: dict[str, Any],
        *,
        scope: Any = None,
    ) -> None:
        """Re-invoke implementation under ``feedback`` — SCOPED when ``scope`` set.

        ``scope is None`` (or a broad scope) → re-implement EVERY task (the
        legacy broad rerun). A scoped :class:`~codd.implement_oracle_scope.OracleRerunScope`
        → re-implement ONLY its ``task_ids``, under a WRITE-FENCE: out-of-scope
        files the SUT writes during the scoped rerun are reverted afterwards (an
        out-of-scope CREATE is removed, an out-of-scope MODIFY is restored), so a
        "targeted" rerun cannot silently regenerate the whole tree. The fence is
        OFF for a broad rerun (broad legitimately rewrites everything).

        Routes through the SAME implement path the stage uses (``implement_tasks``
        with the resolved output paths), threading the normalized oracle feedback
        so the SUT regenerates coherent files.
        """
        from codd.config import load_project_config

        try:
            config = load_project_config(project_root)
        except (FileNotFoundError, ValueError):
            config = {}

        # A rerun is FENCED (scoped execution) when the scope carries non-empty
        # ``allowed_paths`` — this covers a narrow/expanded scope AND a broad-CAMPAIGN
        # PHASE scope (logically broad — rung=broad — but with a per-phase write-fence
        # so the phase re-implements ONLY its tasks and out-of-scope writes revert).
        # The LEGACY whole-project broad (scope None, OR is_broad() with NO
        # allowed_paths and no repair_plan) re-implements every task UNFENCED.
        scoped = scope is not None and bool(getattr(scope, "allowed_paths", ()) or ())
        legacy_broad = scope is None or (
            bool(getattr(scope, "is_broad", lambda: False)())
            and not bool(getattr(scope, "allowed_paths", ()) or ())
            and not getattr(scope, "repair_plan", None)
        )
        if legacy_broad or not scoped:
            self._reimplement_tasks(project_root, tasks, feedback, config)
            return

        # Fenced rerun: only the scope's tasks, fenced to its allowed paths.
        target_ids = set(getattr(scope, "task_ids", ()) or ())
        scoped_tasks = [task for task in tasks if task.task_id in target_ids]
        if not scoped_tasks:
            # Nothing resolvable in this scope (defensive) → broad, never a no-op.
            self.echo("[greenfield] implement-oracle: scoped task set empty — re-running broad.")
            self._reimplement_tasks(project_root, tasks, feedback, config)
            return

        allowed = tuple(getattr(scope, "allowed_paths", ()) or ())
        with _OracleWriteFence(project_root, allowed_paths=allowed, echo=self.echo) as fence:
            self._reimplement_tasks(project_root, scoped_tasks, feedback, config)
            fence.enforce()

    def _reimplement_tasks(
        self,
        project_root: Path,
        tasks: list[ImplementTaskRef],
        feedback: str,
        config: dict[str, Any],
    ) -> dict[str, float]:
        """Re-run ``implement_tasks`` for each given task carrying ``feedback``.

        Returns ``{task_id: elapsed_seconds}`` so a broad-campaign caller can budget
        + audit per-task cost. (The campaign's wall-clock gate measures elapsed
        directly; the per-task map is the finer-grained record.)
        """
        import time

        from codd.implementer import implement_tasks

        elapsed: dict[str, float] = {}
        for task in tasks:
            output_paths = (
                list(task.output_paths)
                if task.output_paths
                else _output_paths_for_task(config, task)
            )
            started = time.monotonic()
            implement_tasks(
                project_root,
                design=task.design_node,
                output_paths=output_paths,
                ai_command=self.ai_command,
                use_derived_steps=True,
                feedback=feedback,
            )
            elapsed[task.task_id] = time.monotonic() - started
        return elapsed

    def _make_vb_rerun_callback(
        self,
        project_root: Path,
        tasks: list[ImplementTaskRef],
        options: dict[str, Any],
    ) -> Callable[[str, Any], None]:
        """A ``rerun(feedback, scope)`` for the VB coverage gate's feedback loop.

        Reuses the oracle's scoped, write-fenced rerun dispatch
        (:meth:`_rerun_tasks_with_feedback`): a TEST-scoped
        :class:`~codd.implement_oracle_scope.OracleRerunScope` re-implements ONLY
        its test tasks, fenced to test files/helpers, so a VB coverage rerun can
        never rewrite production source.
        """

        def _rerun(feedback: str, scope: Any = None) -> None:
            self._rerun_tasks_with_feedback(project_root, tasks, feedback, options, scope=scope)

        return _rerun

    def _make_vb_scope_resolver(
        self,
        project_root: Path,
        tasks: list[ImplementTaskRef],
    ) -> Callable[[list[str]], Any]:
        """A resolver mapping uncovered VB source docs → a TEST-scoped rerun scope.

        Delegates to :func:`codd.vb_rerun_scope.derive_vb_rerun_scope` with the
        pipeline's ``_output_paths_for_task`` so each task's outputs resolve the
        same way the rerun itself resolves them. A derivation failure degrades to
        a broad scope (never aborts the gate).
        """
        from codd.config import load_project_config

        def _resolve(uncovered_source_docs: list[str]) -> Any:
            try:
                config = load_project_config(project_root)
            except (FileNotFoundError, ValueError):
                config = {}
            try:
                from codd.vb_rerun_scope import derive_vb_rerun_scope

                return derive_vb_rerun_scope(
                    uncovered_source_docs,
                    tasks,
                    config=config,
                    path_resolver=_output_paths_for_task,
                )
            except Exception as exc:  # noqa: BLE001 — degrade to broad, never abort.
                self.echo(f"[greenfield] VB rerun: scope derivation failed ({exc}); rerun stays broad.")
                from codd.implement_oracle_scope import SCOPE_BROAD, OracleRerunScope

                return OracleRerunScope(
                    rung=SCOPE_BROAD,  # canonical broad ⇒ is_broad() True ⇒ unfenced
                    task_ids=tuple(task.task_id for task in tasks),
                    allowed_paths=(),
                    detail="VB rerun: scope derivation error — broad fallback",
                )

        return _resolve

    def _resolve_layout_profile(self, project_root: Path) -> Any:
        """Resolve the active :class:`~codd.project_types.LayoutProfile` (or None).

        Used by the marker-authenticity gate to obtain the per-language
        test-block adapter (``profile.test_block_profile()``). Best-effort: any
        resolution failure returns ``None`` so the gate degrades to its
        language-agnostic stage-1 (orphan) check rather than aborting.
        """
        try:
            from codd.project_types import resolve_layout_profile

            config, language, source_dirs, test_dirs = self._layout_inputs(project_root)
            return resolve_layout_profile(
                language=language,
                project_name=self._layout_project_name(project_root, config),
                source_dirs=source_dirs,
                test_dirs=test_dirs,
                config=config,
                project_root=project_root,
            )
        except Exception as exc:  # noqa: BLE001 — authenticity degrades without a profile.
            self.echo(f"[greenfield] VB authenticity: layout profile unavailable ({exc}); stage-1 only.")
            return None

    def _default_implement_task_runner(
        self,
        project_root: Path,
        task: ImplementTaskRef,
        *,
        ai_command: str | None,
        coverage_gate: bool,
        chunk_size: int | None,
        timeout_per_chunk: int,
    ) -> str:
        from codd.config import load_project_config
        from codd.implementer import implement_tasks

        config = load_project_config(project_root)
        output_paths = list(task.output_paths) if task.output_paths else _output_paths_for_task(config, task)
        approved = _derive_and_approve_steps(
            project_root, config, task, output_paths, ai_command=ai_command, echo=self.echo
        )

        if chunk_size:
            import codd.cli as cli_module

            chunked = cli_module._run_chunked_implementation(
                project_root=project_root,
                task_id=task.design_node,
                ai_cmd=ai_command,
                chunk_size=int(chunk_size),
                timeout_per_chunk=timeout_per_chunk,
                history=None,
            )
            if getattr(chunked, "status", "SUCCESS") != "SUCCESS":
                raise StageError(f"task {task.task_id}: chunked implementation status {chunked.status}")
            results: list[Any] = []
        else:
            results = implement_tasks(
                project_root,
                design=task.design_node,
                output_paths=output_paths,
                ai_command=ai_command,
                use_derived_steps=True,
            )
            failed = [result for result in results if result.error]
            if failed:
                raise StageError(f"task {task.task_id}: {failed[0].error}")

            # Contract-aware task-done verification: a task is "done" only if the
            # implementer produced the KIND of artifact the task declared (e.g. a
            # test-writing task must emit at least one test file, not just app
            # code). Drives off the declared expected_outputs — never task names,
            # vendor CLI, or path literals. No-op for tasks with no declared
            # output kind. See _verify_task_contract for the rules.
            _verify_task_contract(task, results, project_root, config, echo=self.echo)

        # NOTE: the verifiable-behavior (VB) coverage gate is intentionally NOT
        # enforced per task here. The gate is PROJECT-WIDE (it reconciles every
        # VB id declared across the test documents against `covers vb=` markers
        # anywhere in the suite), but greenfield runs implement task-by-task.
        # An early task (e.g. test fixtures/helpers) that legitimately writes no
        # covering tests would see ~0 project coverage and hard-fail, even
        # though later tasks add the covering tests. So the once-per-stage
        # project-wide gate runs in :meth:`_stage_implement` AFTER all tasks
        # complete (see :func:`_enforce_stage_coverage_gate`). Each task still
        # gets its per-file syntax/confusable gates and tests via
        # ``implement_tasks``. The ``coverage_gate`` arg is retained for the DI
        # seam / standalone callers; the greenfield default runner ignores it
        # at task granularity.
        del coverage_gate  # gated once at stage end, never per task
        generated = sum(len(result.generated_files) for result in results)
        suffix = f", {approved} step(s) auto-approved" if approved else ""
        return f"{generated} file(s) generated{suffix}"

    # ── stage: verify ───────────────────────────────────────

    def _stage_verify(self, project_root: Path, record: dict[str, Any], options: dict[str, Any]) -> None:
        # Deterministically guarantee the stack's topology + test runner are
        # present before verify runs. The build now contains source + tests
        # (implement finished), but whether a runnable, COHERENT layout exists
        # was left to the generating AI's luck (2026-06 dogfood: source used
        # package-relative imports while tests flat-imported by bare basename, an
        # environment-dependent false green). The harness owns the topology:
        #   1. ensure the layout scaffold + a runnable pyproject (no pythonpath
        #      "."; tests run against the real package);
        #   2. run the AST import-coherence GATE before pytest — incoherent
        #      source/tests FAIL HONESTLY here instead of crashing pytest or
        #      passing by accident.
        # Both are idempotent and non-clobbering, so this also applies on
        # --resume. The verify honesty gate below remains the final authority on
        # whether the (now coherent) build is certifiable.
        self._ensure_test_runner(project_root)
        self._enforce_import_coherence(project_root)

        # Lock-freshness barrier — verify-direct, BEFORE any frozen install in
        # verify (the verify runner's blocking ``npm ci`` preflight below AND the
        # coverage-execution campaign's frozen install later both consume the lock).
        # Implement-end dep-coherence already refreshed the lock ONCE, but the
        # implement stage's VB-coverage / oracle reruns can re-write package.json
        # AFTER that point, leaving the lock STALE (an observed dogfood gap: a
        # transitive omission → frozen ``npm ci`` "Missing from lock file"). This
        # barrier enforces the invariant "no frozen install runs unless the lock is
        # fresh for the current manifest set": it dirty-marks by a content DIGEST
        # (manifest + workspace manifests + .npmrc + pm version + harness profile)
        # and, only when that digest CHANGED since the last freeze, re-reconciles +
        # re-refreshes + re-validates (with a completeness fallback) and re-records
        # the digest. An UNCHANGED manifest is a no-op (verify's own frozen install
        # reproduces the fresh lock). It NEVER loosens verify's frozen install — it
        # moves the freeze BASIS from implement-end to the final manifest. A strict
        # NO-OP for a stack with no toolchain profile (Python today). See
        # codd.dependency_lock_coherence.ensure_lock_freshness_barrier.
        self._ensure_lock_freshness(project_root)

        runner = self.verify_runner or _default_verify_runner
        detail = str(
            runner(
                project_root,
                ai_command=self.ai_command,
                max_repair_attempts=int(options.get("max_repair_attempts") or 10),
                echo=self.echo,
            )
        )

        # Coverage-execution coherence gate (anti-false-green, design
        # /tmp/gpt_vscope_result.txt). The verify runner above proved the
        # STRUCTURAL/typecheck/test-command path; this ADDS the execution-coherence
        # proof: it runs the PROFILE-OWNED verify campaign (the WHOLE VB surface —
        # unit AND e2e, NOT a single SUT script) and reconciles which VB-covering
        # tests actually executed+passed against the static VB coverage map. It
        # HARD-FAILS when a declared behavior is statically "covered" but its
        # covering test was never run (e.g. an e2e-only VB whose e2e suite the
        # detected ``test:unit`` skipped — the dogfood false-green this closes),
        # or when an e2e surface exists but the campaign scanned 0 e2e files. NO-OP
        # for a stack with no profile campaign (Python today). It NEVER weakens the
        # verify runner above — it is an additional, stricter gate on top.
        coherence_detail = self._enforce_coverage_execution_coherence(project_root, options)
        record["detail"] = detail + coherence_detail

    def _enforce_coverage_execution_coherence(
        self, project_root: Path, options: dict[str, Any]
    ) -> str:
        """Run the profile-owned verify campaign + coverage-execution coherence gate.

        Returns a short detail suffix (empty when the gate does not apply to this
        stack). Honors the greenfield ``coverage_gate`` option (the same switch
        that governs the implement-stage VB gates): when the owner turned VB
        gating off, this execution-coherence gate is skipped too. A hard coherence
        failure is raised as a :class:`StageError` so the autopilot stops and
        reports honestly. A CAMPAIGN/observability error (the campaign could not
        run to a parseable report, or an e2e surface was not observed) is ALSO a
        StageError — an unobservable verification is not a pass.
        """
        if not bool(options.get("coverage_gate", True)):
            return ""
        from codd.coverage_execution_coherence import (
            CampaignError,
            CoherenceError,
            certify_verify_campaign_observable,
            coherence_gate_applies,
            enforce_coverage_execution_coherence,
        )

        profile = self._resolve_layout_profile(project_root)
        if profile is None:
            return ""
        # HARD observability gate (contract verify.campaign.observable.v1): a
        # profile that DECLARES a campaign but has no report adapter would make
        # ``coherence_gate_applies`` False below — a silent NO-OP for a stack that
        # asked to be verified. Honest-fail BEFORE that short-circuit instead.
        try:
            certify_verify_campaign_observable(profile)
        except CampaignError as exc:
            raise StageError(
                "verify campaign (coverage-execution coherence) is declared but "
                f"cannot be observed: {exc}"
            ) from exc
        if not coherence_gate_applies(profile):
            return ""
        try:
            from codd.config import load_project_config

            try:
                config = load_project_config(project_root)
            except (FileNotFoundError, ValueError):
                config = {}
            report = enforce_coverage_execution_coherence(
                project_root, profile, config=config, echo=self.echo
            )
        except CoherenceError as exc:
            raise StageError(str(exc)) from exc
        except CampaignError as exc:
            raise StageError(
                "verify campaign (coverage-execution coherence) could not be "
                f"observed: {exc}. An unobservable verification is not a pass — "
                "check the campaign command / runner report output."
            ) from exc
        if not report.applicable:
            return ""
        return f"; coverage-execution coherence OK ({report.detail})"

    def _layout_inputs(self, project_root: Path) -> tuple[dict[str, Any], str | None, Any, Any]:
        """Resolve (config, language, source_dirs, test_dirs) for the stack profile."""
        from codd.config import load_project_config

        try:
            config = load_project_config(project_root)
        except (FileNotFoundError, ValueError):
            config = {}
        project_section = config.get("project") if isinstance(config.get("project"), dict) else {}
        language = self.language or (
            project_section.get("language") if isinstance(project_section, dict) else None
        )
        scan = config.get("scan") if isinstance(config.get("scan"), dict) else {}
        source_dirs = scan.get("source_dirs") if isinstance(scan, dict) else None
        test_dirs = scan.get("test_dirs") if isinstance(scan, dict) else None
        return config, language, source_dirs, test_dirs

    def _layout_project_name(self, project_root: Path, config: dict[str, Any]) -> str:
        project_section = config.get("project") if isinstance(config.get("project"), dict) else {}
        configured = project_section.get("name") if isinstance(project_section, dict) else None
        return str(self.project_name or configured or project_root.name)

    def _ensure_test_runner(self, project_root: Path) -> None:
        """Scaffold the stack topology + a runnable, COHERENT test-runner config.

        Stack-general: dispatches through the layout-profile registry
        (:func:`codd.project_types.resolve_layout_profile` +
        :func:`scaffold_layout`), which is idempotent and non-clobbering and
        derives every path from the project name + configured
        ``scan.source_dirs`` / ``scan.test_dirs``. The emitted pyproject has NO
        ``pythonpath="."`` — tests run against the real package (anti-false-
        green). Advisory — any failure is logged and swallowed; the coherence
        gate and verify honesty gate remain the authorities.
        """
        try:
            from codd.project_types import resolve_layout_profile, scaffold_layout

            config, language, source_dirs, test_dirs = self._layout_inputs(project_root)
            profile = resolve_layout_profile(
                language=language,
                project_name=self._layout_project_name(project_root, config),
                source_dirs=source_dirs,
                test_dirs=test_dirs,
                config=config,
                project_root=project_root,
            )
            if profile is None:
                # No layout profile for this stack: fall back to the runner-only
                # ensure (respects an AI/user-provided detectable setup; the
                # verify honesty gate still refuses to certify an unexecuted build).
                from codd.project_types import ensure_test_runner_config

                result = ensure_test_runner_config(
                    project_root,
                    language=language,
                    project_name=self._layout_project_name(project_root, config),
                    source_dirs=source_dirs,
                    test_dirs=test_dirs,
                )
                if result.action in ("created", "augmented"):
                    self.echo(f"[greenfield] verify: ensured test runner — {result.detail}")
                return
            scaffold = scaffold_layout(project_root, profile)
            if scaffold.created:
                self.echo(
                    f"[greenfield] verify: scaffolded layout ({', '.join(scaffold.created)}) — {scaffold.detail}"
                )
        except Exception as exc:  # noqa: BLE001 — scaffolding is advisory; the gates enforce honesty.
            self.echo(f"[greenfield] verify: layout scaffold skipped (non-blocking): {exc}")

    def _enforce_import_coherence(self, project_root: Path) -> None:
        """Run the AST import-coherence gates BEFORE pytest.

        Stack-general and profile-driven. Three complementary, static (AST-only)
        anti-false-green gates run here, all BEFORE pytest so an incoherence
        fails HONESTLY with an actionable message instead of crashing pytest or
        passing by accident:

        1. **Source/test package coherence** (:func:`check_import_coherence`):
           source + tests must agree on package/import context (no test importing
           a source module by bare basename — an environment-dependent false
           green).
        2. **Test-helper SYMBOL coherence** (:func:`check_test_import_coherence`):
           every symbol a generated test imports from an in-test-tree helper
           (sibling test module / helper package / ``conftest``) must actually be
           defined or re-exported there — otherwise pytest aborts at COLLECTION
           with an opaque exit-2 error (2026-06 dogfood). This gate is scoped to
           the test tree and never duplicates gate (1)'s source-package check.
        3. **E2E-contract (no-runtime-import) coherence**
           (:func:`check_e2e_contract_coherence`): for a CLI/subprocess e2e
           modality, e2e tests AND their shared e2e helpers must invoke the
           entrypoint as a SUBPROCESS and must NOT import the runtime/source
           package (2026-06 dogfood: a function-scoped runtime import in an e2e
           helper violated the project's OWN governance test → a run-phase
           assertion failure auto-repair correctly refused to touch). MODALITY-
           GATED: a no-op for browser/device e2e (which legitimately import a
           client) and for untyped/undecidable projects.

        Any FAIL raises :class:`StageError` and fails the verify stage with the
        DIAGNOSE → REGENERATE message (the scaffold is create-only; --resume will
        not rewrite generated files, and stubs/tests are never auto-created or
        auto-edited). All honor the explicit opt-out
        ``coherence.import_coherence: false`` and are never weakened silently. A
        stack without a layout profile is a passing no-op.
        """
        from codd.e2e_contract_coherence import check_e2e_contract_coherence
        from codd.import_coherence import check_import_coherence
        from codd.test_import_coherence import check_test_import_coherence

        config, language, source_dirs, test_dirs = self._layout_inputs(project_root)
        project_name = self._layout_project_name(project_root, config)
        result = check_import_coherence(
            project_root,
            language=language,
            project_name=project_name,
            source_dirs=source_dirs,
            test_dirs=test_dirs,
            config=config,
        )
        if not result.passed:
            self.echo(f"[greenfield] verify: {result.summary()}")
            raise StageError(result.summary())
        if result.detail:
            self.echo(f"[greenfield] verify: {result.detail}")

        # Test-helper symbol coherence — same hook, same DIAGNOSE → REGENERATE
        # stance, scoped to the test tree (no overlap with the gate above).
        test_result = check_test_import_coherence(
            project_root,
            language=language,
            project_name=project_name,
            source_dirs=source_dirs,
            test_dirs=test_dirs,
            config=config,
        )
        if not test_result.passed:
            self.echo(f"[greenfield] verify: {test_result.summary()}")
            raise StageError(test_result.summary())
        if test_result.detail:
            self.echo(f"[greenfield] verify: {test_result.detail}")

        # E2E-contract (no-runtime-import) coherence — same hook, same DIAGNOSE →
        # REGENERATE stance. MODALITY-GATED: active only for a CLI/subprocess e2e
        # contract (browser/device e2e legitimately imports a client → no-op). It
        # never touches the repair scope-guard / attribution (deferred-B stays
        # deferred) — it only adds an honest RED before pytest.
        e2e_result = check_e2e_contract_coherence(
            project_root,
            language=language,
            project_name=project_name,
            source_dirs=source_dirs,
            test_dirs=test_dirs,
            config=config,
        )
        if not e2e_result.passed:
            self.echo(f"[greenfield] verify: {e2e_result.summary()}")
            raise StageError(e2e_result.summary())
        if e2e_result.detail:
            self.echo(f"[greenfield] verify: {e2e_result.detail}")

    # ── stage: ci_scaffold (make the built system CI-ready) ──

    def _stage_ci_scaffold(self, project_root: Path, record: dict[str, Any], options: dict[str, Any]) -> None:
        del options
        runner = self.ci_scaffold_runner or _default_ci_scaffold_runner
        detail = str(runner(project_root, ai_command=self.ai_command))
        record["detail"] = detail
        if detail.startswith("skipped"):
            record["status"] = STATUS_SKIPPED

    # ── stage: propagate (advisory on a fresh build) ────────

    def _stage_propagate(self, project_root: Path, record: dict[str, Any], options: dict[str, Any]) -> None:
        if not options.get("propagate_commit", True):
            record["status"] = STATUS_SKIPPED
            record["detail"] = "disabled (greenfield.propagate_commit: false)"
            return
        try:
            runner = self.propagate_runner or _default_propagate_runner
            record["detail"] = str(runner(project_root, ai_command=self.ai_command))
        except Exception as exc:  # noqa: BLE001 — nothing-to-propagate is normal on a fresh build.
            record["status"] = STATUS_WARNING
            record["detail"] = f"propagate skipped (non-blocking): {exc}"

    # ── stage: check ────────────────────────────────────────

    def _stage_check(self, project_root: Path, record: dict[str, Any], options: dict[str, Any]) -> None:
        runner = self.check_runner or _default_check_runner
        record["detail"] = str(runner(project_root))

    # ── helpers ─────────────────────────────────────────────

    def _checkpoint(self, project_root: Path) -> None:
        """Flush unit progress to disk after EVERY unit so --resume is safe.

        Stage records are live references into the session dict owned by
        :meth:`run` (held on ``self._session_ref`` for the duration of the
        run), so saving the session persists the just-updated unit maps.
        """
        if self._session_ref is not None:
            save_session(project_root, self._session_ref)

    _session_ref: dict[str, Any] | None = None

    # ── dry run ─────────────────────────────────────────────

    def _dry_run(self, project_root: Path) -> GreenfieldResult:
        options = self._resolve_options(project_root)
        from codd.config import find_codd_dir

        outcomes: list[StageOutcome] = []
        codd_dir = find_codd_dir(project_root)
        if codd_dir is not None:
            init_detail = f"skip — CoDD config dir exists: {codd_dir.name}/"
        elif self.project_name and self.language:
            init_detail = f"codd init {self.project_name} --language {self.language}" + (
                f" --requirements {self.requirements}" if self.requirements else ""
            )
        else:
            init_detail = "BLOCKED — pass --project-name and --language (or init the project first)"
        outcomes.append(StageOutcome("init", STATUS_PENDING, init_detail))

        elicit_detail = (
            "codd elicit && codd elicit apply findings.md (advisory)"
            if options.get("elicit", True)
            else "skip — disabled"
        )
        outcomes.append(StageOutcome("elicit", STATUS_PENDING, elicit_detail))
        outcomes.append(StageOutcome("plan", STATUS_PENDING, "codd plan --init --force"))

        waves: list[int] = []
        if codd_dir is not None:
            try:
                waves = _default_wave_lister(project_root)
            except Exception:  # noqa: BLE001 — dry-run must never fail on missing state.
                waves = []
        generate_detail = (
            f"codd generate --wave N for waves {', '.join(str(w) for w in waves)}"
            if waves
            else "codd generate --all-waves (waves resolved after plan)"
        )
        outcomes.append(StageOutcome("generate", STATUS_PENDING, generate_detail))

        tasks: list[ImplementTaskRef] = []
        if codd_dir is not None:
            try:
                tasks = _default_task_lister(project_root)
            except Exception:  # noqa: BLE001 — dry-run must never fail on missing state.
                tasks = []
        implement_detail = (
            "implement plan/approve/run for tasks: " + ", ".join(task.task_id for task in tasks)
            if tasks
            else "tasks resolved after generate (codd implement list-tasks)"
        )
        outcomes.append(StageOutcome("implement", STATUS_PENDING, implement_detail))
        outcomes.append(
            StageOutcome(
                "verify",
                STATUS_PENDING,
                f"codd verify --auto-repair --max-attempts {options.get('max_repair_attempts')} "
                "--repair-mode automatic",
            )
        )
        outcomes.append(
            StageOutcome(
                "ci_scaffold",
                STATUS_PENDING,
                "generate .github/workflows/ci.yml running the detected test command "
                "(skipped if a workflow exists or ci.provider=none)",
            )
        )
        propagate_detail = (
            "codd propagate --verify && codd propagate --commit (advisory)"
            if options.get("propagate_commit", True)
            else "skip — disabled"
        )
        outcomes.append(StageOutcome("propagate", STATUS_PENDING, propagate_detail))
        outcomes.append(StageOutcome("check", STATUS_PENDING, "codd check"))
        return GreenfieldResult(project_root=project_root, status="dry-run", stages=outcomes)


# ═══════════════════════════════════════════════════════════
# Default stage runners (the same code paths the CLI uses)
# ═══════════════════════════════════════════════════════════

def _default_notifier(topic: str, message: str) -> bool:
    from codd.ask_user_question_adapter import _post_ntfy

    return _post_ntfy(topic, message)


def _default_elicit_runner(project_root: Path, *, ai_command: str | None) -> str:
    import codd.cli as cli_module
    from codd.elicit.apply import ElicitApplyEngine
    from codd.elicit.engine import ElicitEngine

    lexicon_config = cli_module._load_elicit_lexicon_configs(project_root, None)
    result = ElicitEngine(ai_command=ai_command).run(project_root, lexicon_config=lexicon_config)
    applied = ElicitApplyEngine(project_root).apply(result.findings)
    return f"findings={len(result.findings)}, applied={applied.applied_count}, skipped={applied.skipped_count}"


def _default_plan_runner(project_root: Path, *, ai_command: str | None, force: bool) -> int:
    from codd.planner import plan_init

    result = plan_init(project_root, force=force, ai_command=ai_command)
    return len(result.wave_config)


def _default_wave_lister(project_root: Path) -> list[int]:
    from codd.config import load_project_config

    wave_config = load_project_config(project_root).get("wave_config") or {}
    waves: list[int] = []
    for key in wave_config:
        try:
            waves.append(int(key))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"wave_config key must be an integer wave number, got {key!r}") from exc
    return sorted(set(waves))


def _default_generate_wave_runner(project_root: Path, wave: int, *, ai_command: str | None) -> str:
    from codd.generator import generate_wave

    results = generate_wave(project_root, wave, force=False, ai_command=ai_command)
    generated = sum(1 for result in results if result.status == "generated")
    return f"{generated} generated, {len(results) - generated} skipped"


#: Directories never snapshotted/fenced — vendored deps, VCS, the codd cache,
#: build output. A write here is not a SUT source-write the fence governs.
_FENCE_EXCLUDE_DIRS = frozenset(
    {"node_modules", ".git", ".codd", ".hg", ".svn", "dist", "build", "__pycache__", ".pytest_cache"}
)
#: Source/test file suffixes the fence tracks. Restricting to code keeps the
#: snapshot cheap and avoids fighting tooling that touches caches/logs.
_FENCE_TRACKED_SUFFIXES = frozenset(
    {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".py", ".json"}
)


class _OracleWriteFence:
    """Restrict a SCOPED oracle rerun's writes to the scope's ``allowed_paths``.

    Re-implementing only the scoped tasks does NOT stop the SUT from writing
    OUT-of-scope files (the design's #1 implementation note). The fence snapshots
    the tracked source/test tree on entry; on :meth:`enforce` it reverts every
    out-of-scope change made during the rerun — an out-of-scope CREATE is deleted,
    an out-of-scope MODIFY is restored to its pre-rerun bytes. In-scope writes
    (under an allowed file/dir, or a manifest/config) pass untouched. This makes
    a "targeted" rerun genuinely local: it cannot silently regenerate the tree.

    ``allowed_paths`` entries are matched as exact files OR directory prefixes
    (a task that owns ``src/`` may write any file under ``src/``). An EMPTY
    allow-set means "no fence" (the broad rerun's signal) and the caller does not
    construct a fence in that case.
    """

    def __init__(self, project_root: Path, *, allowed_paths: tuple[str, ...], echo: Callable[[str], str]):
        self._root = Path(project_root).resolve()
        self._allowed_files, self._allowed_dirs = self._split_allowed(allowed_paths)
        self._echo = echo
        self._snapshot: dict[str, bytes] = {}

    def __enter__(self) -> "_OracleWriteFence":
        self._snapshot = self._capture()
        return self

    def __exit__(self, *_exc: Any) -> None:
        # The fence does not suppress exceptions; enforce() is called explicitly
        # by the caller on the SUCCESS path so a failing rerun's exception still
        # propagates with the tree left for the caller's error handling.
        return None

    def enforce(self) -> None:
        """Revert every out-of-scope create/modify made since entry."""
        current = self._capture()
        reverted_modified: list[str] = []
        reverted_created: list[str] = []

        # Reverts for modified/created files.
        for rel, content in current.items():
            if self._is_allowed(rel):
                continue
            if rel in self._snapshot:
                if self._snapshot[rel] != content:
                    self._restore(rel, self._snapshot[rel])
                    reverted_modified.append(rel)
            else:
                self._remove(rel)
                reverted_created.append(rel)

        # Re-create any tracked file the scoped rerun DELETED out of scope (a
        # deletion is also an out-of-scope mutation we must undo).
        reverted_deleted: list[str] = []
        for rel, content in self._snapshot.items():
            if rel in current or self._is_allowed(rel):
                continue
            self._restore(rel, content)
            reverted_deleted.append(rel)

        total = len(reverted_modified) + len(reverted_created) + len(reverted_deleted)
        if total:
            self._echo(
                "[greenfield] implement-oracle: write-fence reverted "
                f"{total} out-of-scope change(s) "
                f"(modified={len(reverted_modified)}, created={len(reverted_created)}, "
                f"deleted={len(reverted_deleted)}); the scoped rerun is kept local."
            )

    # ── internals ──
    @staticmethod
    def _split_allowed(allowed_paths: tuple[str, ...]) -> tuple[set[str], list[str]]:
        files: set[str] = set()
        dirs: list[str] = []
        for raw in allowed_paths:
            norm = str(raw).strip().replace("\\", "/").strip("/")
            if not norm:
                continue
            if PurePosixPath(norm).suffix:
                files.add(norm)
            else:
                dirs.append(norm)
        return files, dirs

    def _is_allowed(self, rel: str) -> bool:
        if rel in self._allowed_files:
            return True
        for directory in self._allowed_dirs:
            if rel == directory or rel.startswith(directory + "/"):
                return True
        return False

    def _capture(self) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        for path in self._iter_tracked_files():
            try:
                out[path.relative_to(self._root).as_posix()] = path.read_bytes()
            except OSError:
                continue
        return out

    def _iter_tracked_files(self):
        import os

        for dirpath, dirnames, filenames in os.walk(self._root):
            dirnames[:] = [d for d in dirnames if d not in _FENCE_EXCLUDE_DIRS]
            for name in filenames:
                if PurePosixPath(name).suffix in _FENCE_TRACKED_SUFFIXES:
                    yield Path(dirpath) / name

    def _restore(self, rel: str, content: bytes) -> None:
        target = self._root / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        except OSError as exc:
            self._echo(f"[greenfield] implement-oracle: write-fence could not restore {rel} ({exc}).")

    def _remove(self, rel: str) -> None:
        target = self._root / rel
        try:
            target.unlink()
        except OSError as exc:
            self._echo(f"[greenfield] implement-oracle: write-fence could not remove {rel} ({exc}).")


def _default_task_lister(project_root: Path) -> list[ImplementTaskRef]:
    from codd.implementer import list_implement_tasks

    return [
        ImplementTaskRef(
            task_id=entry["task_id"],
            design_node=entry["design_node"],
            source=entry["source"],
            expected_outputs=tuple(entry.get("expected_outputs") or ()),
            test_kinds=tuple(entry.get("test_kinds") or ()),
        )
        for entry in list_implement_tasks(project_root)
    ]


def _default_task_deriver(project_root: Path, *, ai_command: str | None) -> int:
    """Derive implement tasks from design docs and auto-approve them.

    Mirrors ``codd plan derive`` + ``codd plan approve <doc> --all`` — the
    autopilot equivalent of the HITL task-approval gate.
    """
    import codd.cli as cli_module
    from codd.config import load_project_config
    from codd.deployment.providers.ai_command_factory import get_ai_command
    from codd.llm.plan_deriver import PLAN_DERIVERS, approve_cached_tasks, iter_derived_task_records

    config = load_project_config(project_root)
    provider_name = cli_module._plan_derive_provider(config)
    deriver_cls = PLAN_DERIVERS.get(provider_name)
    if deriver_cls is None:
        raise StageError(f"plan deriver provider not found: {provider_name}")
    nodes = cli_module._plan_design_doc_nodes(project_root, ())
    command = ai_command or cli_module._plan_derive_command(config)
    deriver = deriver_cls(get_ai_command(config, project_root, command_override=command))
    tasks = deriver.derive_tasks(
        nodes,
        "detailed",
        {
            "project_root": project_root,
            "force": False,
            "dry_run": False,
            "write_cache": True,
            "project_context": {"project": config.get("project", {})},
        },
    )
    for cache_path, _record in iter_derived_task_records(project_root):
        approve_cached_tasks(cache_path, approve_all=True)
    return len(tasks)


# ═══════════════════════════════════════════════════════════
# Contract-aware task verification (artifact KIND, not name/path)
# ═══════════════════════════════════════════════════════════
#
# A greenfield task is marked ``done`` only after the implementer produced the
# KIND of artifact the task DECLARED. Without this, a test-writing task that
# emitted only application code (observed cross-CLI: some CLIs laid tests under
# the app's source root and produced zero test files) still passed "produced
# >=1 parseable file" and the stage-end VB gate took the blame. The fix drives
# the task's DECLARED intent (``expected_outputs``) — never task names, vendor
# CLI, ``test_kinds`` (that is coverage-level metadata, not a deliverable
# contract), or hardcoded path literals (roots come from ``scan.*_dirs``).
#
# Classification rules (deliberately conservative to avoid false-RED):
#   • A path under a configured ``test_dirs`` root is a TEST artifact.
#   • A test-SHAPED filename is a TEST artifact — but only via a suffix that is
#     UNAMBIGUOUS on its own (JS/TS's dedicated ``.test.``/``.spec.``/``.e2e.``/
#     ``.cy.`` conventions, Go's tooling-enforced ``_test.go``). A BARE
#     whole-language extension (``.py``, ``.cs``, ``.java``, ``.cpp``/``.cc``/
#     ``.cxx``) is never enough on its own — every file in that language ends in
#     it — so ``.py`` requires ``test_*.py`` / ``*_test.py`` naming, and the
#     other bare-admit languages are left entirely to the ``test_dirs`` check
#     above (see ``_unambiguous_test_suffixes``).
#   • A path under a configured ``source_dirs`` root (that is not test-shaped via
#     an unambiguous test suffix) is a SOURCE artifact.
#   • Anything else is UNKNOWN and imposes no requirement.
# Verification passes when every declared kind is represented among generated
# files (``required_kinds ⊆ produced_kinds``); UNKNOWN never gates.

_KIND_SOURCE = "source"
_KIND_TEST = "test"


def _scan_roots(config: dict[str, Any], key: str) -> list[str]:
    scan = config.get("scan") if isinstance(config.get("scan"), dict) else {}
    raw = scan.get(key) if isinstance(scan, dict) else None
    if not isinstance(raw, list):
        return []
    roots: list[str] = []
    for item in raw:
        text = str(item).strip().replace("\\", "/").strip("/")
        if text:
            roots.append(text)
    return roots


def _path_under_root(rel_path: str, roots: list[str]) -> bool:
    norm = str(rel_path).strip().replace("\\", "/").strip("/")
    if not norm:
        return False
    for root in roots:
        if norm == root or norm.startswith(root + "/"):
            return True
    return False


# ``_TEST_SUFFIXES`` (operational_e2e_audit.py) mixes two different kinds of
# entries: dedicated test-file conventions that are unambiguous on their own —
# JS/TS's ``.spec./.test./.e2e./.cy.`` family and Go's tooling-enforced
# ``_test.go`` (``go build`` structurally excludes it from the non-test binary)
# — and BARE whole-language extensions (``.py``, ``.cs``, ``.java``,
# ``.cpp``/``.cc``/``.cxx``) that scanner admits deliberately broadly because
# IT is always additionally gated by the configured test-dir scope (see the
# per-suffix comments in ``operational_e2e_audit.py``). This module's callers
# do not apply that same scope gate on the SOURCE-exclusion side (see
# ``_produced_kinds``), so reusing a bare extension here would flag EVERY file
# of that language — test or not — as test-shaped and permanently bar it from
# ever counting as SOURCE. ``.py`` already had this carve-out (only
# ``test_*.py``/``*_test.py`` count, never bare ``.py``); the other bare-admit
# languages need the identical treatment — generically, since none of them has
# a tooling/naming-enforced unambiguous test suffix. Confirmed root cause of
# the 2026-06-30 Java (``scaffold_package_skeleton``) and 2026-07-01 C++
# (``scaffold_repository_layout``) greenfield false-REDs: a task's real
# ``src/`` output was reported as having produced "only test".
_AMBIGUOUS_BARE_SOURCE_SUFFIXES: frozenset[str] = frozenset(
    {".py", ".cs", ".java", ".cpp", ".cc", ".cxx"}
)


def _unambiguous_test_suffixes() -> tuple[str, ...]:
    """``_TEST_SUFFIXES`` minus every bare, direction-blind source extension."""
    from codd.operational_e2e_audit import _TEST_SUFFIXES

    return tuple(
        suffix for suffix in _TEST_SUFFIXES if suffix not in _AMBIGUOUS_BARE_SOURCE_SUFFIXES
    )


def _has_test_shape(rel_path: str) -> bool:
    """A filename that is unambiguously a test, language-independent.

    Reuses the project's :data:`_TEST_SUFFIXES` for the suffixes that are
    unambiguous on their own, and recognises the conventional pytest/unittest
    naming for Python (``test_*.py`` / ``*_test.py``) — never bare ``.py``. A
    bare whole-language extension for any OTHER bare-admit language (``.cs``,
    ``.java``, ``.cpp``/``.cc``/``.cxx``) is likewise never enough alone; those
    languages rely on the ``test_dirs`` scope check in :func:`_produced_kinds` /
    ``_classify_declared_output`` instead.
    """
    name = PurePosixPath(str(rel_path).replace("\\", "/")).name
    if name.endswith(_unambiguous_test_suffixes()):
        return True
    if name.endswith(".py"):
        return name.startswith("test_") or name[:-3].endswith("_test")
    return False


def _norm_decl_path(raw: Any) -> str:
    """Normalize a declared-output path for EXACT comparison vs the owned set."""
    return str(raw).strip().replace("\\", "/").strip("/")


def _harness_owned_outputs(config: dict[str, Any], project_root: Path | None) -> frozenset[str]:
    """The CLOSED, profile-declared set of harness-owned scaffold paths (or empty).

    Thin wrapper over :func:`codd.project_types.harness_owned_output_paths` — the
    SINGLE authority for "which declared outputs does the profile say the HARNESS
    owns" (e.g. a C# ``src/<Pkg>/<Pkg>.csproj`` whose manifest lives under
    ``src/``). These artifacts are created by the scaffold, never authored by the
    SUT, so the kind/completeness contract must NOT demand the AI produce them.

    The exemption keyed on this set is CLOSED + EXACT: only a path that the
    profile itself declares harness-owned is exempt — a real SOURCE file the
    profile does not own stays strictly gated (anti-false-green). Fail-closed: any
    resolution failure yields the EMPTY set, so the gate stays strict.
    """
    try:
        from codd.project_types import harness_owned_output_paths

        return harness_owned_output_paths(config, project_root=project_root)
    except Exception:  # noqa: BLE001 — fail-closed: no exemption, strict gate.
        return frozenset()


def _classify_declared_output(rel_path: str, config: dict[str, Any]) -> str | None:
    """Classify ONE ``expected_outputs`` entry as the deliverable KIND it implies.

    Returns ``_KIND_TEST`` / ``_KIND_SOURCE`` / ``None`` (unknown — e.g. a bare
    artifact name, a doc, or a path under no configured root).
    """
    if _path_under_root(rel_path, _scan_roots(config, "test_dirs")):
        return _KIND_TEST
    if _has_test_shape(rel_path):
        return _KIND_TEST
    if _path_under_root(rel_path, _scan_roots(config, "source_dirs")):
        return _KIND_SOURCE
    return None


def _required_kinds(
    task: ImplementTaskRef,
    config: dict[str, Any],
    *,
    harness_owned: frozenset[str] | None = None,
) -> set[str]:
    """The deliverable KIND(s) a task's declared outputs require of the SUT.

    A declared output in the profile's CLOSED ``harness_owned`` set (e.g. a C#
    ``.csproj`` the scaffold creates) imposes NO kind — the harness produces it,
    not the AI. EXACT match only: a real SOURCE file the profile does not own
    stays classified and required (anti-false-green). ``harness_owned`` defaults
    to ``None`` (no exemption), preserving the legacy behaviour for every caller
    that does not resolve the profile.
    """
    kinds: set[str] = set()
    for output in task.expected_outputs:
        if harness_owned and _norm_decl_path(output) in harness_owned:
            continue
        kind = _classify_declared_output(str(output), config)
        if kind is not None:
            kinds.add(kind)
    return kinds


def _produced_kinds(generated_files: list[Any], project_root: Path, config: dict[str, Any]) -> set[str]:
    """Classify the files a task actually produced.

    Source-side is positive-location based (under a configured ``source_dirs``
    root and not an unambiguous test file), NOT "anything that isn't a test" —
    because for a bare-admit language (Python, C#, Java, C++/.cc/.cxx) the
    suffix alone would wrongly call every file of that language a test and make
    the source requirement unsatisfiable (false-RED; confirmed for Python by
    inspection and reproduced for real by Java/C++ greenfield dogfood runs). A
    file that is BOTH test-shaped and under a source root (a colocated test
    such as ``src/foo/test_foo.py`` or ``src/foo.test.ts``) is allowed to count
    for the source side too only when it is not an unambiguous test suffix.
    """
    test_roots = _scan_roots(config, "test_dirs")
    source_roots = _scan_roots(config, "source_dirs")
    unambiguous_test = _unambiguous_test_suffixes()
    try:
        root = Path(project_root).resolve()
    except OSError:
        root = Path(project_root)
    kinds: set[str] = set()
    for raw in generated_files:
        try:
            rel = Path(raw).resolve().relative_to(root)
            rel_path = rel.as_posix()
        except (ValueError, OSError):
            rel_path = PurePosixPath(str(raw).replace("\\", "/")).as_posix()
        in_test_dir = _path_under_root(rel_path, test_roots)
        if in_test_dir or _has_test_shape(rel_path):
            kinds.add(_KIND_TEST)
        if _path_under_root(rel_path, source_roots) and not in_test_dir:
            name = PurePosixPath(rel_path).name
            if not name.endswith(unambiguous_test):
                kinds.add(_KIND_SOURCE)
    return kinds


#: ``implement.declared_output_completeness`` modes.
_DECLARED_OUTPUT_COMPLETENESS_MODES = ("warn", "enforce", "off")
DEFAULT_DECLARED_OUTPUT_COMPLETENESS = "warn"


def _declared_output_completeness_mode(config: dict[str, Any] | None) -> str:
    """``implement.declared_output_completeness`` → ``warn`` | ``enforce`` | ``off``.

    Default ``warn`` (observe + report a missing declared output, never block) —
    the rollout-safe setting for contract ``declared-output-completeness`` (GPT
    round-2 §3.4 / §5). ``enforce`` turns a missing EXACT declared output into a
    hard :class:`StageError`; ``off`` disables the check. An unrecognized value
    falls back to the default rather than erroring (a config typo must never be
    the thing that breaks a build).
    """
    section = config.get("implement") if isinstance(config, dict) else None
    if isinstance(section, dict) and "declared_output_completeness" in section:
        raw = section["declared_output_completeness"]
        value = str(raw).strip().lower()
        if value in _DECLARED_OUTPUT_COMPLETENESS_MODES:
            return value
    return DEFAULT_DECLARED_OUTPUT_COMPLETENESS


def _verify_task_contract(
    task: ImplementTaskRef,
    results: list[Any],
    project_root: Path,
    config: dict[str, Any],
    *,
    echo: Callable[[str], None] = lambda _m: None,
) -> None:
    """Raise :class:`StageError` if the task did not produce a declared KIND.

    No-op when the task declares no recognisable output kinds (skeleton /
    ``skip_generation`` / bare-name outputs) — that path must never false-RED.

    ALSO runs the declared-output-completeness check (contract
    ``declared-output-completeness``, GPT round-2 §3.4): a task that declared
    EXACT file paths in ``expected_outputs`` should have produced them. This is
    ``warn`` by default (echo only — does NOT hard-fail existing runs), and
    ``enforce`` only when ``implement.declared_output_completeness: enforce`` is
    set. The kind check below is UNCHANGED (still a hard gate).

    A declared output the active profile says the HARNESS owns (its CLOSED
    ``harness_owned_scaffold_paths`` — e.g. a C# ``src/<Pkg>/<Pkg>.csproj`` whose
    manifest lives under ``src/``) is created by the scaffold, never authored by
    the SUT, so it imposes NO source-kind / completeness obligation (the C#
    greenfield false-RED). EXACT, closed-set match only — a real SOURCE file the
    profile does not own stays gated (anti-false-green).
    """
    harness_owned = _harness_owned_outputs(config, project_root)
    _check_declared_output_completeness(
        task, results, project_root, config, echo=echo, harness_owned=harness_owned
    )

    required = _required_kinds(task, config, harness_owned=harness_owned)
    if not required:
        return
    generated: list[Any] = []
    for result in results:
        generated.extend(getattr(result, "generated_files", ()) or ())
    produced = _produced_kinds(generated, project_root, config)
    missing = required - produced
    if missing:
        raise StageError(
            f"task {task.task_id}: declared output kind(s) {sorted(required)} "
            f"but produced only {sorted(produced) or ['<none>']} "
            f"(missing {sorted(missing)}); the implementer did not generate the "
            f"intended artifact type"
        )


def _declared_output_is_file_path(raw: str) -> bool:
    """Whether a declared ``expected_outputs`` entry is an EXACT FILE PATH.

    Only real file paths participate in declared-output-completeness; a SYMBOL
    declaration (``Version.__str__``, ``Range.matches(version)``, ``module:range``)
    is left to the kind check. The old test — ``PurePosixPath(out).suffix`` non-empty
    — mis-classified symbols as files because a dotted symbol has a "suffix"
    (``.__str__`` / ``.matches(version)``), producing spurious WARNs (and a latent
    false-RED under ``enforce``). A real path has a ``/`` OR a plausible file
    extension (short + alphanumeric, e.g. ``.py`` / ``.yaml``)."""

    s = raw.strip().replace("\\", "/").strip("/")
    if not s:
        return False
    if ":" in s:
        return False  # node-id (``module:parser.parse`` / ``design:x``), not a file path
    # A GLOB (e.g. ``internal/httpapi/*_test.go``) is not an EXACT file path — like a
    # directory, it is left to the kind check. Its trailing segment carries a real
    # extension (``.go``), so without this guard it would be classed as a file and the
    # literal ``(root / glob).is_file()`` check below would false-flag a produced-but-
    # glob output as "absent on disk" (a WARN today, a false-RED under ``enforce``).
    if any(ch in s for ch in "*?["):
        return False
    # A file path is identified by a plausible file EXTENSION on its LAST segment.
    # A "/" alone is NOT sufficient: a multi-segment DIRECTORY declaration (e.g.
    # ``internal/httpapi`` / ``test/e2e`` — natural for Go, where a package IS a
    # directory) contains "/" but has no extension, and must be left to the kind
    # check. Treating it as an exact file made ``(root / dir).is_file()`` False and
    # falsely reported a produced directory as "absent on disk" (a WARN today, a
    # false-RED under ``enforce``).
    ext = PurePosixPath(s).suffix[1:]  # extension of the last path segment
    return bool(ext) and len(ext) <= 6 and ext.isalnum()


def _check_declared_output_completeness(
    task: ImplementTaskRef,
    results: list[Any],
    project_root: Path,
    config: dict[str, Any],
    *,
    echo: Callable[[str], None],
    harness_owned: frozenset[str] | None = None,
) -> None:
    """WARN-by-default: an EXACT declared ``expected_outputs`` path was not produced.

    Contract ``declared-output-completeness`` (GPT round-2 §3.4), registered
    ``enforcement=warn`` behind ``implement.declared_output_completeness``. Only
    EXACT file paths (a path with a file extension) participate — a directory or
    bare-name declaration is left to the kind check (the design's "directory
    output → at least one source/test file" is the kind gate). A declared file is
    "produced" when it is in the task's generated files OR exists on disk. Missing
    ones are WARNED (default) or, when ``enforce``, raised as a StageError.

    Default warn-only so this PR does NOT hard-fail existing runs; the warn
    signal is collected first (per GPT §5) before any future default-to-enforce.
    """
    mode = _declared_output_completeness_mode(config)
    if mode == "off":
        return
    declared_files: list[str] = []
    for out in task.expected_outputs:
        if not _declared_output_is_file_path(str(out)):
            continue
        rel = _norm_decl_path(out)
        # A profile-declared harness-owned scaffold artifact (e.g. a C# ``.csproj``)
        # is created by the harness, never authored by the SUT — it is not a
        # missing AI deliverable. EXACT, closed-set match only; a real source file
        # the profile does not own stays subject to the completeness check.
        if harness_owned and rel in harness_owned:
            continue
        declared_files.append(rel)
    if not declared_files:
        return

    try:
        root = Path(project_root).resolve()
    except OSError:
        root = Path(project_root)

    produced_rel: set[str] = set()
    for result in results:
        for raw in getattr(result, "generated_files", ()) or ():
            try:
                produced_rel.add(Path(raw).resolve().relative_to(root).as_posix())
            except (ValueError, OSError):
                produced_rel.add(PurePosixPath(str(raw).replace("\\", "/")).as_posix())

    missing: list[str] = []
    for rel in declared_files:
        if rel in produced_rel:
            continue
        if (root / rel).is_file():
            continue
        missing.append(rel)
    if not missing:
        return

    detail = (
        f"task {task.task_id}: declared expected output(s) {sorted(missing)} were "
        f"not produced (not generated and absent on disk)"
    )
    if mode == "enforce":
        raise StageError(
            detail + " — declared-output-completeness gate (enforce). The "
            "implementer must produce every declared artifact path."
        )
    echo(
        f"[greenfield] implement: declared-output-completeness (warn) — {detail}. "
        "Set implement.declared_output_completeness: enforce to make this a hard gate."
    )


def _output_paths_for_task(config: dict[str, Any], task: ImplementTaskRef) -> list[str]:
    import codd.cli as cli_module

    # Contract-aware routing: a TEST-only task whose declared outputs are not
    # already source-rooted should be written under the configured test dirs
    # (the design's intent), instead of falling through to the shared source
    # root. This keeps a CLI from laying tests inside the app's source tree.
    # SOURCE / MIXED / colocated-test / unknown tasks keep the existing default
    # so a source task that also emits a sibling test (e.g. core.py +
    # test_core.py) is never misrouted wholesale into tests/.
    routed = _test_only_output_paths(config, task)
    if routed:
        return routed
    explicit = cli_module._implement_output_paths_for_cli(config, task.task_id)
    # A-core: when the harness owns a layout profile and the task fell through to
    # the bare source-root default (``src``), route SOURCE output INTO the package
    # root (``src/<canonical_package>``) so the model writes a coherent src-layout
    # package — package-absolute imports work and the import-coherence gate passes.
    # The package name is the harness-owned CANONICAL name (config override >
    # derive-from-the-model's-actual-single-package > project-name default), so a
    # model that authored its own internally-coherent package (e.g. ``src/calc/``
    # while the project is ``calc-lib``) is reconciled to ITS name rather than
    # rejected and duplicated. We ALSO keep the bare ``source_root`` as an accepted
    # output destination so that package — and a model emitting straight into
    # ``src/<pkg>/`` — is never dropped as "outside output paths". An explicit
    # ``implement.default_output_paths`` / ``output_root`` is respected.
    return _route_source_into_package(config, explicit, project_root=_task_project_root(config, task))


def _task_project_root(config: dict[str, Any], task: ImplementTaskRef) -> Path | None:
    """Best-effort project root for derive-from-actual canonical resolution.

    ``ImplementTaskRef`` does not carry the project root, but the greenfield
    pipeline always operates relative to CWD for an unconfigured task, and the
    canonical resolver degrades safely to the project-name default when the root
    is wrong/absent (derive-from-actual simply finds no package). Prefer an
    explicit ``project.root`` if a project ever records one; else CWD.
    """
    project_section = config.get("project") if isinstance(config.get("project"), dict) else {}
    root = project_section.get("root") if isinstance(project_section, dict) else None
    if isinstance(root, str) and root.strip():
        return Path(root)
    try:
        return Path.cwd()
    except OSError:
        return None


def _root_module_output_paths(config: dict[str, Any]) -> list[str] | None:
    """Repo-root accept-list for a ROOT-MODULE language, or ``None``.

    Profile-DRIVEN (design §1.2/§1.6): resolve the project's
    :class:`~codd.languages.profile.LanguageProfile` via the registry and check
    ``layout.package_root.kind``. A profile whose ``package_root.kind == "none"``
    (Go and any other root-module language — go.mod / cmd/ / internal/ live at the
    repo root, there is NO single ``src/<pkg>`` source root) must NOT have its
    declared outputs routed under a ``src/`` prefix. For such a language the only
    coherent accept-list is the REPO ROOT itself (``"."``): an empty-component
    prefix matches every declared output path, so ``cmd/server/main.go`` /
    ``internal/store/store.go`` / ``go.mod`` are accepted at their canonical
    repo-relative paths (the single authority being
    :class:`~codd.languages.path_planner.PathPlanner`) instead of forced under
    ``src/`` and dropped/rerooted.

    Returns ``None`` (caller keeps the EXACT legacy ``source_root``→``package_root``
    behavior, byte-identical) when:

    * the profile has a single source/package root (``named_package`` /
      ``path_root`` — Python/TS/node), OR
    * no language is configured / the language has no profile, OR
    * profile resolution fails for any reason.

    This is the only place the routing diverges for root-module languages, and it
    diverges off ``package_root.kind`` — NOT a ``language == "go"`` literal — so a
    future root-module language (declared with ``package_root.kind: none``)
    inherits the correct repo-root routing with zero pipeline changes.
    """
    project_section = config.get("project") if isinstance(config.get("project"), dict) else {}
    language = project_section.get("language") if isinstance(project_section, dict) else None
    if not isinstance(language, str) or not language.strip():
        return None
    try:
        from codd.languages.registry import default_registry

        profile = default_registry.resolve(language)
    except Exception:  # noqa: BLE001 — no/unknown profile ⇒ keep legacy routing.
        return None
    if profile.layout.package_root.kind != "none":
        return None
    return [profile.layout.repo_root or "."]


def _route_source_into_package(
    config: dict[str, Any], explicit: list[str], *, project_root: Path | None = None
) -> list[str]:
    # ROOT-MODULE languages (profile ``package_root.kind == "none"``, e.g. Go)
    # are routed to the repo root, never under ``src/`` (design §1.6). This is
    # resolved from the declarative LanguageProfile and takes precedence over the
    # legacy single-source-root LayoutProfile path below. For every language with
    # a single source root (Python/TS/node) — and any language without a profile —
    # this returns None and the behavior below is byte-identical to before.
    root_module = _root_module_output_paths(config)
    if root_module is not None:
        return root_module
    profile = _resolve_layout_profile_from_config(config, project_root=project_root)
    if profile is None:
        return explicit
    source_root = profile.source_root
    package_root = profile.package_root
    rerouted: list[str] = []
    targets_source = False
    for path in explicit:
        norm = str(path).strip().replace("\\", "/").strip("/")
        # Upgrade the BARE source root itself (e.g. "src") to the CANONICAL package
        # root; leave any more specific path (already under the package, a test
        # dir, or an explicit subpath) exactly as chosen.
        if norm == source_root:
            rerouted.append(package_root)
            targets_source = True
        else:
            rerouted.append(path)
            if norm == package_root or norm.startswith(package_root + "/") or norm.startswith(
                source_root + "/"
            ):
                targets_source = True
    if not targets_source:
        return explicit
    # Accept the bare source_root too: a model that authored its OWN coherent
    # single package (``src/<its-name>/``) lands under source_root and must NOT be
    # dropped as "outside output paths" (the calc/calc_lib duplication root cause).
    # The canonical package_root stays the primary destination; the verify-phase
    # gate + scaffold reconcile every artifact to the canonical name.
    if source_root not in rerouted:
        rerouted.append(source_root)
    # Implementing a module legitimately also emits its tests; allow the test
    # root so generated tests land under tests/ (where the coherence gate + the
    # runner's testpaths expect them) instead of being dropped as "outside output
    # paths". The package root stays the source destination.
    if profile.test_root not in rerouted:
        rerouted.append(profile.test_root)
    return rerouted


def _resolve_layout_profile_from_config(config: dict[str, Any], *, project_root: Path | None = None):
    from codd.project_types import resolve_layout_profile

    project_section = config.get("project") if isinstance(config.get("project"), dict) else {}
    language = project_section.get("language") if isinstance(project_section, dict) else None
    project_name = project_section.get("name") if isinstance(project_section, dict) else None
    scan = config.get("scan") if isinstance(config.get("scan"), dict) else {}
    source_dirs = scan.get("source_dirs") if isinstance(scan, dict) else None
    test_dirs = scan.get("test_dirs") if isinstance(scan, dict) else None
    return resolve_layout_profile(
        language=language,
        project_name=project_name,
        source_dirs=source_dirs,
        test_dirs=test_dirs,
        config=config,
        project_root=project_root,
    )


def _test_only_output_paths(config: dict[str, Any], task: ImplementTaskRef) -> list[str] | None:
    required = _required_kinds(task, config)
    if required != {_KIND_TEST}:
        return None
    source_roots = _scan_roots(config, "source_dirs")
    # If any declared output is already source-rooted (a colocated test), respect
    # the declaration rather than forcing everything into tests/.
    if any(_path_under_root(str(output), source_roots) for output in task.expected_outputs):
        return None
    test_dirs = config.get("scan") if isinstance(config.get("scan"), dict) else {}
    raw = test_dirs.get("test_dirs") if isinstance(test_dirs, dict) else None
    if not isinstance(raw, list):
        return None
    paths = [str(item).strip() for item in raw if str(item).strip()]
    return paths or None


def _derive_and_approve_steps(
    project_root: Path,
    config: dict[str, Any],
    task: ImplementTaskRef,
    output_paths: list[str],
    *,
    ai_command: str | None,
    echo: Callable[[str], None],
) -> int:
    """``codd implement plan --task T`` + ``codd implement steps --task T --approve --all``.

    Advisory: implementation works without derived steps, so any failure here
    is reported and skipped instead of failing the task.
    """
    try:
        import codd.cli as cli_module
        from codd.deployment.providers.ai_command_factory import get_ai_command
        from codd.implementer import ImplementSpec
        from codd.llm.impl_step_deriver import (
            IMPL_STEP_DERIVERS,
            approve_cached_impl_steps,
            impl_step_cache_path,
            read_impl_step_cache,
        )

        provider_name = cli_module._impl_step_provider(config)
        deriver_cls = IMPL_STEP_DERIVERS.get(provider_name)
        if deriver_cls is None:
            echo(f"[greenfield] implement plan skipped for {task.task_id}: provider not found: {provider_name}")
            return 0
        spec = ImplementSpec(design_node=task.design_node, output_paths=output_paths)
        nodes = cli_module._plan_design_doc_nodes(project_root, ())
        command = ai_command or cli_module._impl_step_command(config)
        deriver = deriver_cls(get_ai_command(config, project_root, command_override=command))
        deriver.derive_steps(
            spec,
            nodes,
            {
                "project_root": project_root,
                "force": False,
                "dry_run": False,
                "write_cache": True,
                "config": config,
                "project_context": {"project": config.get("project", {})},
            },
        )
        cache_path = impl_step_cache_path(spec, {"project_root": project_root})
        if read_impl_step_cache(cache_path) is None:
            return 0
        return approve_cached_impl_steps(cache_path, approve_all=True)
    except Exception as exc:  # noqa: BLE001 — step derivation is advisory.
        echo(f"[greenfield] implement plan skipped for {task.task_id} (non-blocking): {exc}")
        return 0


def _enforce_stage_coverage_gate(
    project_root: Path,
    *,
    coverage_gate: bool,
    echo: Callable[[str], None],
    rerun: Callable[[str, Any], None] | None = None,
    rerun_oracle: Callable[[], None] | None = None,
    scope_resolver: Callable[[list[str]], Any] | None = None,
    authenticity_profile: Any = None,
) -> None:
    """Project-wide verifiable-behavior coverage + authenticity gate (implement STAGE).

    Runs ONCE after every implement task has completed, so all covering tests
    already exist. This is the correct granularity for the project-wide VB
    audit: it reconciles every VB id declared across the test documents against
    ``codd: covers vb=`` markers anywhere in the suite. (The per-task gate is
    deliberately disabled in :meth:`GreenfieldPipeline._default_implement_task_runner`
    — an early fixtures/helper task that writes no covering tests would
    otherwise hard-fail against the whole project's VBs.)

    Two gates, both HARD (raise :class:`StageError` on failure):

    1. **Coverage** (:func:`run_implement_coverage_gate`): every declared VB has a
       ``covers``/``blocked`` marker. When a ``rerun`` callback is wired, an
       uncovered gap drives a bounded, TEST-SCOPED re-implementation with gap
       feedback (the previously-dormant feedback loop, now live) before failing.
    2. **Authenticity** (:func:`build_authenticity_report`): each ``covers``
       marker is a *credible* claim — attached to an executable test block that
       contains an assertion (anti-false-green; "add a marker" cannot be
       satisfied by marking an empty/skipped test). Gracefully degrades for
       stacks/files it cannot structurally parse.

    ``rerun(feedback, scope)`` re-implements the scoped TEST tasks under a
    write-fence (source is never edited by a VB rerun). ``scope_resolver`` maps
    the uncovered VB source docs to that scope. ``rerun_oracle`` re-runs the
    native implement-oracle AFTER a VB rerun (a test edit can break a helper
    symbol; the pipeline order is oracle→VB, so the oracle must be re-asserted).

    Honors the greenfield ``coverage_gate`` option and the project-level
    ``test_coverage.gate`` config.
    """
    from codd.config import load_project_config
    from codd.verifiable_behavior_audit import (
        build_vb_coverage_audit,
        coverage_gate_enabled,
        coverage_gate_max_retries,
        format_gap_feedback,
        project_expects_vb_registry,
        run_implement_coverage_gate,
    )

    if not coverage_gate:
        return

    try:
        config = load_project_config(project_root)
    except (FileNotFoundError, ValueError):
        config = {}

    if not coverage_gate_enabled(config):
        return

    # GREENFIELD-ONLY empty-registry hard-fail (rear guard behind the generate
    # gate). For a project that is EXPECTED to own a canonical VB registry (it
    # plans/declares one — see project_expects_vb_registry), an empty audit means
    # NO verifiable behaviors were declared, so orphan never fires and the
    # marker-authenticity gate has nothing to judge — verify could pass over a
    # build whose VB-coverage contract was never generated. Fail honestly here.
    # This does NOT change the brownfield run_implement_coverage_gate
    # "nothing to audit -> pass" rule (that path is untouched); it gates ONLY the
    # greenfield autopilot, and ONLY when a VB surface was expected — a minimal
    # greenfield project with no VB surface still passes.
    if project_expects_vb_registry(project_root, config):
        if not build_vb_coverage_audit(project_root, config=config).rows:
            raise StageError(
                "greenfield requires a non-empty canonical VB registry in "
                "docs/test/test_strategy.md; no verifiable behaviors were declared, "
                "so coverage/authenticity cannot certify the generated system."
            )

    # Pass the configured test dirs as the audited output paths so the gate
    # treats this as a test-related run (it is — every test the build will ever
    # have now exists) and evaluates the FULL project VB universe in one pass.
    test_dirs = (config.get("scan") or {}).get("test_dirs")
    if isinstance(test_dirs, list) and test_dirs:
        audited_paths = [str(item) for item in test_dirs]
    else:
        audited_paths = ["tests/"]

    # --- Gate 1: coverage, with the (now-live) TEST-scoped feedback rerun. ---
    if rerun is not None and scope_resolver is not None:
        # Drive the bounded feedback loop HERE so each rerun is TEST-scoped (the
        # scope is recomputed from the current gap) and the native oracle is
        # re-asserted after each test edit. ``run_implement_coverage_gate`` is
        # then called once more with rerun=None to produce the final verdict +
        # gap reporting (no second loop).
        max_retries = coverage_gate_max_retries(config)
        report = build_vb_coverage_audit(project_root, config=config)
        attempt = 0
        while report.rows and report.uncovered_rows and attempt < max_retries:
            attempt += 1
            uncovered_docs = sorted({row.source_doc for row in report.uncovered_rows if row.source_doc})
            scope = scope_resolver(uncovered_docs)
            echo(
                f"Test coverage gate: {len(report.uncovered_rows)} uncovered verifiable behavior(s); "
                f"re-running TEST tasks with gap feedback (attempt {attempt}/{max_retries})"
            )
            try:
                rerun(format_gap_feedback(report), scope)
                if rerun_oracle is not None:
                    # A VB test rerun can break test↔helper symbol coherence;
                    # re-assert the native oracle so a type/import break never
                    # rides into verify.
                    rerun_oracle()
            except Exception as exc:  # noqa: BLE001
                # A rerun that cannot proceed (e.g. a codegen/environment error)
                # must NOT mask the gate's verdict: log and stop the bounded loop,
                # then fall through to the final coverage audit, which fails
                # HONESTLY on whatever VBs are still uncovered. (The oracle gate
                # treats its rerun failures as hard because they ARE the defect;
                # here the authority is the coverage audit below, not the rerun.)
                echo(
                    f"Test coverage gate: re-run attempt {attempt} could not complete ({exc}); "
                    "stopping the feedback loop and evaluating coverage as-is."
                )
                break
            report = build_vb_coverage_audit(project_root, config=config)

    passed = run_implement_coverage_gate(
        project_root,
        config=config,
        design_node=None,
        output_paths=audited_paths,
        opt_out=not coverage_gate,
        rerun=None,  # the scoped loop (above) already ran; this is the final verdict
        echo=echo,
        echo_error=echo,
    )
    if not passed:
        raise StageError(
            "verifiable-behavior coverage gate failed for the implement stage: "
            "one or more declared verifiable behaviors have no `codd: covers vb=` "
            "marker after all implement tasks completed (see the gap list above)"
        )

    # --- Gate 2: marker authenticity (anti-false-green). HARD gate. ---
    from codd.vb_marker_authenticity import build_authenticity_report

    # strict_observability (authenticity.observable_in_supported_stack.v1): in the
    # autopilot a SUPPORTED test file the adapter recognizes but parses no executable
    # test block out of is a false-green, not a degrade — honest-fail it.
    auth = build_authenticity_report(
        project_root, config=config, profile=authenticity_profile, strict_observability=True
    )
    if auth.degraded_paths:
        echo(
            "Test coverage gate: marker-authenticity attachment/assertion checks skipped for "
            f"{len(auth.degraded_paths)} un-parseable file(s) (stage-1 orphan check still applied): "
            + ", ".join(auth.degraded_paths)
        )
    if not auth.passed:
        for violation in auth.violations:
            echo(violation.message)
        raise StageError(
            "verifiable-behavior marker-authenticity gate failed for the implement stage: "
            f"{len(auth.violations)} `codd: covers vb=` marker(s) are not credible coverage claims "
            "(attached to a skipped/empty test, an orphan id, a test with no assertion, or an "
            "unobservable test structure — a recognized file with no parseable test). A covers "
            "marker must sit on an executable test that asserts the behavior."
        )
    echo(
        f"Test coverage gate: marker authenticity OK ({len(auth.degraded_paths)} file(s) stage-1-only)."
    )


def _format_verify_failure_lines(failures) -> list[str]:
    """Inline-loggable summary of verify failures (failure observability).

    One line per failure naming the check + source + first message line, plus any
    individual failing tests embedded in the message (go/pytest print ``--- FAIL:``
    / ``FAILED`` lines). The C-Go greenfield dogfood surfaced the need: a bare
    ``verify failed (1 failure(s))`` forced a dig into ``.codd/repair_history`` to
    learn it was ``go test`` with three failing tests — and re-observing a dogfood
    failure means re-running the SUT, which burns budget. Pure + side-effect-free
    so the caller controls the log prefix; defensive getattr tolerates any failure
    shape.
    """
    out: list[str] = []
    for f in failures:
        lines = [ln for ln in (getattr(f, "message", "") or "").splitlines() if ln.strip()]
        first = lines[0].strip()[:200] if lines else "(no message)"
        out.append(f"  - {getattr(f, 'check_name', '?')} [{getattr(f, 'source', '?')}]: {first}")
        embedded = [
            ln.strip()
            for ln in lines
            if "--- FAIL" in ln or ln.strip().startswith("FAILED ")
        ][:8]
        out.extend(f"      failing: {ln[:160]}" for ln in embedded)
    return out


def _default_verify_runner(
    project_root: Path,
    *,
    ai_command: str | None,
    max_repair_attempts: int,
    echo: Callable[[str], None],
) -> str:
    """``codd verify --auto-repair --max-attempts N --repair-mode automatic``."""
    from codd.repair.verify_runner import run_standalone_verify

    result = run_standalone_verify(project_root)
    if result.passed:
        return _certify_verify_executed(project_root, result)

    echo(f"[greenfield] verify failed ({len(result.failures)} failure(s)); starting automatic repair")
    for _line in _format_verify_failure_lines(result.failures):
        echo(f"[greenfield]{_line}")
    from codd.config import load_project_config
    from codd.dag import DAG
    from codd.dag.builder import build_dag
    from codd.deployment.providers.ai_command import SubprocessAiCommand
    from codd.repair import RepairLoop, RepairLoopConfig
    from codd.repair.approval_repair import apply_repair_mode
    from codd.repair.schema import VerificationFailureReport

    config = apply_repair_mode(load_project_config(project_root), "automatic")
    repair_section = config.get("repair") if isinstance(config.get("repair"), dict) else {}
    try:
        dag = build_dag(project_root)
    except (FileNotFoundError, ValueError):
        dag = DAG()
    failure = result.failure or VerificationFailureReport(
        check_name="verify",
        failed_nodes=[],
        error_messages=[item.message for item in result.failures],
        dag_snapshot={},
        timestamp=_utc_now(),
    )
    loop_config = RepairLoopConfig(
        max_attempts=max(1, int(max_repair_attempts)),
        approval_mode=str(repair_section.get("approval_mode") or "auto"),  # type: ignore[arg-type]
        history_dir=Path(str(repair_section.get("history_dir") or ".codd/repair_history")),
        engine_name=str(repair_section.get("engine_name") or repair_section.get("engine") or "llm"),
        llm_client=SubprocessAiCommand(command=ai_command, project_root=project_root, config=config),
        repo_path=project_root,
        # Thread the per-run automatic opt-in (apply_repair_mode set
        # repair.allow_auto.require_explicit_optin=true on this copy) to the
        # approval gate. Without this the loop re-reads codd.yaml from disk —
        # which on a fresh greenfield project has no repair: section — and the
        # autopilot dies at REPAIR_FAILED instead of self-healing.
        codd_yaml=config,
    )
    outcome = RepairLoop(loop_config, project_root).run(
        failure,
        dag,
        verify_callable=lambda: run_standalone_verify(project_root),
        initial_verify_result=result,
    )
    if outcome.status != "REPAIR_SUCCESS":
        raise StageError(
            f"verification failed and automatic repair ended with {outcome.status} "
            f"(history: {outcome.history_session_dir})"
        )
    # Repair declared success from its own verify_callable; re-run standalone
    # verify once so the executed-anything honesty gate also covers the
    # post-repair state (a repair that fixed the DAG but left the build with
    # nothing executable must not be certified either).
    final = run_standalone_verify(project_root)
    if not final.passed:
        raise StageError(
            f"automatic repair reported success but a fresh verification failed "
            f"({len(final.failures)} failure(s))"
        )
    _certify_verify_executed(project_root, final)
    return f"verification passed after automatic repair ({len(outcome.attempts)} attempt(s))"


def _certify_verify_executed(project_root: Path, result: Any) -> str:
    """The greenfield half of the FX3 honesty rule.

    Plain ``codd verify`` keeps "structural-only pass" as a pass-with-WARNING
    (existing brownfield/CI configs may intentionally gate only document
    coherence, with the test suite running in another pipeline stage). The
    autopilot has no such excuse: it just built the system unattended and is
    about to certify it, so "verification executed nothing" is a stage
    FAILURE unless the project explicitly opted in via
    ``verify.allow_structural_only: true``.
    """
    from codd.config import load_project_config
    from codd.repair.verify_runner import structural_only_allowed

    if getattr(result, "executed_anything", True):
        evidence = f" (tests executed: {result.test_command})" if getattr(result, "tests_executed", False) else ""
        return f"verification passed{evidence}"
    try:
        config = load_project_config(project_root)
    except (FileNotFoundError, ValueError):
        config = {}
    if structural_only_allowed(config):
        return "verification passed (structural-only, allowed by verify.allow_structural_only)"
    raise StageError(
        "verify executed nothing (no test command detected, no typecheck command "
        "configured, no runtime verification nodes) — autopilot cannot certify an "
        "unexecuted build. Set verify.test_command in codd.yaml, add a detectable "
        "test setup (pytest config, package.json test script, Cargo.toml, go.mod, "
        "Makefile test target), or set verify.allow_structural_only: true to "
        "accept structural-only verification."
    )


def _ci_setup_steps(project_root: Path) -> list[dict[str, Any]]:
    """CI toolchain setup steps from the resolved language profile (``profile.ci``).

    Contract-driven (Contract Kernel, v2.70): the steps come from the project's
    language profile — the pipeline core does NOT branch on a language name or a
    marker file. No CoDD config, no resolvable language, or no ``ci`` section →
    no setup steps (the workflow still runs checkout + the real test command; an
    unsupported ecosystem simply gets no toolchain bootstrap, the honest
    pluggable default). The per-marker hardcoded table (v2.67.0) is gone.
    """
    from codd.config import load_project_config
    from codd.languages import resolve_language_profile

    try:
        config = load_project_config(project_root)
    except (FileNotFoundError, ValueError):
        return []
    try:
        profile = resolve_language_profile(config)
    except Exception:  # noqa: BLE001 — unknown/unsupported language → no setup, never a crash.
        return []
    if profile is None or profile.ci is None:
        return []
    return [dict(step) for step in profile.ci.setup_steps]


def _ci_opt_out_declared(project_root: Path) -> bool:
    """True when codd.yaml declares ``ci.provider=none`` — an explicit opt-out
    that the ci_health gate handles itself; scaffolding a workflow would
    contradict the author's declaration."""
    from codd.config import load_project_config

    try:
        config = load_project_config(project_root)
    except (FileNotFoundError, ValueError):
        return False
    ci = config.get("ci")
    if not isinstance(ci, dict):
        return False
    return str(ci.get("provider", "")).strip().lower() == "none"


def _default_ci_scaffold_runner(project_root: Path, *, ai_command: str | None = None) -> str:
    """Author a minimal, authentic CI workflow so a freshly built system is
    CI-ready and the final ``check`` gate's ci_health requirement is satisfied
    honestly (surfaced by the C-Go greenfield dogfood, where a clean build
    failed ``check`` on ``ci_workflow_missing``).

    DETERMINISTIC by design, not AI-freeform (which could emit a hollow
    ``run: true`` that games ci_health) and not an auto-opt-out (which would
    silently declare the system needs no CI). The workflow runs the project's
    REAL test command — :func:`codd.test_detection.detect_test_command`, the
    SAME source the verify stage uses — so CI authenticity == verify
    authenticity by construction. A project that reached this stage green
    necessarily has a detectable test command (verify ran it), so the scaffold
    succeeds for exactly the projects verify could validate.

    Idempotent: an existing workflow, or an explicit ``ci.provider=none``
    opt-out, is left untouched.
    """
    del ai_command  # signature parity with sibling runners; no AI call needed.
    from codd.test_detection import detect_test_command

    existing = sorted(project_root.glob(".github/workflows/*.yml")) + sorted(
        project_root.glob(".github/workflows/*.yaml")
    )
    if existing:
        rel = existing[0].relative_to(project_root).as_posix()
        return f"skipped — CI workflow already present ({rel})"

    if _ci_opt_out_declared(project_root):
        return "skipped — ci.provider=none opt-out declared in codd.yaml"

    test_command = detect_test_command(project_root)
    if not test_command:
        raise StageError(
            "ci_scaffold: cannot determine the project's test command, so an "
            "authentic CI workflow cannot be authored. Declare verify.test_command "
            "in codd.yaml, or set ci.provider=none with an opt_outs entry to opt out."
        )

    steps: list[dict[str, Any]] = [{"uses": "actions/checkout@v4"}]
    steps.extend(_ci_setup_steps(project_root))
    steps.append({"run": test_command})

    workflow: dict[str, Any] = {
        "name": "ci",
        # The trigger key is the string "on"; PyYAML quotes it on dump and
        # ci_health reads both the quoted-string and the YAML-1.1-bool form.
        "on": {"push": None, "pull_request": None},
        "jobs": {"test": {"runs-on": "ubuntu-latest", "steps": steps}},
    }

    workflow_path = project_root / ".github" / "workflows" / "ci.yml"
    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    workflow_path.write_text(
        yaml.safe_dump(workflow, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    return f"generated .github/workflows/ci.yml (runs: {test_command})"


def _default_propagate_runner(project_root: Path, *, ai_command: str | None) -> str:
    """``codd propagate --verify`` then ``codd propagate --commit``.

    Uses the greenfield/fresh-build diff window
    (:data:`codd.propagator.GREENFIELD_BUILD_DIFF_TARGET`), NOT a plain
    ``HEAD`` diff. A just-built project normally has no commits and all
    generated files untracked, so ``git diff HEAD`` would see nothing and
    propagate would reconcile ZERO docs while the entire generated build sits
    unreconciled (false-green). The build window also includes untracked
    artifacts under the configured source/doc dirs, so propagate reconciles the
    real generated source<->design.
    """
    from codd.propagator import GREENFIELD_BUILD_DIFF_TARGET, run_commit, run_verify

    run_verify(project_root, GREENFIELD_BUILD_DIFF_TARGET, ai_command=ai_command)
    result = run_commit(project_root, reason="codd greenfield autopilot")
    return (
        f"committed={len(result.committed_files)}, "
        f"knowledge={getattr(result, 'knowledge_recorded', 0)}"
    )


def _default_check_runner(project_root: Path) -> str:
    """``codd check`` — the aggregated final health gate."""
    import codd.cli as cli_module

    try:
        cli_module.check_cmd.callback(
            project_path=str(project_root),
            run_full=False,
            apply_fixes=False,
            output_format="text",
        )
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        if code != 0:
            raise StageError(f"codd check failed (exit {code}); run codd check for details") from exc
    return "health check passed"


# ═══════════════════════════════════════════════════════════
# Small shared helpers
# ═══════════════════════════════════════════════════════════

def _resolve_project_root(value: Path | str) -> Path:
    project_root = Path(value).expanduser().resolve()
    if not project_root.exists():
        raise FileNotFoundError(f"target path not found: {project_root}")
    if not project_root.is_dir():
        raise NotADirectoryError(f"target path is not a directory: {project_root}")
    return project_root


def _stage_outcomes(session: dict[str, Any]) -> list[StageOutcome]:
    outcomes: list[StageOutcome] = []
    for name in STAGES:
        record = session.get("stages", {}).get(name) or {}
        outcomes.append(
            StageOutcome(
                name=name,
                status=str(record.get("status") or STATUS_PENDING),
                detail=str(record.get("detail") or ""),
                units={str(key): str(value) for key, value in (record.get("units") or {}).items()},
            )
        )
    return outcomes


def _first_failed_unit(record: dict[str, Any]) -> str | None:
    for unit, status in (record.get("units") or {}).items():
        if status == STATUS_FAILED:
            return str(unit)
    return None


__all__ = [
    "DEFAULT_OPTIONS",
    "GreenfieldPipeline",
    "GreenfieldResult",
    "ImplementTaskRef",
    "STAGES",
    "StageError",
    "StageOutcome",
    "format_greenfield_result",
    "load_session",
    "new_session",
    "save_session",
    "session_path",
]
