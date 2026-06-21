"""Contract Kernel oracle dispatch — SYNTHESIS GENERALIZATION proof (§8, the EXIT GATE).

THE WHOLE POINT: a brand-new language is addable to the implement-time oracle with
NO change to any core dispatch/resolution file (``codd/implement_oracle.py`` /
``codd/languages/contract.py`` / ``codd/languages/registry.py`` /
``codd/languages/oracle_executor.py``). The ONLY artifacts this test introduces are
a synthetic :class:`~codd.languages.profile.LanguageProfile` ("synthlang") + a
synthetic :class:`~codd.languages.adapters.implement_oracle.ImplementOracleAdapter`,
both injected through the real registration seams (the default language registry's
cache + the default adapter registry, via ``monkeypatch``). No file under ``codd/``
is created or edited to make this pass — that is the generality proof.

WHY THIS GATE EXISTS (the gap §8 closed): before §8, the gate's resolution only
synthesized a runnable oracle for a legacy-``LayoutProfile``-less language when its
oracle was ``kind="composite"`` (Go). A legacy-profile-less language whose oracle was
``kind="command"`` or ``kind="adapter"`` fell through to ``None`` → a silent NO-OP
PASS — the run "succeeded" without the oracle ever executing (a false-green, and a
hole in the v3.0 "a new language is core-rewrite-free" claim). This test pins BOTH the
``kind="command"`` and the ``kind="adapter"`` legacy-profile-less paths: a coherent
project is GREEN, a broken one is RED, and in every case ``executed=True`` — the gate
RAN the oracle, never silently passed.

anti-false-green is paramount: the RED assertions prove a broken project is NOT
waved through, and the ``executed`` assertions prove a "pass" is a RAN pass, never a
skipped NO-OP masquerading as green.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

import pytest

from codd.implement_oracle import run_implement_oracle_gate
from codd.implement_oracle_types import (
    EVIDENCE_MISSING_SYMBOL,
    ImplementOracleFinding,
    ImplementOracleResult,
    OracleScopeError,
)
from codd.languages import default_registry
from codd.languages.adapters.implement_oracle import (
    OracleContext,
    OracleStepObservation,
)
from codd.languages.contract import KIND_IMPLEMENT_ORACLE
from codd.languages.profile import (
    CommandSpec,
    Identity,
    ImplementOracleProfileSpec,
    ImplementOracleStepSpec,
    LanguageProfile,
    LayoutSpec,
    SourceSet,
    TestSet,
)
from codd.languages.registry import default_adapter_registry

# A coherent synthetic project carries this marker file; a broken one omits it. The
# synthetic oracle (command OR adapter) treats "marker absent" as an incoherence.
_COHERENCE_MARKER = "COHERENT"


# ── the synthetic LanguageProfile builders (one per kind under test) ──────────


def _synth_layout() -> LayoutSpec:
    """A trivial layout — sources under ``src/``, tests under ``tests/``."""
    return LayoutSpec(
        repo_root=".",
        module_root=".",
        manifest_root=".",
        source_sets=(SourceSet(id="main", root="src", file_globs=("**/*.synth",)),),
        test_sets=(TestSet(id="tests", root="tests", file_globs=("**/*_test.synth",)),),
    )


def _synth_command_profile() -> LanguageProfile:
    """``kind="command"`` synthlang: one oracle command (a static checker analogue).

    The command is a tiny ``python -c`` that exits 0 IFF the coherence marker file is
    present at the project root, non-zero otherwise — a stand-in for a real static
    checker. It carries NO legacy ``LayoutProfile`` (synthlang is unknown to the
    legacy ``resolve_layout_profile``), so it is exactly the case that previously fell
    to the silent NO-OP.
    """
    check = (
        "import os,sys; "
        f"sys.exit(0 if os.path.exists({_COHERENCE_MARKER!r}) else 7)"
    )
    return LanguageProfile(
        identity=Identity(id="synthlang", display_name="SynthLang", aliases=("synth",)),
        layout=_synth_layout(),
        commands=MappingProxyType(
            {
                "typecheck": CommandSpec(
                    id="typecheck",
                    argv=(sys.executable, "-c", check),
                    cwd=".",
                ),
            }
        ),
        implement_oracle=ImplementOracleProfileSpec(
            kind="command",
            adapter="synth-command",
            command="typecheck",
        ),
    )


def _synth_adapter_profile() -> LanguageProfile:
    """``kind="adapter"`` synthlang2: an in-process composite (no shell command).

    This is the path that exercises ``synthesize_minimal_layout_view`` — a
    legacy-profile-less ``kind="adapter"`` language whose adapter reads
    ``source_root`` / ``test_root`` off the synthesized layout VIEW. No ``commands``
    are needed (the adapter's ``execute`` does all the work).
    """
    return LanguageProfile(
        identity=Identity(id="synthlang2", display_name="SynthLang2", aliases=("synth2",)),
        layout=_synth_layout(),
        implement_oracle=ImplementOracleProfileSpec(
            kind="adapter",
            adapter="synth-inprocess",
        ),
    )


# ── the synthetic adapters (the ONLY oracle-tool-semantics this test adds) ────


@dataclass
class _SynthCommandAdapter:
    """A ``command``/``composite`` oracle adapter: certify + normalize, NO execute.

    ``certify_scope`` hard-fails (anti-false-green) when the ``src`` tree the layout
    declares has no files — proving the synthetic stack participates in scope
    certification like the real ones. ``normalize_command_result`` maps a non-zero
    exit to a real ``missing_symbol`` finding (so a broken project REDs with a
    diagnostic, not an opaque env error).
    """

    certified: list[str] = field(default_factory=list)
    normalized: list[str] = field(default_factory=list)

    def certify_scope(self, ctx: OracleContext) -> str:
        src = ctx.project_root / "src"
        if not src.is_dir() or not any(src.rglob("*.synth")):
            raise OracleScopeError(
                "synthlang oracle scope uncertifiable: no `.synth` source under src/ "
                "(a green oracle over an empty scope is a false-green)."
            )
        self.certified.append(ctx.language_profile.id)
        return f"synthlang scope certified ({ctx.language_profile.id})"

    def normalize_command_result(
        self,
        ctx: OracleContext,
        *,
        command_id: str,
        command: CommandSpec,
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> OracleStepObservation:
        self.normalized.append(command_id)
        if returncode == 0:
            return OracleStepObservation(is_clean=True, detail="synthlang typecheck clean")
        return OracleStepObservation(
            is_clean=False,
            findings=(
                ImplementOracleFinding(
                    category=EVIDENCE_MISSING_SYMBOL,
                    code="SYNTH001",
                    message="synthlang detected an incoherence (coherence marker absent)",
                    path="src/app.synth",
                ),
            ),
            failed_paths=("src/app.synth",),
            detail="synthlang incoherence",
        )


@dataclass
class _SynthInProcessAdapter:
    """A ``kind="adapter"`` oracle adapter: an in-process composite with ``execute``.

    Reads ``source_root`` / ``test_root`` off the SYNTHESIZED layout view (the proof
    that ``synthesize_minimal_layout_view`` fed the adapter a usable view), certifies
    the source tree is non-empty, then returns GREEN/RED from the coherence marker.
    """

    executed: list[str] = field(default_factory=list)

    def certify_scope(self, ctx: OracleContext) -> str:
        # Reads source_root off the layout VIEW the gate synthesized for this
        # legacy-profile-less kind="adapter" language (the §5 minimal-layout-view).
        source_root = getattr(ctx.layout_profile, "source_root", None)
        if source_root is None:
            raise OracleScopeError(
                "synthlang2 got no layout view (source_root missing) — the minimal "
                "layout-view synthesis did not run."
            )
        src = ctx.project_root / source_root
        if not src.is_dir() or not any(src.rglob("*.synth")):
            raise OracleScopeError(
                f"synthlang2 oracle scope uncertifiable: no `.synth` under {source_root}/."
            )
        return f"synthlang2 scope certified (source_root={source_root})"

    def normalize_command_result(self, ctx: OracleContext, **_kw):  # pragma: no cover - unused
        raise AssertionError("a kind='adapter' oracle never runs a command sequence")

    def execute(self, ctx: OracleContext) -> ImplementOracleResult:
        self.executed.append(ctx.language_profile.id)
        source_root = getattr(ctx.layout_profile, "source_root", "src")
        coherent = (ctx.project_root / _COHERENCE_MARKER).exists()
        if coherent:
            return ImplementOracleResult(
                passed=True,
                executed=True,
                command=f"{ctx.language_profile.id}-inprocess",
                detail="synthlang2 in-process oracle clean",
            )
        return ImplementOracleResult(
            passed=False,
            executed=True,
            command=f"{ctx.language_profile.id}-inprocess",
            findings=[
                ImplementOracleFinding(
                    category=EVIDENCE_MISSING_SYMBOL,
                    code="SYNTH002",
                    message="synthlang2 in-process oracle found an incoherence",
                    path=f"{source_root}/app.synth",
                )
            ],
            failed_paths=[f"{source_root}/app.synth"],
            detail="synthlang2 in-process incoherence",
        )


# ── injection seam: register the synthetic profile + adapter (NO core edit) ───


def _inject(
    monkeypatch: pytest.MonkeyPatch,
    profile: LanguageProfile,
    adapter_id: str,
    adapter: object,
) -> None:
    """Register ``profile`` + ``adapter`` through the REAL default registries.

    Uses ``monkeypatch.setitem`` on the default language registry's profile cache and
    the default adapter registry's ``(kind, id)`` map — the same dicts the production
    resolution reads — so resolution finds the synthetic language with NO core change,
    and everything is torn down automatically. (Mirrors how the existing
    ``test_synthetic_language`` proves the verify-layer is language-agnostic, extended
    here to the implement-oracle dispatch layer.)
    """
    default_registry.resolve("go")  # force the cache to populate (idempotent)
    monkeypatch.setitem(default_registry._profiles, profile.identity.id.lower(), profile)
    monkeypatch.setitem(
        default_adapter_registry._adapters, (KIND_IMPLEMENT_ORACLE, adapter_id), adapter
    )


def _scaffold_src(root: Path) -> None:
    """A non-empty synthetic source tree so scope certification passes."""
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "app.synth").write_text("// synth source\n", encoding="utf-8")
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "app_test.synth").write_text("// synth test\n", encoding="utf-8")


def _run(language: str, root: Path) -> ImplementOracleResult:
    return run_implement_oracle_gate(
        root, language=language, project_name="demo", config={}, echo=lambda _m: None
    )


# ════════════════════════════════════════════════════════════════════════════
# kind="command" — the path that previously fell to a silent NO-OP (§8 gap)
# ════════════════════════════════════════════════════════════════════════════


def test_synthetic_command_language_coherent_is_green(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A coherent ``kind="command"`` synthetic language → GREEN, and it actually RAN.

    The whole §8 point: a legacy-``LayoutProfile``-less ``kind="command"`` language
    runs its oracle (executed=True) and passes — NOT a silent NO-OP. No core file was
    edited; this test only registered a profile + an adapter.
    """
    adapter = _SynthCommandAdapter()
    _inject(monkeypatch, _synth_command_profile(), "synth-command", adapter)
    _scaffold_src(tmp_path)
    (tmp_path / _COHERENCE_MARKER).write_text("ok\n", encoding="utf-8")  # coherent

    result = _run("synthlang", tmp_path)

    assert result.executed is True, "the gate must RUN the oracle, not silently NO-OP"
    assert result.passed is True, f"a coherent project must be GREEN: {result.findings}"
    assert adapter.normalized == ["typecheck"], "the command oracle must have executed its step"


def test_synthetic_command_language_broken_is_red(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A broken ``kind="command"`` synthetic language → RED with the adapter's finding.

    anti-false-green: the incoherence (coherence marker absent → the command exits
    non-zero) is NOT waved through. The run RAN (executed=True) and failed honestly.
    """
    adapter = _SynthCommandAdapter()
    _inject(monkeypatch, _synth_command_profile(), "synth-command", adapter)
    _scaffold_src(tmp_path)  # source present (scope certifies) but NO coherence marker

    result = _run("synthlang", tmp_path)

    assert result.executed is True
    assert result.passed is False, "a broken project must RED (never a silent pass)"
    assert "SYNTH001" in {f.code for f in result.findings}
    assert adapter.normalized == ["typecheck"]


def test_synthetic_command_language_alias_resolves(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The ``synth`` alias resolves the same oracle (registry alias path, no allowlist)."""
    adapter = _SynthCommandAdapter()
    _inject(monkeypatch, _synth_command_profile(), "synth-command", adapter)
    _scaffold_src(tmp_path)
    (tmp_path / _COHERENCE_MARKER).write_text("ok\n", encoding="utf-8")

    result = _run("synth", tmp_path)

    assert result.executed is True and result.passed is True


# ════════════════════════════════════════════════════════════════════════════
# kind="adapter" — exercises synthesize_minimal_layout_view (the §5 new path)
# ════════════════════════════════════════════════════════════════════════════


def test_synthetic_adapter_language_coherent_is_green(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A coherent legacy-profile-less ``kind="adapter"`` language → GREEN, and RAN.

    Proves ``synthesize_minimal_layout_view`` handed the in-process adapter a usable
    layout view (its ``execute`` read ``source_root`` off it) and the gate ran it.
    """
    adapter = _SynthInProcessAdapter()
    _inject(monkeypatch, _synth_adapter_profile(), "synth-inprocess", adapter)
    _scaffold_src(tmp_path)
    (tmp_path / _COHERENCE_MARKER).write_text("ok\n", encoding="utf-8")

    result = _run("synthlang2", tmp_path)

    assert result.executed is True, "the gate must RUN the in-process oracle, not NO-OP"
    assert result.passed is True, f"a coherent project must be GREEN: {result.findings}"
    assert adapter.executed == ["synthlang2"], "the adapter's execute() must have run"


def test_synthetic_adapter_language_broken_is_red(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A broken legacy-profile-less ``kind="adapter"`` language → RED with a finding.

    anti-false-green for the in-process path: no coherence marker → the adapter's
    ``execute`` reports an incoherence and the gate fails honestly (executed=True).
    """
    adapter = _SynthInProcessAdapter()
    _inject(monkeypatch, _synth_adapter_profile(), "synth-inprocess", adapter)
    _scaffold_src(tmp_path)  # source present (scope certifies) but NO coherence marker

    result = _run("synthlang2", tmp_path)

    assert result.executed is True
    assert result.passed is False, "a broken in-process oracle must RED"
    assert "SYNTH002" in {f.code for f in result.findings}
    assert adapter.executed == ["synthlang2"]


def test_synthetic_adapter_uncertifiable_scope_is_hard_fail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An empty scope HARD-FAILS (OracleScopeError) — the synthetic stack certifies too.

    Proves the synthetic language is NOT a special-cased pass: with no `.synth` source
    the adapter's ``certify_scope`` raises, exactly like the real stacks (a green over
    an uncertified/empty scope is the #1 false-green).
    """
    adapter = _SynthInProcessAdapter()
    _inject(monkeypatch, _synth_adapter_profile(), "synth-inprocess", adapter)
    # NO source scaffolded → scope cannot be certified.
    with pytest.raises(OracleScopeError):
        _run("synthlang2", tmp_path)
    assert adapter.executed == [], "execute() must NOT run when scope certification fails"
