"""``replace_with_proof`` — executable behavioral-subsumption proof gate (v2.77f).

A stack layer may declare that one of its commands/obligations REPLACES another
layer's (e.g. Next.js claiming ``next build`` subsumes the language ``typecheck``).
Per the composable-profile design (``gpt_composable_profile_design.md`` §merge
operators) this ``replace_with_proof`` operator requires a SUBSUMES PROOF: the
replacement must demonstrably catch what the original caught. Contract Kernel v2.77f
makes that proof EXECUTABLE and anti-false-green:

* A different-argv command redefinition (or a same/stronger-severity, different-checker
  obligation redefinition) is normally a composition Conflict → RED (v2.77c/e). With a
  WELL-FORMED ``codd.replace_with_proof`` declaration the composer instead records a
  :class:`PendingReplacementProof` — the replacement is NOT clean-by-syntax; it is
  pending an executed proof.
* :func:`enforce_replacement_proofs` runs the behavioral proof and the central clean
  assertion (``codd.stack.command_plan.assert_stack_contract_clean``) treats the contract
  as clean ONLY if every pending proof has a matching PASSED result (matched by
  *fingerprint*, not id).

The cardinal anti-false-green rule (GPT-5.5 Pro consult 2026-06-21): "proof command
exited 0" is NOT enough — that is gamed by ``true`` or an assertion-free script. The
kernel OBSERVES the original and replacement behavior itself on a BASE+MUTATION witness:

    original  on the MUTATED witness  → must FAIL   (the original catches the defect)
    replacement on the MUTATED witness → must FAIL  (the replacement catches it too)
    replacement on the BASE witness    → must PASS  (no false-RED on clean input)

The negative control is the crux: the replacement must FAIL on the same mutated witness
the original fails on. A no-op replacement (``true``) passes the mutated witness → RED;
a degenerate replacement (``false``) fails the base witness → RED. The proof DECLARATION
describes the witness + expected observations; it never carries its own verdict.

Scope (v2.77f): SAME-id replacement only (a replacement command/obligation keeps the
replaced id). Severity-WEAKENING is never rescued by a proof (it stays a semantic
conflict). The COMMAND proof is executed here (exit-code subsumption); the OBLIGATION
proof representation is modeled and validated, with its checker-based observation reusing
the obligation machinery. The full general live-framework proof engine is v2.77g/h —
this module is the kernel contract + a minimal executable proof for the curated/fixture
cases.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess  # noqa: S404 — argv comes from the trusted resolved contract / proof decl, shell=False.
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from codd.languages.profile import CommandSpec
from .profile import Obligation

#: The namespaced wire key a profile uses to declare a proof-backed replacement. A
#: namespaced key (not a generic ``replaces`` / ``proof``) avoids collisions with
#: framework-specific metadata that also lives in ``extra`` / ``data``.
PROOF_DECL_KEY = "codd.replace_with_proof"

#: The only proof schema/kind v2.77f understands. A declaration with any other schema
#: or proof kind is MALFORMED → RED (never silently accepted).
PROOF_SCHEMA = "codd.replace_with_proof.v1"
PROOF_KIND = "behavioral_subsumption_v1"

#: Wall-clock cap for one proof witness command (mirrors the stack command cap). A
#: timed-out proof step is NEVER a pass.
DEFAULT_PROOF_TIMEOUT_SECONDS = 600


class ReplacementProofError(ValueError):
    """A ``replace_with_proof`` declaration is malformed (RED at compose time).

    Distinct from a proof that RAN and FAILED (that is a gate violation, not a malformed
    declaration). The composer turns this into a ``Conflict(kind="replace_with_proof")``.
    """


@dataclass(frozen=True)
class ProofWitness:
    """A base fixture + a single mutation that injects the defect the proof is about.

    ``base_fixture`` is a project-relative directory that builds/typechecks clean.
    ``mutation`` writes ``content`` to ``path`` (relative to a COPY of the base fixture),
    producing the mutated witness. The kernel runs the original/replacement against both
    the base and the mutated copy and observes the verdicts itself.
    """

    base_fixture: str
    mutation_path: str
    mutation_content: str

    @staticmethod
    def from_mapping(data: Mapping[str, Any]) -> "ProofWitness":
        base = str(data.get("base_fixture", "") or "").strip()
        mutation = data.get("mutation")
        if not base:
            raise ReplacementProofError("proof.base_fixture is required (a clean base fixture dir)")
        if not isinstance(mutation, Mapping):
            raise ReplacementProofError("proof.mutation must be a mapping {path, content}")
        mpath = str(mutation.get("path", "") or "").strip()
        if not mpath:
            raise ReplacementProofError("proof.mutation.path is required (the defect-injection file)")
        if "content" not in mutation:
            raise ReplacementProofError("proof.mutation.content is required (the defect-injecting content)")
        return ProofWitness(
            base_fixture=base,
            mutation_path=mpath,
            mutation_content=str(mutation.get("content") or ""),
        )

    def canonical(self) -> dict[str, Any]:
        return {
            "base_fixture": self.base_fixture,
            "mutation": {"path": self.mutation_path, "content": self.mutation_content},
        }


@dataclass(frozen=True)
class ReplacementProofDeclaration:
    """A typed, validated ``replace_with_proof`` declaration (parsed from the wire).

    ``kind`` is ``"command"`` or ``"obligation"``. ``replaces`` is the id being replaced
    (v2.77f requires it to equal the replacement's own id — same-id replacement only).
    ``witness`` is the base+mutation the kernel observes against.
    """

    kind: Literal["command", "obligation"]
    replaces: str
    case: str
    witness: ProofWitness

    @staticmethod
    def from_mapping(raw: Any, *, expected_kind: str, replacement_id: str) -> "ReplacementProofDeclaration":
        if not isinstance(raw, Mapping):
            raise ReplacementProofError(f"{PROOF_DECL_KEY} must be a mapping")
        schema = str(raw.get("schema", "") or "").strip()
        if schema != PROOF_SCHEMA:
            raise ReplacementProofError(
                f"{PROOF_DECL_KEY}.schema must be {PROOF_SCHEMA!r} (got {schema!r})"
            )
        kind = str(raw.get("kind", "") or "").strip()
        if kind != expected_kind:
            raise ReplacementProofError(
                f"{PROOF_DECL_KEY}.kind must be {expected_kind!r} for a {expected_kind} "
                f"replacement (got {kind!r})"
            )
        replaces = str(raw.get("replaces", "") or "").strip()
        if not replaces:
            raise ReplacementProofError(f"{PROOF_DECL_KEY}.replaces is required")
        if replaces != replacement_id:
            # Same-id replacement only (v2.77f). A cross-id replace is out of lane.
            raise ReplacementProofError(
                f"{PROOF_DECL_KEY}.replaces ({replaces!r}) must equal the replacement id "
                f"({replacement_id!r}) — v2.77f supports same-id replacement only"
            )
        proof = raw.get("proof")
        if not isinstance(proof, Mapping):
            raise ReplacementProofError(f"{PROOF_DECL_KEY}.proof must be a mapping")
        proof_kind = str(proof.get("kind", "") or "").strip()
        if proof_kind != PROOF_KIND:
            raise ReplacementProofError(
                f"{PROOF_DECL_KEY}.proof.kind must be {PROOF_KIND!r} (got {proof_kind!r})"
            )
        witness = ProofWitness.from_mapping(proof)
        return ReplacementProofDeclaration(
            kind=expected_kind,  # type: ignore[arg-type]
            replaces=replaces,
            case=str(proof.get("case", "") or "").strip(),
            witness=witness,
        )

    def canonical(self) -> dict[str, Any]:
        return {
            "schema": PROOF_SCHEMA,
            "kind": self.kind,
            "replaces": self.replaces,
            "case": self.case,
            "proof": {"kind": PROOF_KIND, **self.witness.canonical()},
        }


def extract_proof_declaration(
    spec: CommandSpec | Obligation, *, kind: str
) -> ReplacementProofDeclaration | None:
    """Pull + parse a ``replace_with_proof`` declaration off a command/obligation, or None.

    Reads ``CommandSpec.extra[PROOF_DECL_KEY]`` (command) or ``Obligation.data[PROOF_DECL_KEY]``
    (obligation). Absent → ``None`` (no proof declared). Present-but-malformed →
    :class:`ReplacementProofError` (the composer turns it into a RED conflict). Returns the
    typed declaration on success — the rest of the kernel never reasons over the raw blob.
    """
    if kind == "command":
        bag = getattr(spec, "extra", None)
        replacement_id = getattr(spec, "id", "")
    else:
        bag = getattr(spec, "data", None)
        replacement_id = getattr(spec, "id", "")
    if not isinstance(bag, Mapping) or PROOF_DECL_KEY not in bag:
        return None
    return ReplacementProofDeclaration.from_mapping(
        bag[PROOF_DECL_KEY], expected_kind=kind, replacement_id=str(replacement_id)
    )


def _fingerprint(kind: str, original: Any, replacement: Any, decl: ReplacementProofDeclaration) -> str:
    """Stable hash of (original spec, replacement spec, proof declaration).

    The proof result must approve THIS exact fingerprint — not just the id — so a proof
    for one replacement can never be reused for a different replacement with the same id
    (GPT gaming-vector #6). The lock also covers this (the declaration is in the spec's
    extra/data, which feeds the content hash via the command/obligation set).
    """
    def _spec_canonical(obj: Any) -> Any:
        if isinstance(obj, CommandSpec):
            return {"id": obj.id, "argv": list(obj.argv), "cwd": obj.cwd}
        if isinstance(obj, Obligation):
            return {"id": obj.id, "severity": obj.severity, "checker": obj.checker}
        return repr(obj)

    payload = json.dumps(
        {
            "kind": kind,
            "original": _spec_canonical(original),
            "replacement": _spec_canonical(replacement),
            "declaration": decl.canonical(),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PendingReplacementProof:
    """A declared replacement that is NOT clean until its behavioral proof passes.

    Emitted by the composer in place of an ordinary command/semantic conflict when a
    well-formed ``replace_with_proof`` is present. ``original`` is the spec being replaced;
    ``replacement`` is the replacing spec; ``declaration`` carries the witness;
    ``fingerprint`` binds the proof result to this exact replacement.
    """

    kind: Literal["command", "obligation"]
    id: str
    original: CommandSpec | Obligation
    replacement: CommandSpec | Obligation
    declaration: ReplacementProofDeclaration
    fingerprint: str

    @staticmethod
    def build(
        kind: str,
        original: CommandSpec | Obligation,
        replacement: CommandSpec | Obligation,
        declaration: ReplacementProofDeclaration,
    ) -> "PendingReplacementProof":
        return PendingReplacementProof(
            kind=kind,  # type: ignore[arg-type]
            id=str(getattr(replacement, "id", "")),
            original=original,
            replacement=replacement,
            declaration=declaration,
            fingerprint=_fingerprint(kind, original, replacement, declaration),
        )


@dataclass(frozen=True)
class ReplacementProofViolation:
    fingerprint: str
    id: str
    reason: str


@dataclass(frozen=True)
class ReplacementProofGateResult:
    """Outcome of executing every pending replacement proof.

    ``approved`` is the set of fingerprints whose behavioral proof PASSED. ``violations``
    carry the failures. ``passed`` is True only when there are no violations.
    """

    approved: frozenset[str]
    violations: tuple[ReplacementProofViolation, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.violations

    def approves_all(self, pending: Sequence[PendingReplacementProof]) -> bool:
        """True when every pending proof's fingerprint is approved (no id-only match)."""
        return all(p.fingerprint in self.approved for p in pending)


# ── behavioral proof execution (kernel observes; never trusts a proof's verdict) ──

CommandRunner = Any  # callable(argv, cwd) -> exit code; injectable for tests.


def _default_command_runner(argv: Sequence[str], cwd: Path) -> int:
    env = os.environ.copy()
    try:
        completed = subprocess.run(  # noqa: S603 — trusted argv from the resolved contract, shell=False.
            list(argv),
            shell=False,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=DEFAULT_PROOF_TIMEOUT_SECONDS,
        )
        return int(completed.returncode)
    except (FileNotFoundError, OSError):
        # Un-spawnable proof command → treat as a non-pass (returncode 127-ish). The
        # subsumption checks below interpret "not zero" as fail.
        return 127
    except subprocess.TimeoutExpired:
        return 124


def _materialize_witness(witness: ProofWitness, project_root: Path, dest: Path) -> tuple[Path, Path]:
    """Copy the base fixture into ``dest`` twice (base + mutated) and apply the mutation.

    Returns ``(base_dir, mutated_dir)``. The base is an untouched copy; the mutated copy
    has ``mutation_content`` written to ``mutation_path``. Both live under ``dest`` (a
    caller-provided temp dir) so the proof never mutates the real project tree.
    """
    import shutil

    src = (project_root / witness.base_fixture).resolve()
    if not src.is_dir():
        raise ReplacementProofViolationError(
            f"proof base_fixture {witness.base_fixture!r} is not a directory under the project"
        )
    base_dir = dest / "base"
    mutated_dir = dest / "mutated"
    shutil.copytree(src, base_dir)
    shutil.copytree(src, mutated_dir)
    mutated_file = (mutated_dir / witness.mutation_path).resolve()
    # Containment: the mutation must stay inside the mutated copy.
    try:
        mutated_file.relative_to(mutated_dir.resolve())
    except ValueError as exc:
        raise ReplacementProofViolationError(
            f"proof mutation.path {witness.mutation_path!r} escapes the fixture"
        ) from exc
    mutated_file.parent.mkdir(parents=True, exist_ok=True)
    mutated_file.write_text(witness.mutation_content, encoding="utf-8")
    return base_dir, mutated_dir


class ReplacementProofViolationError(RuntimeError):
    """A proof could not be executed meaningfully (bad fixture/mutation) — a non-pass."""


def _prove_command_subsumption(
    pending: PendingReplacementProof,
    project_root: Path,
    runner: CommandRunner,
) -> str | None:
    """Execute the COMMAND behavioral subsumption proof. Return a reason on FAIL, else None.

    The kernel observes (it does NOT run a proof script that self-reports):

        original   argv on MUTATED  → must be NON-ZERO (original catches the defect)
        replacement argv on MUTATED → must be NON-ZERO (replacement catches it too) [neg. control]
        replacement argv on BASE    → must be ZERO     (no false-RED on clean input)
    """
    import tempfile

    original = pending.original
    replacement = pending.replacement
    if not isinstance(original, CommandSpec) or not isinstance(replacement, CommandSpec):
        return "command proof requires CommandSpec original + replacement"
    witness = pending.declaration.witness
    with tempfile.TemporaryDirectory(prefix="codd-replace-proof-") as tmp:
        try:
            base_dir, mutated_dir = _materialize_witness(witness, project_root, Path(tmp))
        except ReplacementProofViolationError as exc:
            return str(exc)

        orig_on_mutated = runner(original.argv, mutated_dir)
        if orig_on_mutated == 0:
            return (
                "negative control failed: the ORIGINAL command passed on the mutated witness "
                "(the witness does not actually inject a defect the original catches) — cannot "
                "prove subsumption"
            )
        repl_on_mutated = runner(replacement.argv, mutated_dir)
        if repl_on_mutated == 0:
            return (
                "subsumption failed: the REPLACEMENT command PASSED on the mutated witness the "
                "original rejected (a no-op/weaker replacement does not subsume the original) — "
                "this is the false-green the proof must catch"
            )
        repl_on_base = runner(replacement.argv, base_dir)
        if repl_on_base != 0:
            return (
                "validity failed: the REPLACEMENT command FAILED on the clean base witness "
                "(a replacement that reds clean input is not a valid substitute)"
            )
    return None


def enforce_replacement_proofs(
    pending_proofs: Sequence[PendingReplacementProof],
    *,
    project_root: str | Path,
    command_runner: CommandRunner | None = None,
) -> ReplacementProofGateResult:
    """Execute every pending replacement proof and return the gate result.

    For each :class:`PendingReplacementProof`, run its behavioral subsumption proof; a
    PASS adds its fingerprint to ``approved``, a FAIL records a
    :class:`ReplacementProofViolation`. v2.77f executes the COMMAND proof (exit-code
    subsumption). An OBLIGATION proof (checker-based subsumption) is recognized but its
    witness must be exercised through the obligation machinery; until that wiring lands it
    is recorded as a violation (NEVER silently approved — anti-false-green: an unexecuted
    proof is not a pass).
    """
    root = Path(project_root)
    runner = command_runner if command_runner is not None else _default_command_runner
    approved: set[str] = set()
    violations: list[ReplacementProofViolation] = []
    for pending in pending_proofs:
        if pending.kind == "command":
            reason = _prove_command_subsumption(pending, root, runner)
        else:
            reason = (
                "obligation replace_with_proof execution is not wired in v2.77f — an "
                "unexecuted proof is never approved (anti-false-green)"
            )
        if reason is None:
            approved.add(pending.fingerprint)
        else:
            violations.append(
                ReplacementProofViolation(
                    fingerprint=pending.fingerprint, id=pending.id, reason=reason
                )
            )
    return ReplacementProofGateResult(approved=frozenset(approved), violations=tuple(violations))


__all__ = [
    "PROOF_DECL_KEY",
    "PROOF_SCHEMA",
    "PROOF_KIND",
    "ReplacementProofError",
    "ReplacementProofViolationError",
    "ProofWitness",
    "ReplacementProofDeclaration",
    "PendingReplacementProof",
    "ReplacementProofViolation",
    "ReplacementProofGateResult",
    "extract_proof_declaration",
    "enforce_replacement_proofs",
]
