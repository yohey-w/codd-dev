"""Compose a language ⊕ framework(s) ⊕ addon(s) into a ResolvedStackContract.

Per ``dogfood/gpt_composable_profile_design.md`` §1 (合成順序 / merge operators /
衝突解決) and §2 (決定性). The harness body never branches on a concrete layer
type — it consumes only the resolved contract this module produces.

Merge order is topological: language → framework(s) → addon(s). Merging is NOT
"last wins": each contribution is classified, and conflicts are surfaced (never
silently resolved):

* **compatible additive** (``app/`` + ``prisma/`` source sets) — merged.
* **compatible multi-role** (a path that is both a server file AND an api route)
  — merged (file roles union).
* **command conflict** — two layers claim the same command id with different
  argv (e.g. a framework silently replacing the language's ``typecheck``). RED.
* **semantic conflict** — a later layer (typically an addon) redefines an
  obligation id with WEAKER severity (weakening an obligation). RED.

The cardinal anti-false-green rule (design §207): each obligation's required
command must pass INDEPENDENTLY — a green ``unit_test`` never implies ``e2e_test``.
Verification slots are therefore owned, and a cross-layer redefinition of one
with different argv is a conflict, not a silent override.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Mapping, Sequence

from codd.languages.profile import CommandSpec, LanguageProfile
from .profile import AddonProfile, FileRole, FrameworkProfile, Obligation, SourceSet


@dataclass(frozen=True)
class StackLayout:
    """The resolved layout roots the harness needs to RUN a composed command.

    Contract Kernel v2.77g. A composed command slot's ``cwd``/``argv``/``env`` may
    carry LITERAL layout placeholders (``{module_root}`` / ``{repo_root}`` /
    ``{manifest_root}`` / ``{test_root}``) exactly like a language verify command — the
    profile keeps them as templates (full template substitution is PathPlanner's job).
    But the executor must spawn in a REAL directory and write its report to a REAL path,
    so the resolved contract carries the layout roots the plan substitutes at build time
    (the stack twin of :class:`codd.languages.profile.LayoutSpec` +
    :func:`codd.languages.verify_plan._substitute_layout_placeholders`).

    Sourced from the LANGUAGE profile's layout in :func:`compose` (the language owns
    repo topology + test sets; a framework CONTRIBUTES source sets but not the module/
    repo roots). ``test_roots`` carries EVERY declared test set's root so the plan can
    resolve ``{test_root}`` deterministically — a stack's ``verify`` runs the whole test
    tree from ONE root, so a profile with zero OR multiple test roots makes ``{test_root}``
    AMBIGUOUS and the plan reds (rather than guessing — GPT-5.5 Pro consult 2026-06-21).
    The convenience :attr:`test_root` is the single root when exactly one is declared (the
    common case), else ``""`` (the substitution then leaves ``{test_root}`` unresolved →
    the unsubstituted→RED guard fires). NOT part of
    :attr:`ResolvedStackContract.content_hash`: it is fully DERIVED from the already-pinned
    language layer (whose digest is in ``layers``), so it cannot drift independently of a
    pinned stack — folding it into the hash would only spuriously break a committed lock
    when nothing the lock protects changed.
    """

    module_root: str = "."
    repo_root: str = "."
    manifest_root: str = "."
    test_roots: tuple[str, ...] = ()

    @property
    def test_root(self) -> str:
        """The single unambiguous test root, or ``""`` when zero/multiple are declared.

        ``{test_root}`` substitution uses this; ``""`` deliberately leaves the placeholder
        unresolved so the executor's unsubstituted→RED guard fires (an ambiguous test root
        must not be silently guessed — anti-false-green)."""
        return self.test_roots[0] if len(self.test_roots) == 1 else ""

#: Verification command slots — a green in one NEVER implies another (design §207).
#: A cross-layer redefinition of one of these with different argv is a conflict.
#:
#: This is ALSO the materialization allowlist (Contract Kernel v2.77g): the command
#: plan executes ONLY these slots, so a non-verification convenience slot a framework
#: declares (``dev`` / ``start`` — a long-running SERVER that never exits, ``generate``
#: / ``migrate`` — a mutating convenience) is NEVER spawned by ``codd verify`` (running
#: a server slot would hang the gate forever). Every slot that GENUINELY verifies must
#: be in this set or it would be dropped — and a dropped verification = "not verified" =
#: a false-green. So this set is the SINGLE source of truth for "which composed slot is
#: a release check", and it MUST contain every build/test/static check: ``framework_build``
#: / ``build`` (a framework's production build — the ``no_ignore_build_errors_as_typecheck``
#: obligation is enforced AGAINST it being run, so dropping it would be the exact
#: false-green the obligation exists to kill). ``migration_check`` / ``migration_status``
#: (READ-ONLY drift checks) stay verification.
VERIFICATION_SLOTS = frozenset(
    {
        "typecheck",
        "verify",
        "unit_test",
        "integration_test",
        "coverage",
        "lint",
        "e2e_test",
        "build",
        "framework_build",
        "migration_check",
        "migration_status",
        "eval",
    }
)

#: KNOWN non-verification command slots — a framework/addon legitimately declares these
#: (a dev/start SERVER, a codegen/migration convenience) but they are NOT release checks,
#: so the command plan EXCLUDES them (a server would hang the gate; a mutating migration
#: must never run during verify). This is the OTHER half of the three-state slot model
#: (GPT-5.5 Pro consult 2026-06-21): a slot id is EITHER a verification slot (executed),
#: OR a known non-verification slot (excluded), OR UNKNOWN. An UNKNOWN slot id (in neither
#: set) is NOT silently dropped — that would be a false-green if it were a real check the
#: harness did not recognize — it is RED at plan build (``stack_command_plan`` raises),
#: forcing a profile to CLASSIFY every command it declares. ``generate`` (codegen) and
#: ``migrate_deploy`` (mutating apply) / ``migrate`` are non-proof-bearing convenience;
#: ``dev`` / ``start`` are servers. Mirrors the authenticity layer's own classification
#: of these ids as non-verification (``DEFAULT_STACK_COMMAND_OBSERVATION_POLICIES``).
NON_VERIFICATION_SLOTS = frozenset(
    {
        "dev",
        "start",
        "serve",
        "preview",
        "generate",
        "migrate",
        "migrate_deploy",
        "migrate_status",
        "install",
        "format",
        "clean",
    }
)

def _normalize_checker_ref(ref: str | None) -> str:
    """Canonical form of an obligation ``checker`` ref for redefinition comparison.

    ``None`` / empty / whitespace all normalize to ``""`` (no checker) so a later layer
    that nulls or blanks the checker compares as a CHANGE vs. a non-empty ref (a real
    weakening). Surrounding whitespace is stripped; the ref is otherwise compared
    verbatim (we never claim two distinct refs are "equivalent")."""
    return (ref or "").strip()


@dataclass(frozen=True)
class ResolvedLayerRef:
    kind: str
    id: str
    profile_version: str = "0.1.0"
    digest: str = ""


@dataclass(frozen=True)
class Conflict:
    """A composition conflict the strict gate must red on (design §衝突解決).

    ``replace_with_proof`` is the Contract Kernel v2.77f kind: a layer DECLARED a
    proof-backed replacement but the declaration is MALFORMED (bad schema / kind /
    missing witness / cross-id). A WELL-FORMED declaration is NOT a conflict — it is a
    pending proof (:attr:`ResolvedStackContract.pending_replacement_proofs`).
    """

    kind: Literal["exclusive", "command", "semantic", "replace_with_proof"]
    detail: str
    layers: tuple[str, ...] = ()


#: Sentinel returned by :func:`_classify_replacement` when a well-formed
#: ``replace_with_proof`` was recorded as a pending proof (so the caller records neither a
#: command nor a semantic conflict for it).
_REPLACEMENT_PENDING = object()


def _classify_replacement(
    *,
    kind: str,
    cid: str,
    original: Any,
    replacement: Any,
    owner: str,
    replacer: str,
    pending_proofs: list[Any],
) -> Any:
    """Classify a same-id redefinition that carries (or lacks) a ``replace_with_proof``.

    Returns one of:
    * :data:`_REPLACEMENT_PENDING` — a WELL-FORMED declaration was found and appended to
      ``pending_proofs`` (the caller records NO conflict; the proof gate decides clean).
    * a :class:`Conflict` (``kind="replace_with_proof"``) — a declaration was present but
      MALFORMED (anti-false-green: a broken proof declaration is RED, never accepted).
    * ``None`` — no declaration; the caller falls back to its ordinary conflict.

    Imported lazily to keep the dataclass/module load free of an import cycle (the
    replacement_proof module imports profile types from this package).
    """
    from .replacement_proof import (
        PendingReplacementProof,
        ReplacementProofError,
        extract_proof_declaration,
    )

    try:
        decl = extract_proof_declaration(replacement, kind=kind)
    except ReplacementProofError as exc:
        return Conflict(
            kind="replace_with_proof",
            detail=(
                f"{kind} {cid!r} declares a malformed replace_with_proof on {replacer} "
                f"(owner {owner}): {exc}. A proof-backed replacement is RED unless its "
                "declaration is well-formed AND its behavioral proof passes."
            ),
            layers=(owner, replacer),
        )
    if decl is None:
        return None
    pending_proofs.append(
        PendingReplacementProof.build(kind, original, replacement, decl)
    )
    return _REPLACEMENT_PENDING


@dataclass(frozen=True)
class ResolvedStackContract:
    """The single contract the harness consumes (design §1 ResolvedStackContract).

    ``content_hash`` is deterministic over the same inputs (design §決定性) and is
    what ``codd.stack.lock`` pins. ``conflicts`` is empty for a clean stack; any
    entry makes the stack NOT ``strict_ok``.
    """

    stack_id: str
    layers: tuple[ResolvedLayerRef, ...]
    commands: Mapping[str, CommandSpec]
    command_owners: Mapping[str, str]
    source_sets: tuple[SourceSet, ...]
    file_roles: tuple[FileRole, ...]
    obligations: tuple[Obligation, ...]
    obligation_owners: Mapping[str, str]
    build_outputs: tuple[str, ...]
    adapters: Mapping[str, tuple[str, ...]]  # role -> (("layer:adapter_ref"), ...)
    variants: Mapping[str, tuple[str, ...]]  # framework_id -> variant ids (exclusive)
    conflicts: tuple[Conflict, ...]
    content_hash: str
    #: Per-slot-id command AUTHENTICITY observation policy overrides (Contract Kernel
    #: v2.77d). DECLARATIVE extension point: a custom stack profile may declare the
    #: required-observation policy for a slot id the built-in default map
    #: (:data:`codd.stack.command_authenticity.DEFAULT_STACK_COMMAND_OBSERVATION_POLICIES`)
    #: does not cover, or STRENGTHEN a default. Empty for the curated stacks (they use
    #: the built-in defaults). Typed loosely (``Any`` value) to keep ``compose`` free of
    #: an import cycle with the authenticity layer; the values are
    #: ``StackCommandObservationPolicy``. NOT part of ``content_hash`` (it shapes the
    #: gate's strictness, not the resolved command set).
    command_observation_policies: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    #: Project-level TRANSPORT-ONLY command overrides applied to this contract (Contract
    #: Kernel v3.x ``stack.command_overrides``). Maps an overridden slot id to its parsed
    #: :class:`~codd.stack.command_override.ProjectCommandOverride` record (kept for the run
    #: trace / observability — the actual transport change is already baked into
    #: ``commands[slot_id]``). Empty for a no-override contract (the common case), so a
    #: stack with no ``command_overrides`` block is byte-identical. Typed loosely (``Any``
    #: value) to keep ``compose`` free of an import cycle with the override module; the
    #: values are ``ProjectCommandOverride``. NOT separately part of ``content_hash`` — the
    #: override's EFFECT (the changed argv/cwd/env/report) is hashed via the expanded
    #: command canonicalization (:func:`_content_hash` ``include_command_transport=True``),
    #: which is what drifts the lock.
    command_override_records: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    #: Proof-backed replacements (Contract Kernel v2.77f). A command/obligation that
    #: REPLACES another layer's via a well-formed ``codd.replace_with_proof`` is recorded
    #: here as a :class:`~codd.stack.replacement_proof.PendingReplacementProof` INSTEAD of
    #: an ordinary command/semantic Conflict — the replacement is NOT clean-by-syntax: it
    #: is pending an EXECUTED behavioral-subsumption proof. ``is_clean``/``strict_ok`` are
    #: about ordinary conflicts only; the proof gate
    #: (``codd.stack.command_plan.assert_stack_contract_clean`` with a
    #: ``ReplacementProofGateResult``) is what turns a pending proof GREEN. Typed loosely
    #: (``Any``) to keep ``compose`` import-cycle-free at the dataclass level. Empty for the
    #: curated stacks (no profile declares a replacement yet).
    pending_replacement_proofs: tuple[Any, ...] = ()
    #: Resolved layout roots (Contract Kernel v2.77g) — the real directories/paths the
    #: command plan substitutes for a slot's ``{module_root}`` / ``{repo_root}`` /
    #: ``{manifest_root}`` / ``{test_root}`` placeholders so a composed command spawns in
    #: a real dir and writes its report to a real path (the stack twin of the language
    #: verify executor's layout substitution). Sourced from the LANGUAGE profile's layout
    #: in :func:`compose`. NOT part of ``content_hash`` (see :class:`StackLayout`): it is
    #: derived from the already-pinned language layer, so it cannot drift independently of
    #: a pinned stack. Defaulted so an unparameterized contract (and every existing test
    #: constructing a contract directly) is unaffected.
    layout: StackLayout = field(default_factory=StackLayout)

    @property
    def is_clean(self) -> bool:
        return not self.conflicts

    @property
    def strict_ok(self) -> bool:
        """Strict mode reds on ANY conflict (design: weakening an obligation in
        strict mode is not allowed — drop to legacy_compatible to do so)."""
        return not self.conflicts


def _digest_profile(raw: Mapping) -> str:
    payload = json.dumps(_jsonable(raw), sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _jsonable(value):
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _command_hash_record(cid: str, c: CommandSpec) -> dict[str, Any]:
    """The FULL canonical record of a command for the content hash (Contract Kernel v3.x).

    Used ONLY when a project ``command_overrides`` is present (the expanded
    canonicalization, ``include_command_transport=True`` in :func:`_content_hash`). It
    covers every field a transport override can change — ``argv`` / ``cwd`` / ``env`` /
    ``report`` (path/format/adapter/capture) / ``requires_materialized_deps`` — PLUS the
    base-owned ``scope`` (so a lock pins the scope an overridden slot must cover too). A
    change to ANY of these drifts the lock and is re-reviewed (GPT-5.5 Pro consult
    2026-06-21 — "the override must be part of the locked contract hash"). Deterministic:
    env is sorted; ``None`` sub-objects are emitted as ``None``."""
    report = c.report
    scope = c.scope
    return {
        "id": cid,
        "argv": list(c.argv),
        "cwd": c.cwd,
        "env": sorted((str(k), str(v)) for k, v in c.env.items()),
        "requires_materialized_deps": bool(c.requires_materialized_deps),
        "report": None
        if report is None
        else {
            "path": report.path,
            "format": report.format,
            "adapter": report.adapter,
            "capture": report.capture,
        },
        "scope": None
        if scope is None
        else {
            "must_include_source_sets": list(scope.must_include_source_sets),
            "must_include_test_sets": list(scope.must_include_test_sets),
        },
    }


def _content_hash(
    layers: Sequence[ResolvedLayerRef],
    commands: Mapping[str, CommandSpec],
    obligations: Sequence[Obligation],
    file_roles: Sequence[FileRole],
    source_sets: Sequence[SourceSet],
    pending_proofs: Sequence[Any] = (),
    *,
    include_command_transport: bool = False,
) -> str:
    """Deterministic digest of the resolved contract (design §決定性).

    ``include_command_transport`` controls the command canonicalization, and is the
    knob that keeps EXISTING locks valid for the no-override case while letting a project
    command override DRIFT the lock (Contract Kernel v3.x):

    * ``False`` (the DEFAULT — used by :func:`compose` and every no-override resolve):
      commands canonicalize as ``[id, argv]`` ONLY — byte-identical to the pre-override
      hash, so every committed ``stack.lock`` from a no-override stack stays valid.
    * ``True`` (used by :func:`codd.stack.command_override.apply_project_command_overrides`
      WHEN a project declares ``command_overrides``): commands canonicalize as the FULL
      :func:`_command_hash_record` (argv + cwd + env + report + scope +
      requires_materialized_deps), so a transport override (argv/cwd/env/report) changes
      the digest and forces a lock re-review. This expanded form is used ONLY on the
      override path, so a no-override contract NEVER takes it (no spurious lock breakage)."""
    if include_command_transport:
        commands_canonical: Any = sorted(
            (_command_hash_record(cid, c) for cid, c in commands.items()),
            key=lambda rec: rec["id"],
        )
    else:
        commands_canonical = sorted([cid, list(c.argv)] for cid, c in commands.items())
    canonical = {
        "layers": [[l.kind, l.id, l.profile_version] for l in layers],
        "commands": commands_canonical,
        "obligations": sorted([o.id, o.severity] for o in obligations),
        "file_roles": sorted([r.pattern, r.role] for r in file_roles),
        "source_sets": sorted([s.id, s.root] for s in source_sets),
        # Proof-backed replacements (Contract Kernel v2.77f gaming-vector #5): the proof
        # DECLARATION must affect the content hash, so a changed witness/proof drifts the
        # lock (a proof cannot silently change under a stable lock). Each pending proof's
        # fingerprint already hashes (original, replacement, declaration).
        "replacement_proofs": sorted(
            getattr(p, "fingerprint", "") for p in (pending_proofs or ())
        ),
    }
    payload = json.dumps(canonical, sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compose(
    language: LanguageProfile,
    frameworks: Sequence[FrameworkProfile] = (),
    addons: Sequence[AddonProfile] = (),
) -> ResolvedStackContract:
    """Merge a language ⊕ framework(s) ⊕ addon(s) into a ResolvedStackContract."""
    layers: list[ResolvedLayerRef] = [
        ResolvedLayerRef("language", language.id, digest=_digest_profile(language.raw))
    ]
    for fw in frameworks:
        layers.append(
            ResolvedLayerRef(
                "framework", fw.id, fw.identity.profile_version, _digest_profile(fw.raw)
            )
        )
    for ad in addons:
        layers.append(
            ResolvedLayerRef(
                "addon", ad.id, ad.identity.profile_version, _digest_profile(ad.raw)
            )
        )

    conflicts: list[Conflict] = []
    pending_proofs: list[Any] = []

    # -- commands (language base; frameworks/addons add their own slots) --------
    commands: dict[str, CommandSpec] = dict(language.commands)
    command_owners: dict[str, str] = {cid: f"language:{language.id}" for cid in language.commands}
    for layer in [("framework", f) for f in frameworks] + [("addon", a) for a in addons]:
        kind, prof = layer
        for cid, spec in prof.commands.items():
            if cid not in commands:
                commands[cid] = spec
                command_owners[cid] = f"{kind}:{prof.id}"
                continue
            # Collision. Identical argv → harmless (same command). Different argv →
            # a layer is replacing another's command.
            if tuple(commands[cid].argv) == tuple(spec.argv):
                continue
            # A different-argv redefinition is a hard conflict UNLESS the replacing layer
            # carries a well-formed ``replace_with_proof`` (Contract Kernel v2.77f). Then
            # it is recorded as a PendingReplacementProof — NOT clean-by-syntax: the proof
            # gate must execute the behavioral subsumption before the contract is clean. A
            # MALFORMED declaration is its own RED (``replace_with_proof`` conflict kind).
            proof_conflict = _classify_replacement(
                kind="command",
                cid=cid,
                original=commands[cid],
                replacement=spec,
                owner=command_owners[cid],
                replacer=f"{kind}:{prof.id}",
                pending_proofs=pending_proofs,
            )
            if proof_conflict is _REPLACEMENT_PENDING:
                continue  # recorded as a pending proof; do NOT also record a conflict
            if proof_conflict is not None:
                conflicts.append(proof_conflict)
                continue
            conflicts.append(
                Conflict(
                    kind="command",
                    detail=(
                        f"command {cid!r} redefined by {kind}:{prof.id} (owner "
                        f"{command_owners[cid]}) with different argv"
                        + (" [verification slot]" if cid in VERIFICATION_SLOTS else "")
                    ),
                    layers=(command_owners[cid], f"{kind}:{prof.id}"),
                )
            )

    # -- obligations (union; a later layer may not silently REDEFINE an obligation) -
    # An obligation id is a SEMANTIC KEY. A later layer may repeat the same id ONLY if
    # the enforcement-relevant fields (severity + checker ref) are IDENTICAL — an exact
    # idempotent duplicate (e.g. two layers asserting the same e2e obligation). ANY
    # enforcement-relevant change is a semantic conflict (Contract Kernel v2.77e,
    # GPT-5.5 Pro consult 2026-06-21):
    #
    #   * severity mismatch (downgrade OR upgrade) — first-wins would SILENTLY pick one,
    #     hiding the ambiguity. A downgrade weakens; an upgrade silently DROPS a stricter
    #     later layer (a false-green relative to the intended stricter obligation). With
    #     no explicit monotone-merge semantics, ANY severity mismatch is RED (strict
    #     option — less clever, harder to game).
    #   * checker-ref change (including a redefinition that nulls/empties or points the
    #     checker at a different/weaker/unknown adapter) — "same severity, gutted checker"
    #     is real weakening that first-wins hides by luck, not by contract. A later layer
    #     that means a DIFFERENT check must use a DIFFERENT id; one that means the SAME
    #     check must use the SAME checker ref. (We do NOT try to prove two different refs
    #     are "equivalent" — intractable at the contract-kernel level.)
    #
    # first-wins is kept for the resolved set (the FIRST declaration stays authoritative),
    # but a non-idempotent redefinition is RECORDED as a Conflict so the strict gate reds
    # — silence is forbidden (no silent fallback).
    obligations: list[Obligation] = []
    obligation_owners: dict[str, str] = {}
    obl_by_id: dict[str, Obligation] = {}
    for kind, prof in [("framework", f) for f in frameworks] + [("addon", a) for a in addons]:
        for obl in prof.obligations:
            if obl.id not in obl_by_id:
                obl_by_id[obl.id] = obl
                obligations.append(obl)
                obligation_owners[obl.id] = f"{kind}:{prof.id}"
                continue
            existing = obl_by_id[obl.id]
            if obl.severity != existing.severity:
                conflicts.append(
                    Conflict(
                        kind="semantic",
                        detail=(
                            f"obligation {obl.id!r} severity redefined to {obl.severity!r} "
                            f"by {kind}:{prof.id} (owner {obligation_owners[obl.id]} "
                            f"declared {existing.severity!r}) — a severity mismatch is a "
                            "semantic conflict (a downgrade weakens; an upgrade silently "
                            "drops a stricter layer); use the same id only for the same "
                            "obligation at the same severity"
                        ),
                        layers=(obligation_owners[obl.id], f"{kind}:{prof.id}"),
                    )
                )
            elif _normalize_checker_ref(obl.checker) != _normalize_checker_ref(existing.checker):
                # A same-severity, different-CHECKER redefinition. This may be a legitimate
                # proof-backed replacement (a stronger/equivalent checker) IFF the new
                # checker is REAL (non-empty) AND a well-formed replace_with_proof is
                # declared. A null/empty new checker can NEVER be proof-rescued (you cannot
                # prove subsumption with no checker — it would be an unenforced RED anyway),
                # so that path stays a hard semantic conflict.
                proof_outcome = None
                if _normalize_checker_ref(obl.checker):  # new checker is non-empty
                    proof_outcome = _classify_replacement(
                        kind="obligation",
                        cid=obl.id,
                        original=existing,
                        replacement=obl,
                        owner=obligation_owners[obl.id],
                        replacer=f"{kind}:{prof.id}",
                        pending_proofs=pending_proofs,
                    )
                if proof_outcome is _REPLACEMENT_PENDING:
                    continue  # recorded as a pending proof; no conflict
                if proof_outcome is not None:
                    conflicts.append(proof_outcome)
                    continue
                conflicts.append(
                    Conflict(
                        kind="semantic",
                        detail=(
                            f"obligation {obl.id!r} checker redefined to "
                            f"{obl.checker!r} by {kind}:{prof.id} (owner "
                            f"{obligation_owners[obl.id]} declared {existing.checker!r}) — "
                            "a same-id checker-ref change is a semantic conflict (it would "
                            "silently keep one checker and discard the other); two different "
                            "checks need two different obligation ids, the same check needs "
                            "the same checker ref (or an explicit, proof-backed "
                            "replace_with_proof)"
                        ),
                        layers=(obligation_owners[obl.id], f"{kind}:{prof.id}"),
                    )
                )

    # -- source_sets / file_roles (additive union; multi-role allowed) ---------
    source_sets: list[SourceSet] = list(language.layout.source_sets)
    seen_ss = {s.id for s in source_sets}
    for fw in frameworks:
        for ss in fw.layout.source_sets:
            if ss.id not in seen_ss:
                source_sets.append(ss)
                seen_ss.add(ss.id)

    file_roles: list[FileRole] = []
    seen_fr: set[tuple[str, str]] = set()
    for fw in frameworks:
        for fr in fw.file_roles:
            key = (fr.pattern, fr.role)
            if key not in seen_fr:
                file_roles.append(fr)
                seen_fr.add(key)

    # -- artifacts / adapters / variants ---------------------------------------
    build_outputs: list[str] = []
    for fw in frameworks:
        build_outputs.extend(fw.artifacts.build_outputs)
    for ad in addons:
        build_outputs.extend(ad.artifacts.build_outputs)
    build_outputs = list(dict.fromkeys(build_outputs))  # dedupe, order-preserving

    # Adapter pipeline by role (language adapters are wired in a later phase).
    adapters: dict[str, list[str]] = {}
    for prof in list(frameworks) + list(addons):
        for role, ref in prof.adapters.items():
            adapters.setdefault(role, []).append(f"{prof.id}:{ref.id}")

    variants: dict[str, tuple[str, ...]] = {
        fw.id: tuple(v.id for v in fw.variants) for fw in frameworks if fw.variants
    }

    # -- resolved layout (Contract Kernel v2.77g) ------------------------------
    # The LANGUAGE owns repo topology + test sets; copy the roots the command plan
    # needs to substitute a slot's {module_root}/{repo_root}/{manifest_root}/{test_root}.
    # Carry ALL test roots so the plan can detect an ambiguous {test_root} (zero/multiple)
    # and RED rather than guess. Derived from the language layer, NOT hashed.
    lang_layout = language.layout
    test_sets = getattr(lang_layout, "test_sets", ()) or ()
    stack_layout = StackLayout(
        module_root=lang_layout.module_root,
        repo_root=lang_layout.repo_root,
        manifest_root=lang_layout.manifest_root,
        test_roots=tuple(ts.root for ts in test_sets),
    )

    stack_id = "+".join(l.id for l in layers)
    content_hash = _content_hash(
        layers, commands, obligations, file_roles, source_sets, pending_proofs
    )

    return ResolvedStackContract(
        stack_id=stack_id,
        layers=tuple(layers),
        commands=MappingProxyType(dict(commands)),
        command_owners=MappingProxyType(dict(command_owners)),
        source_sets=tuple(source_sets),
        file_roles=tuple(file_roles),
        obligations=tuple(obligations),
        obligation_owners=MappingProxyType(dict(obligation_owners)),
        build_outputs=tuple(build_outputs),
        adapters=MappingProxyType({k: tuple(v) for k, v in adapters.items()}),
        variants=MappingProxyType(dict(variants)),
        conflicts=tuple(conflicts),
        content_hash=content_hash,
        pending_replacement_proofs=tuple(pending_proofs),
        layout=stack_layout,
    )
