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

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
import shlex
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

    ``title``/``description`` carry the same DerivedTask's own scoping text.
    ``design_node`` for a derived task is its ``source_design_doc`` — the
    document the implement prompt reads — and MULTIPLE derived tasks routinely
    share one design doc (a doc's Follow-ups section commonly spawns several
    small tasks). Without ``title``/``description`` the implement prompt has no
    way to tell the model which SLICE of that shared document this particular
    invocation owns, so the model can only guess from the document's full text
    — and may act on an unrelated section (or on the document's OTHER derived
    task) instead of this one. Empty for a configured ``implement_targets``
    mapping (no DerivedTask exists to draw them from).
    """

    task_id: str
    design_node: str
    output_paths: tuple[str, ...] | None = None
    source: str = "configured"
    expected_outputs: tuple[str, ...] = ()
    test_kinds: tuple[str, ...] = ()
    title: str = ""
    description: str = ""
    #: The planner's task-level ``dependencies`` (production-graph edges). FIX-1
    #: (Fable5 ts-v9 ruling): the owner index carries these so the repair
    #: campaign's ``task_dependency_order`` regenerates producers before consumers.
    #: Empty for a configured ``implement_targets`` mapping (no DerivedTask).
    dependencies: tuple[str, ...] = ()


# DI seam signatures (all keyword-overridable on the pipeline constructor).
InitRunner = Callable[..., Any]
ElicitRunner = Callable[..., str]
PlanRunner = Callable[..., int]
WaveLister = Callable[[Path], list[int]]
GenerateWaveRunner = Callable[..., str]
TaskLister = Callable[[Path], list[ImplementTaskRef]]
TaskDeriver = Callable[..., int]
#: Injectable seam listing the project's design-doc nodes — the DERIVATION
#: declaration universe the design-doc→task closure gate diffs against. Default
#: (None) uses ``_default_design_doc_lister`` (the CLI's design-doc DAG loader);
#: tests inject a scripted node list so the closure set-math is exercised with no
#: real DAG build.
DesignDocLister = Callable[[Path], list[Any]]
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
        design_doc_lister: DesignDocLister | None = None,
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
        self.design_doc_lister = design_doc_lister
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
        # Deterministic, default-permissive, fail-safe requirements intake: iff the
        # resolved profile declares >=1 optional deliverable surface, classify which
        # (if any) the requirements exclude and persist them BEFORE the plan runs.
        self._classify_deliverable_surfaces(project_root)
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

    def _classify_deliverable_surfaces(self, project_root: Path) -> None:
        """Plan-stage intake: deterministically classify which OPTIONAL deliverable
        surfaces the requirements exclude, and persist the excluded ids to codd.yaml.

        Runs at the top of the plan stage IFF the resolved profile declares >=1
        optional deliverable surface. Deterministic, default-permissive, fail-safe:
        a surface is excluded ONLY when the model both marks it excluded AND cites a
        verbatim substring of the requirements as evidence; silence, ambiguity, a
        parse failure, or ANY exception leaves the excluded set empty (legacy —
        every optional surface is scaffolded). The whole method is wrapped so a
        classification failure can never break planning; nothing is written when the
        excluded set is empty.
        """
        try:
            profile = self._resolve_layout_profile(project_root)
            if profile is None or not getattr(profile, "optional_surfaces", ()):
                return  # no optional surfaces => nothing to classify (no-op)

            # NB-1: honor a PRE-EXISTING exclusion decision before any AI call.
            # If the RAW project codd.yaml already carries a NON-EMPTY
            # ``deliverable.excluded_surfaces`` — an owner's MANUAL edit OR a prior
            # plan's persisted decision — RETURN EARLY: no re-classification, no AI
            # invocation, no overwrite. This makes a forced plan re-run idempotent
            # and unable to clobber the owner's edit. Verified safe: ``codd init``
            # does NOT materialize this key (it lives only in defaults.yaml, merged
            # at load), so the presence-check can never dead-arm the intake on a
            # fresh project. Fail-safe: ANY read error falls through to
            # classification (legacy), NOT the outer skip.
            try:
                from codd.config import find_codd_dir

                _existing_dir = find_codd_dir(project_root)
                _existing = (
                    yaml.safe_load((_existing_dir / "codd.yaml").read_text(encoding="utf-8"))
                    if _existing_dir is not None
                    else None
                )
                _section = _existing.get("deliverable") if isinstance(_existing, dict) else None
                _prior = _section.get("excluded_surfaces") if isinstance(_section, dict) else None
                if isinstance(_prior, list) and _prior:
                    self.echo(
                        "[greenfield] plan: deliverable-surface intake honored a "
                        "pre-existing exclusion decision (no re-classification): "
                        f"{', '.join(str(s) for s in _prior)}"
                    )
                    return
            except Exception:  # noqa: BLE001 — read error => classify (legacy), never skip.
                pass

            from codd.elicit.engine import _collect_requirements

            requirements = _collect_requirements(project_root, max_chars=40000)
            if not requirements or requirements.strip() == "(none provided)":
                return  # no requirements text => nothing to classify (legacy)

            surfaces = list(profile.optional_surfaces)
            prompt = self._build_deliverable_surface_intake_prompt(requirements, surfaces)

            from codd.config import load_project_config
            from codd.deployment.providers.ai_command_factory import get_ai_command
            from codd.llm.plan_deriver import strip_json_fence

            try:
                config = load_project_config(project_root)
            except (FileNotFoundError, ValueError):
                config = {}

            try:
                raw = get_ai_command(
                    config, project_root, command_override=self.ai_command
                ).invoke(prompt)
                verdicts = json.loads(strip_json_fence(raw))
            except Exception:  # noqa: BLE001 — any invoke/parse failure => legacy (nothing excluded).
                verdicts = {}

            excluded: set[str] = set()
            if isinstance(verdicts, Mapping):
                valid_ids = {s.id for s in surfaces}
                for surface_id, verdict in verdicts.items():
                    if str(surface_id) not in valid_ids or not isinstance(verdict, Mapping):
                        continue  # unknown id / malformed entry ignored
                    if verdict.get("excluded") is not True:
                        continue
                    # DETERMINISTIC GUARD: honor the exclusion ONLY when the evidence
                    # is a non-empty VERBATIM (case-sensitive) substring of the
                    # requirements text — a model assertion with no textual support
                    # is treated as NOT excluded (default-permissive).
                    evidence = verdict.get("evidence")
                    if isinstance(evidence, str) and evidence.strip() and evidence in requirements:
                        excluded.add(str(surface_id))

            if not excluded:
                self.echo(
                    "[greenfield] plan: deliverable-surface intake excluded none "
                    "(legacy — every optional surface is scaffolded)"
                )
                return

            self._persist_excluded_deliverable_surfaces(project_root, sorted(excluded))
            self.echo(
                "[greenfield] plan: deliverable-surface intake excluded "
                f"{len(excluded)} surface(s): {', '.join(sorted(excluded))}"
            )
        except Exception as exc:  # noqa: BLE001 — classification must NEVER break planning.
            self.echo(
                f"[greenfield] plan: deliverable-surface intake skipped (non-blocking): {exc}"
            )
            return

    def _build_deliverable_surface_intake_prompt(
        self, requirements: str, surfaces: list[Any]
    ) -> str:
        """Strict-JSON classification prompt asking which optional deliverable
        surfaces the requirements exclude. Generic: each surface's id and
        description are profile DATA, never a hardcoded surface literal."""
        lines = [
            "You are classifying whether a software project's requirements EXCLUDE "
            "certain OPTIONAL deliverable surfaces from the project's scope.",
            "",
            "A surface is EXCLUDED only when the requirements EXPLICITLY state the "
            "project does not provide it. Silence, ambiguity, or a positive mention "
            "means NOT excluded.",
            "",
            "Optional surfaces (id -- description):",
        ]
        for surface in surfaces:
            lines.append(f"- {surface.id} -- {surface.description}")
        lines.extend(
            [
                "",
                "Requirements text:",
                "<<<REQUIREMENTS",
                requirements,
                "REQUIREMENTS",
                "",
                "Respond with STRICT JSON only (no prose, no code fence): an object "
                "keyed by surface id, each value an object "
                '{"excluded": <true|false>, "evidence": "<verbatim quote copied from '
                'the requirements that justifies exclusion, or an empty string>"}.',
                "The evidence MUST be an exact, verbatim substring of the requirements "
                'text above. If a surface is not excluded, use "excluded": false and '
                '"evidence": "".',
            ]
        )
        return "\n".join(lines)

    def _persist_excluded_deliverable_surfaces(
        self, project_root: Path, excluded: list[str]
    ) -> None:
        """Write the excluded surface-id list to the project codd.yaml under
        ``deliverable.excluded_surfaces`` (non-destructive, idempotent). Only called
        with a NON-EMPTY set — an empty set leaves the config untouched (legacy)."""
        from codd.config import find_codd_dir

        codd_dir = find_codd_dir(project_root)
        if codd_dir is None:
            return
        config_path = codd_dir / "codd.yaml"
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            return
        section = data.setdefault("deliverable", {})
        if not isinstance(section, dict):
            section = {}
            data["deliverable"] = section
        section["excluded_surfaces"] = sorted(excluded)
        config_path.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
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

        # Derive-stage API-facade coverage gate (before scaffold + per-task loop,
        # and strictly before the owner-uniqueness gate): guarantee exactly one
        # derived task owns each SUT-authored package-facade file, re-deriving with
        # deterministic feedback if not. STRICT NO-OP for a stack with no facade.
        tasks = self._enforce_api_facade_coverage(project_root, tasks)

        # Derive-stage deliverable-surface exclusion fence (immediately after the
        # facade gate, before the scaffold + per-task loop): no derived task may
        # author an OPTIONAL deliverable surface the plan-stage intake marked
        # out-of-scope. STRICT NO-OP unless the resolved profile reports excluded
        # surfaces (a stray one would be an unowned orphan that fails the build).
        tasks = self._enforce_deliverable_surface_exclusion(project_root, tasks)

        # Derive-stage plan-intake grounding gate (FIX-4, Fable5 ts-v9 Secondary 1):
        # every authored deliverable must be declared as a CONCRETE path/glob, not as
        # prose describing authored files. A prose declaration cannot own a path in
        # the ownership index, so the file it emits lands as an orphan the gate
        # correctly refuses; ground it at plan-intake with a bounded re-derivation
        # (or honest StageError). STRICT NO-OP unless a derived task declares
        # ungrounded prose output(s).
        tasks = self._enforce_plan_intake_grounding(project_root, tasks)

        # Derive-stage VB coverage-closure synthesis (root fix for the 2026-07 S3
        # StockRoom-mini burn): guarantee every declared verifiable behavior has an
        # OWNING test-authoring task, synthesizing one cross-cutting task for the
        # residual behaviors that the module-scoped derivation left unowned (else the
        # post-implement VB coverage gate honest-stops on a marker no task can emit).
        # STRICT NO-OP when every declared VB is already claimable. Runs BEFORE the
        # per-task loop AND before the VB gate is wired below, so the synthesized task
        # is both implemented and visible to the gate's targeted rerun scope.
        tasks = self._enforce_vb_coverage_closure(project_root, tasks)

        # Derive-stage design-doc→task closure (Item ②, v3.36.0): complete the
        # open-world deriver's output against the DESIGN-DOC declaration universe,
        # the sibling of the VB-closure gate above (which closes against the
        # declared VB set). The bundle is an untruncated full-text concatenation,
        # so as it scales (S3-full ~10x mini) a tail-doc's task silently dropping
        # is the most probable plan-stage failure — a dropped doc's component is
        # never implemented, a consumer imports it and goes RED at a DISTANT
        # typecheck, and repair invents a non-existent producer inside the
        # consumer (the wrong-owner spiral). Placed immediately AFTER VB closure.
        # STRICT NO-OP unless a design doc is classified into an implementation
        # layer and no derived task claims it; a doc with an unclassifiable (or
        # requirement-spec) layer is echo-only fail-open — STEERED, never JUDGED.
        tasks = self._enforce_design_doc_task_closure(project_root, tasks)

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

    def _enforce_api_facade_coverage(
        self,
        project_root: Path,
        tasks: list[ImplementTaskRef],
    ) -> list[ImplementTaskRef]:
        """Derive-stage gate: exactly one derived task owns each package-FACADE file.

        The package facade (its topology harness-owned, its CONTENT SUT-authored —
        see :meth:`~codd.project_types.LayoutProfile.facade_output_paths`) must be
        populated by exactly ONE implement task; 0 owners leaves it the empty
        scaffold placeholder (the downstream public-API import fails the
        implement-oracle), and >=2 owners is an ambiguous-owner topology the
        uniqueness gate would reject. This deterministic set-math check runs at the
        TOP of the implement stage (before the scaffold and the per-task loop, and
        strictly before ``_certify_output_owner_uniqueness``); on a violation it
        FORCES a bounded re-derivation with a deterministic repair directive naming
        the path, re-approves, re-lists, and re-checks. Exhaustion raises
        :class:`StageError` (honest RED). Because the re-derive overwrites the
        cache, a ``--resume`` stays consistent.

        STRICT NO-OP unless the resolved profile declares a facade
        (``facade_output_paths()`` non-empty) AND a derived-SOURCE task exists (an
        all-configured project derives nothing to gate). Fires on an accessor
        value, never a ``language ==`` literal.
        """
        from codd.config import load_project_config

        profile = self._resolve_layout_profile(project_root)
        if profile is None:
            return tasks
        try:
            facade_paths = {p for rel in profile.facade_output_paths() if (p := _norm_decl_path(rel))}
        except Exception:  # noqa: BLE001 — no facade accessor => no gate.
            facade_paths = set()
        if not facade_paths:
            return tasks

        try:
            config = load_project_config(project_root)
        except (FileNotFoundError, ValueError):
            config = {}

        # Gate only when the project actually derives SOURCE tasks (an
        # all-configured project has nothing to re-derive).
        if not any(
            task.source == "derived" and _KIND_SOURCE in _required_kinds(task, config)
            for task in tasks
        ):
            return tasks

        max_retries = _api_facade_coverage_max_retries(config)
        attempt = 0
        while True:
            uncovered = self._facade_owner_violations(tasks, facade_paths)
            if not uncovered:
                break
            if attempt >= max_retries:
                detail = "; ".join(
                    f"`{path}` declared by {count} task(s) (need exactly 1)"
                    for path, count in sorted(uncovered.items())
                )
                raise StageError(
                    "derive-stage API-facade coverage gate failed — the package "
                    "facade file's public API is authored by the SUT but no single "
                    f"implement task owns it: {detail}. Exactly one derived task must "
                    "declare each facade path and populate its designed public API."
                )
            attempt += 1
            feedback = self._build_facade_coverage_feedback(uncovered)
            self.echo(
                "[greenfield] implement: API-facade coverage gate re-deriving with "
                f"repair feedback (attempt {attempt}/{max_retries})"
            )
            deriver = self.task_deriver or _default_task_deriver
            deriver(project_root, ai_command=self.ai_command, force=True, feedback=feedback)
            lister = self.task_lister or _default_task_lister
            tasks = list(lister(project_root))

        # Order rider: stable-move each facade-owner task to the END of the list so
        # it is authored AFTER the modules whose real symbols it re-exports (the
        # aggregator runs after the aggregated). Relative order is otherwise
        # preserved.
        owners = [task for task in tasks if self._task_declares_facade(task, facade_paths)]
        others = [task for task in tasks if not self._task_declares_facade(task, facade_paths)]
        return others + owners

    def _facade_owner_violations(
        self,
        tasks: list[ImplementTaskRef],
        facade_paths: set[str],
    ) -> dict[str, int]:
        """Map each facade path with an owner count != 1 to that count."""
        counts = {path: 0 for path in facade_paths}
        for task in tasks:
            declared = {_norm_decl_path(out) for out in task.expected_outputs}
            for path in facade_paths:
                if path in declared:
                    counts[path] += 1
        return {path: count for path, count in counts.items() if count != 1}

    def _task_declares_facade(self, task: ImplementTaskRef, facade_paths: set[str]) -> bool:
        declared = {_norm_decl_path(out) for out in task.expected_outputs}
        return bool(declared & facade_paths)

    def _build_facade_coverage_feedback(self, uncovered: dict[str, int]) -> str:
        """Deterministic repair directive for a facade-coverage re-derivation."""
        paths = ", ".join(f"`{path}`" for path in sorted(uncovered))
        return (
            "Exactly ONE implement task must declare each of these package-facade "
            "file(s) in its expected_outputs and populate its designed public API "
            f"(re-export the package's public symbols): {paths}. The file currently "
            "contains only the scaffold placeholder docstring. Do not split a facade "
            "across multiple tasks, and do not omit it."
        )

    def _enforce_deliverable_surface_exclusion(
        self,
        project_root: Path,
        tasks: list[ImplementTaskRef],
    ) -> list[ImplementTaskRef]:
        """Derive-stage fence: no task may author an EXCLUDED deliverable surface.

        A project whose plan-stage requirements intake marked an optional
        deliverable surface out-of-scope (persisted to
        ``deliverable.excluded_surfaces`` and threaded onto the resolved profile as
        ``excluded_surface_ids``) must derive NO implement task that creates it. The
        harness does not scaffold an excluded surface and its owned-scaffold
        authority drops it, so a task declaring it would emit an unowned ORPHAN that
        fails the build. This deterministic set-math check runs at the TOP of the
        implement stage (right after the API-facade coverage gate); on a violation
        it FORCES a bounded re-derivation with a deterministic repair directive
        naming the excluded path(s), re-approves, re-lists, and re-checks.
        Exhaustion raises :class:`StageError` (honest RED). Because the re-derive
        overwrites the cache, a ``--resume`` stays consistent.

        STRICT NO-OP unless the resolved profile reports non-empty
        ``excluded_surface_paths()``. Fires on an accessor value, never a hardcoded
        literal.
        """
        from codd.config import load_project_config

        profile = self._resolve_layout_profile(project_root)
        if profile is None:
            return tasks
        try:
            excluded_paths = {
                p for rel in profile.excluded_surface_paths() if (p := _norm_decl_path(rel))
            }
        except Exception:  # noqa: BLE001 — no accessor / no exclusion => no fence.
            excluded_paths = set()
        if not excluded_paths:
            return tasks

        try:
            config = load_project_config(project_root)
        except (FileNotFoundError, ValueError):
            config = {}

        max_retries = _deliverable_surface_max_retries(config)
        attempt = 0
        while True:
            violators = [
                task
                for task in tasks
                if {_norm_decl_path(out) for out in task.expected_outputs} & excluded_paths
            ]
            if not violators:
                break
            offenders = sorted(
                {
                    p
                    for task in violators
                    for out in task.expected_outputs
                    if (p := _norm_decl_path(out)) in excluded_paths
                }
            )
            if attempt >= max_retries:
                listed = ", ".join(f"`{p}`" for p in offenders)
                raise StageError(
                    "derive-stage deliverable-surface exclusion fence failed — the "
                    "requirements exclude these deliverable surface(s), so the harness "
                    "does not create them and no implement task may author them, yet a "
                    f"derived task still declares: {listed}. Re-derive without any task "
                    "that creates an excluded surface."
                )
            attempt += 1
            feedback = self._build_deliverable_surface_exclusion_feedback(offenders)
            self.echo(
                "[greenfield] implement: deliverable-surface exclusion fence re-deriving "
                f"with repair feedback (attempt {attempt}/{max_retries})"
            )
            deriver = self.task_deriver or _default_task_deriver
            deriver(project_root, ai_command=self.ai_command, force=True, feedback=feedback)
            lister = self.task_lister or _default_task_lister
            tasks = list(lister(project_root))
        return tasks

    def _build_deliverable_surface_exclusion_feedback(self, offenders: list[str]) -> str:
        """Deterministic repair directive for a deliverable-surface exclusion re-derivation."""
        paths = ", ".join(f"`{p}`" for p in offenders)
        return (
            "Do NOT author these EXCLUDED deliverable surfaces (the requirements "
            f"exclude them): {paths}. The harness does not create these paths and no "
            "implement task may declare them among its expected_outputs — a file "
            "emitted here is an unowned orphan that fails the build. Re-derive without "
            "any task that creates them."
        )

    def _enforce_plan_intake_grounding(
        self,
        project_root: Path,
        tasks: list[ImplementTaskRef],
    ) -> list[ImplementTaskRef]:
        """Derive-stage gate: every authored deliverable is declared as a CONCRETE path.

        FIX-4 (Fable5 ts-v9 Secondary 1): the plan deriver is the open→closed
        boundary for artifact ownership. A design may legitimately UNDERSPECIFY a
        path ("exact path not specified by design"), but the derived task must still
        GROUND it — an ``expected_outputs`` entry that is PROSE describing authored
        codebase files (rather than a concrete path/glob, a prose gate, or a
        non-codebase artifact) reaches disk as an orphan the ownership gate correctly
        refuses to own (the ts-v9 ``implement_ci_dependency_purity_gates`` case:
        ``.github/scripts/*.mjs`` authored under a prose declaration, found ownerless).

        On a violation this FORCES a bounded re-derivation with a deterministic
        "declare concrete paths" directive naming the task + its prose entries,
        re-approves, re-lists, and re-checks. Exhaustion raises :class:`StageError`
        (honest RED naming the task). Because the re-derive overwrites the cache, a
        ``--resume`` stays consistent.

        STRICT NO-OP unless some derived task declares an ungrounded prose output.
        Deterministic set-math over declared strings — no LLM judgment in the check
        (choosing a path is generation; declaring it is the contract), no
        per-language / per-symbol branch. Anti-false-green: this fails EARLIER and
        CLOSED (a would-be orphan never reaches the tree), never greener; the orphan
        gate stays byte-identical and header-based self-ownership is NOT conferred.
        """
        from codd.config import load_project_config

        try:
            config = load_project_config(project_root)
        except (FileNotFoundError, ValueError):
            config = {}

        max_retries = _plan_intake_grounding_max_retries(config)
        attempt = 0
        while True:
            violations = self._plan_intake_grounding_violations(tasks, config)
            if not violations:
                break
            if attempt >= max_retries:
                detail = "; ".join(
                    f"`{task_id}` declares prose output(s) that describe authored files "
                    f"instead of concrete path(s): {', '.join(repr(o) for o in outs)}"
                    for task_id, outs in sorted(violations.items())
                )
                raise StageError(
                    "derive-stage plan-intake grounding gate failed — a derived task "
                    "declares an authored deliverable as PROSE rather than a concrete "
                    f"path/glob, so no task can own the file(s) it emits: {detail}. Each "
                    "authored artifact must be declared as a concrete path or glob in "
                    "expected_outputs."
                )
            attempt += 1
            feedback = self._build_plan_intake_grounding_feedback(violations)
            self.echo(
                "[greenfield] implement: plan-intake grounding gate re-deriving with "
                f"repair feedback (attempt {attempt}/{max_retries})"
            )
            deriver = self.task_deriver or _default_task_deriver
            deriver(project_root, ai_command=self.ai_command, force=True, feedback=feedback)
            lister = self.task_lister or _default_task_lister
            tasks = list(lister(project_root))
        return tasks

    def _plan_intake_grounding_violations(
        self,
        tasks: list[ImplementTaskRef],
        config: dict[str, Any],
    ) -> dict[str, list[str]]:
        """Map each task_id with ungrounded prose output(s) to those entries."""
        violations: dict[str, list[str]] = {}
        for task in tasks:
            ungrounded = _ungrounded_prose_outputs(task, config)
            if ungrounded:
                violations[task.task_id] = ungrounded
        return violations

    def _build_plan_intake_grounding_feedback(self, violations: dict[str, list[str]]) -> str:
        """Deterministic repair directive for a plan-intake grounding re-derivation."""
        lines = [
            "Each authored deliverable MUST be declared as a CONCRETE file path or glob "
            "in expected_outputs — never as a prose description. The following task(s) "
            "declare prose that describes authored codebase files whose path is left "
            "unspecified; a file emitted under such a declaration is an unowned orphan "
            "that fails the build. Re-derive each with concrete path(s) for every file "
            "it authors (choose a conventional location if the design does not specify "
            "one — e.g. a CI/tooling script under a concrete scripts directory):",
        ]
        for task_id, outs in sorted(violations.items()):
            listed = ", ".join(f"`{o}`" for o in outs)
            lines.append(f"- `{task_id}`: replace prose output(s) {listed} with concrete path(s).")
        return "\n".join(lines)

    def _enforce_vb_coverage_closure(
        self,
        project_root: Path,
        tasks: list[ImplementTaskRef],
    ) -> list[ImplementTaskRef]:
        """Derive-stage guarantee: every declared VB is CLAIMABLE by a derived task.

        :func:`_ensure_canonical_vb_doc_planned` guarantees the VB registry DOCUMENT
        is planned (so behaviors get declared); this extends the guarantee one level
        — every declared behavior must have an OWNING test-authoring task. Module-
        scoped derivation gives cross-cutting behaviors (suite-level / static-source
        / universally-quantified invariants that map to no single module) no owning
        task, so their covering ``codd: covers vb=`` marker can never be emitted and
        the post-implement VB coverage gate honest-stops (the 2026-07 S3 StockRoom-
        mini burn: 10 of 41 VBs left unowned). This synthesizes ONE cross-cutting
        test-authoring task that owns exactly the RESIDUAL behaviors and authors
        their declared owner test files, its prompt carrying the residual VB rows +
        the covering-marker contract. STRICT NO-OP when every declared behavior is
        already claimable (or the registry declares no owner test files) — see
        :func:`codd.planner.synthesize_vb_coverage_closure_task`. It does NOT weaken
        the coverage gate: the gate still fails-closed on a genuinely uncoverable VB;
        this makes VBs COVERABLE by giving them an owning task.
        """
        from codd.config import load_project_config
        from codd.planner import synthesize_vb_coverage_closure_task
        from codd.verifiable_behavior_audit import coverage_gate_enabled, load_verifiable_behaviors

        try:
            config = load_project_config(project_root)
        except (FileNotFoundError, ValueError):
            config = {}
        if not coverage_gate_enabled(config):
            return tasks

        try:
            behaviors = load_verifiable_behaviors(project_root, config=config)
        except Exception as exc:  # noqa: BLE001 — no registry / parse issue ⇒ no closure.
            self.echo(f"[greenfield] implement: VB coverage-closure skipped ({exc}).")
            return tasks
        if not behaviors:
            return tasks

        closure = synthesize_vb_coverage_closure_task(
            behaviors,
            [list(task.expected_outputs) for task in tasks],
            config=config,
        )
        if closure is None:
            return tasks
        if any(task.task_id == closure.task_id for task in tasks):
            return tasks  # idempotent (a --resume re-synthesizes the same task deterministically)

        preview = ", ".join(closure.owned_vb_ids[:8])
        if len(closure.owned_vb_ids) > 8:
            preview += ", …"
        self.echo(
            "[greenfield] implement: VB coverage-closure synthesized 1 test-authoring task "
            f"'{closure.task_id}' owning {len(closure.owned_vb_ids)} verifiable behavior(s) no "
            f"derived task's test outputs claim ({preview})"
        )
        return list(tasks) + [
            ImplementTaskRef(
                task_id=closure.task_id,
                design_node=closure.design_node,
                output_paths=closure.expected_outputs,
                source="synthesized",
                expected_outputs=closure.expected_outputs,
                test_kinds=closure.test_kinds,
                title=closure.title,
                description=closure.description,
            )
        ]

    def _enforce_design_doc_task_closure(
        self,
        project_root: Path,
        tasks: list[ImplementTaskRef],
    ) -> list[ImplementTaskRef]:
        """Derive-stage closure: every implementation-layer design doc is CLAIMED.

        The plan deriver is an OPEN-WORLD producer — from a full-text design-doc
        bundle (untruncated concatenation) it emits a task set whose cardinality
        the harness does not control. Dropping a tail document (the most probable
        plan-stage failure as the bundle scales — S3-full is ~10x the mini
        bundle) silently omits that document's designed component; a consumer that
        imports it then goes RED at a DISTANT typecheck, and the repair, seeing no
        producer, invents a non-existent one inside the consumer (the wrong-owner
        spiral, F7.1 family).

        This CLOSES the derivation against the design-doc DECLARATION UNIVERSE —
        the sibling of the VB-closure gate just above (which closes against the
        declared VB set) and structurally identical to
        :meth:`_enforce_api_facade_coverage`: a deterministic diff = {design docs
        classified into an IMPLEMENTATION layer by the ``v_model_layer``
        classification} − {docs claimed by some task via its ``design_node`` /
        ``source_design_doc``}. A non-empty diff FORCES a bounded re-derivation
        with a deterministic directive enumerating the dropped doc(s), re-lists,
        and re-checks; exhaustion raises :class:`StageError` (honest RED naming the
        docs). Because the re-derive overwrites the cache, a ``--resume`` stays
        consistent.

        STEERED, NEVER JUDGED (the v3.22.0 principle — do not hard-fail on
        ambiguity): a doc whose ``v_model_layer`` is UNCLASSIFIABLE (or is the
        top-of-V ``requirement`` spec layer, which authors acceptance evidence,
        not an implementation unit whose omission strands a consumer) is
        ECHO-ONLY / FAIL-OPEN — never a violation source. STRICT NO-OP when the
        universe cannot be determined (no design docs / unbuildable DAG) or no doc
        is classified into an implementation layer, so an existing project whose
        docs declare no layer is unaffected. Pure id-set math over classification
        DATA — no language / framework / domain literal, and file-exclusivity is
        left to the owner-uniqueness gate (never duplicated here).
        """
        from codd.config import load_project_config
        from codd.llm.plan_deriver import VALID_V_MODEL_LAYERS, declarative_v_model_layer

        doc_lister = self.design_doc_lister or _default_design_doc_lister
        try:
            nodes = list(doc_lister(project_root))
        except Exception:  # noqa: BLE001 — universe undeterminable ⇒ fail-open no-op.
            return tasks
        if not nodes:
            return tasks

        # The V-model IMPLEMENTATION layers are every layer EXCEPT the top-of-V
        # ``requirement`` spec layer. Derived from the deriver's own layer
        # vocabulary — classification DATA, never a hardcoded literal set.
        implementation_layers = set(VALID_V_MODEL_LAYERS) - {"requirement"}

        classified: list[tuple[str, frozenset[str]]] = []  # (display key, claim keys)
        fail_open = 0
        for node in nodes:
            keys = _design_doc_claim_keys(node)
            if not keys:
                continue
            display = _norm_decl_path(getattr(node, "path", None) or getattr(node, "id", ""))
            if declarative_v_model_layer(node) in implementation_layers:
                classified.append((display, keys))
            else:
                fail_open += 1

        # No-silent-scale telemetry: the DATA a future threshold re-measurement
        # uses. Data-derived counts only — no invented doc-importance heuristic.
        self.echo(
            "[greenfield] implement: design-doc→task closure — "
            f"{len(classified)} implementation-layer doc(s) gated, {fail_open} "
            f"echo-only fail-open, of {len(nodes)} design doc(s)"
        )
        if not classified:
            return tasks

        try:
            config = load_project_config(project_root)
        except (FileNotFoundError, ValueError):
            config = {}

        max_retries = _design_doc_task_closure_max_retries(config)
        attempt = 0
        while True:
            dropped = self._design_doc_closure_violations(classified, tasks)
            if not dropped:
                break
            if attempt >= max_retries:
                listed = ", ".join(f"`{doc}`" for doc in dropped)
                raise StageError(
                    "derive-stage design-doc→task closure gate failed — these "
                    "design document(s) are classified into an implementation "
                    "layer but no derived task claims them (their designed "
                    f"component would be dropped): {listed}. Each such document "
                    "must be the source_design_doc of at least one derived task."
                )
            attempt += 1
            feedback = self._build_design_doc_closure_feedback(dropped)
            self.echo(
                "[greenfield] implement: design-doc→task closure gate re-deriving "
                f"with repair feedback (attempt {attempt}/{max_retries})"
            )
            deriver = self.task_deriver or _default_task_deriver
            deriver(project_root, ai_command=self.ai_command, force=True, feedback=feedback)
            lister = self.task_lister or _default_task_lister
            tasks = list(lister(project_root))
        return tasks

    def _design_doc_closure_violations(
        self,
        classified: list[tuple[str, frozenset[str]]],
        tasks: list[ImplementTaskRef],
    ) -> list[str]:
        """Classified design docs whose identifiers match NO task's ``design_node``.

        Returns the sorted display keys of the dropped docs. A doc is CLAIMED when
        any of its identifiers (path / id / node_id) equals some task's
        ``design_node``; matching ANY identifier keeps the closure conservative —
        it never reports a doc as dropped that a task actually claims under an
        alias (anti-false-RED)."""
        claimed = {key for task in tasks if (key := _norm_decl_path(task.design_node))}
        return sorted(display for display, keys in classified if not (keys & claimed))

    def _build_design_doc_closure_feedback(self, dropped: list[str]) -> str:
        """Deterministic repair directive for a design-doc→task closure re-derivation."""
        docs = ", ".join(f"`{doc}`" for doc in dropped)
        return (
            "Every design document that belongs to an implementation layer MUST be "
            "covered by at least one derived task that names it as source_design_doc. "
            f"The following design document(s) have NO owning task and would be "
            f"dropped: {docs}. Re-derive so that each is the source_design_doc of at "
            "least one task that implements its designed component; do not omit any."
        )

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

    def _provision_env(self, project_root: Path) -> None:
        """Run the verify-time test-environment barrier (see the call in _stage_verify).

        Dispatches through the profile realizer registry
        (:func:`codd.project_types.provision_project_env`) — a strict NO-OP for a
        stack that declares no env provisioner (returns ``ok=True,
        action="unsupported"``, no side effects). For a stack that does, the
        provisioner realizes the isolated, materialized execution environment its
        layout declares and records a harness-owned state artifact the verify spawn
        consumes. A build failure is an honest environment/toolchain error (NOT a
        code defect) raised as a :class:`StageError`, symmetric with the lock-freshness
        barrier. Language-agnostic: this method knows only "provision the stack's env".
        """
        from codd.project_types import provision_project_env

        config, language, source_dirs, test_dirs = self._layout_inputs(project_root)
        project_name = self._layout_project_name(project_root, config)
        result = provision_project_env(
            project_root,
            language=language,
            project_name=project_name,
            source_dirs=source_dirs,
            test_dirs=test_dirs,
            config=config,
        )
        if not result.ok:
            raise StageError(
                "test-execution environment provisioning failed before verify: "
                f"{result.detail}. This is an environment/toolchain failure (the "
                "isolated execution environment could not be built), not a code defect."
            )
        if result.action == "provisioned":
            self.echo(f"[greenfield] verify: provisioned test environment — {result.detail}")

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
        feedback_rows: Any = None,
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
        so the SUT regenerates coherent files. ``feedback_rows``, when given
        (the VB gate's raw uncovered ``VBAuditRow`` list), is forwarded to
        :meth:`_reimplement_tasks` unchanged so a multi-task rerun can re-scope
        the feedback per task rather than reusing one batched string for all of
        them; the oracle-gate caller never passes it, so its plain-string
        behavior is unaffected.
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
            self._reimplement_tasks(project_root, tasks, feedback, config, feedback_rows=feedback_rows)
            return

        # Fenced rerun: only the scope's tasks, fenced to its allowed paths.
        target_ids = set(getattr(scope, "task_ids", ()) or ())
        scoped_tasks = [task for task in tasks if task.task_id in target_ids]
        if not scoped_tasks:
            # Nothing resolvable in this scope (defensive) → broad, never a no-op.
            self.echo("[greenfield] implement-oracle: scoped task set empty — re-running broad.")
            self._reimplement_tasks(project_root, tasks, feedback, config, feedback_rows=feedback_rows)
            return

        # FIX-3 (Secondary 2): drop no-authored-artifact tasks from the repair scope
        # at CONSTRUCTION time too (belt-and-braces with the ``_reimplement_tasks``
        # skip), so the write-fence and logs reflect only repairable tasks. A scope
        # that RESOLVED to targets but is ALL no-op collapses to a no-op RETURN — it
        # must NOT fall back to broad (which would wrongly re-run the whole tree);
        # the distinction from the "empty scope" case above is exactly that the scope
        # DID resolve, there is simply nothing here that can repair anything.
        repairable = [t for t in scoped_tasks if not _task_declares_no_authored_artifact(t, config)]
        if not repairable:
            self.echo(
                "[greenfield] implement-oracle: scope contained only no-authored-artifact "
                "task(s) — nothing to repair (skipping)."
            )
            return
        scoped_tasks = repairable

        allowed = tuple(getattr(scope, "allowed_paths", ()) or ())
        with _OracleWriteFence(project_root, allowed_paths=allowed, echo=self.echo) as fence:
            self._reimplement_tasks(project_root, scoped_tasks, feedback, config, feedback_rows=feedback_rows)
            fence.enforce()

    def _reimplement_tasks(
        self,
        project_root: Path,
        tasks: list[ImplementTaskRef],
        feedback: str,
        config: dict[str, Any],
        *,
        feedback_rows: Any = None,
    ) -> dict[str, float]:
        """Re-run ``implement_tasks`` for each given task carrying ``feedback``.

        ``feedback_rows`` (a ``list[VBAuditRow]``, optional): when the caller
        has the raw uncovered rows behind ``feedback`` — only the VB coverage
        gate does — each task's OWN feedback is re-scoped to its own module via
        :func:`codd.verifiable_behavior_audit.scope_uncovered_rows` instead of
        reusing the single batched ``feedback`` string for every task. This
        matters because the gate's rerun scope can legitimately span EVERY test
        task at once (see below), and without it, e.g. a tokenizer-only test
        task would be handed parser/evaluator gap feedback it has no way to
        close (a task-scoped implement call can't cover another module's
        behavior — nothing to import it from). A task whose scoped list comes
        back empty (every remaining gap belongs to a DIFFERENT module) is
        skipped entirely rather than re-run with vacuous feedback. Omitted
        (``None``, the default) preserves the legacy behavior of handing every
        task the identical ``feedback`` string — used by the native-oracle gate,
        whose feedback is compiler/type-error text with no VB rows behind it.

        Returns ``{task_id: elapsed_seconds}`` so a broad-campaign caller can budget
        + audit per-task cost. (The campaign's wall-clock gate measures elapsed
        directly; the per-task map is the finer-grained record.) A task that did
        not complete (see below) has no entry.

        A single task's bounded generation budget exhausting (``CoddCLIError`` —
        ``implement_tasks`` produced zero usable/valid output after its OWN
        no-usable-output/syntax-gate retries) is logged and SKIPPED rather than
        propagated: the loop continues to the REMAINING tasks in ``tasks``
        instead of aborting the whole (possibly multi-task) rerun on the first
        stuck one. This is shared plumbing for both multi-task-scope callers:

        * the VB coverage gate, whose scope can legitimately span EVERY test
          task at once (:func:`codd.vb_rerun_scope.derive_vb_rerun_scope`'s
          batched fallback/targeting); and
        * the native-oracle gate, whose narrow/expanded scopes are "both ends
          of a broken edge" — i.e. already >=2 tasks by design
          (:mod:`codd.implement_oracle_scope`).

        Before this, ANY single task's transient malformed/empty AI output
        during a multi-task rerun raised UNCAUGHT out of this loop, which (a)
        denied every OTHER task in that same scope a chance to regenerate, (b)
        for the coverage gate, short-circuited its configured ``max_retries``
        down to a single effective attempt (the caller's except-and-break in
        :func:`_enforce_stage_coverage_gate` saw the exception and stopped
        immediately), and (c) for the oracle, bypassed its own
        signature-based escalation ladder entirely — narrow->expanded->broad
        never got a chance to run because the exception fired before the
        compiler was ever re-checked (``_invoke_rerun`` in
        ``codd/implement_oracle.py`` has no try/except of its own).

        Every caller already RE-DERIVES ground truth (the coverage audit / the
        oracle's compiler re-run) immediately after this returns, so skipping a
        stuck task loses no signal: whatever did not actually get fixed still
        shows up as still-broken in that re-check, and the caller's existing
        retry/escalation logic acts on it exactly as it would after a call that
        "succeeded" without progress. Any OTHER exception (a systemic/
        environment failure, not a per-task content exhaustion) still
        propagates immediately, unchanged.
        """
        import time

        from codd.cli import CoddCLIError
        from codd.implementer import implement_tasks

        elapsed: dict[str, float] = {}
        failed_task_ids: list[str] = []
        for task in tasks:
            # FIX-3 (Fable5 ts-v9 Secondary 2): a task that authors NO artifact can
            # repair nothing, so it must never consume an AI call inside a repair
            # scope. The first-pass runner already short-circuits these (see
            # ``_default_implement_task_runner`` above); the rerun / broad-campaign
            # path funnels through HERE, so this is the belt-and-braces skip that
            # closes it for EVERY scope (legacy-broad, fenced narrow/expanded, and
            # the chunked-broad phase that scopes all tasks). It echoes the skip —
            # exactly like the first-pass short-circuit — instead of re-running the
            # doc/gate task and emitting the "outside output paths ['src','tests']"
            # noise the wasted retries produced (8 calls/campaign in the ts-v9 plan).
            if _task_declares_no_authored_artifact(task, config):
                self.echo(
                    f"[greenfield] re-implement {task.task_id}: skip — verification/gate "
                    "or non-codebase task (no authored artifact; nothing to repair)."
                )
                continue
            task_feedback = feedback
            if feedback_rows is not None:
                from codd.verifiable_behavior_audit import format_gap_feedback, scope_uncovered_rows

                scoped_rows = scope_uncovered_rows(
                    feedback_rows, project_root=project_root, design_node=task.design_node
                )
                if not scoped_rows:
                    self.echo(
                        f"[greenfield] re-implement {task.task_id}: no uncovered VB relevant to "
                        "this task's own module — skipping (gap belongs to another module's task)."
                    )
                    continue
                task_feedback = format_gap_feedback(scoped_rows)

            output_paths = (
                list(task.output_paths)
                if task.output_paths
                else _output_paths_for_task(config, task)
            )
            started = time.monotonic()
            try:
                implement_tasks(
                    project_root,
                    design=task.design_node,
                    output_paths=output_paths,
                    expected_outputs=list(task.expected_outputs),
                    task_title=task.title,
                    task_description=task.description,
                    ai_command=self.ai_command,
                    use_derived_steps=True,
                    feedback=task_feedback,
                )
            except CoddCLIError as exc:
                failed_task_ids.append(task.task_id)
                self.echo(
                    f"[greenfield] re-implement {task.task_id}: produced no usable output "
                    f"this attempt ({exc}); continuing with the remaining scoped task(s) "
                    "rather than aborting the whole rerun."
                )
                continue
            elapsed[task.task_id] = time.monotonic() - started
        if failed_task_ids:
            self.echo(
                f"[greenfield] re-implement: {len(failed_task_ids)}/{len(tasks)} scoped "
                f"task(s) produced no usable output this attempt: {', '.join(failed_task_ids)}"
            )
        return elapsed

    def _make_vb_rerun_callback(
        self,
        project_root: Path,
        tasks: list[ImplementTaskRef],
        options: dict[str, Any],
    ) -> Callable[..., None]:
        """A ``rerun(feedback, scope, feedback_rows=...)`` for the VB gate's feedback loop.

        Reuses the oracle's scoped, write-fenced rerun dispatch
        (:meth:`_rerun_tasks_with_feedback`): a TEST-scoped
        :class:`~codd.implement_oracle_scope.OracleRerunScope` re-implements ONLY
        its test tasks, fenced to test files/helpers, so a VB coverage rerun can
        never rewrite production source. ``feedback_rows`` (the raw uncovered
        ``VBAuditRow`` list, when the caller has it) lets the fan-out over
        potentially several test tasks re-scope the feedback PER TASK instead of
        handing every task the identical batched gap text — see
        :meth:`_reimplement_tasks`.
        """

        def _rerun(feedback: str, scope: Any = None, feedback_rows: Any = None) -> None:
            self._rerun_tasks_with_feedback(
                project_root, tasks, feedback, options, scope=scope, feedback_rows=feedback_rows
            )

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

        # A VERIFICATION / RELEASE-GATE task declares no authored artifact — its
        # deliverable is an ACTION + its output (``run_full_pytest_release_gate``:
        # ``expected_outputs: ['pytest -q output', ...]``), not a file. The
        # implementer has nothing to write, honestly emits 0 files, and the
        # 0-generated-files gate then hard-fails it (the 2026-07-03 Python
        # greenfield false-RED). Its real work — install + run the suite green with
        # SKIP=0 — is exactly the VERIFY stage's job, so implement treats it as a
        # no-op (deferred to verify) rather than demanding generation. No gate is
        # weakened: verify re-runs the full suite as the release gate, and any task
        # that DOES declare a codebase artifact stays fully gated. Also saves the
        # wasted derive-steps + AI generation the 0-file honest miss would burn.
        if _task_declares_no_authored_artifact(task, config):
            return (
                "0 file(s) generated — verification/gate task "
                "(no authored artifact; release gate enforced at verify)"
            )

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
            # Contract-aware task-done verification: a task is "done" only if the
            # implementer produced the KIND of artifact the task declared (e.g. a
            # test-writing task must emit at least one test file, not just app
            # code). Drives off the declared expected_outputs — never task names,
            # vendor CLI, or path literals. No-op for tasks with no declared
            # output kind. See _verify_task_contract for the rules.
            #
            # v3.17.0: bounded feedback re-drive when the produced kinds do not yet
            # cover the declared kinds (a source+test task that emitted only source).
            # Evaluation is UNION across attempts (every produced file is on disk),
            # so a re-drive that adds only the missing test satisfies the contract
            # together with attempt 1's source. Budget exhausted → the gate itself
            # raises the SAME hard StageError (honest RED; anti-false-green intact).
            all_results: list[Any] = []
            kind_feedback: str | None = None
            max_kind_retries = _kind_contract_max_retries(config)
            for attempt in range(1 + max_kind_retries):
                results = implement_tasks(
                    project_root,
                    design=task.design_node,
                    output_paths=output_paths,
                    expected_outputs=list(task.expected_outputs),
                    task_title=task.title,
                    task_description=task.description,
                    ai_command=ai_command,
                    use_derived_steps=True,
                    feedback=kind_feedback,
                )
                failed = [result for result in results if result.error]
                if failed:
                    raise StageError(f"task {task.task_id}: {failed[0].error}")
                all_results.extend(results)
                try:
                    _verify_task_contract(task, all_results, project_root, config, echo=self.echo)
                    break
                except StageError:
                    if attempt >= max_kind_retries:
                        raise  # budget exhausted → honest RED, gate's own message
                    kind_feedback = _kind_contract_feedback(task, all_results, project_root, config)
                    self.echo(
                        f"[greenfield] task {task.task_id}: declared output kind(s) not yet "
                        f"produced; re-driving implement with feedback "
                        f"(attempt {attempt + 2}/{1 + max_kind_retries})"
                    )
            results = all_results

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

        # Test-execution environment barrier — verify-direct, BEFORE the verify runner
        # spawns any test command. A stack whose profile declares an env provisioner
        # (its topology needs an isolated, materialized execution environment before
        # its tests can honestly run) gets that environment realized here, and a
        # harness-owned state artifact recorded so the verify spawn can find it. This
        # is a hard barrier (a build failure is an environment/toolchain error raised
        # as a StageError — never a code-repair target), mirroring the lock-freshness
        # barrier above. GREENFIELD-ONLY (a plain ``codd verify`` on a brownfield repo
        # never reaches here, so no environment is grown outside the autopilot). A
        # strict NO-OP for a stack that declares no provisioner (dispatched through the
        # profile realizer registry — never a language-name branch).
        self._provision_env(project_root)

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

    def rollback(self) -> None:
        """Revert the ENTIRE tracked tree to the entry snapshot — for a CRASHED
        scoped rerun, which must leave NO partial transcription (in- OR out-of-scope).

        Unlike :meth:`enforce`, which KEEPS in-scope writes (the success path's whole
        purpose), rollback restores every tracked file to its pre-rerun bytes: an
        out-of-scope OR in-scope MODIFY is restored, a CREATE is deleted, a DELETE is
        recreated. Used by the F7 re-derivation runner's crash-containment path so a
        draw that raised (e.g. an implementer honestly emitting 0 files) is undone in
        full before the honest RED terminal — no half-written test survives."""
        current = self._capture()
        reverted_modified: list[str] = []
        reverted_created: list[str] = []
        for rel, content in current.items():
            if rel in self._snapshot:
                if self._snapshot[rel] != content:
                    self._restore(rel, self._snapshot[rel])
                    reverted_modified.append(rel)
            else:
                self._remove(rel)
                reverted_created.append(rel)
        reverted_deleted: list[str] = []
        for rel, content in self._snapshot.items():
            if rel not in current:
                self._restore(rel, content)
                reverted_deleted.append(rel)
        total = len(reverted_modified) + len(reverted_created) + len(reverted_deleted)
        if total:
            self._echo(
                "[greenfield] implement-oracle: write-fence ROLLED BACK "
                f"{total} change(s) after a crashed scoped rerun "
                f"(modified={len(reverted_modified)}, created={len(reverted_created)}, "
                f"deleted={len(reverted_deleted)}); the tree is restored to entry."
            )

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
            title=str(entry.get("title") or ""),
            description=str(entry.get("description") or ""),
            dependencies=tuple(entry.get("dependencies") or ()),
        )
        for entry in list_implement_tasks(project_root)
    ]


def _default_task_deriver(
    project_root: Path,
    *,
    ai_command: str | None,
    force: bool = False,
    feedback: str | None = None,
) -> int:
    """Derive implement tasks from design docs and auto-approve them.

    Mirrors ``codd plan derive`` + ``codd plan approve <doc> --all`` — the
    autopilot equivalent of the HITL task-approval gate.

    ``force`` re-derives even when a cache exists (busting the cached list), and
    ``feedback`` threads a deterministic repair directive into the derivation
    prompt (via the project-context payload). Both are used by the derive-stage
    coverage gate to re-drive a derivation that failed a deterministic
    completeness check; the defaults preserve the legacy single-derivation path.
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
    project_context: dict[str, Any] = {"project": config.get("project", {})}
    if feedback:
        project_context["derivation_repair_feedback"] = feedback
    tasks = deriver.derive_tasks(
        nodes,
        "detailed",
        {
            "project_root": project_root,
            "force": bool(force),
            "dry_run": False,
            "write_cache": True,
            "project_context": project_context,
        },
    )
    for cache_path, _record in iter_derived_task_records(project_root):
        approve_cached_tasks(cache_path, approve_all=True)
    return len(tasks)


def _default_design_doc_lister(project_root: Path) -> list[Any]:
    """List the project's design-doc nodes — the derivation declaration universe.

    Thin wrapper over the CLI's design-doc DAG loader (the SAME node set the
    derive stage feeds the plan deriver). Fail-open: any failure (no design docs,
    an unbuildable DAG) yields ``[]`` so the design-doc→task closure gate degrades
    to a strict NO-OP rather than a false RED.
    """
    try:
        import codd.cli as cli_module

        return list(cli_module._plan_design_doc_nodes(project_root, ()))
    except Exception:  # noqa: BLE001 — universe undeterminable ⇒ fail-open no-op.
        return []


def _design_doc_claim_keys(node: Any) -> frozenset[str]:
    """The normalized identifiers by which a task may CLAIM a design-doc node.

    A derived task references its source document by path (``design_node`` =
    canonicalized ``source_design_doc``); a doc may also be aliased by its
    declared ``node_id``. Collecting the doc's path / id / node_id lets the
    closure treat a doc as claimed when a task matches ANY of them — conservative
    (anti-false-RED)."""
    attributes = getattr(node, "attributes", None) or {}
    candidates = (
        getattr(node, "path", None),
        getattr(node, "id", None),
        attributes.get("node_id"),
    )
    return frozenset(k for c in candidates if c and (k := _norm_decl_path(c)))


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
#   • A test-SHAPED filename is a TEST artifact — but only via a suffix that is
#     UNAMBIGUOUS on its own (JS/TS's dedicated ``.test.``/``.spec.``/``.e2e.``/
#     ``.cy.`` conventions, Go's tooling-enforced ``_test.go``). A BARE
#     whole-language extension (``.py``, ``.cs``, ``.java``, ``.cpp``/``.cc``/
#     ``.cxx``) is never enough on its own — every file in that language ends in
#     it — so ``.py`` requires ``test_*.py`` / ``*_test.py`` naming, and the
#     other bare-admit languages are left entirely to the ``test_dirs`` check
#     below (see ``_unambiguous_test_suffixes``).
#   • A FILE (or glob) under a configured ``test_dirs`` root is a TEST artifact.
#   • A FILE (or glob) under a configured ``source_dirs`` root (that is not
#     test-shaped via an unambiguous test suffix) is a SOURCE artifact.
#   • A BARE DIRECTORY declaration (``tests/``, ``tests/e2e/``, ``src/pkg/``) is
#     UNKNOWN — it is structural scaffold intent created by ``mkdir``, never an
#     authored artifact, so it imposes no author-kind obligation (the same stance
#     the completeness gate already takes). See ``_is_bare_directory_decl``.
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


# ``_has_test_shape`` / ``_unambiguous_test_suffixes`` /
# ``_AMBIGUOUS_BARE_SOURCE_SUFFIXES`` now live next to their sole data source
# (``_TEST_SUFFIXES``) in ``codd.operational_e2e_audit`` — a LEAF module — so BOTH
# this kind gate AND ``codd.implementer`` (the test-root re-key normalization) can
# reuse them without ``implementer`` having to import ``greenfield.pipeline``
# (which imports ``implementer`` — a cycle). Re-imported here so this module's
# call sites (``_classify_declared_output`` / ``_produced_kinds``) are unchanged.
from codd.operational_e2e_audit import (  # noqa: E402
    _has_test_shape,
    _unambiguous_test_suffixes,
)


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


def _is_bare_directory_decl(raw: str) -> bool:
    """Whether a declared ``expected_outputs`` entry denotes a DIRECTORY, not file(s).

    A directory declaration (``tests/``, ``tests/e2e``, ``src/pkg``) is neither a
    node-id, a glob, nor an exact file path — it is a plain path with no file
    extension on its last segment. Such an entry is STRUCTURAL scaffold intent
    (the harness/scaffold creates it with ``mkdir``); it is never AUTHORED as a
    source/test artifact, so it carries no deliverable-KIND obligation — mirroring
    the completeness gate, which likewise leaves a directory declaration
    unchecked. A concrete file (``token_bucket.py``), a glob (``*_test.go`` —
    denotes file[s]), and a node-id (``module:parser.parse``) all return False.
    """
    s = str(raw).strip().replace("\\", "/").strip("/")
    if not s:
        return False
    if ":" in s:  # node-id (``module:parser.parse``), not a filesystem path
        return False
    if any(ch in s for ch in "*?["):  # a glob denotes file(s), not a bare directory
        return False
    return not _declared_output_is_file_path(s)


def _classify_declared_output(rel_path: str, config: dict[str, Any]) -> str | None:
    """Classify ONE ``expected_outputs`` entry as the deliverable KIND it implies.

    Returns ``_KIND_TEST`` / ``_KIND_SOURCE`` / ``None`` (unknown — e.g. a bare
    artifact name, a doc, a bare DIRECTORY, or a path under no configured root).
    """
    # A test-SHAPED name (a concrete test file OR a test-name glob such as
    # ``*_test.go`` / ``**/*.test.ts``) is unambiguously a TEST deliverable,
    # independent of where it sits — check it FIRST so the directory guard below
    # never strips a real test obligation (a glob is not a file path).
    if _has_test_shape(rel_path):
        return _KIND_TEST
    # A BARE DIRECTORY declaration (``tests/``, ``tests/e2e/``, ``src/pkg/``) is
    # STRUCTURAL scaffold intent: it is created by ``mkdir`` and never AUTHORED as
    # a source/test artifact, so location under a configured root imposes NO
    # author-kind obligation — exactly as the completeness gate already leaves a
    # directory declaration unchecked. Gating it produced the scaffold-task
    # false-RED (Python ``scaffold_package_and_pyproject`` 2026-07-03: declared
    # ``tests/`` + ``tests/e2e/`` → demanded a produced test file although the task
    # only creates empty dirs, "populated later"; the same false-RED CLASS as the
    # Java/C++/C# scaffold cases). Only entries that denote FILE(s) — a concrete
    # path or a glob — carry a location-derived kind (anti-false-green: a real
    # test task declares a test FILE or a test-name GLOB, both still gated above /
    # below).
    if _is_bare_directory_decl(rel_path):
        return None
    if _path_under_root(rel_path, _scan_roots(config, "test_dirs")):
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


def _kind_contract_max_retries(config: dict[str, Any]) -> int:
    """``implement.kind_contract_max_retries`` — bounded feedback re-drives when a
    task produced only SOME of its declared output kinds (v3.17.0). Default 2 — a
    DISTINCT knob from the syntax / no-usable budgets (the implementer's "different
    failure classes get different knobs" rule), so tuning one never perturbs another.
    ``0`` restores the legacy hard-fail-on-first-miss (the gate stays hard)."""
    section = config.get("implement") if isinstance(config, Mapping) else None
    if isinstance(section, Mapping) and "kind_contract_max_retries" in section:
        try:
            value = int(section["kind_contract_max_retries"])
        except (TypeError, ValueError):
            return 2
        return value if value >= 0 else 2
    return 2


def _kind_contract_feedback(
    task: ImplementTaskRef, results: list[Any], project_root: Path, config: dict[str, Any]
) -> str:
    """Restate the kind contract for a bounded re-drive: which declared kind(s) are
    still missing and the verbatim declared outputs that map to them. Same discipline
    as the syntax-gate feedback (restate the contract; never hint at relaxing it), and
    explicitly forbids hollow tests so the re-drive cannot game the downstream
    authenticity gate. Recomputed with the SAME kind-classification helpers the gate
    uses — the single authority stays in this module."""
    required = _required_kinds(task, config)
    generated = [f for result in results for f in getattr(result, "generated_files", [])]
    produced = _produced_kinds(generated, project_root, config)
    missing = sorted(required - produced)
    missing_outputs = [
        str(out).strip()
        for out in task.expected_outputs
        if str(out).strip() and _classify_declared_output(str(out), config) in set(missing)
    ]
    outputs_line = ", ".join(missing_outputs) if missing_outputs else "(the declared outputs for the missing kind)"
    return (
        f"The previous attempt for this task produced kind(s) {sorted(produced) or ['<none>']}, but "
        f"the task declares {sorted(required)} — the {missing} deliverable(s) were NOT produced. "
        f"Author the missing declared deliverable(s) now from the task description and design "
        f"document: {outputs_line}. Produce a real, executable implementation — for a test, a "
        f"genuine test with assertions that exercises the behavior, never an empty, skipped, or "
        f"assertion-free test. Keep the file(s) already produced."
    )


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
    bare-name declaration carries NO deliverable obligation in EITHER gate: it is
    structural scaffold intent (created by ``mkdir``), so the completeness check
    skips it AND the kind check (:func:`_classify_declared_output` /
    :func:`_is_bare_directory_decl`) imposes no author-kind on it. (Historically
    the kind gate DID demand "a directory output → at least one file of that
    kind", which false-RED'd scaffold tasks that only create empty dirs — Python
    ``scaffold_package_and_pyproject`` 2026-07-03; the two gates now agree that a
    bare directory is not a deliverable.) A declared file is "produced" when it is
    in the task's generated files OR exists on disk. Missing ones are WARNED
    (default) or, when ``enforce``, raised as a StageError.

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


def _augment_with_declared_test_roots(
    config: dict[str, Any], task: ImplementTaskRef, base_paths: list[str]
) -> list[str]:
    """Envelope alignment (v3.17.0): expose the configured test dirs when a task
    DECLARES a ``test`` kind but its resolved output paths are source-pure.

    A MIXED task (declares both ``source`` and ``test``) routes source-pure by
    default (:func:`_test_only_output_paths` returns ``None`` for non-test-only
    tasks), so its declared test file falls OUTSIDE the output fence and is dropped
    — the kind gate then fails a task the model could satisfy. Gated on the DECLARED
    kind, so a pure-source task is never handed a test root; test-only tasks already
    routed above never reach here. A ``"."`` test root (a root-module language that
    colocates tests) is excluded — it needs no extra root. Purely additive: it only
    widens where the model MAY write, never any judgement."""
    if _KIND_TEST not in _required_kinds(task, config):
        return base_paths
    test_roots = [
        r for r in _scan_roots(config, "test_dirs") if r and r.strip() and r.strip() != "."
    ]
    if not test_roots:
        return base_paths
    if any(_path_under_root(_norm_decl_path(p), test_roots) for p in base_paths):
        return base_paths  # already in-fence for tests
    merged = list(base_paths)
    for root in test_roots:
        if root not in merged:
            merged.append(root)
    return merged


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
    base = _route_source_into_package(config, explicit, project_root=_task_project_root(config, task))
    return _augment_with_declared_test_roots(config, task, base)


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


def _output_is_prose_declaration(out: str, source_roots: list[str], test_roots: list[str]) -> bool:
    """A PROSE action-and-output declaration (a verification/gate deliverable), not
    an authored file. Contains WHITESPACE (``pytest -q output`` reads as a sentence;
    ``src/pkg/x.py`` and a bare token ``some_artifact`` do not), and is not a
    concrete file path, a glob, or a path under a configured source/test root. This
    is the original (2026-07-03) rule, unchanged."""
    if not any(ch.isspace() for ch in out):
        return False  # a whitespace-free token is a plausible authored artifact name
    if _declared_output_is_file_path(out):
        return False  # a real path (even one with a space) is an authored file
    if any(ch in out for ch in "*?["):  # a glob denotes authored file(s)
        return False
    norm = _norm_decl_path(out)
    if _path_under_root(norm, source_roots) or _path_under_root(norm, test_roots):
        return False  # a package/test location under a root IS authored
    return True


def _no_op_impl_extensions(config: dict[str, Any]) -> frozenset[str]:
    """Implementation-file extensions for the project language, or the union of ALL
    known impl extensions when the language is unresolved (fail-closed — an unknown
    language must never let an implementation file pass as a non-codebase artifact).
    Reads the language→extension registry DATA table, never a language branch."""
    from codd.implementer import LANGUAGE_EXT_MAP, _implementation_language_extensions

    language = None
    project = config.get("project") if isinstance(config, Mapping) else None
    if isinstance(project, Mapping):
        language = project.get("language")
    if language:
        return frozenset(ext.lower() for ext in _implementation_language_extensions(language))
    union: set[str] = set()
    for exts in LANGUAGE_EXT_MAP.values():
        union.update(ext.lower() for ext in exts)
    return frozenset(union)


def _output_is_non_codebase_artifact(
    out: str, source_roots: list[str], test_roots: list[str], impl_exts: frozenset[str]
) -> bool:
    """A declared output that a LATER pipeline stage provisions, not an implement
    authored file (v3.16.0).

    True for a MULTI-COMPONENT path (contains ``/``) that is NOT under any
    configured source/test root, carries NO implementation-language extension, is
    NOT test-shaped, and is NOT a glob — i.e. an artifact a LATER pipeline stage
    provisions after implementation. A bare single token (``some_artifact``) stays a
    real generation target; a codebase path (under a root, or an impl extension even
    if misplaced) stays owed. Decided from harness declaration DATA only (scan
    roots, the language→extension table, test-shape) — no doc name / node type /
    language / framework literal."""
    if any(ch in out for ch in "*?["):
        return False  # a glob denotes authored file(s)
    norm = _norm_decl_path(out)
    if "/" not in norm:
        return False  # a bare single token is a plausible authored artifact name
    if _path_under_root(norm, source_roots) or _path_under_root(norm, test_roots):
        return False  # under an authored root → owed
    if _has_test_shape(norm):
        return False  # test-shaped → owed
    suffix = PurePosixPath(norm).suffix.lower()
    if suffix and suffix in impl_exts:
        return False  # an implementation-language file (even misplaced) → owed
    return True


def _task_declares_no_authored_artifact(task: ImplementTaskRef, config: dict[str, Any]) -> bool:
    """Whether a task's declared outputs are all NON-authored — a deterministic
    implement no-op (no AI call), not a demand for generation.

    A derived task normally declares the FILE(S) it authors (``src/pkg/x.py``,
    ``tests/test_x.py``, a glob, or a package/test DIRECTORY under a configured
    root). Two kinds of task legitimately author NOTHING and would otherwise trip
    the 0-generated-files gate as a false-RED:

    * a VERIFICATION / RELEASE-GATE task — an ACTION and its OUTPUT, e.g.
      ``run_full_pytest_release_gate`` with ``['pytest -q output', ...]`` (the
      2026-07-03 false-RED; the work is what the VERIFY stage already performs); and
    * (v3.16.0) a NON-CODEBASE-ARTIFACT task — one that declares only artifacts a
      LATER pipeline stage provisions after implementation. The generation prompt
      demands concrete source files unconditionally, so a run where the model
      correctly emits nothing hard-fails; a run where it fabricates the artifact
      "succeeds" but usurps the owning stage's deterministic provisioning. This is
      generation-variance surfaced as a stochastic implement halt.

    Returns True when EVERY declared output is either a prose gate declaration
    (:func:`_output_is_prose_declaration`) or a non-codebase artifact
    (:func:`_output_is_non_codebase_artifact`). Otherwise False (fail-closed): an
    EMPTY ``expected_outputs`` (absence of a contract is ambiguous, not a sanctioned
    skip), a bare single token, a glob, or ANY path-shaped codebase artifact (under
    a root, or an impl extension even if misplaced) still owes generation — the
    0-files gate + completeness/kind gates + ``skip_generation`` HITL seam remain
    byte-identical. Anti-false-green: a no-op'd task runs NO model, so a generation
    failure cannot masquerade as 0-file success; a mis-derived module surfaces as a
    downstream RED (verify / VB coverage+authenticity / check), never a false-GREEN.
    """
    # ``getattr`` guard: the rerun/campaign filters (FIX-3) may hand this a
    # duck-typed task object that carries only ``output_paths`` (no declared
    # ``expected_outputs``). Absent/empty outputs → False (fail-closed, "repairable"),
    # exactly the empty-contract case below.
    declared = getattr(task, "expected_outputs", ()) or ()
    outputs = [str(out).strip() for out in declared if str(out).strip()]
    if not outputs:
        return False
    source_roots = _scan_roots(config, "source_dirs")
    test_roots = _scan_roots(config, "test_dirs")
    impl_exts = _no_op_impl_extensions(config)
    for out in outputs:
        if _output_is_prose_declaration(out, source_roots, test_roots):
            continue
        if _output_is_non_codebase_artifact(out, source_roots, test_roots, impl_exts):
            continue
        return False  # a codebase artifact (or ambiguous bare/empty token) is owed
    return True


def _ungrounded_prose_outputs(task: ImplementTaskRef, config: dict[str, Any]) -> list[str]:
    """The ``expected_outputs`` entries of a CODE-AUTHORING task that are PROSE
    DESCRIBING authored codebase files rather than concrete paths (FIX-4, Fable5
    ts-v9 Secondary 1 — the open→closed boundary the plan deriver must ground).

    A task whose outputs are ALL prose/non-codebase is a legitimate verification/
    gate / non-codebase no-op (:func:`_task_declares_no_authored_artifact`): its
    prose is a real declaration, and nothing is returned here. But once a task
    authors ANY concrete codebase file, every OTHER entry must ALSO be a concrete
    path/glob or a non-codebase artifact — a PROSE entry there (whitespace, not a
    file path, not a glob, not under a scan root; the ts-v9
    ``"CI ... check scripts (exact path not specified by design)"`` case) describes
    authored codebase files whose path the design left unpinned. Prose cannot own a
    path in the ``TaskOutputIndex``, so those files reach disk as ORPHANS the gate
    correctly refuses to own. Returned so plan-intake can force the deriver to
    DECLARE concrete paths.

    Deterministic; reuses the existing ``_output_is_prose_declaration`` /
    ``_output_is_non_codebase_artifact`` predicates verbatim — the CHOOSING of a
    path is generation (open-world), only the DECLARING is the contract, so there
    is NO LLM judgment in this check and no per-language/symbol branch.
    """
    declared = getattr(task, "expected_outputs", ()) or ()
    outputs = [str(out).strip() for out in declared if str(out).strip()]
    if not outputs:
        return []  # no contract to ground (a configured target) — not this gate's job
    if _task_declares_no_authored_artifact(task, config):
        return []  # a pure gate/verification/non-codebase task — its prose is legit
    source_roots = _scan_roots(config, "source_dirs")
    test_roots = _scan_roots(config, "test_dirs")
    impl_exts = _no_op_impl_extensions(config)
    ungrounded: list[str] = []
    for out in outputs:
        if _output_is_non_codebase_artifact(out, source_roots, test_roots, impl_exts):
            continue  # a later-stage-provisioned artifact — a legit declaration
        if _output_is_prose_declaration(out, source_roots, test_roots):
            ungrounded.append(out)  # prose in a code-authoring task → ungrounded
    return ungrounded


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


def _authenticity_failure_message(violations: Sequence[Any]) -> str:
    """The canonical marker-authenticity StageError message (shared by both exits)."""
    return (
        "verifiable-behavior marker-authenticity gate failed for the implement stage: "
        f"{len(violations)} `codd: covers vb=` marker(s) are not credible coverage claims "
        "(attached to a skipped/empty test, an orphan id, a test with no assertion, or an "
        "unobservable test structure — a recognized file with no parseable test). A covers "
        "marker must sit on an executable test that asserts the behavior."
    )


def _vb_rework_max_rounds(config: dict[str, Any] | None) -> int:
    """Bounded authenticity-rework rounds (``greenfield.vb_rework.max_rounds``, default 2).

    ``0`` = legacy behavior (fail the authenticity gate immediately, no rework).
    A versioned, visible default — not a silent activation.
    """
    section = ((config or {}).get("greenfield") or {}).get("vb_rework")
    if isinstance(section, dict):
        value = section.get("max_rounds")
        if isinstance(value, int) and value >= 0:
            return value
    return 2


def _api_facade_coverage_max_retries(config: dict[str, Any] | None) -> int:
    """Bounded derive-stage re-derivations for the API-facade coverage gate
    (``derive.api_facade_coverage_max_retries``, default 2).

    ``0`` = legacy behavior (no re-derive: a facade with the wrong owner count
    fails the gate immediately). A versioned, visible default — not a silent
    activation.
    """
    section = (config or {}).get("derive")
    if isinstance(section, dict):
        value = section.get("api_facade_coverage_max_retries")
        if isinstance(value, int) and value >= 0:
            return value
    return 2


def _deliverable_surface_max_retries(config: dict[str, Any] | None) -> int:
    """Bounded derive-stage re-derivations for the deliverable-surface exclusion
    fence (``derive.deliverable_surface_max_retries``, default 2).

    ``0`` = legacy behavior (no re-derive: a task authoring an excluded surface
    fails the fence immediately). A versioned, visible default — not a silent
    activation.
    """
    section = (config or {}).get("derive")
    if isinstance(section, dict):
        value = section.get("deliverable_surface_max_retries")
        if isinstance(value, int) and value >= 0:
            return value
    return 2


def _plan_intake_grounding_max_retries(config: dict[str, Any] | None) -> int:
    """Bounded derive-stage re-derivations for the plan-intake grounding gate
    (``derive.plan_intake_grounding_max_retries``, default 2).

    ``0`` = gate on, no re-derivation retry (a task declaring prose that describes
    authored files fails the grounding gate immediately). This differs from the
    pre-FIX-4 behavior, which had no grounding gate at all. A versioned, visible
    default — not a silent activation.
    """
    section = (config or {}).get("derive")
    if isinstance(section, dict):
        value = section.get("plan_intake_grounding_max_retries")
        if isinstance(value, int) and value >= 0:
            return value
    return 2


def _design_doc_task_closure_max_retries(config: dict[str, Any] | None) -> int:
    """Bounded derive-stage re-derivations for the design-doc→task closure gate
    (``derive.design_doc_task_closure_max_retries``, default 2).

    ``0`` = no re-derive (a classified-but-unclaimed design doc fails the gate
    immediately). A versioned, visible default — not a silent activation (the gate
    itself is a strict no-op unless a doc is classified into an implementation
    layer and left unclaimed).
    """
    section = (config or {}).get("derive")
    if isinstance(section, dict):
        value = section.get("design_doc_task_closure_max_retries")
        if isinstance(value, int) and value >= 0:
            return value
    return 2


def _build_authenticity_rework_feedback(
    violations: Sequence[Any],
    uncovered_rows: Sequence[Any],
    *,
    contract: str,
) -> str:
    """Structured rework feedback: verbatim findings + coverage gaps + VB contract.

    Carries a "why the last output was rejected" header, the per-marker findings
    (file:line/kind/reason), any coverage rows that regressed, and the closed VB
    contract block (same ``render_vb_contract`` truth source the first pass saw),
    so the re-driven test tasks fix the tests rather than invent new orphans.
    """
    from codd.verifiable_behavior_audit import format_gap_feedback

    lines = [
        "A previous test-authoring attempt was REJECTED by the deterministic "
        "verifiable-behavior marker-authenticity gate. Each finding below is a "
        "`codd: covers vb=<id>` marker that does NOT credibly prove its behavior. "
        "Fix the marked test (or the marker) so it (a) names a VB id from the "
        "closed contract list below and (b) actually executes the system under "
        "test and asserts on the OBSERVED result. Do NOT delete tests or markers "
        "to silence the gate, and do NOT edit the VB registry table — the declared "
        "id set is fixed and changing it during implement fails the build.",
        "",
        "Rejected `codd: covers vb=` markers:",
    ]
    for violation in violations:
        path = getattr(violation, "path", "?")
        line = getattr(violation, "line", "?")
        kind = getattr(violation, "kind", "?")
        message = getattr(violation, "message", "")
        lines.append(f"- {path}:{line} [{kind}] {message}")
    if uncovered_rows:
        lines.extend(["", format_gap_feedback(uncovered_rows)])
    if contract:
        lines.extend(["", contract])
    return "\n".join(lines)


def _drive_vb_authenticity_rework(
    project_root: Path,
    *,
    config: dict[str, Any],
    authenticity_profile: Any,
    echo: Callable[[str], None],
    rerun: Callable[..., None],
    rerun_oracle: Callable[[], None] | None,
    scope_resolver: Callable[[list[str]], Any],
    max_rounds: int,
) -> None:
    """Bounded, deterministic rework loop for authenticity (+ coverage-regression) findings.

    Re-drives the TEST tasks with the verbatim authenticity findings + the closed
    VB contract, up to ``max_rounds`` rounds, re-judging with the UNCHANGED gate
    after each round. Returns on convergence (authenticity passes AND coverage did
    not regress). Raises :class:`StageError` (fail-closed) when:

    * the round budget is exhausted with findings remaining;
    * the finding count stops strictly shrinking round-over-round (oscillation
      guard — kills "fix A, break B"); or
    * the declared VB-id set is edited DURING rework (tampering guard — a test
      task must not legalize an orphan or drop a coverage obligation by mutating
      the VB registry; VB-table changes belong to generate/propagate).

    The gate itself is never loosened — this only grants bounded retries, each
    judged by the same deterministic authenticity + coverage audit.
    """
    from codd.vb_marker_authenticity import build_authenticity_report
    from codd.verifiable_behavior_audit import (
        _normalize_vb_id,
        build_vb_coverage_audit,
        collect_declared_vb_ids,
        render_vb_contract,
    )

    def _declared_ids() -> frozenset:
        return frozenset(
            _normalize_vb_id(b.vb_id)
            for b in collect_declared_vb_ids(project_root, config=config)
        )

    def _authenticity():
        return build_authenticity_report(
            project_root, config=config, profile=authenticity_profile, strict_observability=True
        )

    def _uncovered():
        return build_vb_coverage_audit(project_root, config=config).uncovered_rows

    baseline_ids = _declared_ids()
    auth = _authenticity()
    uncovered = _uncovered()
    prev_count = len(auth.violations) + len(uncovered)

    for round_no in range(1, max_rounds + 1):
        contract = render_vb_contract(collect_declared_vb_ids(project_root, config=config))
        feedback = _build_authenticity_rework_feedback(auth.violations, uncovered, contract=contract)
        echo(
            f"Test coverage gate: marker-authenticity gate found {len(auth.violations)} "
            "non-credible marker(s); re-running TEST tasks with findings + VB contract "
            f"(authenticity rework round {round_no}/{max_rounds})"
        )
        try:
            scope = scope_resolver([])
            rerun(feedback, scope, None)
            if rerun_oracle is not None:
                # A test rewrite can break test↔helper symbol coherence; re-assert
                # the native oracle so a type/import break never rides into verify.
                rerun_oracle()
        except Exception as exc:  # noqa: BLE001 — a stalled rerun must not mask the verdict.
            echo(
                f"Test coverage gate: authenticity rework round {round_no} could not complete "
                f"({exc}); evaluating the gate as-is."
            )
            break

        # Tampering guard: the declared VB-id set must NOT change during rework.
        if _declared_ids() != baseline_ids:
            raise StageError(
                "verifiable-behavior marker-authenticity gate failed for the implement stage: "
                "the declared VB-id set was MODIFIED during authenticity rework. A test task "
                "must fix the TEST to prove a declared behavior, never edit the VB registry to "
                "legalize an orphan marker or drop a coverage obligation (VB-table changes belong "
                "to the generate/propagate stages, not implement rework)."
            )

        auth = _authenticity()
        uncovered = _uncovered()
        if auth.passed and not uncovered:
            echo(
                f"Test coverage gate: marker authenticity OK after {round_no} rework round(s) "
                f"({len(auth.degraded_paths)} file(s) stage-1-only)."
            )
            return

        new_count = len(auth.violations) + len(uncovered)
        if new_count >= prev_count:
            echo(
                f"Test coverage gate: authenticity findings did not shrink "
                f"({prev_count} -> {new_count}) after round {round_no}; aborting rework "
                "(oscillation guard) and failing the gate on the remaining findings."
            )
            break
        prev_count = new_count

    # Budget exhausted / aborted / rerun stalled — fail-closed on remaining findings.
    for violation in auth.violations:
        echo(violation.message)
    if auth.violations:
        raise StageError(_authenticity_failure_message(auth.violations))
    # Authenticity passed but coverage regressed during a rework round.
    raise StageError(
        "verifiable-behavior coverage gate failed for the implement stage: "
        f"{len(uncovered)} declared verifiable behavior(s) became uncovered during "
        "authenticity rework (a fix removed a covering marker without replacing it)."
    )


def _enforce_stage_coverage_gate(
    project_root: Path,
    *,
    coverage_gate: bool,
    echo: Callable[[str], None],
    rerun: Callable[..., None] | None = None,  # (feedback, scope, feedback_rows=...)
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
                # Pass the raw rows too (not just the pre-rendered project-wide
                # string): the scope can batch several test tasks together
                # (derive_vb_rerun_scope's per-doc targeting is inert once every
                # VB shares one canonical registry doc), and each of THOSE
                # tasks' own implement call must only see the gap feedback
                # relevant to ITS OWN module — see _reimplement_tasks.
                rerun(format_gap_feedback(report.uncovered_rows), scope, report.uncovered_rows)
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

    # Marker distribution (VISIBILITY ONLY — never a cap; a table-driven test may
    # legitimately cover several related VBs). Surfaces marker stacking for audit.
    try:
        from codd.verifiable_behavior_audit import summarize_marker_distribution

        distribution = summarize_marker_distribution(
            build_vb_coverage_audit(project_root, config=config)
        )
        if distribution:
            top = "; ".join(f"{path}×{count}" for path, count in list(distribution.items())[:3])
            echo(
                f"Test coverage gate: `covers` markers span {len(distribution)} test file(s) "
                f"(most-marked: {top})."
            )
    except Exception:  # noqa: BLE001 — a reporting helper must never gate the build.
        pass

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
    if auth.passed:
        echo(
            f"Test coverage gate: marker authenticity OK ({len(auth.degraded_paths)} file(s) stage-1-only)."
        )
        return

    # Authenticity FAILED. Unlike the coverage gate (which already re-drives the
    # owning test tasks with gap feedback above), the authenticity gate used to
    # fail-closed immediately here — a permanent RED the moment the model wrote
    # one orphan/assertion-less marker, with no way to feed the finding back and
    # let it converge (the ExprCalc Python greenfield dogfood stalled here with
    # 24 such markers). Mirror the coverage loop's semantics: on an authenticity
    # failure, re-drive the TEST tasks with the verbatim findings + the closed VB
    # contract, bounded by greenfield.vb_rework.max_rounds, and let the UNCHANGED
    # deterministic gate re-judge. The gate is NEVER loosened — the model only
    # gets bounded retries. When rework is unwired (DI callers with no rerun) or
    # turned off (max_rounds = 0), the legacy immediate fail-closed applies.
    max_rounds = _vb_rework_max_rounds(config)
    if rerun is None or scope_resolver is None or max_rounds <= 0:
        for violation in auth.violations:
            echo(violation.message)
        raise StageError(_authenticity_failure_message(auth.violations))

    _drive_vb_authenticity_rework(
        project_root,
        config=config,
        authenticity_profile=authenticity_profile,
        echo=echo,
        rerun=rerun,
        rerun_oracle=rerun_oracle,
        scope_resolver=scope_resolver,
        max_rounds=max_rounds,
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
    def _run_repair(seed_result: Any) -> Any:
        seed_failure = seed_result.failure or VerificationFailureReport(
            check_name="verify",
            failed_nodes=[],
            error_messages=[item.message for item in seed_result.failures],
            dag_snapshot={},
            timestamp=_utc_now(),
        )
        return RepairLoop(loop_config, project_root).run(
            seed_failure,
            dag,
            verify_callable=lambda: run_standalone_verify(project_root),
            initial_verify_result=seed_result,
        )

    outcome = RepairLoop(loop_config, project_root).run(
        failure,
        dag,
        verify_callable=lambda: run_standalone_verify(project_root),
        initial_verify_result=result,
    )
    if outcome.status != "REPAIR_SUCCESS":
        # F7 — impl-blind test RE-DERIVATION intercept. When repair dead-ended on a
        # DEFECTIVE test transcription (a scope-guard block whose offenders were all
        # test files — T1 — or a legal ``test_defect_claim`` — T2), route the defect
        # to the phase that HAS test-write authority: re-derive the named test(s)
        # STRICTLY from the design + VB contract (write-fenced), then let a FRESH
        # verify decide green. No arbiter — the design arbitrates operationally. On
        # RED, a BOUNDED FIXPOINT loop re-enters re-derivation while follow-up repair
        # keeps surfacing FRESH blocked tests for DISTINCT tasks (the per-task budget
        # bounds it → no oscillation) before the honest terminal.
        rederived = _drive_test_rederivation(
            project_root, outcome, config, ai_command, echo, run_repair=_run_repair
        )
        if rederived is not None:
            return rederived
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


def _drive_test_rederivation(
    project_root: Path,
    outcome: Any,
    config: dict[str, Any],
    ai_command: str | None,
    echo: Callable[[str], None],
    *,
    run_repair: Callable[[Any], Any],
) -> str | None:
    """F7 route — re-derive blocked test transcription(s), then fresh-verify + certify.

    A BOUNDED FIXPOINT LOOP (F7.1 part C). One re-derivation draw per RUN is at most
    ``max_per_task`` per task; when a post-re-derivation follow-up repair surfaces
    FRESH blocked test(s) for a DIFFERENT (not-yet-spent) task, the loop RE-ENTERS
    ``run_test_rederivation`` with the SAME ``budget_used`` dict. Termination is
    guaranteed: every RUNNING iteration strictly consumes ≥1 unspent per-task budget,
    so Σ ≤ ``max_per_task`` × |tasks|; a repeat claim on an already-spent task returns
    ``not_applicable`` → the loop exits. There is NO new knob — the per-run budget is
    emergent (Σ per-task). This SUPERSEDES the old hard ONE-loop count, which wrongly
    capped the independent-second-task case (the per-task budget IS the oscillation
    guard).

    Returns a success detail string when a fresh verify goes GREEN after
    re-derivation (the caller returns it), or ``None`` when re-derivation never
    applied at all (the caller keeps the original honest terminal). Raises
    :class:`StageError` on an honest terminal that NAMES the re-derivation state — it
    NEVER surfaces a "produced 0 generated files" stage crash for a blocked-test
    outcome (a crashed draw is contained inside ``run_test_rederivation`` as RED).
    """
    from codd.greenfield.test_rederivation import (
        STATUS_GREEN,
        blocked_test_paths,
        rederivation_enabled,
        run_test_rederivation,
    )
    from codd.implementer import implement_tasks
    from codd.repair.verify_runner import run_standalone_verify

    if not rederivation_enabled(config) or not blocked_test_paths(outcome):
        return None

    try:
        tasks = _default_task_lister(project_root)
    except Exception:  # noqa: BLE001 — no derivable tasks ⇒ nothing to re-derive.
        return None

    def _implement_runner(task: ImplementTaskRef, feedback: str) -> None:
        # The implement WRITE-FENCE (:4682-4684 vicinity) KEEPS the path resolver —
        # that's correct HERE (where MAY this task write). Ownership no longer uses it.
        output_paths = (
            list(task.output_paths) if task.output_paths else _output_paths_for_task(config, task)
        )
        implement_tasks(
            project_root,
            design=task.design_node,
            output_paths=output_paths,
            expected_outputs=list(task.expected_outputs),
            task_title=task.title,
            task_description=task.description,
            ai_command=ai_command,
            use_derived_steps=True,
            feedback=feedback,
        )

    def _oracle_check() -> Any:
        """One-shot native-oracle run for transcription acceptance (rerun=None).

        Best-effort: an oracle that cannot run here (unresolvable stack /
        uncertifiable scope) returns ``None`` → the re-derivation degrades to its
        prior behavior with the fresh verify as the backstop — never a new
        failure mode, never a silent pass (verify still gates)."""
        from codd.implement_oracle import run_implement_oracle_gate

        project_section = (
            config.get("project") if isinstance(config.get("project"), dict) else {}
        )
        scan = config.get("scan") if isinstance(config.get("scan"), dict) else {}
        try:
            return run_implement_oracle_gate(
                project_root,
                language=(
                    project_section.get("language")
                    if isinstance(project_section, dict)
                    else None
                ),
                project_name=str(
                    (project_section or {}).get("name") or project_root.name
                ),
                source_dirs=scan.get("source_dirs") if isinstance(scan, dict) else None,
                test_dirs=scan.get("test_dirs") if isinstance(scan, dict) else None,
                config=config,
                echo=echo,
            )
        except Exception:  # noqa: BLE001 — best-effort acceptance probe.
            return None

    budget_used: dict[str, int] = {}
    current = outcome
    any_ran = False
    while True:
        rederived = run_test_rederivation(
            project_root,
            outcome=current,
            config=config,
            tasks=tasks,
            implement_runner=_implement_runner,
            verify=lambda: run_standalone_verify(project_root),
            echo=echo,
            budget_used=budget_used,
            history_session_dir=getattr(outcome, "history_session_dir", None),
            trigger="T2" if getattr(current, "test_defect_claim", None) else "T1",
            oracle_check=_oracle_check,
        )

        if rederived.status == STATUS_GREEN:
            # GREEN only via fresh verify; re-run standalone once more so the
            # executed-anything honesty gate also covers the re-derived state
            # (pipeline.py:4608-4614 re-check parity).
            final = run_standalone_verify(project_root)
            if not final.passed:
                raise StageError(
                    "test re-derivation reported green but a fresh verification failed "
                    f"({len(final.failures)} failure(s))"
                )
            _certify_verify_executed(project_root, final)
            return (
                "verification passed after impl-blind test re-derivation "
                f"(tasks: {', '.join(rederived.rederived_tasks) or 'n/a'})"
            )

        if not rederived.ran:
            # Re-derivation did not apply THIS iteration. A budget-block (skipped)
            # arises when the owning task's budget is already spent — an honest
            # terminal (no oscillation). A re-entry that resolves to nothing (empty
            # skipped) after we ALREADY re-derived is still an honest terminal.
            if rederived.skipped_paths:
                raise StageError(
                    "verification failed and automatic repair blocked test edit(s); "
                    f"test re-derivation did not apply ({rederived.reason}). "
                    f"Blocked test path(s): {', '.join(blocked_test_paths(current))}."
                )
            if any_ran:
                raise StageError(
                    "verification failed; impl-blind test re-derivation occurred but "
                    f"did not converge ({rederived.reason})."
                )
            return None  # nothing ever re-derived → keep the original honest terminal

        any_ran = True
        # RED after re-derivation. A fresh verify (a re-derivation may already be
        # green even though the outcome carried RED), else a follow-up repair.
        result2 = run_standalone_verify(project_root)
        if result2.passed:
            _certify_verify_executed(project_root, result2)
            return "verification passed after impl-blind test re-derivation"
        followup = run_repair(result2)
        if followup.status == "REPAIR_SUCCESS":
            final = run_standalone_verify(project_root)
            if not final.passed:
                raise StageError(
                    "automatic repair reported success but a fresh verification failed "
                    f"({len(final.failures)} failure(s))"
                )
            _certify_verify_executed(project_root, final)
            return "verification passed after test re-derivation + repair"

        # FIXPOINT: does the follow-up name FRESH blocked test(s)? If so, re-enter
        # with the SAME budget dict. A repeat claim on an already-spent task will hit
        # the budget gate inside run_test_rederivation next iteration (→ not_applicable
        # → honest StageError above); an independent second task consumes its own
        # unspent budget. Either way the loop is bounded by Σ per-task budget.
        if blocked_test_paths(followup):
            current = followup
            continue

        # No new blocked test path → honest terminal naming the re-derivation state.
        raise StageError(
            "verification failed; impl-blind test re-derivation occurred "
            f"(tasks: {', '.join(rederived.rederived_tasks) or 'n/a'}) and the re-derived "
            "test still fails a fresh verify (a genuine impl/design defect, or the "
            "transcription did not converge within the re-derivation budget)."
        )


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


def _ci_scaffold_profile_test_command(project_root: Path) -> str | None:
    """Fallback test command sourced from the resolved language profile's verify
    command (``commands[<verify.command>].argv``) — closes the ci_scaffold gap
    for stacks :func:`codd.test_detection.detect_test_command` has NO file
    heuristic for at all (Maven/``pom.xml`` — surfaced by the Java greenfield
    dogfood; C#/``*.csproj``+``*.sln`` and C++/``CMakeLists.txt`` share the same
    gap and the same fix for free, since this reads the profile generically).

    Deliberately the LOWEST priority — consulted ONLY when ``detect_test_command``
    already returned ``None`` — never higher. A profile's verify campaign may
    legitimately run a DIFFERENT (often stricter) command than the legacy
    heuristic for a stack that already has one: Go's profile campaign is
    ``go test -json ./...`` (adds a machine-readable report) while the legacy
    heuristic is the simpler ``go test ./...`` — an intentional, documented
    divergence (see ``tests/languages/test_verify_plan.py``'s shadow-mode
    comparison), not a bug to reconcile here. Firing only on ``None`` means this
    can only turn an honest ``StageError`` into a real, profile-sourced command;
    it can never override an already-working (and possibly deliberately
    different) heuristic answer for Go/Python/JS/TS/Rust/bats/Make projects.

    Reuses ``commands.verify`` — the SAME argv the coverage-execution-coherence
    campaign runs (:func:`codd.project_types._synthesize_verify_campaign`) — so
    CI authenticity stays sourced from ONE place for these stacks too, instead
    of a second, independently-maintained guess.

    Declines (returns ``None``, falling through to the pre-existing
    ``StageError``) when: no ``project.language`` is configured; the profile
    declares no ``verify.command`` or no matching ``commands`` entry; the argv
    is empty; the command's ``cwd`` is not repo-root (this fallback assumes
    plain ``run:`` execution right after checkout, no ``working-directory:``);
    or the argv references a ``{test_root}``/``{report}`` substitution
    placeholder (e.g. JS/TS's vitest campaign) — that shape needs the full
    :class:`codd.project_types.VerifyCampaignSpec` resolution machinery, not a
    bare shell string.
    """
    from codd.config import load_project_config
    from codd.languages import resolve_language_profile

    try:
        config = load_project_config(project_root)
    except (FileNotFoundError, ValueError):
        return None
    try:
        profile = resolve_language_profile(config)
    except Exception:  # noqa: BLE001 — unknown/unsupported language → no fallback, never a crash.
        return None
    if profile is None or profile.verify is None:
        return None
    command_id = profile.verify.command
    if not command_id:
        return None
    command_spec = profile.commands.get(command_id)
    if command_spec is None or not command_spec.argv:
        return None
    if command_spec.cwd not in (None, ".", "{module_root}"):
        return None
    argv = command_spec.argv
    if any("{" in arg for arg in argv):
        return None
    return shlex.join(argv)


def _default_ci_scaffold_runner(project_root: Path, *, ai_command: str | None = None) -> str:
    """Author a minimal, authentic CI workflow so a freshly built system is
    CI-ready and the final ``check`` gate's ci_health requirement is satisfied
    honestly (surfaced by the C-Go greenfield dogfood, where a clean build
    failed ``check`` on ``ci_workflow_missing``).

    DETERMINISTIC by design, not AI-freeform (which could emit a hollow
    ``run: true`` that games ci_health) and not an auto-opt-out (which would
    silently declare the system needs no CI). The workflow runs the project's
    REAL test command, resolved in two tiers:

    1. :func:`codd.test_detection.detect_test_command` — the SAME legacy
       heuristic ladder the basic verify runner uses (pytest/npm/Cargo/go.mod/
       bats/Makefile). Explicit ``verify.test_command`` / ``fix.test_command``
       config IS honored here (the codd.yaml-documented escape hatch below) —
       this call now passes ``config=``, which it previously did not.
    2. :func:`_ci_scaffold_profile_test_command` — a FALLBACK consulted only
       when (1) finds nothing, sourcing the command from the resolved language
       profile's ``commands.verify`` (Maven/``mvn -q verify``, and
       C#/C++'s equivalents — stacks tier 1 has no file heuristic for at all;
       their REAL verify proof already comes entirely from the profile-owned
       coverage-execution-coherence campaign, never from tier 1). See that
       function's docstring for why its priority is deliberately LOWEST, not
       highest.

    A project that reached this stage green necessarily has a detectable test
    command from one of the two tiers (verify ran it, one way or the other), so
    the scaffold succeeds for exactly the projects verify could validate.

    Idempotent: an existing workflow, or an explicit ``ci.provider=none``
    opt-out, is left untouched.
    """
    del ai_command  # signature parity with sibling runners; no AI call needed.
    from codd.config import load_project_config
    from codd.test_detection import detect_test_command

    existing = sorted(project_root.glob(".github/workflows/*.yml")) + sorted(
        project_root.glob(".github/workflows/*.yaml")
    )
    if existing:
        rel = existing[0].relative_to(project_root).as_posix()
        return f"skipped — CI workflow already present ({rel})"

    if _ci_opt_out_declared(project_root):
        return "skipped — ci.provider=none opt-out declared in codd.yaml"

    try:
        ci_config = load_project_config(project_root)
    except (FileNotFoundError, ValueError):
        ci_config = None
    test_command = detect_test_command(project_root, config=ci_config) or _ci_scaffold_profile_test_command(
        project_root
    )
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
