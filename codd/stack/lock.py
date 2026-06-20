"""``codd.stack.lock`` — pin a resolved stack for deterministic CI (design §2/§決定性).

Auto-detection (package.json has ``next`` → use the Next.js profile) is convenient
for ``local init`` only. In CI the lock is the SOURCE OF TRUTH: the resolved
contract is recomputed and any divergence from the lock (a layer version bump, a
profile edit changing a digest, a different resolved-contract hash) is RED, so a
profile change can never silently alter a project's contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import yaml

from .compose import ResolvedStackContract

LOCK_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class LockedLayer:
    id: str
    kind: str
    version: str
    digest: str


@dataclass(frozen=True)
class StackLock:
    schema_version: int
    stack_id: str
    layers: tuple[LockedLayer, ...]
    resolved_contract_digest: str
    adapter_digests: Mapping[str, str] = field(default_factory=dict)
    permissions: Mapping[str, Any] = field(default_factory=dict)


def build_lock(
    contract: ResolvedStackContract,
    *,
    adapter_digests: Mapping[str, str] | None = None,
    permissions: Mapping[str, Any] | None = None,
) -> StackLock:
    """Derive a :class:`StackLock` from a resolved contract."""
    return StackLock(
        schema_version=LOCK_SCHEMA_VERSION,
        stack_id=contract.stack_id,
        layers=tuple(
            LockedLayer(id=l.id, kind=l.kind, version=l.profile_version, digest=l.digest)
            for l in contract.layers
        ),
        resolved_contract_digest=contract.content_hash,
        adapter_digests=dict(adapter_digests or {}),
        permissions=dict(permissions or {}),
    )


def lock_to_dict(lock: StackLock) -> dict[str, Any]:
    return {
        "schema_version": lock.schema_version,
        "stack_id": lock.stack_id,
        "layers": [
            {"id": l.id, "kind": l.kind, "version": l.version, "digest": l.digest}
            for l in lock.layers
        ],
        "resolved_contract_digest": lock.resolved_contract_digest,
        "adapter_digests": dict(lock.adapter_digests),
        "permissions": dict(lock.permissions),
    }


def dump_lock(lock: StackLock) -> str:
    """Serialize a lock to YAML (stable key order for clean diffs)."""
    return yaml.safe_dump(lock_to_dict(lock), sort_keys=True, default_flow_style=False)


def parse_lock(data: str | Mapping[str, Any]) -> StackLock:
    doc = yaml.safe_load(data) if isinstance(data, str) else dict(data)
    if not isinstance(doc, Mapping):
        raise ValueError("codd.stack.lock must be a mapping at the top level")
    layers = tuple(
        LockedLayer(
            id=str(l["id"]),
            kind=str(l.get("kind", "")),
            version=str(l.get("version", "")),
            digest=str(l.get("digest", "")),
        )
        for l in (doc.get("layers") or [])
    )
    return StackLock(
        schema_version=int(doc.get("schema_version", LOCK_SCHEMA_VERSION)),
        stack_id=str(doc.get("stack_id", "")),
        layers=layers,
        resolved_contract_digest=str(doc.get("resolved_contract_digest", "")),
        adapter_digests=dict(doc.get("adapter_digests") or {}),
        permissions=dict(doc.get("permissions") or {}),
    )


def verify_lock(contract: ResolvedStackContract, lock: StackLock) -> tuple[bool, list[str]]:
    """Check a freshly-resolved contract against a pinned lock (CI gate).

    Returns ``(ok, diffs)``. ``ok`` is False (CI RED) when the resolved contract
    diverges from the lock — a layer set/version/digest change, or a different
    resolved-contract digest (a profile edit that silently changed the contract).
    """
    diffs: list[str] = []
    if contract.stack_id != lock.stack_id:
        diffs.append(f"stack_id: contract={contract.stack_id!r} lock={lock.stack_id!r}")

    contract_layers = {l.id: l for l in contract.layers}
    lock_layers = {l.id: l for l in lock.layers}
    for lid in sorted(set(contract_layers) | set(lock_layers)):
        if lid not in contract_layers:
            diffs.append(f"layer {lid!r}: in lock but not resolved")
            continue
        if lid not in lock_layers:
            diffs.append(f"layer {lid!r}: resolved but not in lock")
            continue
        c, k = contract_layers[lid], lock_layers[lid]
        if c.profile_version != k.version:
            diffs.append(f"layer {lid!r} version: contract={c.profile_version} lock={k.version}")
        if c.digest and k.digest and c.digest != k.digest:
            diffs.append(f"layer {lid!r} digest changed (profile edited)")

    if contract.content_hash != lock.resolved_contract_digest:
        diffs.append(
            "resolved_contract_digest changed "
            f"(contract={contract.content_hash[:23]}… lock={lock.resolved_contract_digest[:23]}…)"
        )
    return (not diffs, diffs)
