"""``codd.stack.lock`` — pin a resolved stack for deterministic CI (design §2/§決定性).

Auto-detection (package.json has ``next`` → use the Next.js profile) is convenient
for ``local init`` only. In CI the lock is the SOURCE OF TRUTH: the resolved
contract is recomputed and any divergence from the lock (a layer version bump, a
profile edit changing a digest, a different resolved-contract hash) is RED, so a
profile change can never silently alter a project's contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Mapping

import yaml

from codd.profile_version import is_strict_profile_upgrade, profile_version_key

from .compose import ResolvedStackContract

#: The lock file lives next to ``codd.yaml`` inside the project's CoDD config dir
#: (so ``<project>/codd/stack.lock`` or ``<project>/.codd/stack.lock``). The
#: module docstring's ``codd.stack.lock`` is this file.
LOCK_FILENAME = "stack.lock"

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


class StackLockUpdateError(ValueError):
    """A committed lock cannot take the narrow, proof-backed profile-update path."""


@dataclass(frozen=True)
class StackLockLayerUpdate:
    """One exact profile revision accepted by a stack-lock update plan."""

    id: str
    kind: str
    old_version: str
    new_version: str
    old_digest: str
    new_digest: str


@dataclass(frozen=True)
class StackLockUpdatePlan:
    """A version-gated candidate; constructing it never writes the lock."""

    current: StackLock
    candidate: StackLock
    layer_updates: tuple[StackLockLayerUpdate, ...]

    @property
    def candidate_fingerprint(self) -> str:
        """Digest binding acceptance to every serialized candidate-lock field."""
        return stack_lock_fingerprint(self.candidate)


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


def stack_lock_fingerprint(lock: StackLock) -> str:
    """Hash the complete canonical lock, including every raw layer digest."""
    return "sha256:" + hashlib.sha256(dump_lock(lock).encode("utf-8")).hexdigest()


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
        # Missing is not silently upgraded to the current schema.  A lock that
        # does not declare which schema produced it is unversioned input and the
        # read-only gate must fail closed.
        schema_version=int(doc.get("schema_version", 0)),
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
    if lock.schema_version != LOCK_SCHEMA_VERSION:
        diffs.append(
            f"schema_version: supported={LOCK_SCHEMA_VERSION} lock={lock.schema_version}"
        )
    if contract.stack_id != lock.stack_id:
        diffs.append(f"stack_id: contract={contract.stack_id!r} lock={lock.stack_id!r}")

    contract_ids = [l.id for l in contract.layers]
    lock_ids = [l.id for l in lock.layers]
    duplicate_contract_ids = sorted({lid for lid in contract_ids if contract_ids.count(lid) > 1})
    duplicate_lock_ids = sorted({lid for lid in lock_ids if lock_ids.count(lid) > 1})
    if duplicate_contract_ids:
        diffs.append(f"resolved contract has duplicate layer ids: {duplicate_contract_ids}")
    if duplicate_lock_ids:
        diffs.append(f"lock has duplicate layer ids: {duplicate_lock_ids}")

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
        if c.kind != k.kind:
            diffs.append(f"layer {lid!r} kind: contract={c.kind!r} lock={k.kind!r}")
        if c.profile_version != k.version:
            diffs.append(f"layer {lid!r} version: contract={c.profile_version} lock={k.version}")
        if not k.digest:
            diffs.append(f"layer {lid!r} digest missing from lock")
        if not c.digest:
            diffs.append(f"layer {lid!r} digest missing from resolved contract")
        if c.digest and k.digest and c.digest != k.digest:
            diffs.append(f"layer {lid!r} digest changed (profile edited)")

    if contract.content_hash != lock.resolved_contract_digest:
        diffs.append(
            "resolved_contract_digest changed "
            f"(contract={contract.content_hash[:23]}… lock={lock.resolved_contract_digest[:23]}…)"
        )
    return (not diffs, diffs)


def plan_stack_lock_update(
    contract: ResolvedStackContract,
    lock: StackLock,
) -> StackLockUpdatePlan:
    """Plan the only supported update of an EXISTING lock: profile upgrades.

    This is deliberately narrower than ``build_lock(contract)``.  The declared
    stack and ordered layer identities must be unchanged, every changed raw
    profile digest must carry a strictly newer stable ``profile_version``, and a
    downgrade is always rejected.  In particular, a same-version digest edit is
    still classified as tampering/unversioned drift and remains RED.

    The returned candidate is not proof and this function performs no write.  The
    CLI binds owner intent to the complete candidate-lock fingerprint, runs the candidate's
    real stack commands/obligations plus ordinary verification, re-resolves to
    close the proof-to-write race, and only then calls
    :func:`commit_stack_lock_update`.
    """
    if lock.schema_version != LOCK_SCHEMA_VERSION:
        raise StackLockUpdateError(
            f"unsupported stack lock schema {lock.schema_version}; expected "
            f"{LOCK_SCHEMA_VERSION}"
        )
    if not lock.resolved_contract_digest:
        raise StackLockUpdateError(
            "committed stack lock has no resolved_contract_digest; an unpinned "
            "lock cannot authorize an update"
        )
    if contract.stack_id != lock.stack_id:
        raise StackLockUpdateError(
            "stack_id changed; profile-update mode cannot accept a project stack "
            f"declaration change ({lock.stack_id!r} -> {contract.stack_id!r})"
        )

    resolved_keys = tuple((layer.kind, layer.id) for layer in contract.layers)
    locked_keys = tuple((layer.kind, layer.id) for layer in lock.layers)
    if len({layer.id for layer in contract.layers}) != len(contract.layers):
        raise StackLockUpdateError("resolved contract contains duplicate layer ids")
    if len({layer.id for layer in lock.layers}) != len(lock.layers):
        raise StackLockUpdateError("committed stack lock contains duplicate layer ids")
    if resolved_keys != locked_keys:
        raise StackLockUpdateError(
            "ordered stack layers changed; profile-update mode requires the exact "
            f"locked layers (lock={locked_keys!r}, resolved={resolved_keys!r})"
        )

    updates: list[StackLockLayerUpdate] = []
    for resolved, locked in zip(contract.layers, lock.layers, strict=True):
        try:
            old_key = profile_version_key(
                locked.version, where=f"locked layer {locked.id!r} profile_version"
            )
            new_key = profile_version_key(
                resolved.profile_version,
                where=f"resolved layer {resolved.id!r} profile_version",
            )
        except ValueError as exc:
            raise StackLockUpdateError(str(exc)) from exc

        if new_key == old_key:
            if resolved.digest != locked.digest:
                raise StackLockUpdateError(
                    f"layer {resolved.id!r} digest changed without a profile_version "
                    f"increase ({locked.version}); same-version profile drift remains RED"
                )
            continue
        if not is_strict_profile_upgrade(locked.version, resolved.profile_version):
            raise StackLockUpdateError(
                f"layer {resolved.id!r} profile_version is not a strict upgrade "
                f"({locked.version} -> {resolved.profile_version}); rollback/non-upgrade "
                "updates are refused"
            )
        if not locked.digest or not resolved.digest:
            raise StackLockUpdateError(
                f"layer {resolved.id!r} lacks a profile digest; an unpinned profile "
                "cannot be accepted as an upgrade"
            )
        updates.append(
            StackLockLayerUpdate(
                id=resolved.id,
                kind=resolved.kind,
                old_version=locked.version,
                new_version=resolved.profile_version,
                old_digest=locked.digest,
                new_digest=resolved.digest,
            )
        )

    if not updates:
        ok, diffs = verify_lock(contract, lock)
        if ok:
            raise StackLockUpdateError("stack lock already matches the resolved contract")
        raise StackLockUpdateError(
            "lock drift is not attributable to a strict profile_version upgrade: "
            + "; ".join(diffs)
        )

    candidate = build_lock(
        contract,
        adapter_digests=lock.adapter_digests,
        permissions=lock.permissions,
    )
    candidate_ok, candidate_diffs = verify_lock(contract, candidate)
    if not candidate_ok:  # Defensive: a plan must never emit an unverifiable candidate.
        raise StackLockUpdateError(
            "internal error: generated update candidate does not match the contract: "
            + "; ".join(candidate_diffs)
        )
    return StackLockUpdatePlan(
        current=lock,
        candidate=candidate,
        layer_updates=tuple(updates),
    )


def commit_stack_lock_update(
    plan: StackLockUpdatePlan,
    project_root: str | Path,
    *,
    expected_lock_text: str,
) -> Path:
    """Atomically commit a pre-verified update plan with stale-input defense.

    This function is intentionally not a verifier.  It is the final write step
    used by ``codd stack update-lock`` *after* candidate verification.  It refuses
    to write if the on-disk lock differs byte-for-byte from the text that was
    planned/proved, preventing a stale proof from overwriting a concurrent edit.
    Normal ``codd verify`` and :func:`enforce_stack_lock` never call it.
    """
    path = stack_lock_path(project_root)
    try:
        current_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StackLockUpdateError(f"cannot read committed stack lock {path}: {exc}") from exc
    if current_text != expected_lock_text:
        raise StackLockUpdateError(
            "stack lock changed after the update was planned; refusing to overwrite "
            "a concurrent/stale lock"
        )
    try:
        parsed_current = parse_lock(current_text)
    except Exception as exc:  # noqa: BLE001 - malformed current input must fail closed.
        raise StackLockUpdateError(f"committed stack lock became unparseable: {exc}") from exc
    if parsed_current != plan.current:
        raise StackLockUpdateError(
            "planned current lock does not equal the committed lock; refusing stale update"
        )

    candidate_text = dump_lock(plan.candidate)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(candidate_text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_path, stat.S_IMODE(path.stat().st_mode))
        except OSError:
            pass  # Permissions are not proof-bearing; content checks remain fail-closed.

        # Check again immediately before the atomic replace.  The replace is one
        # filesystem operation; cooperative writers using this API cannot clobber
        # each other with a stale proof.
        if path.read_text(encoding="utf-8") != expected_lock_text:
            raise StackLockUpdateError(
                "stack lock changed while the candidate was being written; refusing "
                "to overwrite a concurrent/stale lock"
            )
        os.replace(temp_path, path)
    except Exception as exc:
        try:
            temp_path.unlink()
        except OSError:
            pass
        if isinstance(exc, StackLockUpdateError):
            raise
        if isinstance(exc, OSError):
            raise StackLockUpdateError(
                f"could not atomically replace committed stack lock {path}: {exc}"
            ) from exc
        raise
    return path


# ── enforcement gate (Contract Kernel v2.77b — Stack Lock Enforcement) ───────
#
# v2.77a brought the stack contract LIVE (intake-only). v2.77b turns the
# already-existing lock logic above into a GATE so stack-contract drift is RED.
#
# Anti-false-green is the entire point of this step. The design (B′, confirmed by
# a GPT-5.5 Pro consult — see the task report) SPLITS the two responsibilities so
# the gate has NO "verify-or-create" mixed semantics — the one and only thing that
# could turn a drift-RED into green:
#
#   * :func:`enforce_stack_lock` is STRICTLY READ-ONLY. It NEVER writes/refreshes
#     a lock. missing → RED, parse-error → RED, drift → RED, valid → GREEN. Both
#     the verify path and the greenfield path call THIS for the verdict.
#   * :func:`bootstrap_stack_lock` is the only AUTOMATIC/creation writer and uses
#     EXCLUSIVE create (``open(..., "x")``): it writes only when the lock is ABSENT
#     and is invoked only on a positively-identified project-creation path
#     (greenfield first generation). It refuses to overwrite, so it cannot refresh
#     a drift.
#   * An EXISTING lock can be changed only through the separate, explicit
#     ``codd stack update-lock`` path: strict profile-version increase, digest-bound
#     owner acceptance, real verification proof, re-resolution, then atomic replace.
#     The enforcement gate and auto-repair never reach that writer.
#
# ANTI-GAMING (exit gate 3, the crux): ``verify_lock(contract,
# build_lock(contract))`` is ALWAYS ok by construction — a drift can be MASKED by
# rewriting the lock to match. So the verify path can't detect gaming; the control
# is "who may WRITE a lock, and when". Here: the read-only gate writes nothing
# ever, and bootstrap writes only on exclusive-create in the creation path. A
# drift against a committed lock is RED on every path and is never refreshed by
# either enforcement function; auto-repair re-running the gate keeps seeing
# drift-RED. A declared profile revision is accepted only through the explicit
# proof-backed update workflow above; same-version digest drift is never eligible.
#
# WHY NOT "missing + absent-session ⇒ generate": absence of a session is NOT
# proof of first generation (it can be a deleted session, a copied project, an
# existing repo, or auto-repair having cleaned local state). Generation therefore
# belongs to an explicit creation path (bootstrap), and the gate fails CLOSED
# (missing = RED) everywhere else — the delete-and-regenerate attack cannot
# silence a drift because the gate never participates in regeneration.

#: ``StackLockGate.status`` values.
LOCK_OK = "ok"  # a committed lock matches the resolved contract (GREEN).
LOCK_DRIFT = "drift"  # a committed lock diverges / is unparseable (RED).
LOCK_MISSING = "missing"  # no committed lock where one must exist (RED).
LOCK_GENERATED = "generated"  # bootstrap wrote the first lock (GREEN, traced).


def stack_lock_path(project_root: str | Path) -> Path:
    """Return the path of a project's stack lock file (next to ``codd.yaml``).

    Resolved from the canonical project root via the same config-dir discovery as
    :func:`codd.config.load_project_config` (``codd/`` then ``.codd/``), NOT from a
    mutable CWD. If no config dir is discoverable yet, defaults to
    ``<project_root>/codd/stack.lock`` so a first-generation greenfield run (which
    has just created ``codd/``) bootstraps the lock in the canonical place.
    """
    from codd.config import find_codd_dir

    root = Path(project_root)
    codd_dir = find_codd_dir(root)
    if codd_dir is None:
        codd_dir = root / "codd"
    return codd_dir / LOCK_FILENAME


@dataclass(frozen=True)
class StackLockGate:
    """The verdict of the (read-only) stack-lock enforcement gate (v2.77b).

    ``red`` is the single anti-false-green signal the pipeline/verify call-sites
    act on: ``drift`` and ``missing`` are RED; ``ok`` is GREEN; ``generated`` is the
    GREEN result returned right after :func:`bootstrap_stack_lock` writes the first
    lock. ``reasons`` carry the human-readable drift diffs.
    """

    status: str
    red: bool
    reasons: tuple[str, ...] = ()
    lock_path: str = ""

    @property
    def message(self) -> str:
        if self.status == LOCK_OK:
            return f"stack lock OK ({self.lock_path})"
        if self.status == LOCK_GENERATED:
            return f"stack lock generated (first generation) at {self.lock_path}"
        if self.status == LOCK_MISSING:
            return (
                f"stack lock MISSING at {self.lock_path}: a project that declares a "
                "`stack:` block must commit a stack lock — an unpinned stack contract is "
                "unverifiable (anti-false-green). It is generated once on a first "
                "`codd greenfield` run; the enforcement gate is read-only and will never "
                "silently create it (a missing lock outside first generation is RED, so a "
                "deleted lock cannot be silently regenerated to green)."
            )
        # drift (incl. present-but-unparseable lock)
        joined = "; ".join(self.reasons) if self.reasons else "resolved contract diverges from the lock"
        return (
            f"stack lock DRIFT ({self.lock_path}): the resolved stack contract no longer "
            f"matches the committed lock [{joined}]. This is RED. The gate is read-only "
            "and never refreshes a lock. Revert an unintended change; for an intentional "
            "profile revision with a strictly newer profile_version, review "
            "`codd stack update-lock --dry-run --reason <reason>` and accept its exact "
            "candidate digest. Same-version profile digest drift is not update-eligible."
        )


def enforce_stack_lock(
    contract: ResolvedStackContract, project_root: str | Path
) -> StackLockGate:
    """Read-only stack-lock gate for a resolved contract (the v2.77b enforcement).

    ``contract`` is the freshly-resolved :class:`ResolvedStackContract` (the caller
    has already intaken it — v2.77a). This function reads the project's committed
    lock and returns a verdict; it NEVER writes or refreshes the lock (so it cannot
    turn a drift-RED into green — exit gate 3):

    * **missing** — no committed lock → ``red=True`` (``status=missing``). A stack
      project with no pin is unverifiable; generation is the separate
      :func:`bootstrap_stack_lock` responsibility (creation path only).
    * **parse error** — a present-but-unparseable lock → ``red=True``
      (``status=drift``). A broken lock is NEVER treated as "missing" (which could
      invite regeneration).
    * **drift** — a committed lock exists but :func:`verify_lock` reports
      divergence → ``red=True`` (``status=drift``).
    * **valid** — a committed lock matches → ``status=ok``, ``red=False``.
    """
    path = stack_lock_path(project_root)
    path_str = str(path)

    if not path.exists():
        return StackLockGate(status=LOCK_MISSING, red=True, lock_path=path_str)

    try:
        lock = parse_lock(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — any read/parse failure is RED, never "missing".
        # A present-but-unreadable/corrupt lock is NOT a pass and is NOT "missing"
        # (which could invite regeneration): it is drift (RED).
        return StackLockGate(
            status=LOCK_DRIFT,
            red=True,
            reasons=(f"lock file present but unparseable: {type(exc).__name__}: {exc}",),
            lock_path=path_str,
        )

    ok, diffs = verify_lock(contract, lock)
    if ok:
        return StackLockGate(status=LOCK_OK, red=False, lock_path=path_str)
    # DRIFT — RED. Read-only: the lock is left untouched (anti-gaming, exit gate 3).
    return StackLockGate(status=LOCK_DRIFT, red=True, reasons=tuple(diffs), lock_path=path_str)


def bootstrap_stack_lock(
    contract: ResolvedStackContract,
    project_root: str | Path,
    *,
    adapter_digests: Mapping[str, str] | None = None,
    permissions: Mapping[str, Any] | None = None,
) -> StackLockGate:
    """Write a project's FIRST stack lock — the only automatic/creation writer.

    Invoked ONLY on a positively-identified project-creation path (greenfield first
    generation), never by the enforcement gate, never by verify/resume, never by
    repair. Uses EXCLUSIVE create (``open(..., "x")``): it writes the lock iff it is
    ABSENT and refuses to overwrite an existing lock. This is what makes the
    delete-and-regenerate / drift-refresh attacks impossible — bootstrap cannot
    refresh a drifted (existing) lock, and a missing lock outside the creation path
    is RED at the read-only gate.

    Returns the read-only :func:`enforce_stack_lock` verdict computed AFTER the
    write (so the freshly-written lock is immediately verified — ``status=ok`` on
    success). If the lock already exists, NOTHING is written and the existing lock
    is enforced as-is (so a pre-existing drift stays RED).
    """
    path = stack_lock_path(project_root)
    if not path.exists():
        lock = build_lock(contract, adapter_digests=adapter_digests, permissions=permissions)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Exclusive create — refuse to clobber an existing lock (TOCTOU-safe).
            with open(path, "x", encoding="utf-8") as fh:
                fh.write(dump_lock(lock))
        except FileExistsError:
            # Raced: a lock appeared between the check and the create. Fall through
            # to enforce the now-existing lock read-only (never overwrite it).
            pass
        else:
            # Immediately enforce the freshly-written lock (read-only); on success
            # surface it as GENERATED (traced) rather than a plain OK.
            gate = enforce_stack_lock(contract, project_root)
            if gate.status == LOCK_OK:
                return StackLockGate(status=LOCK_GENERATED, red=False, lock_path=gate.lock_path)
            return gate

    # Lock already exists (or appeared in a race) — enforce it read-only, NEVER
    # overwrite. A pre-existing drift therefore stays RED.
    return enforce_stack_lock(contract, project_root)


def orphan_stack_lock(project_root: str | Path) -> StackLockGate | None:
    """Catch a removed ``stack:`` declaration that still has a committed lock.

    Closes the "drop the ``stack:`` block to dodge the gate" bypass (GPT-consult
    point #1): a project with a committed ``stack.lock`` is demonstrably
    stack-governed, so silently removing its declaration would convert it to an
    ungoverned project (false-green). This is called ONLY when the project has NO
    resolved stack contract (``stack_contract_intake`` returned ``None``):

    * a committed lock still present → RED (``status=drift``): the declaration was
      removed but the project is still pinned. Decommission explicitly (delete the
      lock) to opt out.
    * no lock → ``None`` (a genuine non-stack project — byte-identical, no gate).

    Returning ``None`` is the byte-identical path that preserves the "non-stack
    projects are completely unaffected" guarantee (those have no lock file).
    """
    path = stack_lock_path(project_root)
    if not path.exists():
        return None
    return StackLockGate(
        status=LOCK_DRIFT,
        red=True,
        reasons=(
            "a committed stack.lock exists but the project no longer declares a "
            "`stack:` block — the stack declaration was removed while the lock "
            "remains. A stack-governed project cannot silently become ungoverned "
            "(anti-false-green). Restore the `stack:` block, or decommission "
            "explicitly by deleting the lock.",
        ),
        lock_path=str(path),
    )
