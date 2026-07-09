"""F7 — impl-blind test RE-DERIVATION route (arbitration without an arbiter).

The greenfield ② rate unblocker. A generated test is a *transcription* of the
design, not an oracle; the design is the oracle. When repair blocks on a
DEFECTIVE test transcription — an assertion NO design-conforming implementation
can satisfy (a tautology like ``expect(false).toBe(true)``, or a wrong
transcription constant contradicting a design pin) — verify/repair has no
test-write authority and the run dies at REPAIR_FAILED. This module routes that
defect to the phase that HAS the authority (implement's test authorship): it
re-derives the named test(s) STRICTLY from the design + VB contract, then lets a
FRESH verify decide green. There is NO arbiter: no LLM verdict rules test-vs-code;
the design arbitrates operationally, because a re-derived test that copies the old
broken assertion stays RED and a genuinely-buggy impl leaves the re-derived test
RED too — nothing is conceded either way.

ANTI-FALSE-GREEN (the invariants this module must hold — see the Fable5 ruling
``dogfood/fable5_reply_2026-07-10_js-repair-direction.md`` §④):

* The re-derivation draw is the ORIGINAL authoring distribution MINUS the SUT:
  design closure + VB contract + dependency-producer files + the CURRENT test file
  — and NEVER the owning task's src bodies, NEVER verify observations. This module
  carries a FIXED feedback string with NO verify output, NO expected/received
  values, NO RCA/proposal text, so no information flows from a buggy impl into the
  new test.
* A human-authored test WITHOUT the codd generation header is NEVER re-derived
  (the brownfield safety line).
* The regenerated file must pass the UNCHANGED implement-side gates (VB
  marker-authenticity + coverage reconciliation) BEFORE verify runs — dropping a
  disputed ``covers vb=`` marker fails the build, not the gate.
* Repair still cannot edit tests; the scope guard is byte-unchanged. GREEN is
  decided ONLY by a fresh verify. The rerun is WRITE-FENCED to the test paths (the
  same ``_OracleWriteFence`` the VB gate uses) so even a model editing src is undone.
* Bounded: ``repair.test_rederivation.max_per_task`` (default 1/run) — a second
  claim on the same task is budget-blocked, so there is no oscillation.

GENERALITY: everything here is language-free DATA — paths, tasks, the codd
generation header, the write-fence. No ``language ==`` dispatch, no per-language
literals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping


#: The design-only re-derivation feedback. It carries NO verify output, NO
#: expected/received values, and NO RCA / proposal text — only the instruction to
#: re-derive every expected value from the design + VB contract. This is the
#: structural anti-false-green line: nothing observed from the (possibly buggy)
#: implementation can leak into the regenerated test.
REDERIVATION_FEEDBACK = (
    "a previous transcription of this design node's tests contained expectations "
    "not derivable from the design; re-derive EVERY expected value strictly from "
    "the design documents and the VB contract; do not carry over any expectation "
    "without re-deriving it"
)

#: The provenance marker every codd-generated file carries (see
#: ``codd.implementer._build_traceability_comment``). A test file WITHOUT it is
#: human-authored and is NEVER re-derived (the brownfield safety line).
_CODD_GENERATION_HEADER = "@generated-by: codd implement"

#: Status values for a re-derivation attempt.
STATUS_GREEN = "green"
STATUS_RED = "red"
STATUS_NOT_APPLICABLE = "not_applicable"


@dataclass
class RederivationOutcome:
    """Outcome of one F7 re-derivation attempt."""

    status: str
    trigger: str = ""
    rederived_paths: list[str] = field(default_factory=list)
    rederived_tasks: list[str] = field(default_factory=list)
    skipped_paths: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def ran(self) -> bool:
        """True when a re-derivation actually executed (green or red verdict)."""
        return self.status in (STATUS_GREEN, STATUS_RED)


# ── config knobs ─────────────────────────────────────────────────────────────

def rederivation_enabled(config: Mapping[str, Any] | None) -> bool:
    """``repair.test_rederivation.enabled`` — default-ON (F7 is a bounded green
    convergence route whose worst failure is one wasted fenced rerun at a
    currently-guaranteed-death point). Any explicit ``false`` opts out."""
    section = _rederivation_section(config)
    if "enabled" in section:
        return bool(section.get("enabled"))
    return True


def rederivation_max_per_task(config: Mapping[str, Any] | None) -> int:
    """``repair.test_rederivation.max_per_task`` — re-derivation budget per task per
    run (default 1). A non-positive / non-integer value falls back to the default."""
    section = _rederivation_section(config)
    if "max_per_task" in section:
        try:
            value = int(section.get("max_per_task"))
        except (TypeError, ValueError):
            return 1
        return value if value >= 1 else 1
    return 1


def _rederivation_section(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    repair = config.get("repair") if isinstance(config, Mapping) else None
    if isinstance(repair, Mapping):
        section = repair.get("test_rederivation")
        if isinstance(section, Mapping):
            return section
    return {}


# ── outcome inspection ───────────────────────────────────────────────────────

def blocked_test_paths(outcome: Any) -> list[str]:
    """The union of the outcome's T1 ``blocked_test_paths`` and the T2 claim files.

    Both channels name test paths a design-conforming impl cannot satisfy: T1 is
    the guard-event (the model tried to patch the test → scope guard blocked it),
    T2 is the legal claim (the model reported the defect without touching the test).
    """
    paths: list[str] = []
    seen: set[str] = set()

    def _add(raw: Any) -> None:
        text = _norm(raw)
        if text and text not in seen:
            seen.add(text)
            paths.append(text)

    for raw in list(getattr(outcome, "blocked_test_paths", None) or []):
        _add(raw)
    for entry in list(getattr(outcome, "test_defect_claim", None) or []):
        if isinstance(entry, Mapping):
            _add(entry.get("file"))
    return paths


# ── provenance + task mapping ────────────────────────────────────────────────

def has_codd_generation_header(project_root: Path, rel_path: str) -> bool:
    """True when the file at ``rel_path`` bears the codd generation header.

    A header-less test is human-authored and is NEVER re-derived (PROVENANCE — the
    brownfield safety line). A missing / unreadable file is treated as NOT
    codd-generated (fail-closed toward never re-deriving a file we cannot vouch for).
    """
    resolved = (Path(project_root) / rel_path)
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError:
        return False
    return _CODD_GENERATION_HEADER in text


def owning_task_for_path(
    rel_path: str,
    tasks: list[Any],
    *,
    config: Mapping[str, Any] | None,
    path_resolver: Callable[[Mapping[str, Any], Any], list[str]] | None,
) -> Any | None:
    """The implement task whose declared/resolved outputs OWN ``rel_path``.

    A task owns the path when the path equals — or sits under — one of its output
    paths (declared ``output_paths``, else resolved via ``path_resolver``) or its
    declared ``expected_outputs``. Returns the FIRST such task, or ``None`` when no
    derived task claims the path (a path with no owning task is never re-derived).
    """
    target = _norm(rel_path)
    if not target:
        return None
    for task in tasks:
        for candidate in _task_output_paths(task, config, path_resolver):
            owned = _norm(candidate)
            if owned and (target == owned or target.startswith(owned + "/")):
                return task
        for candidate in getattr(task, "expected_outputs", ()) or ():
            owned = _norm(candidate)
            if owned and (target == owned or target.startswith(owned + "/")):
                return task
    return None


def _task_output_paths(
    task: Any,
    config: Mapping[str, Any] | None,
    path_resolver: Callable[[Mapping[str, Any], Any], list[str]] | None,
) -> list[str]:
    declared = list(getattr(task, "output_paths", ()) or [])
    if declared:
        return declared
    if path_resolver is not None:
        try:
            return list(path_resolver(dict(config or {}), task) or [])
        except Exception:  # noqa: BLE001 — a task whose paths fail contributes none.
            return []
    return []


# ── the route ────────────────────────────────────────────────────────────────

def run_test_rederivation(
    project_root: Path,
    *,
    outcome: Any,
    config: Mapping[str, Any] | None,
    tasks: list[Any],
    implement_runner: Callable[[Any, str], None],
    verify: Callable[[], Any],
    echo: Callable[[str], None] = print,
    path_resolver: Callable[[Mapping[str, Any], Any], list[str]] | None = None,
    implement_gate: Callable[[], None] | None = None,
    budget_used: dict[str, int] | None = None,
    history_session_dir: Path | None = None,
    trigger: str = "",
    test_dirs: list[str] | None = None,
) -> RederivationOutcome:
    """Re-derive the blocked test transcription(s) from the design, then fresh-verify.

    Gating (in order): the feature is enabled; the outcome carries blocked test
    paths; each path bears the codd generation header; it maps to an owning derived
    task; that task's re-derivation budget is unspent. Only paths clearing EVERY
    gate are re-derived — the rest are recorded as skipped and the caller keeps the
    honest terminal for them.

    Execution: build a WRITE-FENCE to the eligible test paths + the configured test
    dirs, re-run each owning task under the design-only feedback INSIDE the fence
    (any out-of-scope write is mechanically reverted), consume the budget, re-check
    the UNCHANGED implement-side gates, then run a FRESH verify. GREEN only via that
    fresh verify.
    """
    project_root = Path(project_root)
    budget = budget_used if budget_used is not None else {}
    max_per_task = rederivation_max_per_task(config)

    if not rederivation_enabled(config):
        return RederivationOutcome(
            STATUS_NOT_APPLICABLE, trigger=trigger,
            reason="test re-derivation disabled (repair.test_rederivation.enabled=false)",
        )

    paths = blocked_test_paths(outcome)
    if not paths:
        return RederivationOutcome(
            STATUS_NOT_APPLICABLE, trigger=trigger,
            reason="no blocked test paths / test_defect_claim on the outcome",
        )

    # Resolve eligible (path, task) pairs, recording why each ineligible path is
    # skipped so the caller can produce an HONEST terminal.
    eligible: list[tuple[str, Any]] = []
    skipped: list[str] = []
    budget_blocked = False
    header_missing = False
    for path in paths:
        if not has_codd_generation_header(project_root, path):
            # PROVENANCE: a human-authored test is NEVER re-derived.
            skipped.append(path)
            header_missing = True
            echo(f"[greenfield] test re-derivation: '{path}' has no codd generation header — human-authored, never re-derived.")
            continue
        task = owning_task_for_path(path, tasks, config=config, path_resolver=path_resolver)
        if task is None:
            skipped.append(path)
            echo(f"[greenfield] test re-derivation: '{path}' maps to no derived task — not re-derived.")
            continue
        task_id = str(getattr(task, "task_id", "") or "")
        if budget.get(task_id, 0) >= max_per_task:
            skipped.append(path)
            budget_blocked = True
            echo(f"[greenfield] test re-derivation: task '{task_id}' budget spent ({max_per_task}/run) — not re-derived (no oscillation).")
            continue
        eligible.append((path, task))

    if not eligible:
        reason = "no eligible test path (header-less / unmapped / budget-blocked)"
        if budget_blocked:
            reason = "re-derivation budget already spent for the owning task(s) this run"
        elif header_missing:
            reason = "blocked test(s) are human-authored (no codd generation header) — never re-derived"
        return RederivationOutcome(
            STATUS_NOT_APPLICABLE, trigger=trigger, skipped_paths=skipped, reason=reason,
        )

    eligible_paths = [path for path, _task in eligible]
    # De-dupe owning tasks, order-stable.
    eligible_tasks: list[Any] = []
    eligible_task_ids: list[str] = []
    for _path, task in eligible:
        task_id = str(getattr(task, "task_id", "") or "")
        if task_id not in eligible_task_ids:
            eligible_task_ids.append(task_id)
            eligible_tasks.append(task)

    old_hashes = {p: _file_hash(project_root / p) for p in eligible_paths}

    # WRITE-FENCE to the eligible test paths + the configured test dirs (the test
    # surface only). Reuse the shipped ``_OracleWriteFence`` (the vb_rerun_scope
    # fence): it mechanically reverts any out-of-scope write, so even a model that
    # tries to edit src cannot. Lazy import to avoid a module-load cycle.
    from codd.greenfield.pipeline import _OracleWriteFence

    allowed = _fence_allowed_paths(eligible_paths, test_dirs, config)
    echo(
        f"[greenfield] test re-derivation ({trigger or 'blocked-test'}): re-deriving "
        f"{len(eligible_paths)} test file(s) from the design across {len(eligible_tasks)} "
        f"task(s); fenced to {len(allowed)} test path(s)."
    )
    with _OracleWriteFence(project_root, allowed_paths=allowed, echo=echo) as fence:
        for task in eligible_tasks:
            implement_runner(task, REDERIVATION_FEEDBACK)
        fence.enforce()

    # Consume the budget for every re-driven task (a second claim on the same task
    # this run is now budget-blocked → no oscillation).
    for task_id in eligible_task_ids:
        budget[task_id] = budget.get(task_id, 0) + 1

    new_hashes = {p: _file_hash(project_root / p) for p in eligible_paths}
    _record_event(
        history_session_dir,
        trigger=trigger,
        paths=eligible_paths,
        tasks=eligible_task_ids,
        old_hashes=old_hashes,
        new_hashes=new_hashes,
    )

    # The regenerated test file must pass the UNCHANGED implement-side gates (VB
    # marker-authenticity + coverage reconciliation) BEFORE verify runs — dropping a
    # disputed ``covers vb=`` marker fails the build here, not the gate.
    gate = implement_gate if implement_gate is not None else _default_implement_gate(project_root, config, echo)
    try:
        gate()
    except Exception as exc:  # noqa: BLE001 — a failed implement-side gate is RED, not a crash.
        echo(f"[greenfield] test re-derivation: regenerated test failed the implement-side gate ({exc}).")
        return RederivationOutcome(
            STATUS_RED, trigger=trigger, rederived_paths=eligible_paths,
            rederived_tasks=eligible_task_ids, skipped_paths=skipped,
            reason=f"regenerated test failed implement-side gate: {exc}",
        )

    # GREEN is decided ONLY by a fresh verify.
    result = verify()
    if bool(getattr(result, "passed", False)):
        echo("[greenfield] test re-derivation: fresh verify GREEN after re-deriving the test(s) from the design.")
        return RederivationOutcome(
            STATUS_GREEN, trigger=trigger, rederived_paths=eligible_paths,
            rederived_tasks=eligible_task_ids, skipped_paths=skipped,
            reason="fresh verify green after re-derivation",
        )
    echo("[greenfield] test re-derivation: fresh verify STILL RED after re-derivation (a real impl/design defect or an unconverged transcription).")
    return RederivationOutcome(
        STATUS_RED, trigger=trigger, rederived_paths=eligible_paths,
        rederived_tasks=eligible_task_ids, skipped_paths=skipped,
        reason="re-derived test still fails fresh verify",
    )


# ── helpers ──────────────────────────────────────────────────────────────────

def _fence_allowed_paths(
    eligible_paths: list[str], test_dirs: list[str] | None, config: Mapping[str, Any] | None
) -> tuple[str, ...]:
    """Write-fence allow-set: the eligible test files UNION the configured test dirs.

    Test dirs are permitted so a re-derivation that legitimately touches a sibling
    test helper under the same test tree is not reverted, while production source
    stays out of scope (a test rewrite can never rewrite src)."""
    allowed: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        norm = _norm(raw)
        if norm and norm not in seen:
            seen.add(norm)
            allowed.append(norm)

    for path in eligible_paths:
        _add(path)
    dirs = test_dirs if test_dirs is not None else _configured_test_dirs(config)
    for prefix in dirs:
        _add(prefix)
    return tuple(allowed)


def _configured_test_dirs(config: Mapping[str, Any] | None) -> list[str]:
    scan = config.get("scan") if isinstance(config, Mapping) else None
    raw = scan.get("test_dirs") if isinstance(scan, Mapping) else None
    if isinstance(raw, list) and raw:
        return [str(item) for item in raw]
    return ["tests/"]


def _default_implement_gate(
    project_root: Path, config: Mapping[str, Any] | None, echo: Callable[[str], None]
) -> Callable[[], None]:
    """The UNCHANGED implement-side coverage/authenticity gate as a callable.

    Raises when the regenerated suite leaves a declared verifiable behavior
    uncovered (a dropped ``covers vb=`` marker) or a non-credible marker. Best-effort
    resolution of the audit helpers; a project with no VB surface is a strict no-op.
    """

    def _gate() -> None:
        cfg = dict(config or {})
        try:
            from codd.verifiable_behavior_audit import build_vb_coverage_audit
            from codd.vb_marker_authenticity import build_authenticity_report
        except Exception:  # noqa: BLE001 — no VB machinery ⇒ nothing to gate.
            return
        uncovered = build_vb_coverage_audit(project_root, config=cfg).uncovered_rows
        if uncovered:
            raise _ImplementGateError(
                f"{len(uncovered)} declared verifiable behavior(s) became uncovered "
                "after re-derivation (a re-derived test dropped a covering marker)."
            )
        auth = build_authenticity_report(project_root, config=cfg, strict_observability=True)
        if not auth.passed:
            raise _ImplementGateError(
                f"{len(auth.violations)} non-credible marker(s) after re-derivation "
                "(the re-derived test failed marker-authenticity)."
            )

    return _gate


class _ImplementGateError(RuntimeError):
    """Raised by the default implement-side gate when the regenerated test regresses it."""


def _record_event(
    history_session_dir: Path | None,
    *,
    trigger: str,
    paths: list[str],
    tasks: list[str],
    old_hashes: dict[str, str],
    new_hashes: dict[str, str],
) -> None:
    """Append the re-derivation event to ``<session>/test_rederivation.yaml``.

    Records paths, owning tasks, the trigger, and old/new content hashes so a run's
    re-derivation history is auditable. Best-effort: a write failure never aborts the
    route (the verdict already stands on the fresh verify)."""
    if history_session_dir is None:
        return
    try:
        import yaml

        session = Path(history_session_dir)
        session.mkdir(parents=True, exist_ok=True)
        record_path = session / "test_rederivation.yaml"
        events: list[Any] = []
        if record_path.exists():
            loaded = yaml.safe_load(record_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                events = loaded
        events.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "trigger": trigger,
                "paths": list(paths),
                "tasks": list(tasks),
                "old_hashes": dict(old_hashes),
                "new_hashes": dict(new_hashes),
            }
        )
        record_path.write_text(yaml.safe_dump(events, sort_keys=False), encoding="utf-8")
    except Exception:  # noqa: BLE001 — recording is observability, never a gate.
        return


def _file_hash(path: Path) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def _norm(raw: Any) -> str:
    text = str(raw or "").strip().replace("\\", "/")
    if not text:
        return ""
    if text.startswith("./"):
        text = text[2:]
    return text.strip("/")


__all__ = [
    "REDERIVATION_FEEDBACK",
    "RederivationOutcome",
    "STATUS_GREEN",
    "STATUS_NOT_APPLICABLE",
    "STATUS_RED",
    "blocked_test_paths",
    "has_codd_generation_header",
    "owning_task_for_path",
    "rederivation_enabled",
    "rederivation_max_per_task",
    "run_test_rederivation",
]
