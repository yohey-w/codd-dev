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
from fnmatch import fnmatchcase
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


#: The provenance marker naming the SOURCE design node a codd-generated file was
#: authored from (``@generated-from: <design-doc-path> (<node-id>)`` — see
#: ``codd.implementer._build_traceability_comment``). This is AUTHORSHIP ground
#: truth: the file itself records which design node produced it.
_GENERATED_FROM_MARKER = "@generated-from:"


def first_generated_from(project_root: Path, rel_path: str) -> tuple[str | None, str | None]:
    """Parse ``(design-doc-path, node-id)`` from a file's FIRST ``@generated-from``
    header, or ``(None, None)`` when the file is absent/unreadable/header-less.

    Comment-prefix-agnostic and language-free: the marker is located ANYWHERE on a
    line (past any ``//`` / ``#`` / ``--`` / ``/*`` banner), so every language's
    comment style parses identically — no per-language literal. Either half of the
    value may be empty (a path-only or node-id-only header)."""
    resolved = Path(project_root) / rel_path
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError:
        return (None, None)
    return _parse_generated_from(text)


def _parse_generated_from(text: str) -> tuple[str | None, str | None]:
    """Pure-text sibling of :func:`first_generated_from` (testable without a file)."""
    for line in text.splitlines():
        idx = line.find(_GENERATED_FROM_MARKER)
        if idx == -1:
            continue
        value = line[idx + len(_GENERATED_FROM_MARKER):].strip()
        if value.endswith("*/"):  # a block-comment banner close, if any.
            value = value[:-2].strip()
        if not value:
            continue
        node_id: str | None = None
        path = value
        if value.endswith(")") and "(" in value:
            open_idx = value.rfind("(")
            node_id = value[open_idx + 1:-1].strip() or None
            path = value[:open_idx].strip()
        return (_norm(path) or None, node_id)
    return (None, None)


# Owner-resolution ranks: an exact/glob declared match outranks a dir-prefix match.
_RANK_NONE = 0
_RANK_PREFIX = 1
_RANK_EXACT = 2


def owning_task_for_path(
    project_root: Path,
    rel_path: str,
    tasks: list[Any],
    *,
    config: Mapping[str, Any] | None,
) -> Any | None:
    """The implement task that AUTHORED the test file at ``rel_path``.

    Resolved by AUTHORSHIP EVIDENCE ONLY — NEVER the write-fence path resolver
    (which prefix-owns every test file and would mis-award a test to a no-output
    requirements gate task; that was the F7 live crash). In order:

    1. PROVENANCE (ground truth): the file's first ``@generated-from`` design node
       (path OR node-id) is matched to the task whose ``design_node`` equals it.
       Within that provenance-matched set, DECLARED evidence
       (``output_paths`` ∪ ``expected_outputs``) is ranked exact/glob (posix
       ``fnmatch``) ABOVE dir-prefix; a singleton owns outright.
    2. No provenance match → the SAME declared-evidence ranking across ALL tasks.
    3. Nothing resolves → ``None`` (fail-closed; the caller keeps the honest
       terminal — ``pipeline.py`` re-derivation-did-not-apply path).

    A task that authors no artifact (:func:`_task_declares_no_authored_artifact`)
    can NEVER own a test file and is excluded up front (belt-and-suspenders with
    provenance: a gate task neither matches provenance nor survives this filter)."""
    target = _norm(rel_path)
    if not target:
        return None

    # A no-authored-artifact task (a verification/gate task) can never own a test.
    candidates = [t for t in tasks if not _declares_no_authored_artifact(t, config)]
    if not candidates:
        return None

    prov_path, prov_node = first_generated_from(project_root, rel_path)
    if prov_path or prov_node:
        provenance_set = [t for t in candidates if _provenance_matches(t, prov_path, prov_node)]
        if provenance_set:
            if len(provenance_set) == 1:
                return provenance_set[0]  # provenance alone: a singleton owns outright
            ranked = _best_declared_owner(provenance_set, target)
            if ranked is not None:
                return ranked
            # Provenance vouches but no declared evidence disambiguates the shared
            # design node → first in order (stable), still within the provenance set.
            return provenance_set[0]

    # No provenance match (or a header-less file) → declared evidence across ALL tasks.
    return _best_declared_owner(candidates, target)


def _provenance_matches(task: Any, prov_path: str | None, prov_node: str | None) -> bool:
    design_raw = str(getattr(task, "design_node", "") or "").strip()
    if not design_raw:
        return False
    design_norm = _norm(design_raw)
    if prov_path and design_norm == prov_path:
        return True
    if prov_node:
        node = str(prov_node).strip()
        if node and (design_raw == node or design_norm == _norm(node)):
            return True
    return False


def _best_declared_owner(tasks: list[Any], target: str) -> Any | None:
    """The task with the strongest DECLARED-evidence match against ``target``
    (exact/glob outranks dir-prefix), order-stable on ties. ``None`` when no task
    declares evidence that owns the target."""
    best_task: Any | None = None
    best_rank = _RANK_NONE
    for task in tasks:
        rank = _declared_owner_rank(task, target)
        if rank > best_rank:
            best_rank = rank
            best_task = task
    return best_task


def _declared_owner_rank(task: Any, target: str) -> int:
    best = _RANK_NONE
    for candidate in _declared_evidence(task):
        owned = _norm(candidate)
        if not owned:
            continue
        if owned == target or fnmatchcase(target, owned):
            return _RANK_EXACT  # exact / glob match — the strongest, short-circuit.
        if target.startswith(owned + "/"):
            best = _RANK_PREFIX  # dir-prefix — keep looking for a stronger match.
    return best


def _declared_evidence(task: Any) -> list[Any]:
    """Declared authorship evidence: ``output_paths`` ∪ ``expected_outputs``."""
    evidence: list[Any] = []
    evidence.extend(getattr(task, "output_paths", ()) or ())
    evidence.extend(getattr(task, "expected_outputs", ()) or ())
    return evidence


def _declares_no_authored_artifact(task: Any, config: Mapping[str, Any] | None) -> bool:
    """Whether ``task`` authors no codebase artifact (a verification / release-gate
    or non-codebase task). Delegates to the pipeline classifier (lazy import to
    avoid a module-load cycle). A resolution failure fails OPEN (the task may own),
    so ownership is never silently lost to an import hiccup."""
    try:
        from codd.greenfield.pipeline import _task_declares_no_authored_artifact
    except Exception:  # noqa: BLE001 — classifier unavailable ⇒ do not exclude.
        return False
    try:
        return bool(_task_declares_no_authored_artifact(task, dict(config or {})))
    except Exception:  # noqa: BLE001 — a task the classifier cannot judge may own.
        return False


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
    implement_gate: Callable[[], None] | None = None,
    budget_used: dict[str, int] | None = None,
    history_session_dir: Path | None = None,
    trigger: str = "",
    test_dirs: list[str] | None = None,
    oracle_check: Callable[[], Any] | None = None,
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
        task = owning_task_for_path(project_root, path, tasks, config=config)
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
    def _run_draw(feedback: str) -> Exception | None:
        """One fenced re-derivation draw with CRASH CONTAINMENT (the honesty fix).

        A draw that raised (e.g. the implementer honestly emitting 0 files → an
        unhandled CoddCLIError, the F7 live crash) must never surface as a
        misleading stage crash: the write-fence rolls back to its ENTRY snapshot
        so a crashed draw leaves NO partial transcription (in- OR out-of-scope),
        and the exception is RETURNED for the caller's honest RED terminal.
        """
        with _OracleWriteFence(project_root, allowed_paths=allowed, echo=echo) as fence:
            try:
                for task in eligible_tasks:
                    implement_runner(task, feedback)
                fence.enforce()
            except Exception as exc:  # noqa: BLE001 — a crashed draw must NOT escape.
                fence.rollback()
                return exc
        return None

    crash = _run_draw(REDERIVATION_FEEDBACK)

    # Consume the budget for every re-driven task — a crash counts as a spent draw
    # (attempted == spent), so a crash cannot grant a re-roll. A second claim on the
    # same task this run is now budget-blocked → no oscillation.
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
        error=str(crash) if crash is not None else None,
    )

    if crash is not None:
        echo(
            "[greenfield] test re-derivation: the re-derivation draw crashed "
            f"({crash}); the write-fence rolled the tree back to entry (no partial "
            "transcription) and the task budget is consumed (a crash is not a re-roll)."
        )
        return RederivationOutcome(
            STATUS_RED, trigger=trigger, rederived_paths=eligible_paths,
            rederived_tasks=eligible_task_ids, skipped_paths=skipped,
            reason=f"re-derivation draw crashed: {crash}",
        )

    # NATIVE-ORACLE ACCEPTANCE — a transcription that fails the native oracle is
    # not a transcription. The re-derived test must COMPILE before coverage /
    # authenticity / fresh-verify mean anything (csharp3 exprcalc dogfood,
    # 2026-07-11: T2 re-derived an immutability test that ASSIGNED to read-only
    # properties — CS0200 — and the compile-red fresh verify was misread as "a
    # real impl/design defect"). One diagnostics-informed completion retry
    # belongs to the SAME draw: the budget above is already consumed and is NOT
    # re-claimed (a retry is a completion, not a second claim). Still failing
    # after the retry → an honest RED that NAMES the oracle. ``oracle_check`` is
    # opt-in (None → prior behavior; the fresh verify stays the backstop).
    if oracle_check is not None:
        ora = oracle_check()
        if ora is not None and _oracle_rejects_transcription(ora):
            diag = "; ".join(
                str(getattr(f, "message", "")).strip()
                for f in _non_environment_findings(ora)[:6]
                if str(getattr(f, "message", "")).strip()
            )
            echo(
                "[greenfield] test re-derivation: the transcription failed the "
                f"NATIVE ORACLE ({diag[:300] or 'no diagnostics'}); one "
                "diagnostics-informed completion retry (same draw, no new budget)."
            )
            retry_feedback = (
                REDERIVATION_FEEDBACK
                + "\n\nNATIVE-ORACLE DIAGNOSTICS — the re-derived test file(s) do "
                "not pass the toolchain (they must COMPILE). Fix the TEST "
                "transcription only; the diagnostics are:\n"
                + (diag or "(opaque toolchain failure)")
            )
            crash = _run_draw(retry_feedback)
            _record_event(
                history_session_dir,
                trigger=f"{trigger}-oracle-retry" if trigger else "oracle-retry",
                paths=eligible_paths,
                tasks=eligible_task_ids,
                old_hashes=new_hashes,
                new_hashes={p: _file_hash(project_root / p) for p in eligible_paths},
                error=str(crash) if crash is not None else None,
            )
            if crash is not None:
                echo(
                    "[greenfield] test re-derivation: the oracle-retry draw crashed "
                    f"({crash}); the write-fence rolled back (no partial transcription)."
                )
                return RederivationOutcome(
                    STATUS_RED, trigger=trigger, rederived_paths=eligible_paths,
                    rederived_tasks=eligible_task_ids, skipped_paths=skipped,
                    reason=f"re-derivation oracle-retry draw crashed: {crash}",
                )
            ora = oracle_check()
            if ora is not None and _oracle_rejects_transcription(ora):
                echo(
                    "[greenfield] test re-derivation: the transcription STILL fails "
                    "the native oracle after the retry — honest RED (an invalid "
                    "transcription, not evidence of an impl/design defect)."
                )
                return RederivationOutcome(
                    STATUS_RED, trigger=trigger, rederived_paths=eligible_paths,
                    rederived_tasks=eligible_task_ids, skipped_paths=skipped,
                    reason=(
                        "re-derived test failed the native oracle after one retry "
                        "(the transcription does not compile)"
                    ),
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

def _non_environment_findings(ora: Any) -> list[Any]:
    """The oracle findings that are SUT-fixable (not environment_build_error)."""
    return [
        f
        for f in list(getattr(ora, "findings", ()) or ())
        if str(getattr(f, "category", "") or "") != "environment_build_error"
    ]


def _oracle_rejects_transcription(ora: Any) -> bool:
    """Whether a native-oracle result REJECTS the re-derived transcription.

    Only a failed result carrying at least one NON-environment finding rejects:
    an environment-only red (missing toolchain, opaque env failure — the
    zero-infra clause) is never the transcription's fault, and a failed result
    with no findings proves nothing about the test file. Both degrade to the
    prior behavior (fresh verify stays the backstop) — anti-false-RED without a
    false-green (verify still gates).
    """
    if bool(getattr(ora, "passed", True)):
        return False
    return bool(_non_environment_findings(ora))


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
    error: str | None = None,
) -> None:
    """Append the re-derivation event to ``<session>/test_rederivation.yaml``.

    Records paths, owning tasks, the trigger, old/new content hashes, and — when the
    draw crashed — the ``error`` field, so a run's re-derivation history (including a
    contained crash) is auditable. Best-effort: a write failure never aborts the
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
        event: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trigger": trigger,
            "paths": list(paths),
            "tasks": list(tasks),
            "old_hashes": dict(old_hashes),
            "new_hashes": dict(new_hashes),
        }
        if error is not None:
            event["error"] = error
        events.append(event)
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
    "first_generated_from",
    "has_codd_generation_header",
    "owning_task_for_path",
    "rederivation_enabled",
    "rederivation_max_per_task",
    "run_test_rederivation",
]
