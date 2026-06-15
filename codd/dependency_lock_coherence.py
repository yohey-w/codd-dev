"""Manifest↔lock coherence: implement-end finalization + verify-time freshness
barrier (design: /tmp/gpt_result_dep.txt + /tmp/gpt_lock_result.txt, GPT-5.5 Pro
consults 2026-06-15/16; verdict (b) primary + (a) finalization + (c) forbidden,
then (c) dirty-marking + final lock-freshness barrier at position (a) verify-direct).

THE LOCK-FRESHNESS BARRIER (the codex15 sequencing gap this adds)
================================================================
The implement-end finalization below refreshes the lock ONCE, at implement-end.
But the greenfield implement stage can re-write ``package.json`` AFTER that point
— a VB-coverage rerun and an implement-oracle broad rerun both re-invoke the
implementer, which can change deps. Observed (codex15): implement-end
dep-coherence succeeded (03:51), then later reruns re-modified ``package.json``
(06:00), leaving the 03:51 lock STALE (a ufo/path-key transitive omission). The
verify campaign's frozen ``npm ci`` then hard-failed with "Missing from lock
file". The freeze basis was pinned to implement-end, not to the FINAL manifest.

THE FIX (design /tmp/gpt_lock_result.txt): a LOCK-FRESHNESS BARRIER, run verify-
direct (BEFORE any frozen install in verify), that enforces the invariant::

    No frozen install may run unless the lock is fresh for the current manifest set.

It DIRTY-MARKS by a content DIGEST (not mtime): on every entry it computes the
current manifest digest (root manifest + workspace manifests + ``.npmrc`` +
package-manager version + the harness-owned dependency profile) and compares it to
the ``last_frozen_manifest_digest`` recorded at the last freeze. If they DIFFER it
re-runs the same barrier (reconcile harness-owned deps → refresh the lock →
validate with a frozen install → record the digest), with a completeness FALLBACK
(``npm install`` re-resolve) when ``--package-lock-only`` leaves the lock still
incoherent. If they MATCH it is a NO-OP (no wasted refresh — verify just runs its
``npm ci``). The freeze BASIS thus moves from implement-end to the FINAL manifest,
which preserves reproducibility (verify stays a FROZEN install — it is never the
place a lock is repaired) while making the codex15 rerun-modified manifest pass.

WHAT (implement-end finalization)
=================================
After the greenfield IMPLEMENT stage (the SUT has finished authoring source,
tests, AND ``package.json``) and BEFORE verify, this module:

  1. RECONCILES the harness-owned test-toolchain dependency versions in the SUT's
     manifest back to the profile's pins (verdict (b): vitest/typescript/
     @types/node are the VERIFIER's tooling, not the app's business deps — the
     harness recovers its OWN property; app/domain deps are never touched).
  2. REFRESHES the lock deterministically to match the reconciled manifest
     (verdict (a): ``npm install --package-lock-only`` updates ONLY
     ``package-lock.json``, no frozen check, no SUT feedback loop — a harness
     FINALIZATION).
  3. optionally MATERIALIZES node_modules with a FROZEN install (``npm ci``) so a
     same-process implement-oracle typecheck has its deps against the now-coherent
     lock.

WHY (the bug it closes)
=======================
verify's install preflight is a FROZEN install (``npm ci`` — see
``codd/repair/verify_runner.py`` + ``codd.project_types.node_install_command``).
That is CORRECT and stays (verdict (c): loosening ``npm ci`` → ``npm install``
would make verify a repair, not verification). The bug is that the SUT writes
``package.json`` with an OLD toolchain dep (``"vitest": "^1.6.0"``) while the
scaffold/gate install already produced a lock with the LATEST resolution
(``@vitest/expect@3.2.6``); ``npm ci`` then hard-fails on the lock↔manifest
mismatch. This module makes the manifest+lock COHERENT before verify, so the
frozen ``npm ci`` passes HONESTLY.

PROFILE-DRIVEN + GENERAL (design section D)
===========================================
This is the language-independent manifest↔lock coherence contract
(package-lock.json / uv.lock / poetry.lock / Cargo.lock / go.sum). The commands +
the reconcile RULE come from
:class:`codd.project_types.ToolchainDependencyProfile` on the stack's
:class:`~codd.project_types.LayoutProfile`. TS/npm is implemented now; Python/
Rust/Go are added as a profile entry + a reconcile-adapter entry here — never a
core edit. A stack whose profile declares ``toolchain_dependencies=None`` (Python
today) makes the whole finalization a strict NO-OP.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codd.project_types import (
    LayoutProfile,
    ToolchainDependency,
    ToolchainDependencyProfile,
    resolve_layout_profile,
)


__all__ = [
    "DependencyLockResult",
    "LockFreshnessResult",
    "compute_manifest_digest",
    "ensure_lock_freshness_barrier",
    "finalize_dependency_lock_coherence",
    "reconcile_manifest_toolchain_deps",
    "resolve_toolchain_profile",
]


#: Lock refresh + materialize can pull a tree on a cold cache. Bounded; the same
#: magnitude verify's preflight + the implement-oracle install use (override via
#: ``implement.lock_refresh_timeout_seconds``).
DEFAULT_LOCK_REFRESH_TIMEOUT_SECONDS = 900.0


@dataclass
class DependencyLockResult:
    """Outcome of the implement-end manifest↔lock finalization.

    ``applied`` is True when the finalization actually ran for a stack that has a
    toolchain profile (even if no dep needed changing — the lock was still
    refreshed). ``skipped`` is True for a NO-OP (no toolchain profile, opted out,
    or no manifest present). ``ok`` is False ONLY on a hard failure (a lock
    refresh that exited non-zero / timed out) — an honest ``environment_build_error``
    the caller surfaces.
    """

    ok: bool = True
    applied: bool = False
    skipped: bool = False
    reconciled: dict[str, tuple[str | None, str]] = field(default_factory=dict)
    lock_refresh_command: str | None = None
    materialize_command: str | None = None
    detail: str = ""

    @property
    def reconciled_count(self) -> int:
        return len(self.reconciled)


# ── opt-out + timeout config ─────────────────────────────────


def _opt_out(config: Mapping[str, Any] | None) -> bool:
    """``implement.dependency_lock_coherence: false`` — the explicit opt-out.

    Default OFF (the finalization runs). Opting out re-opens the npm-ci-vs-lock
    false-fail the contract closes, so it is never the default and never silent.
    """
    section = (config or {}).get("implement") if isinstance(config, Mapping) else None
    if isinstance(section, Mapping) and "dependency_lock_coherence" in section:
        return section["dependency_lock_coherence"] is False
    return False


def _timeout_seconds(config: Mapping[str, Any] | None) -> float:
    section = (config or {}).get("implement") if isinstance(config, Mapping) else None
    if isinstance(section, Mapping):
        raw = section.get("lock_refresh_timeout_seconds")
        try:
            value = float(raw)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return DEFAULT_LOCK_REFRESH_TIMEOUT_SECONDS


# ── resolution ───────────────────────────────────────────────


def resolve_toolchain_profile(
    project_root: Path,
    *,
    language: str | None,
    project_name: str | None,
    source_dirs: Any = None,
    test_dirs: Any = None,
    config: Mapping[str, Any] | None = None,
    profile: LayoutProfile | None = None,
) -> tuple[LayoutProfile, ToolchainDependencyProfile] | None:
    """Resolve the (layout, toolchain) profiles for a stack, or ``None`` (NO-OP).

    ``None`` when: the gate is opted out, the stack has no layout profile, or the
    profile declares no ``toolchain_dependencies`` (Python today). The caller
    treats ``None`` as "this stack has no manifest↔lock contract — skip silently".
    """
    if _opt_out(config):
        return None
    if profile is None:
        profile = resolve_layout_profile(
            language=language,
            project_name=project_name,
            source_dirs=source_dirs,
            test_dirs=test_dirs,
            config=config,
            project_root=project_root,
        )
    if profile is None or profile.toolchain_dependencies is None:
        return None
    return profile, profile.toolchain_dependencies


# ── manifest reconciliation (npm adapter; the per-ecosystem seam) ──


def reconcile_manifest_toolchain_deps(
    project_root: Path,
    toolchain: ToolchainDependencyProfile,
) -> dict[str, tuple[str | None, str]]:
    """Reconcile the SUT manifest's HARNESS-OWNED toolchain dep versions in place.

    Dispatches on the manifest filename (the per-ecosystem seam). TS/npm
    (``package.json``) is implemented now: every dep in ``toolchain.deps`` is
    written into the dev/prod-deps section with EXACTLY the profile's version
    spec — overriding a SUT-authored version (the incoherence we recover from) and
    ADDING any the SUT omitted (the scaffold's scripts need them). The SUT's app/
    domain deps are NEVER in ``toolchain.deps`` and are left byte-for-byte.

    Returns ``{name: (old_spec_or_None, new_spec)}`` for every dep that CHANGED
    (added or overridden); an empty dict means the manifest was already coherent.
    A manifest that does not exist, or whose filename has no adapter, is a no-op
    (returns ``{}``) — the caller still refreshes the lock so a stale lock from a
    prior install is reconciled to whatever the manifest actually declares.
    """
    manifest = project_root / toolchain.manifest_filename
    if not manifest.is_file():
        return {}
    if toolchain.manifest_filename == "package.json":
        return _reconcile_package_json(manifest, toolchain)
    # No adapter for this manifest format yet (pyproject.toml / Cargo.toml are
    # DEFERRED): do not guess an edit for a manifest we cannot reason about.
    return {}


def _reconcile_package_json(
    manifest: Path,
    toolchain: ToolchainDependencyProfile,
) -> dict[str, tuple[str | None, str]]:
    """npm adapter: force each harness-owned toolchain dep to the profile version.

    Reads ``package.json`` as JSON (a non-JSON/unreadable manifest is left
    untouched — the verify honesty/parse gates are the backstop). Each toolchain
    dep is written to ``devDependencies`` (``dev=True``) or ``dependencies``,
    de-duplicating: if the SUT placed the same dep in the OTHER section, that
    stale entry is removed so the lock cannot resolve two ranges for one name.
    Re-serializes with 2-space indent + trailing newline (matching the scaffold's
    own ``package.json`` writes), preserving every other field and key order.
    """
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}

    changed: dict[str, tuple[str | None, str]] = {}
    for dep in toolchain.deps:
        old = _current_dep_spec(payload, dep.name)
        if _apply_dep(payload, dep):
            changed[dep.name] = (old, dep.version)

    if changed:
        manifest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return changed


_DEP_SECTIONS = ("dependencies", "devDependencies")


def _current_dep_spec(payload: dict[str, Any], name: str) -> str | None:
    """The version spec the manifest currently declares for ``name`` (any section)."""
    for section in _DEP_SECTIONS:
        block = payload.get(section)
        if isinstance(block, dict) and name in block:
            return str(block.get(name))
    return None


def _apply_dep(payload: dict[str, Any], dep: ToolchainDependency) -> bool:
    """Write ``dep`` at the profile's version into the correct section.

    Returns True when the manifest CHANGED (the dep was absent, was at a different
    version, or lived in the wrong section). Removes a same-named entry from the
    other section so one toolchain dep never has two competing ranges.
    """
    target_section = "devDependencies" if dep.dev else "dependencies"
    other_section = "dependencies" if dep.dev else "devDependencies"

    changed = False

    # Drop a stale entry in the WRONG section (a SUT that put vitest in
    # dependencies, say) so the refreshed lock resolves a single range.
    other = payload.get(other_section)
    if isinstance(other, dict) and dep.name in other:
        del other[dep.name]
        if not other:
            del payload[other_section]
        changed = True

    block = payload.get(target_section)
    if not isinstance(block, dict):
        block = {}
        payload[target_section] = block
    if block.get(dep.name) != dep.version:
        block[dep.name] = dep.version
        changed = True
    return changed


# ── lock refresh + materialization (deterministic finalization) ──


def _run(command: str, project_root: Path, timeout: float) -> subprocess.CompletedProcess[str] | None:
    """Run a finalization command from the project root; ``None`` on timeout."""
    try:
        return subprocess.run(
            command,
            shell=True,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None


def _output_tail(stdout: str | None, stderr: str | None, limit: int = 4000) -> str:
    combined = "\n".join(part.strip() for part in (stdout, stderr) if part and part.strip())
    if len(combined) <= limit:
        return combined
    return f"... (truncated) ...\n{combined[-limit:]}"


# ── public entry: the stage-level finalization ──


def finalize_dependency_lock_coherence(
    project_root: Path | str,
    *,
    language: str | None,
    project_name: str | None,
    source_dirs: Any = None,
    test_dirs: Any = None,
    config: Mapping[str, Any] | None = None,
    echo: Callable[[str], None] = print,
    profile: LayoutProfile | None = None,
) -> DependencyLockResult:
    """Reconcile harness-owned toolchain deps + refresh the lock, at implement-end.

    Sequence (TS/npm; profile-driven so other ecosystems slot in):

      1. Resolve the stack's toolchain profile. None declared → a NO-OP
         (``skipped=True``) so Python/bash are untouched.
      2. RECONCILE the manifest's harness-owned toolchain dep versions to the
         profile (verdict (b)). App/domain deps are never touched.
      3. REFRESH the lock to match the reconciled manifest with the profile's
         deterministic ``lock_refresh_command`` (``npm install
         --package-lock-only`` — verdict (a)). A non-zero/timeout is an honest
         ``environment_build_error`` → ``ok=False``.
      4. optionally MATERIALIZE node_modules with the FROZEN
         ``materialize_command`` (``npm ci``) so a same-process implement-oracle
         typecheck has its deps against the now-coherent lock. A materialize
         failure here is the SAME honest failure verify would report (the lock is
         coherent, so this should succeed); surfaced as ``ok=False``.

    verify's OWN install stays a FROZEN ``npm ci`` (verdict (c)) — this function
    never touches that path; it only makes the lock MATCH first.
    """
    root = Path(project_root)
    resolved = resolve_toolchain_profile(
        root,
        language=language,
        project_name=project_name,
        source_dirs=source_dirs,
        test_dirs=test_dirs,
        config=config,
        profile=profile,
    )
    if resolved is None:
        return DependencyLockResult(
            ok=True,
            skipped=True,
            detail=f"no manifest↔lock toolchain contract for language {language!r} (skipped)",
        )
    _profile, toolchain = resolved

    manifest = root / toolchain.manifest_filename
    if not manifest.is_file():
        # No manifest authored (an unusual node build, or implement produced none):
        # nothing to reconcile and no lock to anchor — skip, never fail.
        return DependencyLockResult(
            ok=True,
            skipped=True,
            detail=f"no {toolchain.manifest_filename} present; manifest↔lock finalization skipped",
        )

    # 2. Reconcile harness-owned toolchain dep versions (verdict (b)).
    reconciled = reconcile_manifest_toolchain_deps(root, toolchain)
    if reconciled:
        summary = ", ".join(
            f"{name}: {old or '<absent>'} → {new}" for name, (old, new) in sorted(reconciled.items())
        )
        echo(
            f"[greenfield] dependency-lock: reconciled {len(reconciled)} harness-owned "
            f"toolchain dep(s) in {toolchain.manifest_filename} ({summary})"
        )
    else:
        echo(
            f"[greenfield] dependency-lock: toolchain deps in {toolchain.manifest_filename} "
            "already at profile versions"
        )

    timeout = _timeout_seconds(config)

    # 3. Refresh the lock to match the reconciled manifest (verdict (a)).
    refresh_cmd = toolchain.lock_refresh_command
    completed = _run(refresh_cmd, root, timeout)
    if completed is None:
        detail = f"lock refresh timed out after {timeout:g}s: {refresh_cmd}"
        echo(f"[greenfield] dependency-lock: {detail}")
        return DependencyLockResult(
            ok=False,
            applied=True,
            reconciled=reconciled,
            lock_refresh_command=refresh_cmd,
            detail=detail,
        )
    if completed.returncode != 0:
        tail = _output_tail(completed.stdout, completed.stderr)
        detail = f"lock refresh failed (exit {completed.returncode}): {refresh_cmd}\n{tail}".rstrip()
        echo(f"[greenfield] dependency-lock: {detail}")
        return DependencyLockResult(
            ok=False,
            applied=True,
            reconciled=reconciled,
            lock_refresh_command=refresh_cmd,
            detail=detail,
        )
    echo(f"[greenfield] dependency-lock: lock refreshed to match manifest ({refresh_cmd})")

    # 4. Optionally materialize node_modules with a FROZEN install so a
    # same-process implement-oracle typecheck has its deps (verdict: keep frozen).
    materialize_cmd = toolchain.materialize_command
    if materialize_cmd:
        completed = _run(materialize_cmd, root, timeout)
        if completed is None:
            detail = f"dependency materialize timed out after {timeout:g}s: {materialize_cmd}"
            echo(f"[greenfield] dependency-lock: {detail}")
            return DependencyLockResult(
                ok=False,
                applied=True,
                reconciled=reconciled,
                lock_refresh_command=refresh_cmd,
                materialize_command=materialize_cmd,
                detail=detail,
            )
        if completed.returncode != 0:
            tail = _output_tail(completed.stdout, completed.stderr)
            detail = (
                f"dependency materialize failed (exit {completed.returncode}): "
                f"{materialize_cmd}\n{tail}"
            ).rstrip()
            echo(f"[greenfield] dependency-lock: {detail}")
            return DependencyLockResult(
                ok=False,
                applied=True,
                reconciled=reconciled,
                lock_refresh_command=refresh_cmd,
                materialize_command=materialize_cmd,
                detail=detail,
            )
        echo(f"[greenfield] dependency-lock: node_modules materialized (frozen: {materialize_cmd})")

    # Record the frozen manifest digest so the verify-time lock-freshness barrier
    # treats an UNCHANGED manifest as a no-op (and only re-freezes when a later
    # implement-stage rerun actually changes the manifest set — the codex15 gap).
    # Best-effort: a digest-record failure never fails the finalization (the
    # barrier degrades to "no recorded digest" ⇒ it re-freezes, which is safe).
    try:
        digest = compute_manifest_digest(root, toolchain)
        _write_frozen_digest(root, digest)
    except Exception as exc:  # noqa: BLE001 — recording is best-effort; never block.
        echo(f"[greenfield] dependency-lock: digest record skipped ({exc})")

    return DependencyLockResult(
        ok=True,
        applied=True,
        reconciled=reconciled,
        lock_refresh_command=refresh_cmd,
        materialize_command=materialize_cmd,
        detail=(
            f"reconciled {len(reconciled)} toolchain dep(s); lock refreshed via {refresh_cmd}"
            + (f"; materialized via {materialize_cmd}" if materialize_cmd else "")
        ),
    )


# ── manifest digest (dirty-marking by CONTENT, not mtime) ──
#
# The barrier's dirty-marking is a CONTENT digest, never an mtime: a same-content
# rewrite (an idempotent rerun) must NOT trigger a needless re-freeze, and a
# workspace-manifest / ``.npmrc`` / package-manager-version / harness-profile
# change MUST trigger one even if the root manifest's mtime is unchanged (GPT
# design /tmp/gpt_lock_result.txt §C: "impl は mtime ではなく digest にすべき").


#: Where the barrier records the digest the lock was last frozen for. A harness
#: artifact under ``.codd/`` (never the SUT's), sibling to the verify report.
_LOCK_STATE_RELPATH = ".codd/dependency_lock_state.json"
_DIGEST_VERSION = "manifest-digest/v1"


def _read_text_or_empty(path: Path) -> str:
    """File content as text, or ``""`` when absent/unreadable (a missing optional
    config — ``.npmrc`` — contributes an empty component, so adding it later
    changes the digest)."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _iter_workspace_manifests(
    project_root: Path, toolchain: ToolchainDependencyProfile
) -> list[Path]:
    """Resolve the stack's WORKSPACE manifests (deterministic order).

    Each ``workspace_manifest_globs`` entry is globbed from the project root; the
    results are de-duplicated and SORTED so the digest is order-stable across
    filesystems. The root manifest is handled separately by the caller, so a glob
    that also matches it is filtered out here (no double counting).
    """
    root_manifest = (project_root / toolchain.manifest_filename).resolve()
    seen: dict[str, Path] = {}
    for pattern in toolchain.workspace_manifest_globs:
        for match in project_root.glob(pattern):
            if not match.is_file():
                continue
            resolved = match.resolve()
            if resolved == root_manifest:
                continue
            seen[resolved.as_posix()] = match
    return [seen[key] for key in sorted(seen)]


def _package_manager_version(
    project_root: Path, toolchain: ToolchainDependencyProfile, timeout: float
) -> str:
    """Best-effort package-manager version string for the digest.

    A manager UPGRADE can change lock format / resolution, so it belongs in the
    digest. Best-effort: a missing/failing probe contributes ``""`` (its absence
    is itself stable) rather than failing the barrier — the lockfile-content
    component still dominates correctness."""
    command = toolchain.package_manager_version_command
    if not command:
        return ""
    completed = _run(command, project_root, timeout)
    if completed is None or completed.returncode != 0:
        return ""
    return (completed.stdout or "").strip()


def compute_manifest_digest(
    project_root: Path | str,
    toolchain: ToolchainDependencyProfile,
    *,
    timeout: float = 30.0,
) -> str:
    """Content digest of the stack's full MANIFEST SET (the barrier's dirty-mark).

    Folds, in a deterministic order:

      1. the harness-owned dependency PROFILE (its ``to_dict`` — so a profile
         version bump, or a change to the toolchain dep set / commands, re-freezes);
      2. the ROOT manifest content (``package.json``);
      3. every WORKSPACE manifest content, in sorted path order
         (``packages/*/package.json`` …);
      4. every dependency-resolution CONFIG file content, in declared order
         (``.npmrc`` — flags that change the resolved tree);
      5. the package-manager VERSION string (``npm --version``).

    Each component is length-prefixed + path-tagged so concatenation is
    unambiguous (no boundary-collision between two files). Returns a hex SHA-256.
    The same inputs always yield the same digest (model-independent, reproducible);
    ``timeout`` bounds only the version probe.
    """
    root = Path(project_root)
    hasher = hashlib.sha256()

    def _feed(tag: str, content: str) -> None:
        encoded = content.encode("utf-8", errors="replace")
        hasher.update(tag.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(str(len(encoded)).encode("ascii"))
        hasher.update(b"\x00")
        hasher.update(encoded)
        hasher.update(b"\x00")

    _feed("version", _DIGEST_VERSION)
    # (1) harness-owned profile — deterministic JSON so key order never drifts.
    _feed("profile", json.dumps(toolchain.to_dict(), sort_keys=True, ensure_ascii=False))
    # (2) root manifest.
    _feed(
        f"manifest:{toolchain.manifest_filename}",
        _read_text_or_empty(root / toolchain.manifest_filename),
    )
    # (3) workspace manifests (sorted, relative-tagged).
    for manifest in _iter_workspace_manifests(root, toolchain):
        try:
            rel = manifest.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel = manifest.name
        _feed(f"workspace:{rel}", _read_text_or_empty(manifest))
    # (4) dependency-resolution config files (declared order).
    for config_name in toolchain.config_filenames:
        _feed(f"config:{config_name}", _read_text_or_empty(root / config_name))
    # (5) package-manager version.
    _feed("pm-version", _package_manager_version(root, toolchain, timeout))

    return hasher.hexdigest()


def _read_frozen_digest(project_root: Path) -> str | None:
    """The digest the lock was last frozen for, or ``None`` (never frozen / unreadable).

    ``None`` makes the barrier conservatively re-freeze (a missing record is
    treated as "not known fresh"), which is always safe."""
    path = project_root / _LOCK_STATE_RELPATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("last_frozen_manifest_digest")
    return str(value) if isinstance(value, str) and value else None


def _write_frozen_digest(project_root: Path, digest: str) -> None:
    """Record ``digest`` as the manifest set the lock is now frozen for."""
    path = project_root / _LOCK_STATE_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"version": _DIGEST_VERSION, "last_frozen_manifest_digest": digest},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


# ── the lock-freshness barrier (verify-direct; the new invariant) ──


@dataclass
class LockFreshnessResult:
    """Outcome of the verify-time lock-freshness barrier.

    ``ran`` is True when the barrier did work (the manifest set changed since the
    last freeze, so it reconciled + refreshed + re-froze). ``skipped`` is True
    when the digest matched (no-op — verify's own frozen install reproduces the
    fresh lock, so no refresh was needed) OR the stack has no toolchain contract.
    ``ok`` is False ONLY on a hard failure (the lock could not be made to satisfy
    a frozen install even after the completeness fallback) — an honest
    ``environment_build_error`` the caller surfaces. ``used_fallback`` records that
    the completeness fallback (full re-resolve) was needed.
    """

    ok: bool = True
    ran: bool = False
    skipped: bool = False
    used_fallback: bool = False
    reconciled: dict[str, tuple[str | None, str]] = field(default_factory=dict)
    digest: str | None = None
    detail: str = ""


def _frozen_install_validates(
    project_root: Path,
    toolchain: ToolchainDependencyProfile,
    timeout: float,
) -> tuple[bool, str]:
    """Whether the FROZEN install reproduces the current lock+manifest.

    Runs ``toolchain.frozen_install_command`` (``npm ci``) — the SAME frozen check
    verify will run — INSIDE the barrier so the lock is proven coherent BEFORE
    verify consumes it. Returns ``(ok, output_tail)``; a timeout is a non-ok with a
    timeout message. This is the barrier's invariant check ("lock fresh for the
    current manifest set"), never a verify-time repair.
    """
    command = toolchain.frozen_install_command
    completed = _run(command, project_root, timeout)
    if completed is None:
        return False, f"frozen install timed out after {timeout:g}s: {command}"
    if completed.returncode != 0:
        return False, _output_tail(completed.stdout, completed.stderr)
    return True, ""


def ensure_lock_freshness_barrier(
    project_root: Path | str,
    *,
    language: str | None,
    project_name: str | None,
    source_dirs: Any = None,
    test_dirs: Any = None,
    config: Mapping[str, Any] | None = None,
    echo: Callable[[str], None] = print,
    profile: LayoutProfile | None = None,
) -> LockFreshnessResult:
    """Enforce "no frozen install runs unless the lock is fresh for the manifest set".

    Run VERIFY-DIRECT (before any frozen install in verify). Sequence:

      1. Resolve the toolchain profile. None → strict NO-OP (Python today).
      2. Compute the current manifest digest. If it MATCHES the recorded
         ``last_frozen_manifest_digest`` → NO-OP (``skipped=True``); verify's own
         ``npm ci`` will reproduce the already-fresh lock. (No wasted refresh — the
         design's "rerun が複数回続くと無駄が大きい" guard.)
      3. If it DIFFERS, re-run the barrier:
         a. RECONCILE harness-owned toolchain deps to the profile (same rule as
            implement-end — recovers the verifier's own property; app deps
            untouched).
         b. REFRESH the lock deterministically (``npm install
            --package-lock-only``).
         c. VALIDATE with the FROZEN install (``npm ci``). On failure, run the
            completeness FALLBACK (``npm install`` full re-resolve) then re-validate
            — the ufo/path-key transitive-omission recovery, kept INSIDE the
            barrier (never a verify-time repair).
         d. On success RECORD the new digest. On failure return ``ok=False`` (an
            honest environment failure).

    A strict NO-OP for a stack with no toolchain profile. The frozen install + the
    refresh hold the SAME ``.npmrc``/flags (the profile's single
    ``frozen_install_command``/``lock_refresh_command``), so lock generation and
    npm ci see identical resolution inputs (GPT §D).
    """
    root = Path(project_root)
    resolved = resolve_toolchain_profile(
        root,
        language=language,
        project_name=project_name,
        source_dirs=source_dirs,
        test_dirs=test_dirs,
        config=config,
        profile=profile,
    )
    if resolved is None:
        return LockFreshnessResult(
            ok=True,
            skipped=True,
            detail=f"no manifest↔lock toolchain contract for language {language!r} (barrier skipped)",
        )
    _profile, toolchain = resolved

    manifest = root / toolchain.manifest_filename
    if not manifest.is_file():
        # No manifest to anchor a digest/lock — skip, never fail (mirrors the
        # finalization's no-manifest branch).
        return LockFreshnessResult(
            ok=True,
            skipped=True,
            detail=f"no {toolchain.manifest_filename} present; lock-freshness barrier skipped",
        )

    timeout = _timeout_seconds(config)
    current = compute_manifest_digest(root, toolchain, timeout=min(timeout, 30.0))
    recorded = _read_frozen_digest(root)
    if recorded is not None and recorded == current:
        echo(
            "[greenfield] lock-freshness: manifest set unchanged since last freeze "
            "— lock is fresh (no refresh needed)"
        )
        return LockFreshnessResult(
            ok=True,
            skipped=True,
            digest=current,
            detail="manifest set unchanged since last freeze; frozen install will reproduce the lock",
        )

    # The manifest set changed (a post-implement-end rerun re-wrote it — codex15)
    # or was never frozen → re-run the barrier so verify's frozen install passes.
    echo(
        "[greenfield] lock-freshness: manifest set changed since last freeze "
        f"({'no prior record' if recorded is None else 'digest differs'}) — re-freezing the lock before verify"
    )

    # (a) reconcile harness-owned toolchain dep versions.
    reconciled = reconcile_manifest_toolchain_deps(root, toolchain)
    if reconciled:
        summary = ", ".join(
            f"{name}: {old or '<absent>'} → {new}" for name, (old, new) in sorted(reconciled.items())
        )
        echo(f"[greenfield] lock-freshness: reconciled {len(reconciled)} toolchain dep(s) ({summary})")

    # (b) deterministic lock refresh (--package-lock-only).
    refresh_cmd = toolchain.lock_refresh_command
    completed = _run(refresh_cmd, root, timeout)
    if completed is None:
        detail = f"lock refresh timed out after {timeout:g}s: {refresh_cmd}"
        echo(f"[greenfield] lock-freshness: {detail}")
        return LockFreshnessResult(ok=False, ran=True, reconciled=reconciled, detail=detail)
    if completed.returncode != 0:
        tail = _output_tail(completed.stdout, completed.stderr)
        detail = f"lock refresh failed (exit {completed.returncode}): {refresh_cmd}\n{tail}".rstrip()
        echo(f"[greenfield] lock-freshness: {detail}")
        return LockFreshnessResult(ok=False, ran=True, reconciled=reconciled, detail=detail)
    echo(f"[greenfield] lock-freshness: lock refreshed to match manifest ({refresh_cmd})")

    # (c) validate with the FROZEN install; completeness fallback on incoherence.
    used_fallback = False
    ok, output = _frozen_install_validates(root, toolchain, timeout)
    if not ok:
        fallback_cmd = toolchain.completeness_refresh_command
        if not fallback_cmd:
            detail = (
                f"frozen install ({toolchain.frozen_install_command}) failed after lock refresh "
                f"and no completeness fallback is configured:\n{output}"
            ).rstrip()
            echo(f"[greenfield] lock-freshness: {detail}")
            return LockFreshnessResult(ok=False, ran=True, reconciled=reconciled, detail=detail)
        echo(
            "[greenfield] lock-freshness: frozen install still incoherent after "
            f"--package-lock-only — running completeness fallback ({fallback_cmd})"
        )
        used_fallback = True
        completed = _run(fallback_cmd, root, timeout)
        if completed is None:
            detail = f"completeness fallback timed out after {timeout:g}s: {fallback_cmd}"
            echo(f"[greenfield] lock-freshness: {detail}")
            return LockFreshnessResult(
                ok=False, ran=True, used_fallback=True, reconciled=reconciled, detail=detail
            )
        if completed.returncode != 0:
            tail = _output_tail(completed.stdout, completed.stderr)
            detail = f"completeness fallback failed (exit {completed.returncode}): {fallback_cmd}\n{tail}".rstrip()
            echo(f"[greenfield] lock-freshness: {detail}")
            return LockFreshnessResult(
                ok=False, ran=True, used_fallback=True, reconciled=reconciled, detail=detail
            )
        ok, output = _frozen_install_validates(root, toolchain, timeout)
        if not ok:
            detail = (
                f"frozen install ({toolchain.frozen_install_command}) STILL failed after the "
                f"completeness fallback ({fallback_cmd}) — lock cannot reproduce the manifest:\n{output}"
            ).rstrip()
            echo(f"[greenfield] lock-freshness: {detail}")
            return LockFreshnessResult(
                ok=False, ran=True, used_fallback=True, reconciled=reconciled, detail=detail
            )

    # (d) success — record the digest the lock is now frozen for. The fallback may
    # have rewritten the lock (and thus the manifest set is the SAME, but recompute
    # the digest to be exact about what we froze).
    final_digest = compute_manifest_digest(root, toolchain, timeout=min(timeout, 30.0))
    _write_frozen_digest(root, final_digest)
    echo(
        "[greenfield] lock-freshness: lock is fresh for the current manifest set "
        f"(frozen install validated{'; via completeness fallback' if used_fallback else ''})"
    )
    return LockFreshnessResult(
        ok=True,
        ran=True,
        used_fallback=used_fallback,
        reconciled=reconciled,
        digest=final_digest,
        detail=(
            f"re-froze lock for changed manifest set; reconciled {len(reconciled)} toolchain dep(s)"
            + ("; used completeness fallback" if used_fallback else "")
        ),
    )
