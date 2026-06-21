"""Implement-oracle adapter PROTOCOL + context (Contract Kernel oracle dispatch §3).

This is the seam the per-language oracle tool-semantics will plug into once the
dispatch switch happens (steps 5–7): a ``go-toolchain`` / ``typescript-tsc`` /
``python-composite`` adapter, resolved from the ``LanguageProfile``'s
``implement_oracle`` declaration, that owns the language-specific knowledge the
generic machinery deliberately does NOT have — how to certify the oracle's scope,
how to turn one command's exit/stdout/stderr into normalized findings, and (for the
``adapter`` kind) how to run a wholly in-process composite.

ADDITIVE / UNCONNECTED. Nothing here is wired into the live oracle gate yet. The
gate (:mod:`codd.implement_oracle`) still dispatches on ``profile.language`` and
runs its own per-language executors; the concrete ``oracle_go`` / ``oracle_python``
/ ``oracle_typescript`` adapters land WITH those switch steps. This module ships
ONLY the Protocol, the context object, and the small value object the generic
command-sequence executor (:mod:`codd.languages.oracle_executor`) consumes — so
that executor + its fixtures can be written against a STABLE contract now.

LEAF rule (no import cycle). This module imports ONLY:
  * stdlib (``pathlib``, ``typing``),
  * the oracle value-objects leaf (:mod:`codd.implement_oracle_types`), and
  * the profile model (:mod:`codd.languages.profile`).
It MUST NOT import :mod:`codd.implement_oracle` (the gate), the registry, or the
generic executor — the dependency edge runs gate → executor → adapters → leaf
types, never back up. (Mirrors the runner_report adapter leaf.)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from codd.implement_oracle_types import ImplementOracleFinding, ImplementOracleResult
from codd.languages.profile import (
    CommandSpec,
    ImplementOracleProfileSpec,
    LanguageProfile,
    LayoutSpec,
)


@dataclass(frozen=True)
class OracleContext:
    """Everything an oracle adapter needs to certify scope + normalize + execute.

    It carries the ALREADY-RESOLVED ``LanguageProfile`` (the dispatch hands the
    adapter the profile the resolver produced — the adapter never re-resolves a
    language, so there is no ``resolve("go")`` inside an adapter), plus the project
    root, the layout (for cwd/env placeholder substitution), the oracle declaration
    (``kind`` + ``command``/``steps`` + ``adapter`` id), and the run config.

    Frozen: a context is an immutable description of one oracle run.
    """

    project_root: Path
    #: The resolved layout topology (``module_root`` / ``repo_root`` / ``manifest_root``
    #: / ``source_sets`` / ``test_sets`` / ``package_root``). It is ALWAYS the resolved
    #: :class:`LayoutSpec` (``language_profile.layout``) — Cut A.3 retired the legacy
    #: ``LayoutProfile`` layout-VIEW override: NO adapter reads a legacy
    #: ``source_root``/``package_root`` off this field anymore (the Python adapter
    #: derives its source/package/test roots from ``source_sets``/``package_root`` +
    #: :attr:`package_name`, like the TS/Go adapters read ``source_sets``/``module_root``).
    layout_profile: LayoutSpec
    #: The resolved language contract the dispatch already produced — the adapter
    #: reads ``commands[command_id]`` from it; it NEVER re-resolves a language.
    language_profile: LanguageProfile
    #: The profile's implement-oracle declaration (kind + command/steps + adapter id).
    oracle: ImplementOracleProfileSpec
    #: The run config (``implement.*`` knobs: timeouts, etc.). ``None`` ⇒ defaults.
    config: Mapping[str, Any] | None = None
    #: The harness-owned RESOLVED package name (``resolve_canonical_package_name``:
    #: config override → single top-level package on disk → project-name default).
    #: The gate resolves it (it has ``project_name``); an adapter whose layout
    #: template carries ``{package_name}`` (Python's ``src/{package_name}``)
    #: SUBSTITUTES this — it is NOT the adapter's job to re-resolve a name. ``None``
    #: when the stack's layout has no package-name placeholder (Go/TS): substituting
    #: an absent ``{package_name}`` is a HARD FAIL, never a silent ``src`` fallback
    #: (a wrong package_root is a false-green — Cut A.3 nuance).
    package_name: str | None = None


@dataclass(frozen=True)
class OracleStepObservation:
    """One command step's normalized observation (the adapter's verdict on a step).

    The adapter turns a raw ``(returncode, stdout, stderr)`` into this: the
    language-neutral ``findings`` (each an :class:`ImplementOracleFinding`), the
    editable ``failed_paths`` the findings attribute to, and whether — in the
    adapter's judgement — the step was COHERENT (no incoherence proven).

    The anti-false-green contract the generic executor relies on:

    * ``is_clean`` is True ONLY when the adapter is positively satisfied the step
      proved coherence (e.g. ``tsc`` exited 0 over a certified scope). An adapter
      that cannot decide must return ``is_clean=False`` — never default to clean.
    * A non-clean step with EMPTY ``findings`` is an OPAQUE failure: the command
      did not pass but the adapter could not explain why. The executor turns that
      into an ``environment_build_error`` RED (never a benign pass) — so "exited
      nonzero, no parsed diagnostic" can never read as green.
    """

    is_clean: bool
    findings: tuple[ImplementOracleFinding, ...] = ()
    failed_paths: tuple[str, ...] = ()
    #: The structured per-diagnostic objects (for the scoped-rerun derivation), if
    #: the adapter produced any. Opaque to the executor; threaded into the result.
    diagnostics: tuple[Any, ...] = ()
    #: A short human reason (always populated for a non-clean observation).
    detail: str = ""


@runtime_checkable
class ImplementOracleAdapter(Protocol):
    """The tool-semantics an oracle stack plugs in (Contract Kernel §3).

    Resolved from ``LanguageProfile.implement_oracle.adapter`` via the adapter
    registry under kind ``"implement_oracle"``. The dispatch calls:

    * :meth:`certify_scope` ALWAYS (before any command runs) — an uncertifiable
      scope is an :class:`~codd.implement_oracle_types.OracleScopeError` (a green
      oracle over an unknown/empty scope is the #1 false-green), so this method
      either returns a human-readable certification detail or raises.
    * :meth:`normalize_command_result` for EACH command of a ``command`` /
      ``composite`` oracle — the generic command-sequence executor spawns the
      command (argv/cwd/env from the referenced :class:`CommandSpec`) and hands the
      raw result here for a language-neutral verdict.
    * :meth:`execute` ONLY for a ``kind="adapter"`` oracle (Python's in-process
      compile + import-resolver + collect-only composite) — there is no shell
      command sequence; the adapter runs the whole oracle and returns the result.

    A ``command`` / ``composite`` adapter does NOT need to implement ``execute``; an
    ``adapter``-kind adapter MUST. The dispatch checks for ``execute`` and reds a
    missing one (a declared ``kind="adapter"`` with no executor is an incomplete
    contract, never a silent pass).
    """

    def certify_scope(self, ctx: OracleContext) -> str:
        """Certify the oracle's scope covers what it must, else raise.

        Returns a human-readable certification detail on success; raises
        :class:`~codd.implement_oracle_types.OracleScopeError` when the scope
        cannot be proven to cover source + tests (anti-false-green).
        """
        ...

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
        """Normalize one command's raw result → a language-neutral observation.

        MUST be conservative: only report ``is_clean=True`` when the step
        positively proved coherence. A non-zero exit with no recognizable
        diagnostic should yield ``is_clean=False`` with EMPTY findings, which the
        executor escalates to an opaque ``environment_build_error`` RED.
        """
        ...


@runtime_checkable
class ImplementOracleExecutorAdapter(ImplementOracleAdapter, Protocol):
    """An :class:`ImplementOracleAdapter` that ALSO runs a ``kind="adapter"`` oracle.

    The Python composite (compile + first-party import/symbol resolver +
    ``pytest --collect-only``) is not a shell command sequence — it runs in-process
    and inspects the file lists each layer observed. Such a stack declares
    ``kind="adapter"`` and its adapter implements :meth:`execute`, which the
    dispatch calls instead of the generic command-sequence executor.
    """

    def execute(self, ctx: OracleContext) -> ImplementOracleResult:
        """Run the whole in-process composite oracle and return its result."""
        ...


def adapter_supports_execute(adapter: object) -> bool:
    """True when ``adapter`` provides a callable ``execute`` (the kind=adapter check).

    The dispatch uses this to fail-closed on a ``kind="adapter"`` declaration whose
    registered adapter has no executor: a declared in-process oracle with no
    ``execute`` is an incomplete contract (RED), never a silent pass.
    """
    candidate = getattr(adapter, "execute", None)
    return callable(candidate)


__all__ = [
    "ImplementOracleAdapter",
    "ImplementOracleExecutorAdapter",
    "OracleContext",
    "OracleStepObservation",
    "adapter_supports_execute",
]
