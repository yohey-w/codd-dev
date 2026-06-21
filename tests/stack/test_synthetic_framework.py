"""Contract Kernel — v2.77h SYNTHETIC FRAMEWORK EXTENSIBILITY proof (the Cut-B heart).

THE WHOLE POINT (mandatory for claiming framework-pluggable): a brand-new framework
is addable to the LIVE stack pipeline with NO change to any core file. The ONLY
artifacts this test introduces are a synthetic :class:`~codd.stack.profile.FrameworkProfile`
("synthframework") + a synthetic obligation CHECKER, both injected through the REAL
registration seams — the default framework registry's profile cache
(``default_framework_registry._profiles``) and the obligation-checker registry
(``codd.stack.adapters.OBLIGATION_CHECKERS``), via ``monkeypatch.setitem``. No file
under ``codd/`` is created or edited to make this pass — that is the generality proof
(mirrors ``tests/languages/test_oracle_synthetic_language.py`` for the LANGUAGE layer,
extended here to the framework-stack layer).

The synthetic framework FLOWS THROUGH the real greenfield pipeline a-f
(``_intake_stack_contract`` → ``_enforce_stack_lock`` → ``_materialize_stack_commands``
→ authenticity → ``_enforce_stack_obligations``) exactly like the curated Next.js/
Prisma/Playwright stack — driven by the SAME ``_StageStubPipeline`` harness the
v2.77a-e tests use (real ``run()`` gates, stage BODIES stubbed; a recording executor
so the composed commands are "invoked" without real tooling). The live pipeline READS
the synthetic obligation: satisfied → GREEN, broken → RED, and the obligation is proven
ENFORCED (executed), never silently skipped.

anti-false-green is paramount (the cardinal rule): the RED assertions prove a broken
synthetic obligation is NOT waved through (it reds for the RIGHT reason — the synthetic
checker firing), and the "checker removed → unenforced RED" assertion proves an ERROR
obligation with no checker is never a silent pass (mirrors v2.77e exit gate 3). A green
over a synthetic obligation that never RAN would be the false-green this gate forbids.

PLUGGABILITY VERDICT: the pipeline was ALREADY framework-agnostic — every core path
(resolve/compose/lock/materialize/authenticity/obligation gate) branches on the
RESOLVED CONTRACT + slot/obligation/checker DATA, never on a framework NAME — so NO
core file needed editing. This test now PROVES it for a framework the core has never
heard of. The synthetic framework declares only BUILT-IN slot ids (``framework_build``
→ the default BUILD_EXECUTION authenticity policy) so its commands pass authenticity
with no curated knowledge; a custom obligation id + custom checker ref carry the
framework-specific semantics, resolved purely through the registry.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codd.greenfield.pipeline import GreenfieldPipeline, load_session
from codd.languages.profile import CommandSpec
from codd.languages.registry import default_registry as LANG
from codd.stack.adapters import OBLIGATION_CHECKERS, resolve_checker
from codd.stack.adapters._base import ObligationFinding
from codd.stack.command_plan import StackCommandSlot, StackCommandSlotResult
from codd.stack.compose import compose
from codd.stack.lock import build_lock, dump_lock, stack_lock_path
from codd.stack.profile import (
    FrameworkProfile,
    LayerIdentity,
    LayerRequirements,
    LanguageRequirement,
    Obligation,
)
from codd.stack.project import (
    StackObligationGateError,
    enforce_stack_obligation_gate,
)
from codd.stack.registry import default_framework_registry
from codd.stack.resolve import resolve_stack_from_declaration

# ── the synthetic framework's vocabulary (test-only; NOT a curated profile) ──

#: A coherent synthetic project carries this marker file at the project root; a broken
#: one omits it. The synthetic obligation checker treats "marker absent" as a violation
#: (the framework-agnostic analogue of the Next.js ``ignoreBuildErrors`` filesystem
#: checker — pure ``project_root`` read, no TEST report needed).
_SYNTH_MARKER = "SYNTHFRAMEWORK_COHERENT"

#: The synthetic obligation id + its checker ref. The id is the obligation's semantic
#: key; the ref string is what the registry resolves to the enforcing callable — NOT a
#: framework name baked into core, just a registry key the synthetic profile declares.
_SYNTH_OBLIGATION_ID = "synthframework_marker_required"
_SYNTH_CHECKER_REF = "synthframework_adapter:check_marker"


# ── the synthetic obligation CHECKER (the ONLY enforcement semantics this test adds) ──


class _SynthChecker:
    """The synthetic framework's obligation checker: marker present → satisfied, absent → RED.

    Records every invocation in :attr:`calls` so the test can PROVE the obligation was
    ENFORCED (the checker actually RAN) — not silently skipped (the false-green this
    gate forbids). Shaped exactly like the curated checkers: takes ``project_root`` +
    ``**kwargs`` and returns a ``list[ObligationFinding]`` (empty == satisfied).
    """

    def __init__(self) -> None:
        self.calls: list[Path] = []

    def __call__(self, project_root=None, **_kw) -> list[ObligationFinding]:  # noqa: ANN001
        root = Path(project_root) if project_root is not None else Path(".")
        self.calls.append(root)
        if (root / _SYNTH_MARKER).exists():
            return []  # coherent → obligation SATISFIED
        return [
            ObligationFinding(
                obligation_id=_SYNTH_OBLIGATION_ID,
                location=_SYNTH_MARKER,
                detail=(
                    "synthframework coherence marker absent — the synthetic framework "
                    "obligation is VIOLATED (a synthetic broken fixture must genuinely RED)"
                ),
            )
        ]


def _synth_framework_profile() -> FrameworkProfile:
    """A SYNTHETIC framework profile the core has never heard of (test-only).

    Declares: an id + alias, a host-language requirement (it sits on the same language
    layer as any framework), one BUILT-IN-slot command (``framework_build`` → the
    default BUILD_EXECUTION authenticity policy, so it passes authenticity with NO
    curated knowledge and NO custom observation policy), and one ERROR obligation whose
    checker ref resolves through the registry to :class:`_SynthChecker`. NOTHING about
    this profile lives under ``codd/`` — it is injected at runtime via monkeypatch.
    """
    return FrameworkProfile(
        identity=LayerIdentity(
            id="synthframework",
            kind="framework",
            display_name="SynthFramework",
            aliases=("synthfw",),
        ),
        requires=LayerRequirements(
            any_language=(LanguageRequirement(id="typescript"),)
        ),
        commands={
            # A BUILT-IN slot id (``framework_build``) → resolves to the default
            # BUILD_EXECUTION observation policy (reject no-op + exit 0). The argv is a
            # synthetic non-no-op command (NOT ``true``/``echo``) so authenticity passes
            # under the recording executor. No report needed (build kind).
            "framework_build": CommandSpec(
                id="framework_build",
                argv=("synthframework", "build"),
            ),
        },
        obligations=(
            Obligation(
                id=_SYNTH_OBLIGATION_ID,
                description="synthframework requires its coherence marker file at the root",
                checker=_SYNTH_CHECKER_REF,
                severity="error",  # release-blocking → an unenforced/violated one is RED
            ),
        ),
    )


# ── injection seam: register the synthetic profile + checker (NO core edit) ──


def _inject_framework(monkeypatch: pytest.MonkeyPatch, profile: FrameworkProfile) -> None:
    """Register ``profile`` into the REAL default framework registry's profile cache.

    Uses ``monkeypatch.setitem`` on ``default_framework_registry._profiles`` — the same
    dict the production resolver (``_LayerRegistry.resolve`` →
    ``resolve_stack_from_declaration``) reads — so a project declaring
    ``stack: {frameworks: [synthframework]}`` resolves the synthetic framework with NO
    core change, and the registration is torn down automatically. (Mirrors how
    ``test_oracle_synthetic_language`` injects a synthetic LanguageProfile into the
    language registry's profile cache, extended here to the framework registry.)
    """
    default_framework_registry._ensure_loaded()  # populate the cache (idempotent)
    monkeypatch.setitem(
        default_framework_registry._profiles, profile.identity.id.lower(), profile
    )


def _inject_checker(
    monkeypatch: pytest.MonkeyPatch, ref: str, checker: object
) -> None:
    """Register ``checker`` under ``ref`` in the REAL obligation-checker registry.

    ``monkeypatch.setitem`` on ``codd.stack.adapters.OBLIGATION_CHECKERS`` — the exact
    dict ``resolve_checker`` reads at dispatch time — so the synthetic obligation's
    checker ref resolves with NO core edit, auto-reverted after the test.
    """
    monkeypatch.setitem(OBLIGATION_CHECKERS, ref, checker)


# ── REAL-pipeline harness (stage bodies stubbed; intake→...→obligation gate run) ──


class _StageStubPipeline(GreenfieldPipeline):
    """The REAL pipeline with every STAGE BODY a no-op — so the real intake + lock +
    materialization + authenticity + v2.77e obligation gate (all in ``run()``, BEFORE the
    stage loop) are exercised on the synthetic framework. (Identical technique to the
    v2.77a-e tests; the gates under proof live in ``run()``, not in the stage bodies.)"""

    def _stage_init(self, project_root, record, options):  # noqa: ANN001
        record["status"] = "done"

    def _stage_elicit(self, project_root, record, options):  # noqa: ANN001
        record["status"] = "done"

    def _stage_plan(self, project_root, record, options):  # noqa: ANN001
        record["status"] = "done"

    def _stage_generate(self, project_root, record, options):  # noqa: ANN001
        record["status"] = "done"

    def _stage_implement(self, project_root, record, options):  # noqa: ANN001
        record["status"] = "done"

    def _stage_verify(self, project_root, record, options):  # noqa: ANN001
        record["status"] = "done"

    def _stage_ci_scaffold(self, project_root, record, options):  # noqa: ANN001
        record["status"] = "done"

    def _stage_propagate(self, project_root, record, options):  # noqa: ANN001
        record["status"] = "done"

    def _stage_check(self, project_root, record, options):  # noqa: ANN001
        record["status"] = "done"


class _RecordingExecutor:
    """Invokes the composed stack command slots (exit 0, spawned) WITHOUT real tooling.

    Records the invoked slot ids so the test can prove the synthetic framework's command
    flowed into the materialized plan. It writes NO report — the synthetic framework's
    only command is a BUILD-kind slot, which authenticity passes on "non-no-op + exit 0"
    with no report required (anti-false-green for build kind is the no-op rejection,
    which a real argv passes)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, slot: StackCommandSlot, project_root: Path, *, timeout: float):
        self.calls.append(slot.slot_id)
        return StackCommandSlotResult(
            slot_id=slot.slot_id,
            owner=slot.owner,
            command_str=slot.command_str,
            spawned=True,
            returncode=0,
            timed_out=False,
        )


_SYNTH_STACK = {"language": "typescript", "frameworks": ["synthframework"]}


def _make_project(tmp_path: Path, *, coherent: bool) -> Path:
    """A pre-initialized CoDD project declaring the synthetic-framework stack.

    ``coherent`` writes the synthetic coherence marker (obligation satisfied → GREEN);
    otherwise it is omitted (the checker fires → RED)."""
    project = tmp_path / "proj"
    codd_dir = project / "codd"
    codd_dir.mkdir(parents=True)
    config = {
        "project": {"name": "proj", "language": "typescript"},
        "stack": _SYNTH_STACK,
    }
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )
    if coherent:
        (project / _SYNTH_MARKER).write_text("ok\n", encoding="utf-8")
    return project


def _write_lock(project: Path) -> Path:
    """Pin the resolved synthetic-framework contract (the lock gate recomputes + matches).

    Proves the lock layer is framework-agnostic too: it hashes WHATEVER layers the
    resolved contract carries (no framework allowlist)."""
    contract = resolve_stack_from_declaration(_SYNTH_STACK)
    path = stack_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_lock(build_lock(contract)), encoding="utf-8")
    return path


def _run(project: Path, executor):
    lines: list[str] = []
    result = _StageStubPipeline(
        echo=lines.append, stack_command_executor=executor
    ).run(project)
    return result, lines


def _synth_contract():
    """Compose the synthetic-framework contract directly (pure-seam driving)."""
    ts = LANG.resolve("typescript")
    return compose(ts, [default_framework_registry.resolve("synthframework")])


# ═══════════════════════════════════════════════════════════════════════════
# Exit gate 1 — the synthetic framework works WITHOUT core modification + flows a-f
# ═══════════════════════════════════════════════════════════════════════════


def test_synthetic_framework_resolves_core_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``stack: {frameworks: [synthframework]}`` declaration RESOLVES + COMPOSES with
    NO core edit — proving resolve/compose are framework-agnostic. The synthetic
    framework is found by id AND alias through the real registry; the resolved contract
    carries its command + its ERROR obligation."""
    _inject_framework(monkeypatch, _synth_framework_profile())

    # Resolves by declaration (the production path), by id, and by alias.
    by_decl = resolve_stack_from_declaration(_SYNTH_STACK)
    by_alias = resolve_stack_from_declaration(
        {"language": "typescript", "frameworks": ["synthfw"]}
    )

    assert "framework:synthframework" in [f"{r.kind}:{r.id}" for r in by_decl.layers]
    assert by_decl.stack_id == by_alias.stack_id == "typescript+synthframework"
    assert "framework_build" in by_decl.commands  # its command flowed in
    assert any(o.id == _SYNTH_OBLIGATION_ID for o in by_decl.obligations)
    assert by_decl.is_clean and by_decl.strict_ok  # composes cleanly, no conflict


def test_synthetic_framework_valid_is_green_through_real_pipeline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """EXIT GATE (valid → GREEN): a coherent synthetic-framework project flows through the
    REAL greenfield pipeline a-f (intake→lock→materialize→authenticity→obligation gate)
    and is GREEN. NO core file was edited — the test only registered a profile + a checker.

    Proves the synthetic obligation was READ + ENFORCED (the checker RAN — ``checker.calls``
    is non-empty) and the framework's command flowed into the materialized plan
    (``executor.calls`` includes ``framework_build``); the obligation is recorded as
    checked and is NOT in the unenforced set."""
    checker = _SynthChecker()
    _inject_framework(monkeypatch, _synth_framework_profile())
    _inject_checker(monkeypatch, _SYNTH_CHECKER_REF, checker)

    project = _make_project(tmp_path, coherent=True)  # marker present → satisfied
    _write_lock(project)
    executor = _RecordingExecutor()

    result, lines = _run(project, executor)

    assert result.status == "success", (
        f"a coherent synthetic-framework stack must be GREEN; {getattr(result, 'error', None)}"
    )
    # The synthetic obligation was actually ENFORCED — the checker RAN (not skipped).
    assert checker.calls, "the synthetic obligation checker must have RUN (not silently skipped)"
    # The synthetic framework's command flowed into the materialized, executed plan.
    assert "framework_build" in executor.calls
    assert any("stack obligation gate" in line and "checked" in line for line in lines)
    # The run trace records the synthetic obligation as CHECKED and NOT unenforced.
    trace = load_session(project)["stack_contract"]
    assert trace["stack_obligations_checked"] >= 1
    assert _SYNTH_OBLIGATION_ID not in trace["stack_obligations_unenforced"]


# ═══════════════════════════════════════════════════════════════════════════
# Exit gate 2 — the synthetic BROKEN fixture is RED (for the RIGHT reason)
# ═══════════════════════════════════════════════════════════════════════════


def test_synthetic_framework_broken_is_red_through_real_pipeline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """EXIT GATE (broken → RED): a synthetic-framework project MISSING the coherence marker
    makes the synthetic obligation checker FIRE → the REAL pipeline reds at the
    ``stack_obligations`` gate. anti-false-green: the broken synthetic obligation is NOT
    waved through, and it reds for the RIGHT reason (the synthetic obligation id appears in
    the failure), the checker having genuinely RUN."""
    checker = _SynthChecker()
    _inject_framework(monkeypatch, _synth_framework_profile())
    _inject_checker(monkeypatch, _SYNTH_CHECKER_REF, checker)

    project = _make_project(tmp_path, coherent=False)  # NO marker → checker fires
    _write_lock(project)
    executor = _RecordingExecutor()

    result, lines = _run(project, executor)

    assert result.status == "failed", "a broken synthetic obligation must genuinely RED"
    assert result.failed_stage == "stack_obligations", (
        "the synthetic obligation must gate (RED at the obligation gate, not elsewhere)"
    )
    # RED for the RIGHT reason: the synthetic obligation id (the synthetic checker firing).
    assert _SYNTH_OBLIGATION_ID in (result.error or "")
    assert checker.calls, "the checker must have RUN to produce the violation (not a skip)"
    assert any("stack obligation gate" in line for line in lines)


def test_synthetic_framework_broken_is_red_pure_seam(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pure-seam mirror (no pipeline): ``enforce_stack_obligation_gate`` on the synthetic
    contract raises when the marker is absent — the synthetic obligation gates directly."""
    checker = _SynthChecker()
    _inject_framework(monkeypatch, _synth_framework_profile())
    _inject_checker(monkeypatch, _SYNTH_CHECKER_REF, checker)

    contract = _synth_contract()
    # tmp_path has NO marker → the synthetic checker fires → RED.
    with pytest.raises(StackObligationGateError) as excinfo:
        enforce_stack_obligation_gate(contract, tmp_path)
    assert _SYNTH_OBLIGATION_ID in str(excinfo.value)
    assert checker.calls  # it RAN


def test_synthetic_framework_valid_is_green_pure_seam(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pure-seam mirror: with the marker present the synthetic obligation gate PASSES
    (returns a result whose ``passed`` is True), and the checker RAN."""
    checker = _SynthChecker()
    _inject_framework(monkeypatch, _synth_framework_profile())
    _inject_checker(monkeypatch, _SYNTH_CHECKER_REF, checker)

    contract = _synth_contract()
    (tmp_path / _SYNTH_MARKER).write_text("ok\n", encoding="utf-8")  # coherent

    result = enforce_stack_obligation_gate(contract, tmp_path)
    assert result is not None and result.passed
    assert checker.calls  # the obligation was genuinely enforced, not skipped


# ═══════════════════════════════════════════════════════════════════════════
# Regression guard — checker REMOVED → unenforced RED (mirrors v2.77e gate 3)
# ═══════════════════════════════════════════════════════════════════════════


def test_synthetic_framework_unenforced_without_checker_is_red(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """REAL regression guard: register the synthetic framework but NOT its checker → the
    ERROR obligation becomes UNENFORCED → RED even with the marker present. This proves the
    gate is real enforcement (an ERROR release-blocker with no registered checker is
    unverifiable → RED, never a silent green) — the synthetic analogue of v2.77e exit gate
    3, and the anti-false-green guarantee that the GREEN above is a RAN green, not a
    coincidence of the checker being absent."""
    _inject_framework(monkeypatch, _synth_framework_profile())
    # Deliberately DO NOT register _SYNTH_CHECKER_REF — the obligation is unenforced.
    assert resolve_checker(_SYNTH_CHECKER_REF) is None  # no checker registered

    project = _make_project(tmp_path, coherent=True)  # marker present, yet still RED
    _write_lock(project)
    executor = _RecordingExecutor()

    result, _lines = _run(project, executor)

    assert result.status == "failed", "an unenforced ERROR obligation must be RED"
    assert result.failed_stage == "stack_obligations"
    assert _SYNTH_OBLIGATION_ID in (result.error or "")
    assert "unenforced" in (result.error or "").lower()


# ═══════════════════════════════════════════════════════════════════════════
# anti-false-green corner — a synthetic checker that returns None is a FAULT (RED)
# ═══════════════════════════════════════════════════════════════════════════


def test_synthetic_framework_checker_returning_none_is_red(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A synthetic checker that FALLS THROUGH (returns None) is NOT 'satisfied' — it is a
    FAULT → RED for the ERROR obligation (the canonical ``checker(...) or []`` false-green
    is closed even for a user-added framework checker)."""
    _inject_framework(monkeypatch, _synth_framework_profile())
    _inject_checker(monkeypatch, _SYNTH_CHECKER_REF, lambda **_: None)  # unimplemented

    contract = _synth_contract()
    (tmp_path / _SYNTH_MARKER).write_text("ok\n", encoding="utf-8")  # marker present, still RED

    with pytest.raises(StackObligationGateError):
        enforce_stack_obligation_gate(contract, tmp_path)
