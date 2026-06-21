"""``codd.stack.lock`` — pin a resolved stack for deterministic CI (design §2/§決定性).

Auto-detection (package.json has ``next`` → use the Next.js profile) is convenient
for ``local init`` only. In CI the lock is the SOURCE OF TRUTH: the resolved
contract is recomputed and any divergence from the lock (a layer version bump, a
profile edit changing a digest, a different resolved-contract hash) is RED, so a
profile change can never silently alter a project's contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

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
#   * :func:`bootstrap_stack_lock` is the ONLY writer, and it uses EXCLUSIVE
#     create (``open(..., "x")``): it writes only when the lock is ABSENT and is
#     invoked only on a positively-identified project-creation path (greenfield
#     first generation). It refuses to overwrite, so it cannot refresh a drift.
#
# ANTI-GAMING (exit gate 3, the crux): ``verify_lock(contract,
# build_lock(contract))`` is ALWAYS ok by construction — a drift can be MASKED by
# rewriting the lock to match. So the verify path can't detect gaming; the control
# is "who may WRITE a lock, and when". Here: the read-only gate writes nothing
# ever, and bootstrap writes only on exclusive-create in the creation path. A
# drift against a committed lock is RED on every path and is never refreshed by
# either function; auto-repair re-running the gate keeps seeing drift-RED.
# Refreshing a drifted lock requires an explicit out-of-band proof
# (``replace_with_proof``-style; full repair governance is v2.77f).
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
            f"matches the committed lock [{joined}]. This is RED. Rewriting the lock to "
            "match does NOT clear this — the gate is read-only and never refreshes a lock; "
            "a drift requires reverting the contract change or an explicit proof-backed "
            "lock update (replace_with_proof)."
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
    """Write a project's FIRST stack lock (creation path only) — the ONLY writer.

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
