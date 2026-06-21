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

#: Verification command slots — a green in one NEVER implies another (design §207).
#: A cross-layer redefinition of one of these with different argv is a conflict.
VERIFICATION_SLOTS = frozenset(
    {
        "typecheck",
        "verify",
        "unit_test",
        "integration_test",
        "coverage",
        "lint",
        "e2e_test",
        "migration_check",
        "migration_status",
        "eval",
    }
)

_SEVERITY_RANK = {"warn": 0, "error": 1}


@dataclass(frozen=True)
class ResolvedLayerRef:
    kind: str
    id: str
    profile_version: str = "0.1.0"
    digest: str = ""


@dataclass(frozen=True)
class Conflict:
    """A composition conflict the strict gate must red on (design §衝突解決)."""

    kind: Literal["exclusive", "command", "semantic"]
    detail: str
    layers: tuple[str, ...] = ()


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


def _content_hash(
    layers: Sequence[ResolvedLayerRef],
    commands: Mapping[str, CommandSpec],
    obligations: Sequence[Obligation],
    file_roles: Sequence[FileRole],
    source_sets: Sequence[SourceSet],
) -> str:
    canonical = {
        "layers": [[l.kind, l.id, l.profile_version] for l in layers],
        "commands": sorted([cid, list(c.argv)] for cid, c in commands.items()),
        "obligations": sorted([o.id, o.severity] for o in obligations),
        "file_roles": sorted([r.pattern, r.role] for r in file_roles),
        "source_sets": sorted([s.id, s.root] for s in source_sets),
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
            # a layer is replacing another's command. For a verification slot this
            # is a hard conflict (no silent weakening); elsewhere too (must be an
            # explicit replace_with_proof, which no profile declares yet).
            if tuple(commands[cid].argv) == tuple(spec.argv):
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

    # -- obligations (union; an addon must not WEAKEN a framework obligation) ---
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
            if _SEVERITY_RANK[obl.severity] < _SEVERITY_RANK[existing.severity]:
                conflicts.append(
                    Conflict(
                        kind="semantic",
                        detail=(
                            f"obligation {obl.id!r} weakened to {obl.severity!r} by "
                            f"{kind}:{prof.id} (owner {obligation_owners[obl.id]} "
                            f"declared {existing.severity!r})"
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

    stack_id = "+".join(l.id for l in layers)
    content_hash = _content_hash(layers, commands, obligations, file_roles, source_sets)

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
    )
