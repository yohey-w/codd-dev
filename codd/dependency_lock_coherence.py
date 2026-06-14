"""Implement-end manifest↔lock coherence finalization (design:
/tmp/gpt_result_dep.txt, GPT-5.5 Pro consult 2026-06-15; verdict (b) primary +
(a) finalization + (c) forbidden).

WHAT
====
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
