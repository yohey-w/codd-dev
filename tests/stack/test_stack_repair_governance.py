"""Stack Repair Governance — Contract Kernel v2.77f (STEP 0 characterization).

v2.77b–e turned the framework-stack contract into red/green gates (lock drift,
command conflict/authenticity, obligation enforcement). v2.77f governs the
AUTO-REPAIR system against those gates: a repair may FIX the SUT to *satisfy*
a stack obligation, but it must NEVER game a stack gate by editing the CONTRACT —
weakening an obligation, deleting/unregistering a checker, inserting a no-op
command, or silently refreshing the stack lock. A repair that silences a stack
gate is a faithful violation → RED (cardinal rule: false-GREEN absolutely
forbidden; false-RED minimized — a genuine SUT fix is GREEN).

This is an ENFORCEMENT step, so anti-false-green is the whole point. The defense
is TWO-LAYERED:

* **Scope-fence (the new primary control)** — ``codd.repair.auto_scope_guard``
  fences the stack contract artefacts (``stack.lock`` / a ``codd/stack/`` profile,
  obligation, or checker) OUT of an AUTO repair's editable scope: a patch path
  targeting one is rejected (escalate → non-interactive reject → REPAIR_FAILED),
  UNCONDITIONALLY. Exercised here through the REAL ``codd verify`` CLI + the real
  ``RepairLoop`` + the real engine edit-application path (mirroring
  ``tests/repair/test_auto_repair_optin_and_scope.py``).
* **Defense in depth (already-shipped v2.77b–e gates)** — even if a repair edit
  somehow reached the contract, the existing gates red the gaming: weakening →
  compose semantic conflict (v2.77c); checker deletion → unenforced (v2.77e);
  no-op command → authenticity (v2.77d); lock refresh → the read-only lock gate
  re-detects drift (v2.77b). We PROVE each catches the corresponding vector.

Exit gates (v3_goal_contract_kernel.md §"v2.77f — Repair Governance"):
  1. repair weakening fixture is RED;
  2. repair checker-deletion fixture is RED;
  3. repair no-op-command fixture is RED;
  4. repair satisfy fixture is GREEN;
  5. replace_with_proof fixture is GREEN only with executable proof (RED without).

Plus the behaviour-preserving guarantee: a project WITHOUT a ``stack:`` block is
byte-identical — no stack governance is added to the repair scope guard (a non-stack
repair is unaffected).

Scope note (kept in lane): this is repair governance for the STACK layer. It does
NOT touch v2.77g (Next.js live greenfield E2E) or v2.77h (synthetic framework).
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
from codd.repair import engine as engine_registry
from codd.repair.auto_scope_guard import (
    AutoScopeDecision,
    _is_stack_contract_artifact,
    evaluate_auto_patch_scope,
)
from codd.repair.engine import RepairEngine, register_repair_engine
from codd.repair.schema import (
    ApplyResult,
    FilePatch,
    RepairProposal,
    RootCauseAnalysis,
    VerificationFailureReport,
)
from codd.repair.verify_runner import run_standalone_verify

from codd.stack.command_plan import (
    StackContractConflictError,
    assert_stack_contract_clean,
    stack_command_plan,
)
from codd.stack.compose import compose
from codd.stack.lock import (
    LOCK_DRIFT,
    build_lock,
    dump_lock,
    enforce_stack_lock,
    stack_lock_path,
)
from codd.stack.profile import AddonProfile, CommandSpec, FrameworkProfile, LayerIdentity, Obligation
from codd.stack.project import (
    StackObligationGateError,
    build_obligation_checker_inputs,
    enforce_stack_obligation_gate,
)
from codd.stack.resolve import resolve_stack_from_declaration
from codd.stack.replacement_proof import (
    PROOF_KIND,
    PROOF_SCHEMA,
    ReplacementProofError,
    ReplacementProofGateResult,
    enforce_replacement_proofs,
    extract_proof_declaration,
)

from codd.languages.registry import default_registry as LANG


PYTEST = f"{sys.executable} -m pytest --tb=short -q -p no:cacheprovider"

# A failing src + test (same shape as the repair scope e2e suite): the bug RAISES
# inside src/calc.py → B0 attributes an editable SOURCE target and classes it
# runtime_exception/code_addressable=True (the scope guard's protections engage).
_BUGGY_SOURCE = 'def add(a, b):\n    raise ValueError("boom")\n'
_FIXED_SOURCE = "def add(a, b):\n    return a + b\n"
_TEST = "from src.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    """Each test registers its own engines into a clean registry (mirror the repair
    scope suite). Force the standalone verify path (no codd-pro handler)."""
    monkeypatch.setattr(engine_registry, "_REPAIR_ENGINES", {})
    monkeypatch.setattr(cli, "get_command_handler", lambda name: None)


def _repo(tmp_path: Path, *, with_stack_lock: bool = False, source: str = _BUGGY_SOURCE) -> Path:
    """A one-commit project with a real failing pytest.

    When ``with_stack_lock`` is set, the project additionally declares a ``stack:``
    block (a generic 'python' language stack is enough for the lock-file scope
    fence — the fence is contract-artefact recognition, not stack resolution) and
    commits a ``codd/stack.lock`` so a repair could *attempt* to refresh it.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    (tmp_path / "codd").mkdir()
    config: dict = {
        "project": {"type": "generic"},
        "scan": {"source_dirs": ["src"]},
        "verify": {"test_command": PYTEST},
    }
    (tmp_path / "codd" / "codd.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text(source, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(_TEST, encoding="utf-8")
    if with_stack_lock:
        # A committed stack.lock the repair could try to refresh (any well-formed
        # lock content suffices — the fence keys on the PATH, not the contents).
        lock_path = tmp_path / "codd" / "stack.lock"
        lock_path.write_text(
            "schema_version: 1\nstack_id: python\nlayers: []\n"
            "resolved_contract_digest: sha256:deadbeef\nadapter_digests: {}\npermissions: {}\n",
            encoding="utf-8",
        )
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class _PatchEngine(RepairEngine):
    """Deterministic engine (mirror the repair scope suite): RCA from attributed
    nodes, propose ``PATCHES``, apply by writing files. The apply path here is the
    repair engine's REAL edit-application path the scope guard fences BEFORE."""

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


def _run_automatic(repo: Path, engine: str):
    return CliRunner().invoke(
        main,
        ["verify", "--path", str(repo), "--auto-repair", "--repair-mode", "automatic", "--engine", engine],
    )


# ── unit helper: drive the scope guard directly (role-aware classification) ──

def _scope(patch_path: str, *, failure_class: str, code_addressable: bool, allowlist: list[str]):
    failure = VerificationFailureReport(
        "test_command", list(allowlist), ["x"], {}, "t",
        failure_class=failure_class, code_addressable=code_addressable,
    )
    rca = RootCauseAnalysis("c", list(allowlist), "full_file_replacement", 0.9, "t")
    proposal = RepairProposal([FilePatch(patch_path, "full_file_replacement", "x\n")], "r", 0.9, "t", "t")
    return evaluate_auto_patch_scope(proposal, failure, rca, project_root="/proj")


_CODE_ADDR = {"failure_class": "runtime_exception", "code_addressable": True, "allowlist": ["src/calc.py"]}


# ═══════════════════════════════════════════════════════════════════════════
# SCOPE-FENCE recognition (unit) — stack contract artefacts are OFF-LIMITS
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "path",
    [
        "codd/stack.lock",  # the lock basename (next to codd.yaml)
        ".codd/stack.lock",
        "stack.lock",  # basename anywhere
        "deep/nested/dir/stack.lock",
        ".codd/stack/checkers/guard.py",  # under .codd/stack — harness state, unconditional
        ".codd/stack/proofs/p1.yaml",
    ],
)
def test_stack_contract_artifact_recognized_unconditionally(path: str) -> None:
    """``stack.lock`` basename + anything under ``.codd/stack/`` are stack artefacts
    with NO provenance needed (~zero false-positive risk)."""
    assert _is_stack_contract_artifact(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "src/app.py",
        "codd/codd.yaml",  # the codd.yaml stack-block fence handles this, not this helper
        "codd/languages/profile.py",
        "tests/test_x.py",
        "my_stack_lock.py",  # not the stack.lock basename
        "stacklock",  # not the basename
        # GPT-flagged FALSE-POSITIVE class: real CoDD framework SOURCE under codd/stack/
        # must NOT be fenced (when CoDD itself is the SUT this is a legitimate repair
        # target). Fenced only via PROVENANCE (a declared stack-contract path), never a
        # blind codd/stack/ directory rule.
        "codd/stack/compose.py",
        "codd/stack/lock.py",
        "codd/stack/command_plan.py",
        "codd/stack/profiles/myfw.yaml",  # under codd/stack but NOT declared → not fenced here
    ],
)
def test_non_provenanced_path_not_recognized_as_stack_artifact(path: str) -> None:
    assert _is_stack_contract_artifact(path) is False


def test_codd_stack_source_fenced_only_with_provenance() -> None:
    """A ``codd/stack/`` path is fenced ONLY when the project's ``stack:`` declaration
    references it (provenance) — proving real CoDD framework source is not blanket-fenced
    while a genuine project-local declared profile IS."""
    declared = frozenset({"codd/stack/profiles/customfw.yaml"})
    # Declared → fenced.
    assert _is_stack_contract_artifact(
        "codd/stack/profiles/customfw.yaml", declared_stack_paths=declared
    ) is True
    # Real CoDD source under the same dir but NOT declared → NOT fenced (no false-RED).
    assert _is_stack_contract_artifact(
        "codd/stack/compose.py", declared_stack_paths=declared
    ) is False


def test_scope_rejects_stack_lock_edit_unconditionally() -> None:
    """A repair patch targeting stack.lock is rejected REGARDLESS of failure class —
    even for a structural (non-code-addressable) failure where the oracle fence would
    NOT engage. The lock is never a legitimate auto-repair target."""
    for fc, ca in [("runtime_exception", True), ("", False), ("harness_contract_violation", True)]:
        decision = _scope(
            "codd/stack.lock", failure_class=fc, code_addressable=ca, allowlist=["codd/stack.lock"]
        )
        assert decision.allowed is False, f"stack.lock must be fenced (failure_class={fc!r})"
        assert decision.escalate is True
        assert "codd/stack.lock" in decision.offending_paths


def test_scope_rejects_dot_codd_stack_edit_unconditionally() -> None:
    """A project-local stack checker/profile/proof under ``.codd/stack/`` is fenced
    unconditionally (harness state — never SUT source, no provenance needed)."""
    decision = _scope(
        ".codd/stack/checkers/guard.py",
        failure_class="runtime_exception",
        code_addressable=True,
        allowlist=["src/calc.py"],
    )
    assert decision.allowed is False
    assert ".codd/stack/checkers/guard.py" in decision.offending_paths


def test_scope_rejects_codd_yaml_stack_block_edit_unconditionally() -> None:
    """A codd.yaml patch that touches the ``stack:`` block is fenced even for a
    NON-code-addressable failure (closing the hole the whole-file oracle fence — gated on
    code_addressable — leaves). A codd.yaml patch that does NOT touch ``stack:`` keeps its
    existing oracle-fence behaviour (code-addressable-gated)."""
    # Non-code-addressable codd.yaml edit that weakens the stack block → fenced.
    failure = VerificationFailureReport("tc", [], ["x"], {}, "t", failure_class="", code_addressable=False)
    rca = RootCauseAnalysis("c", [], "full_file_replacement", 0.9, "t")
    stack_patch = RepairProposal(
        [FilePatch("codd/codd.yaml", "full_file_replacement",
                   "project:\n  type: generic\nstack:\n  language: go\n")],
        "r", 0.9, "t", "t",
    )
    dec = evaluate_auto_patch_scope(stack_patch, failure, rca, project_root="/proj")
    assert dec.allowed is False
    assert "codd/codd.yaml" in dec.offending_paths
    assert "stack:" in dec.reason or "stack contract" in dec.reason

    # A non-stack codd.yaml edit, non-code-addressable → NOT fenced by the stack rule
    # (preserves the existing behaviour; the oracle fence only engages for
    # code_addressable, so a structural non-stack codd.yaml edit is allowed as before).
    nonstack_patch = RepairProposal(
        [FilePatch("codd/codd.yaml", "full_file_replacement",
                   "project:\n  type: generic\nverify:\n  test_command: pytest\n")],
        "r", 0.9, "t", "t",
    )
    dec2 = evaluate_auto_patch_scope(nonstack_patch, failure, rca, project_root="/proj")
    assert dec2.allowed is True


def test_scope_still_allows_in_scope_source_with_stack_present() -> None:
    """The fence is narrow: a genuine SUT-source fix (the satisfy path) is still
    allowed — only the stack contract artefacts are off-limits."""
    assert _scope("src/calc.py", **_CODE_ADDR).allowed is True


def test_scope_allows_codd_stack_source_repair_when_codd_is_sut() -> None:
    """FALSE-RED guard (GPT-flagged): when CoDD itself is the SUT, a code-addressable
    repair to real ``codd/stack/`` framework SOURCE is ALLOWED (it is not a contract
    artefact — only declared project-local stack files / stack.lock / .codd/stack are)."""
    decision = _scope(
        "codd/stack/compose.py",
        failure_class="runtime_exception",
        code_addressable=True,
        allowlist=["codd/stack/compose.py"],
    )
    assert decision.allowed is True


# ═══════════════════════════════════════════════════════════════════════════
# EXIT GATE (scope-fence, e2e) — a repair editing a stack artefact is REJECTED
#
# These drive the REAL path: the pre-verify fails on the SOURCE bug (a
# code-addressable runtime_exception → repair IS attempted), the rogue engine then
# proposes editing a STACK CONTRACT artefact INSTEAD of fixing the source. The
# scope-fence must reject that, so the artefact is left untouched, the source bug
# remains, and the run does NOT green.
#
# Design notes that make these UNAMBIGUOUS (reject for the right reason — the FENCE,
# not another gate):
#   * The rogue patches target ``.codd/stack/`` paths (fenced UNCONDITIONALLY — harness
#     state, no provenance/stack-declaration needed). This keeps NO ``stack:`` block in
#     codd.yaml, so the verify does NOT run the language command plan (which in CI fails
#     on an unsubstituted ``{module_root}`` layout placeholder) — the verify fails ONLY
#     on the SOURCE bug, so repair IS attempted and the fence is what stops the rogue
#     patch. (A ``codd/stack/`` PROVENANCED variant is covered separately below.)
#   * NO ``stack.lock`` is committed (a lock with no ``stack:`` block trips the
#     orphan-lock gate, same masking problem). The committed-lock refresh rejection is
#     proven directly at the decision level by
#     ``test_scope_rejects_stack_lock_edit_unconditionally``.
#   * A built-in CONTROL: the SAME project with a legitimate SOURCE fix GREENS,
#     proving the project is otherwise repairable and the only thing stopping the
#     rogue run is the fence on the stack-artefact patch.
# ═══════════════════════════════════════════════════════════════════════════

def _commit_local_stack_file(repo: Path, rel: str, content: str) -> Path:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", f"add {rel}"], cwd=repo, check=True)
    return target


def test_control_same_project_greens_with_legit_source_fix(tmp_path: Path) -> None:
    """CONTROL for the fence e2e tests below: a project carrying a project-local
    ``.codd/stack/`` file but NO ``stack:`` block is fully repairable — a legitimate
    SOURCE fix GREENS. So when the rogue runs below DON'T green, it is the FENCE on the
    stack-artefact patch that stopped them, not some other gate."""
    repo = _repo(tmp_path, with_stack_lock=False)
    _commit_local_stack_file(
        repo, ".codd/stack/checkers/guard.py", "def check(**_):\n    return ['violation']\n"
    )
    _register_patch_engine(
        "control_fix", [FilePatch("src/calc.py", "full_file_replacement", _FIXED_SOURCE)]
    )

    result = _run_automatic(repo, "control_fix")

    assert result.exit_code == 0, result.output
    assert "REPAIR_SUCCESS" in result.output
    assert _read(repo / "src" / "calc.py") == _FIXED_SOURCE


def test_repair_neutering_local_checker_is_rejected_e2e(tmp_path: Path) -> None:
    """A repair that, faced with a SOURCE failure, tries to neuter a project-local
    stack checker (under ``.codd/stack/``) into a no-op is rejected through the real
    path — the checker file is unchanged and the run does not green. This is the
    "gut a checker into ``return []``" gaming vector at the scope-fence layer (the
    callable-but-empty checker the defense-in-depth obligation gate can also catch)."""
    repo = _repo(tmp_path, with_stack_lock=False)
    checker = _commit_local_stack_file(
        repo, ".codd/stack/checkers/guard.py", "def check(**_):\n    return ['violation']\n"
    )

    # The rogue repair neuters the checker into a no-op (always-satisfied).
    _register_patch_engine(
        "gut_checker",
        [FilePatch(".codd/stack/checkers/guard.py", "full_file_replacement",
                   "def check(**_):\n    return []\n")],
    )

    result = _run_automatic(repo, "gut_checker")

    assert result.exit_code != 0
    assert "REPAIR_SUCCESS" not in result.output
    assert _read(checker) == "def check(**_):\n    return ['violation']\n"
    assert _read(repo / "src" / "calc.py") == _BUGGY_SOURCE


# NOTE: the ``codd/stack/`` PROVENANCED-profile fence (a stack profile under codd/stack/
# that the codd.yaml ``stack:`` block declares) is proven at the DECISION level by
# ``test_codd_stack_source_fenced_only_with_provenance`` +
# ``test_scope_rejects_codd_yaml_stack_block_edit_unconditionally``, NOT via a full e2e
# CLI run: a ``stack:`` block rich enough to carry provenance also triggers stack
# RESOLUTION, which (without a full live language/framework toolchain) reds the verify on
# intake BEFORE the source-bug repair is reached — masking whether the FENCE did the work.
# The unconditional ``.codd/stack/`` path is the faithful e2e vehicle (above); the
# provenance variant is a pure scope-decision concern, fully covered by the unit tests.


# ═══════════════════════════════════════════════════════════════════════════
# EXIT GATE 4 — a repair that fixes SUT SOURCE to SATISFY a failure is GREEN
# ═══════════════════════════════════════════════════════════════════════════

def test_repair_satisfy_source_fix_is_green_e2e(tmp_path: Path) -> None:
    """The legitimate repair path: editing SUT SOURCE to make the obligation pass is
    GREEN (not over-RED'd by the stack-artefact fence). This is the false-RED guard —
    the fence is NARROW, it gates the contract, never the source fix.

    (A standalone verify is used, not a stack-declaring project, because the satisfy
    path under test is the SOURCE-FIX scope decision; the "fence is narrow even with a
    stack present" claim is proven at the unit level by
    ``test_scope_still_allows_in_scope_source_with_stack_present`` — driving a full
    curated-stack verify through the CLI would need a live node/npx toolchain, which is
    v2.77g's lane, not v2.77f's.)"""
    repo = _repo(tmp_path, with_stack_lock=False)
    _register_patch_engine(
        "fix_source",
        [FilePatch("src/calc.py", "full_file_replacement", _FIXED_SOURCE)],
    )

    result = _run_automatic(repo, "fix_source")

    assert result.exit_code == 0, result.output
    assert "REPAIR_SUCCESS" in result.output
    assert _read(repo / "src" / "calc.py") == _FIXED_SOURCE
    assert run_standalone_verify(repo).passed is True


# ═══════════════════════════════════════════════════════════════════════════
# DEFENSE IN DEPTH — the existing v2.77b–e gates catch the gaming if it reaches
# the contract (proving the fence is not the ONLY thing standing between a
# repair and a false-green).
# ═══════════════════════════════════════════════════════════════════════════

def test_defense_weakening_obligation_is_compose_conflict_red() -> None:
    """If a repair weakened an obligation (downgraded severity), the composed contract
    carries a semantic conflict → the conflict gate reds (v2.77c). Defense in depth for
    the "weaken an obligation" vector."""
    ts = LANG.resolve("typescript")
    strong = FrameworkProfile(
        identity=LayerIdentity(id="sfw", kind="framework"),
        obligations=(Obligation(id="must_hold", severity="error", checker="x:y"),),
    )
    weakened = AddonProfile(
        identity=LayerIdentity(id="wad", kind="addon"),
        obligations=(Obligation(id="must_hold", severity="warn", checker="x:y"),),
    )
    contract = compose(ts, [strong], [weakened])
    assert any(c.kind == "semantic" for c in contract.conflicts)
    assert not contract.strict_ok
    with pytest.raises(StackContractConflictError):
        # The conflict gate reds a weakened contract before any command/obligation runs.
        build_obligation_checker_inputs(contract, project_root=Path("."))


def test_defense_checker_deletion_is_unenforced_red() -> None:
    """If a repair deleted/unregistered a checker, the ERROR obligation becomes
    UNENFORCED → the obligation gate reds (v2.77e). Defense in depth for the
    "delete a checker" vector."""
    ts = LANG.resolve("typescript")
    fw = FrameworkProfile(
        identity=LayerIdentity(id="needs_checker", kind="framework"),
        obligations=(
            # An ERROR obligation whose checker ref resolves to NOTHING (deleted).
            Obligation(id="release_blocker", severity="error", checker="deleted_adapter:gone"),
        ),
    )
    contract = compose(ts, [fw])
    assert contract.strict_ok  # no conflict — the gaming is the MISSING checker
    with pytest.raises(StackObligationGateError):
        enforce_stack_obligation_gate(contract, Path("."))


def test_defense_lock_refresh_does_not_clear_drift() -> None:
    """If a repair refreshed the lock to mask a drift, the READ-ONLY lock gate
    re-detects the drift against the REAL resolved contract (v2.77b). A rewritten
    lock that does not match the resolved contract is still DRIFT → RED. Defense in
    depth for the "silently refresh the lock" vector."""
    ts = LANG.resolve("typescript")
    contract = compose(ts)
    # A tampered lock (resolved_contract_digest the repair forged to "match" something
    # else) does NOT match the real resolved contract → drift, even though it parses.
    forged = build_lock(contract)
    from dataclasses import replace as dc_replace

    tampered = dc_replace(forged, resolved_contract_digest="sha256:forged-by-repair")
    project = Path(".")  # the lock path is resolved relative to project_root
    # Write the tampered lock where the gate reads it, then enforce.
    lock_path = stack_lock_path(project)
    backup = lock_path.read_text(encoding="utf-8") if lock_path.exists() else None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(dump_lock(tampered), encoding="utf-8")
        gate = enforce_stack_lock(contract, project)
        assert gate.red is True
        assert gate.status == LOCK_DRIFT
    finally:
        # Restore — never leave a tampered lock in the repo working tree.
        if backup is None:
            lock_path.unlink(missing_ok=True)
        else:
            lock_path.write_text(backup, encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════
# NON-STACK byte-identical — no stack governance added to the scope guard
# ═══════════════════════════════════════════════════════════════════════════

def test_non_stack_repair_is_byte_identical_e2e(tmp_path: Path) -> None:
    """A project WITHOUT any stack artefact self-heals exactly as before — the
    stack-artefact fence adds nothing to a non-stack repair (a normal in-scope source
    fix is GREEN, unchanged behaviour)."""
    repo = _repo(tmp_path, with_stack_lock=False)
    assert not (repo / "codd" / "stack.lock").exists()
    _register_patch_engine(
        "plain_fix",
        [FilePatch("src/calc.py", "full_file_replacement", _FIXED_SOURCE)],
    )

    result = _run_automatic(repo, "plain_fix")

    assert result.exit_code == 0, result.output
    assert "REPAIR_SUCCESS" in result.output
    assert _read(repo / "src" / "calc.py") == _FIXED_SOURCE


def test_non_stack_scope_decision_unaffected_by_stack_fence() -> None:
    """At the unit level, a non-stack source path is allowed and a non-stack oracle
    path keeps its existing (code-addressable-gated) behaviour — the stack fence does
    not perturb the non-stack classification."""
    # A plain in-scope source fix is allowed.
    assert _scope("src/calc.py", **_CODE_ADDR).allowed is True
    # codd.yaml stays handled by the ORACLE fence (code-addressable), not the stack fence.
    dec = _scope("codd/codd.yaml", **_CODE_ADDR)
    assert dec.allowed is False
    assert "oracle/spec/test-harness/gate-control" in dec.reason


# ═══════════════════════════════════════════════════════════════════════════
# EXIT GATE 5 — replace_with_proof: GREEN only with EXECUTABLE proof
#
# A command/obligation replacement (a different-argv command, or a same/stronger-
# severity different-checker obligation) is normally a composition Conflict → RED. A
# WELL-FORMED ``codd.replace_with_proof`` declaration makes the composer record a
# PendingReplacementProof instead, and the contract is clean ONLY once the behavioral
# subsumption proof has been EXECUTED and PASSED. The proof is anti-false-green: the
# kernel OBSERVES original/replacement behavior on a base+mutation witness — "proof
# command exited 0" is never enough; a no-op (true) or degenerate (false) replacement
# is RED via the negative control.
# ═══════════════════════════════════════════════════════════════════════════

# Two runnable "checker" scripts: each exits non-zero IFF a sentinel string "BAD" is
# present in any *.txt under cwd. The ORIGINAL and a GOOD replacement both behave this
# way (the replacement subsumes the original). A NO-OP replacement (``true``) ignores
# the defect; a DEGENERATE replacement (``false``) always fails.
_CHECKER_SRC = (
    "import sys, glob\n"
    "bad = any('BAD' in open(f, encoding='utf-8').read() for f in glob.glob('**/*.txt', recursive=True))\n"
    "sys.exit(1 if bad else 0)\n"
)


def _proof_decl(checker_path: str, *, replaces: str = "framework_build", base_fixture: str = "") -> dict:
    return {
        "schema": PROOF_SCHEMA,
        "kind": "command",
        "replaces": replaces,
        "proof": {
            "kind": PROOF_KIND,
            "case": "bad_is_rejected",
            "base_fixture": base_fixture,
            "mutation": {"path": "src.txt", "content": "BAD\n"},
        },
    }


def _build_proof_fixture(tmp_path: Path) -> tuple[Path, str, str]:
    """Create a clean base fixture + the original/good checker scripts on disk.

    Returns ``(project_root, base_fixture_relpath, checker_relpath)``. The base fixture
    contains a clean ``src.txt`` (no ``BAD``). Both the original and the good-replacement
    command run the same checker script (so the replacement genuinely subsumes the
    original); the proof's mutation injects ``BAD`` into the mutated copy. The checker
    ignores extra argv, so a good replacement uses a DIFFERENT argv (an extra harmless
    flag) — different command, identical subsumption behavior.
    """
    root = tmp_path / "proj"
    base = root / "fixtures" / "clean"
    base.mkdir(parents=True)
    (base / "src.txt").write_text("ok\n", encoding="utf-8")
    checker = root / "checker.py"
    checker.write_text(_CHECKER_SRC, encoding="utf-8")
    return root, "fixtures/clean", "checker.py"


def _good_replacement_argv(root: Path, checker_rel: str) -> tuple[str, ...]:
    """A replacement command with DIFFERENT argv than the original but identical behavior
    (the checker ignores the extra flag) — so it is a real replace that genuinely subsumes."""
    return (sys.executable, str(root / checker_rel), "--as-build")


def _compose_with_command_replacement(root: Path, checker_rel: str, *, replacement_argv, base_fixture: str):
    """Compose a stack where a framework REPLACES another framework's ``framework_build``
    command (the 'original' is a runnable checker script) via replace_with_proof.

    Using two frameworks (not the language) keeps the ORIGINAL command a real runnable
    script the proof can observe — the language ``typecheck`` (``tsc``) is not present in
    CI, so it cannot be the observed original.
    """
    ts = LANG.resolve("typescript")
    original_argv = (sys.executable, str(root / checker_rel))
    base_fw = FrameworkProfile(
        identity=LayerIdentity(id="basefw", kind="framework"),
        commands={"framework_build": CommandSpec(id="framework_build", argv=original_argv)},
    )
    repl_fw = FrameworkProfile(
        identity=LayerIdentity(id="replfw", kind="framework"),
        commands={
            "framework_build": CommandSpec(
                id="framework_build",
                argv=tuple(replacement_argv),
                extra={"codd.replace_with_proof": _proof_decl(checker_rel, base_fixture=base_fixture)},
            )
        },
    )
    return compose(ts, [base_fw, repl_fw])


def test_replace_with_proof_declaration_only_is_red(tmp_path: Path) -> None:
    """A well-formed replace_with_proof recorded as a pending proof is NOT clean by
    declaration alone — assert_stack_contract_clean WITHOUT a proof result is RED."""
    root, base_fixture, checker_rel = _build_proof_fixture(tmp_path)
    good_argv = _good_replacement_argv(root, checker_rel)
    contract = _compose_with_command_replacement(
        root, checker_rel, replacement_argv=good_argv, base_fixture=base_fixture
    )
    assert contract.pending_replacement_proofs, "a well-formed proof must be recorded as pending"
    assert not contract.conflicts, "a well-formed proof is NOT an ordinary conflict"
    # Declaration-only (no proof gate run) → RED.
    with pytest.raises(StackContractConflictError, match="replacement proof not executed"):
        assert_stack_contract_clean(contract)


def test_replace_with_proof_passing_proof_is_green(tmp_path: Path) -> None:
    """A replacement that genuinely subsumes the original (catches the mutated witness,
    passes the clean base) → the executed proof PASSES → the contract is clean (GREEN)."""
    root, base_fixture, checker_rel = _build_proof_fixture(tmp_path)
    good_argv = _good_replacement_argv(root, checker_rel)  # different argv, same subsumption
    contract = _compose_with_command_replacement(
        root, checker_rel, replacement_argv=good_argv, base_fixture=base_fixture
    )
    result = enforce_replacement_proofs(contract.pending_replacement_proofs, project_root=root)
    assert result.passed, f"a genuine subsumption proof must pass; violations={result.violations}"
    # GREEN with the passing proof result attached.
    assert_stack_contract_clean(contract, replacement_proofs=result)  # must NOT raise


def test_replace_with_proof_noop_replacement_is_red(tmp_path: Path) -> None:
    """The negative control: a NO-OP replacement (``true``, ignores the defect) PASSES the
    mutated witness the original rejects → the proof FAILS → RED. This is the false-green
    the proof exists to kill — "exit 0" is not subsumption."""
    root, base_fixture, checker_rel = _build_proof_fixture(tmp_path)
    contract = _compose_with_command_replacement(
        root, checker_rel, replacement_argv=("true",), base_fixture=base_fixture
    )
    result = enforce_replacement_proofs(contract.pending_replacement_proofs, project_root=root)
    assert not result.passed, "a no-op replacement must FAIL the subsumption proof"
    assert any("PASSED on the mutated witness" in v.reason for v in result.violations)
    with pytest.raises(StackContractConflictError, match="replacement proof failed"):
        assert_stack_contract_clean(contract, replacement_proofs=result)


def test_replace_with_proof_degenerate_replacement_is_red(tmp_path: Path) -> None:
    """A DEGENERATE replacement (``false``, always fails) reds the clean base witness →
    the proof FAILS → RED (a replacement that reds clean input is not a valid substitute)."""
    root, base_fixture, checker_rel = _build_proof_fixture(tmp_path)
    contract = _compose_with_command_replacement(
        root, checker_rel, replacement_argv=("false",), base_fixture=base_fixture
    )
    result = enforce_replacement_proofs(contract.pending_replacement_proofs, project_root=root)
    assert not result.passed
    assert any("clean base witness" in v.reason for v in result.violations)


def test_replace_with_proof_malformed_declaration_is_compose_conflict(tmp_path: Path) -> None:
    """A MALFORMED replace_with_proof (here: wrong proof kind) is its own RED at compose —
    a ``replace_with_proof`` conflict, never silently accepted."""
    ts = LANG.resolve("typescript")
    base_fw = FrameworkProfile(
        identity=LayerIdentity(id="basefw", kind="framework"),
        commands={"framework_build": CommandSpec(id="framework_build", argv=("a", "b"))},
    )
    bad_decl = {
        "schema": PROOF_SCHEMA,
        "kind": "command",
        "replaces": "framework_build",
        "proof": {"kind": "totally_bogus", "base_fixture": "x", "mutation": {"path": "p", "content": "c"}},
    }
    repl_fw = FrameworkProfile(
        identity=LayerIdentity(id="replfw", kind="framework"),
        commands={
            "framework_build": CommandSpec(
                id="framework_build", argv=("c", "d"), extra={"codd.replace_with_proof": bad_decl}
            )
        },
    )
    contract = compose(ts, [base_fw, repl_fw])
    assert any(c.kind == "replace_with_proof" for c in contract.conflicts)
    assert not contract.pending_replacement_proofs  # malformed → NOT a pending proof
    with pytest.raises(StackContractConflictError):
        assert_stack_contract_clean(contract)


def test_replace_with_proof_cross_id_replacement_is_rejected() -> None:
    """v2.77f supports SAME-id replacement only: a ``replaces`` that differs from the
    replacement's own id is malformed → RED (no cross-id replace)."""
    bad = {
        "schema": PROOF_SCHEMA,
        "kind": "command",
        "replaces": "some_other_id",  # != the replacement's id "framework_build"
        "proof": {"kind": PROOF_KIND, "base_fixture": "x", "mutation": {"path": "p", "content": "c"}},
    }
    spec = CommandSpec(id="framework_build", argv=("x",), extra={"codd.replace_with_proof": bad})
    with pytest.raises(ReplacementProofError, match="same-id replacement only"):
        extract_proof_declaration(spec, kind="command")


def test_replace_with_proof_result_matched_by_fingerprint_not_id(tmp_path: Path) -> None:
    """A proof result approves by FINGERPRINT, not id: a result that approves a DIFFERENT
    replacement (same id, different argv/declaration) does NOT clear this contract's
    pending proof (anti-reuse, GPT gaming-vector #6)."""
    root, base_fixture, checker_rel = _build_proof_fixture(tmp_path)
    good_argv = _good_replacement_argv(root, checker_rel)
    contract = _compose_with_command_replacement(
        root, checker_rel, replacement_argv=good_argv, base_fixture=base_fixture
    )
    # A result that "approves" some unrelated fingerprint (not this contract's).
    bogus = ReplacementProofGateResult(approved=frozenset({"sha256:unrelated"}), violations=())
    assert not bogus.approves_all(contract.pending_replacement_proofs)
    with pytest.raises(StackContractConflictError, match="replacement proof failed"):
        assert_stack_contract_clean(contract, replacement_proofs=bogus)


def test_replace_with_proof_declaration_affects_content_hash(tmp_path: Path) -> None:
    """The proof declaration affects the content hash (so a changed witness drifts the
    lock — GPT gaming-vector #5: a proof cannot silently change under a stable lock)."""
    root, base_fixture, checker_rel = _build_proof_fixture(tmp_path)
    good_argv = _good_replacement_argv(root, checker_rel)
    c1 = _compose_with_command_replacement(
        root, checker_rel, replacement_argv=good_argv, base_fixture=base_fixture
    )
    # Same shape but a DIFFERENT mutation content → a different proof → different hash.
    ts = LANG.resolve("typescript")
    base_fw = FrameworkProfile(
        identity=LayerIdentity(id="basefw", kind="framework"),
        commands={"framework_build": CommandSpec(id="framework_build", argv=good_argv)},
    )
    decl2 = _proof_decl(checker_rel, base_fixture=base_fixture)
    decl2["proof"]["mutation"]["content"] = "DIFFERENT-BAD\n"
    repl_fw = FrameworkProfile(
        identity=LayerIdentity(id="replfw", kind="framework"),
        commands={
            "framework_build": CommandSpec(
                id="framework_build", argv=good_argv + ("--x",), extra={"codd.replace_with_proof": decl2}
            )
        },
    )
    c2 = compose(ts, [base_fw, repl_fw])
    assert c1.content_hash != c2.content_hash, "a changed proof witness must change the content hash"
