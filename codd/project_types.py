"""Central, extensible registry of CoDD project types and their capabilities.

This module is the single source of truth for "what project types does CoDD
support". Historically the supported set was a hardcoded tuple duplicated across
``required_artifacts_deriver.py``, ``requirement_completeness_auditor.py`` and
``preflight/__init__.py``; unknown configured types silently fell back to
``web`` (wrong: a ``library`` project would be handed web artifacts).

Design goals:

* **Discovery over enumeration.** Supported types are discovered by scanning the
  shipped ``required_artifacts/defaults/*.yaml`` filenames, so dropping a new
  ``<type>.yaml`` registers the type with no core edit.
* **Extensibility without forking.** A project may add its own types by placing
  ``<codd-dir>/required_artifacts_defaults/<name>.yaml`` (project-local override
  dir) or by pointing ``project.type_defaults_dir`` in ``codd.yaml`` at a
  directory of ``<name>.yaml`` profiles. Project-local types are checked first.
* **No silent web fallback.** Unknown configured types resolve to the
  conservative ``generic`` baseline (plus a caller-emitted warning), never web.
* **Capability model.** Each profile may declare a small, orthogonal
  ``capabilities:`` block that the generation pipeline (a later step) consults to
  adapt output (UI vs none, network surface, e2e modality, long-running server).
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from codd.config import load_project_config


GENERIC_PROJECT_TYPE = "generic"
CUSTOM_PROJECT_TYPE = "custom"

# Shipped per-type artifact profiles. Filenames here define the built-in types.
SHIPPED_DEFAULTS_DIR = Path(__file__).parent / "required_artifacts" / "defaults"

# Project-local override directory (relative to a project root). A project can
# register or override a type by dropping ``<type>.yaml`` here.
PROJECT_LOCAL_DEFAULTS_SUBDIR = Path("required_artifacts_defaults")


@dataclass(frozen=True)
class ProjectCapabilities:
    """Orthogonal capability flags a profile may declare for generation.

    Defaults are deliberately conservative — they match the ``generic`` baseline
    so that a profile which omits ``capabilities:`` behaves like a plain,
    non-UI, no-network, CLI-tested, non-server project. The generation pipeline
    consults these to decide whether to emit UI/UX artifacts, route e2e tests,
    derive operations runbooks, etc.
    """

    user_interface: bool = False
    network_surface: str = "none"  # "http" | "none"
    e2e_modality: str = "cli"  # "browser" | "cli" | "device" | "none"
    long_running_service: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_interface": self.user_interface,
            "network_surface": self.network_surface,
            "e2e_modality": self.e2e_modality,
            "long_running_service": self.long_running_service,
        }


def _project_local_defaults_dir(project_root: Path | None) -> Path | None:
    """Resolve a project's local type-defaults directory, if configured/present.

    Precedence:
      1. ``project.type_defaults_dir`` in ``codd.yaml`` (explicit pointer).
      2. ``<project_root>/required_artifacts_defaults/`` (convention).
    """

    if project_root is None:
        return None
    root = Path(project_root)
    try:
        config = load_project_config(root)
    except (FileNotFoundError, ValueError):
        config = {}

    project_section = config.get("project", {})
    if isinstance(project_section, dict):
        configured = project_section.get("type_defaults_dir")
        if configured:
            configured_path = Path(str(configured))
            if not configured_path.is_absolute():
                configured_path = root / configured_path
            return configured_path

    convention = root / PROJECT_LOCAL_DEFAULTS_SUBDIR
    return convention


def _discover_types_in_dir(directory: Path | None) -> set[str]:
    if directory is None or not directory.is_dir():
        return set()
    return {path.stem for path in directory.glob("*.yaml") if path.stem}


def supported_project_types(project_root: Path | None = None) -> list[str]:
    """Return the sorted set of known project types.

    Discovered from shipped ``required_artifacts/defaults/*.yaml`` plus any
    project-local override profiles. ``generic`` is always included. ``custom``
    is a reserved sentinel (empty artifacts) and is intentionally NOT listed as a
    profile here; callers handle it explicitly where supported.
    """

    types: set[str] = _discover_types_in_dir(SHIPPED_DEFAULTS_DIR)
    types |= _discover_types_in_dir(_project_local_defaults_dir(project_root))
    types.add(GENERIC_PROJECT_TYPE)
    return sorted(types)


def is_known_project_type(project_type: str | None, project_root: Path | None = None) -> bool:
    if not project_type:
        return False
    return project_type.lower() in set(supported_project_types(project_root))


def resolve_project_type(
    configured: str | None,
    detected: str | None = None,
    project_root: Path | None = None,
) -> tuple[str, str]:
    """Resolve the effective project type and a human-readable reason.

    Precedence:
      1. explicit ``configured`` when it is a known type → use it.
      2. ``configured`` set but unknown → ``generic`` + reason naming the unknown
         type (caller is expected to warn). NEVER falls back to ``web``.
      3. ``detected`` when known → use it.
      4. otherwise → ``generic``.

    Note: ``custom`` is passed through as-is (callers treat it as the
    empty-artifacts sentinel); it is not coerced to generic.
    """

    known = set(supported_project_types(project_root))
    configured_norm = (configured or "").strip().lower()
    detected_norm = (detected or "").strip().lower()

    if configured_norm == CUSTOM_PROJECT_TYPE:
        return CUSTOM_PROJECT_TYPE, "configured project_type 'custom' (empty-artifacts sentinel)"

    if configured_norm and configured_norm in known:
        return configured_norm, f"configured project_type '{configured_norm}'"

    if configured_norm and configured_norm not in known:
        reason = (
            f"project_type '{configured_norm}' is not a known profile; "
            f"using '{GENERIC_PROJECT_TYPE}' baseline. Add "
            f"codd/required_artifacts/defaults/{configured_norm}.yaml or a "
            f"project-local override to define it."
        )
        return GENERIC_PROJECT_TYPE, reason

    if detected_norm and detected_norm in known:
        return detected_norm, f"detected project_type '{detected_norm}'"

    return GENERIC_PROJECT_TYPE, f"no known project_type configured or detected; using '{GENERIC_PROJECT_TYPE}' baseline"


def _profile_path(project_type: str, project_root: Path | None) -> Path | None:
    """Return the profile YAML path for a type (project-local first, then shipped)."""

    filename = f"{project_type}.yaml"
    local_dir = _project_local_defaults_dir(project_root)
    if local_dir is not None:
        local_path = local_dir / filename
        if local_path.is_file():
            return local_path
    shipped = SHIPPED_DEFAULTS_DIR / filename
    if shipped.is_file():
        return shipped
    return None


def load_capabilities(
    project_type: str,
    project_root: Path | None = None,
) -> ProjectCapabilities:
    """Load the ``capabilities:`` block for a type with conservative defaults.

    Missing profile or missing/invalid keys fall back to the conservative
    generic capability values defined on ``ProjectCapabilities``.
    """

    path = _profile_path((project_type or "").strip().lower(), project_root)
    if path is None:
        return ProjectCapabilities()

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return ProjectCapabilities()

    block = payload.get("capabilities") if isinstance(payload, dict) else None
    if not isinstance(block, dict):
        return ProjectCapabilities()

    defaults = ProjectCapabilities()
    return ProjectCapabilities(
        user_interface=_as_bool(block.get("user_interface"), defaults.user_interface),
        network_surface=_as_choice(
            block.get("network_surface"), {"http", "none"}, defaults.network_surface
        ),
        e2e_modality=_as_choice(
            block.get("e2e_modality"),
            {"browser", "cli", "device", "none"},
            defaults.e2e_modality,
        ),
        long_running_service=_as_bool(
            block.get("long_running_service"), defaults.long_running_service
        ),
    )


def _as_bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    return fallback


def _as_choice(value: Any, allowed: set[str], fallback: str) -> str:
    if isinstance(value, str) and value.strip().lower() in allowed:
        return value.strip().lower()
    return fallback


def effective_e2e_modality(
    capabilities: ProjectCapabilities, profile: LayoutProfile | None
) -> str:
    """The project's e2e modality after applying the excluded-surface downgrade.

    ``ProjectCapabilities.e2e_modality`` is a STATIC per-type capability (default
    ``"cli"``). It never consults the OPTIONAL deliverable surfaces a project
    excluded. But a modality is only realizable while the surface that BACKS it
    exists: the ``"cli"`` modality's subprocess e2e (``python -m <pkg>``) requires
    the runnable entry point. When a project EXCLUDES the backing surface (a pure
    library with no ``__main__``) that e2e cannot pass, so the modality is
    downgraded to ``"none"`` (which still emits in-process unit + integration
    behavioral tests — no coverage is suppressed).

    The downgrade is a pure DATA join over the profile — a ``SurfaceSpec`` in
    ``profile.optional_surfaces`` whose ``backs_e2e_modality`` equals the loaded
    modality AND whose id is in ``profile.excluded_surface_ids`` — with NO
    language/framework literal (``"cli"``/``"none"`` are the existing e2e_modality
    vocabulary, not stack names). FAIL-SAFE: a ``None`` profile, a surface that
    declares no backing (``backs_e2e_modality is None`` never equals a real
    modality), a non-matching modality, or ANY error returns the loaded modality
    unchanged (legacy behavior).
    """
    modality = capabilities.e2e_modality
    try:
        if profile is None:
            return modality
        excluded = profile.excluded_surface_ids
        for surface in profile.optional_surfaces:
            if surface.backs_e2e_modality == modality and surface.id in excluded:
                return "none"
    except Exception:  # noqa: BLE001 — an undecidable profile keeps the legacy modality.
        return modality
    return modality


# ═══════════════════════════════════════════════════════════
# Stack layout profiles (the harness OWNS repo topology / module resolution)
# ═══════════════════════════════════════════════════════════
#
# Model-independence principle (A-core): the harness REMOVES the degrees of
# freedom the model should NOT vary. The repository TOPOLOGY and the
# MODULE-RESOLUTION contract are harness-owned; the model only fills the
# CONTENTS (domain logic, behavior, test behavior, messages). A greenfield build
# that lets the model invent project structure produces source + tests that
# DISAGREE on package/import context (observed 2026-06 cross-vendor: source uses
# package-relative ``from .todo_store import X`` while tests flat-import
# ``import todo_store`` — a real import failure masked only by an accidental
# ``pythonpath="."``, an environment-dependent FALSE GREEN).
#
# A ``LayoutProfile`` is the single, stack-specific declaration of that
# topology: the package name (derived deterministically from the project name),
# the source/package/test roots (derived from ``scan.*_dirs``), the test runner,
# the install mode, and the test IMPORT POLICY the coherence gate enforces. One
# profile per stack, centralized here in the registry — Python is implemented
# now; node/go/rust are future profiles added as one entry each, with NO
# scattered "src"/"tests"/"<package>" literals anywhere in the pipeline.

_VALID_TEST_IMPORT_POLICIES = {"package_absolute", "flat", "relative"}


# ═══════════════════════════════════════════════════════════
# Contract-Kernel language resolution seam (v2.71: de-literalize the dispatch)
# ═══════════════════════════════════════════════════════════
#
# The Contract Kernel rule (Cut Condition A): the core branches on a RESOLVED
# language contract + registered adapter capability, NEVER on a language-NAME
# literal. The functions below resolve a runtime language VALUE (a string the
# caller already holds — allowed) to the declarative ``LanguageProfile`` (loaded
# from ``codd/languages/profiles/*.yaml``) so layout/scaffold/test-block
# decisions read profile DATA instead of ``self.language == "python"`` dispatch.
#
# Anti-false-green: a declared-but-UNKNOWN language degrades to ``None`` (the
# conservative non-stack behaviour — no scaffolder engages, no stack paths are
# declared), mirroring ``codd.repair.verify_runner._declared_language_profile``.
# It is NEVER coerced to a wrong-layout default (that would be a silent green).


def _resolve_kernel_language_profile(language: str | None) -> Any:
    """Resolve a runtime language value to its declarative ``LanguageProfile``.

    Returns the loaded profile (matched by id OR alias, case-insensitively
    through :data:`codd.languages.registry.default_registry`) or ``None`` when
    the language is blank/unknown. The language string is a RUNTIME VALUE the
    caller already holds (not a hardcoded literal), so this is the language-free
    resolution seam — it replaces the removed ``self.language == "<name>"`` /
    language-name-keyed builder-dict dispatch. Import is lazy so the language
    package is not pulled in at ``project_types`` import time / on the hot path.
    """
    if not language or not str(language).strip():
        return None
    try:
        from codd.languages.registry import UnknownLanguageError, default_registry
    except Exception:  # noqa: BLE001 — registry optional; degrade (never crash).
        return None
    try:
        return default_registry.resolve(str(language))
    except UnknownLanguageError:
        return None
    except Exception:  # noqa: BLE001 — a malformed profile is the loader's gate; degrade here.
        return None


# ── Legacy project-types bridge (the byte-identical, language-name-free seam) ──
#
# The LEGACY ``codd.project_types`` builders / scaffolders / test-runner ensurers
# still carry HARNESS POLICY (runner / install_mode / oracle / toolchain / verify
# campaign) the declarative ``LanguageProfile`` does NOT yet model — full policy
# externalization is the v3.0.0 gate. THIS increment removes the language-NAME
# DISPATCH only. The seam: a profile declares a ``legacy_project_types:`` block
# (preserved in ``profile.extra``) with
#   * ``accepted_names``       — the EXACT runtime names the legacy path accepts
#     for this stack (so support stays byte-identical: ``python`` accepts only
#     ``python``, NOT every registry alias like ``py``/``python3``; ``typescript``
#     accepts only ``typescript``+``node``, NOT ``ts``/``js``/``javascript``).
#   * realizer ids            — HARNESS-POLICY capability names (``layout_builder``
#     / ``scaffolder`` / ``test_runner_ensurer``) the core maps to the concrete
#     legacy function via the registries at the bottom of this module. The keys are
#     policy capability ids, NEVER language names — so the core stays language-free.
#
# A profile WITHOUT this block (Go) is not bridged to the legacy path at all (Go's
# coherence is owned by the contract/implement-oracle path). Anti-false-green: an
# unknown / unaccepted name resolves to NO realizer (the conservative degradation —
# the caller stays on its no-profile path, never a wrong builder writing a wrong
# layout). Language names live in the PROFILE YAML (an allowed zone), never here.

_LEGACY_BRIDGE_KEY = "legacy_project_types"


def _legacy_bridge_block(language: str | None) -> Mapping[str, Any] | None:
    """The profile's ``legacy_project_types`` block IFF ``language`` is accepted.

    Resolves the runtime ``language`` to its :class:`LanguageProfile`, then returns
    the declared ``legacy_project_types`` mapping ONLY when ``language`` (case-
    insensitively) is in that block's ``accepted_names``. ``None`` otherwise — a
    blank/unknown language, an alias the legacy path historically did NOT accept,
    or a profile that declares no bridge (Go). This is the byte-identical support
    gate: it restores the legacy dict's EXACT accepted-name set without a
    language-name literal in core code.
    """
    profile = _resolve_kernel_language_profile(language)
    extra = getattr(profile, "extra", None) if profile is not None else None
    block = extra.get(_LEGACY_BRIDGE_KEY) if isinstance(extra, Mapping) else None
    if not isinstance(block, Mapping):
        return None
    accepted = block.get("accepted_names")
    accepted_set = {
        str(n).strip().lower() for n in (accepted or ()) if str(n).strip()
    } if isinstance(accepted, (list, tuple)) else set()
    if (language or "").strip().lower() not in accepted_set:
        return None
    return block


def _legacy_realizer_id(language: str | None, field: str) -> str | None:
    """The realizer capability id for ``field`` from an ACCEPTED legacy bridge.

    ``field`` is one of ``layout_builder`` / ``scaffolder`` / ``test_runner_ensurer``.
    ``None`` when the language is not bridged/accepted or the field is undeclared —
    the conservative degradation (no legacy realizer engages).
    """
    block = _legacy_bridge_block(language)
    if block is None:
        return None
    value = block.get(field)
    return str(value) if value else None


# ═══════════════════════════════════════════════════════════
# Implement-time native-oracle spec (the "first head" of the Artifact Contract
# Graph → Native Oracle Adapter: see memory/project_codd_language_generality_acg)
# ═══════════════════════════════════════════════════════════
#
# A compiler-class stack (TS=tsc, later Go=go build, Rust=cargo check) can PROVE
# artifact-to-artifact symbol/module coherence statically, BEFORE running a line
# of code. The greenfield IMPLEMENT stage is the right place to exercise that
# proof: there the SUT can still freely edit ALL files (source AND tests), so an
# incoherence (a test importing ``repoRoot`` while the helper exports
# ``projectRoot``; ``src/index.ts`` importing ``runCli`` that ``./cli`` never
# exports → TS2305/2724/2459) is made COHERENT before the run ever reaches verify
# — where auto-repair is scope-blocked from rewriting test files and the symbol
# mismatch ships as a permanent verify failure.
#
# The spec is the language-NEUTRAL declaration of that oracle: the command to run
# and the SCOPE it must demonstrably cover (anti-false-green: a compiler proves
# NOTHING about files outside its include scope — see :class:`OracleScopeSpec`).
# It lives on the :class:`LayoutProfile` so a new compiler stack is one profile
# entry + an evidence-normalizer entry, never a core edit. Stacks without a
# compiler oracle (Python's composite, bash, …) declare ``None`` and the gate is
# a strict NO-OP for them (their backstop stays the existing verify-stage gates).
_VALID_ORACLE_KINDS = {"compiler", "composite"}


# ═══════════════════════════════════════════════════════════
# Verify campaign: the harness OWNS the verification test command + its machine-
# readable report (design: /tmp/gpt_vscope_result.txt, GPT-5.5 Pro consult
# 2026-06-15; verdict A=(c)+(d): profile-defined campaign + execution reconcile).
# ═══════════════════════════════════════════════════════════
#
# THE BUG (greenfield codex14): verify resolved the test command by the SUT's
# ``package.json`` script priority (``detect_test_command``: test:unit > test >
# test:e2e), ran ``test:unit`` (39 unit tests), exited 0, and declared
# "verification passed" — while 28 declared verifiable behaviors were covered
# ONLY by ``tests/e2e/*.e2e.test.ts`` files that ``test:unit`` NEVER runs. The
# static VB coverage gate saw those markers and called them "covered"; verify
# never executed them. Two SEPARATE proof systems (static coverage vs. one
# detected command) let "covered but unexecuted" pass green.
#
# THE FIX (design verdict A): greenfield verify must NOT pick one SUT script. The
# harness/profile OWNS a CANONICAL verification CAMPAIGN — a command that runs the
# WHOLE VB-bearing test surface (unit AND e2e) and emits a MACHINE-READABLE report
# the coverage-execution coherence gate reconciles against (see
# :mod:`codd.coverage_execution_coherence`). The SUT may keep ``test:unit`` /
# ``test:e2e`` scripts for its own convenience, but they are NEVER the pass
# authority. ``detect_test_command`` stays UNCHANGED for brownfield/fixer
# watch/partial-run use cases — this is a greenfield-verify-only campaign.
#
# GENERAL + PER-PROFILE: the campaign COMMAND + the report FORMAT are per-stack
# (vitest JSON now; pytest JUnit XML / go test -json are documented extension
# points). The MEANING (run the whole VB surface, parse executed+passed test
# cases, reconcile with static coverage) is core. A stack whose profile declares
# ``verify_campaign=None`` (Python today) keeps the existing verify-stage path
# unchanged — the coherence gate is then a strict NO-OP for it.


@dataclass(frozen=True)
class CampaignReportSpec:
    """One machine-readable report artifact a campaign step must leave behind
    (design: multi-report verify campaigns, 2026-07-02).

    Maven's Surefire/Failsafe split is the motivating case: ONE ``mvn verify``
    invocation writes TWO report directories (``target/surefire-reports/`` for
    unit tests, ``target/failsafe-reports/`` for ``*IT`` integration tests) — a
    one-invocation/N-artifact shape shared by any build orchestrator that fans
    test execution across sub-units (.NET's multi-test-project ``dotnet test``,
    a multi-suite CTest run).

    * ``relpath`` — project-relative; a FILE or a DIRECTORY (runner-defined shape
      — Surefire/Failsafe write a directory of one file per test class).
    * ``format`` — the runner-report registry key
      (:func:`~codd.coverage_execution_coherence.resolve_runner_report_adapter`);
      Surefire and Failsafe share ONE format (``surefire-xml`` — Failsafe emits
      the identical ``TEST-*.xml`` schema), so this is data, never a second
      adapter to write.
    * ``capture`` — mirrors :class:`~codd.languages.profile.ReportSpec.capture`
      (``"stdout"`` ⇒ the step's captured stdout is persisted to ``relpath`` after
      it runs). Carrying this through closes a gap in the pre-generalization
      ``_synthesize_verify_campaign``, which silently dropped ``report.capture``
      (harmless while every synthesized campaign was file-written, but would have
      silently broken a future stdout-reporting language the day it opts in).
    * ``optional`` — this report's ABSENCE (or an unparseable-but-present report;
      see :func:`~codd.coverage_execution_coherence.run_verify_campaign`) is
      tolerated as "no evidence from this report", never presumed green. Whether
      an e2e surface that NEEDED this evidence actually exists is decided
      DOWNSTREAM, by reconciliation (:mod:`codd.coverage_execution_coherence`'s
      ``e2e_scan_zero`` observability check + per-VB reconciliation) — never by
      this flag. A wrongly-``optional`` report can therefore cause an honest RED,
      never a false GREEN.
    """

    relpath: str
    format: str
    capture: str | None = None
    optional: bool = False


@dataclass(frozen=True)
class VerifyCampaignStep:
    """One command invocation within a verify campaign, and the report artifact(s)
    it must leave behind (design: multi-report verify campaigns, 2026-07-02).

    "step" — not "phase" (a Maven-specific noun) — matches the vocabulary
    :class:`ImplementOracleProfileSpec` already uses for a composite oracle's own
    command sequence, so the same word means "one command invocation" everywhere
    in this module.

    A step declares its command in ONE of two forms, EXACTLY one (never both,
    never neither — validated in ``__post_init__``):

    * ``command_template`` — a SHELL string, run with ``shell=True``;
      ``{test_root}`` / ``{report}`` are substituted before execution.
    * ``command_argv``     — an ARGV list, run with ``shell=False`` so an argument
      containing shell metacharacters (``trx;LogFileName=test.trx``) is passed
      VERBATIM, never split/interpreted by a shell.

    ``{report}`` substitution (when a template/argv element references it)
    resolves against the FIRST declared report only — a step whose ONE command
    produces N artifacts (Maven: one ``mvn verify`` writes both
    ``surefire-reports/`` and ``failsafe-reports/``) has no single ``{report}``
    slot to fill for the others; Java's own ``command_argv`` (``["mvn", "-q",
    "verify"]``) does not reference ``{report}`` at all, so this does not affect
    it in practice.
    """

    reports: tuple[CampaignReportSpec, ...] = ()
    command_template: str | None = None
    command_argv: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not self.reports:
            raise ValueError("VerifyCampaignStep requires at least one report")
        if bool(self.command_template) == bool(self.command_argv):
            raise ValueError(
                "VerifyCampaignStep requires EXACTLY ONE of command_template (shell) "
                "or command_argv (argv) — a step with neither cannot run, and one "
                "declaring both is ambiguous about which form to execute."
            )

    def resolve_command(self, *, test_root: str, report_path: str) -> str:
        """The runnable SHELL command with ``{test_root}`` / ``{report}`` substituted."""
        if self.command_template is None:
            raise ValueError(
                "this step is argv-based (no command_template); use resolve_argv()"
            )
        return self.command_template.format(test_root=test_root, report=report_path)

    def resolve_argv(self, *, test_root: str, report_path: str) -> tuple[str, ...]:
        """The runnable ARGV with ``{test_root}`` / ``{report}`` substituted per element.

        Only elements containing a ``{`` placeholder are ``.format``-substituted, so a
        literal argv element with shell metacharacters but no placeholder (C#'s
        ``trx;LogFileName=test.trx``) is passed through verbatim.
        """
        if self.command_argv is None:
            raise ValueError(
                "this step is shell-based (no command_argv); use resolve_command()"
            )
        return tuple(
            (a.format(test_root=test_root, report=report_path) if "{" in a else a)
            for a in self.command_argv
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_template": self.command_template,
            "command_argv": list(self.command_argv) if self.command_argv is not None else None,
            "reports": [
                {
                    "relpath": r.relpath,
                    "format": r.format,
                    "capture": r.capture,
                    "optional": r.optional,
                }
                for r in self.reports
            ],
        }


@dataclass(frozen=True)
class VerifyCampaignSpec:
    """A stack's harness-owned greenfield verification campaign (profile-driven).

    Additively generalized (2026-07-02) from "one command + one report" to an
    ordered tuple of :class:`VerifyCampaignStep`, each with one-or-more
    :class:`CampaignReportSpec` artifacts — the cardinality Maven's Surefire/
    Failsafe split actually evidences (ONE invocation, TWO report roots), while
    every existing single-command/single-report stack (TS/C#/C++/Go's documented
    extension point) stays on the four flat fields below, UNCHANGED:

    * ``command_template`` / ``command_argv`` — see :class:`VerifyCampaignStep`
      (same two command forms, same substitution rules).
    * ``report_relpath`` / ``report_format`` — where the runner writes its report
      and which adapter parses it. See :class:`CampaignReportSpec`.
    * ``requires_node_install`` — whether the BLOCKING dependency-install
      preflight must run first (TS: yes — vitest/deps must be materialized).
    * ``steps`` — the GENERAL form (Java: one step, two reports). EXACTLY one of
      ``steps`` or the four flat fields is populated (validated in
      ``__post_init__``); :meth:`resolved_steps` is what every consumer
      (:func:`~codd.coverage_execution_coherence.run_verify_campaign`) actually
      iterates, so callers never need to branch on which form was used.

    The COMMAND(s) + FORMAT(s) are per-language; the MEANING (run the whole VB
    surface, reconcile executed+passed with static coverage) is core. ``None`` on
    a profile makes the coherence gate a strict NO-OP for that stack.
    """

    report_relpath: str | None = None
    report_format: str | None = None
    command_template: str | None = None
    command_argv: tuple[str, ...] | None = None
    requires_node_install: bool = False
    steps: tuple[VerifyCampaignStep, ...] = ()

    def __post_init__(self) -> None:
        legacy_fields_set = bool(
            self.report_relpath or self.report_format or self.command_template or self.command_argv
        )
        if self.steps:
            if legacy_fields_set:
                raise ValueError(
                    "VerifyCampaignSpec: 'steps' and the legacy single-report fields "
                    "(report_relpath/report_format/command_template/command_argv) are "
                    "mutually exclusive — declare a campaign in ONE form, not both."
                )
            return
        if not self.report_relpath or not self.report_format:
            raise ValueError(
                "VerifyCampaignSpec requires report_relpath + report_format (the "
                "legacy single-report form) or a non-empty 'steps' (the general, "
                "multi-step/multi-report form)."
            )
        if not self.command_template and not self.command_argv:
            raise ValueError(
                "VerifyCampaignSpec requires a command_template (shell) OR a "
                "command_argv (argv); a campaign with no command cannot run."
            )

    def resolved_steps(self) -> tuple[VerifyCampaignStep, ...]:
        """This campaign as an ordered tuple of steps — the shape every consumer
        (:func:`~codd.coverage_execution_coherence.run_verify_campaign`) iterates.

        Returns ``steps`` when declared; otherwise wraps the legacy flat fields as
        ONE step with ONE report — so every pre-existing single-report profile
        resolves to the exact shape it always implicitly had, byte-identical.
        """
        if self.steps:
            return self.steps
        return (
            VerifyCampaignStep(
                reports=(CampaignReportSpec(relpath=self.report_relpath, format=self.report_format),),
                command_template=self.command_template,
                command_argv=self.command_argv,
            ),
        )

    def resolve_command(self, *, test_root: str, report_path: str) -> str:
        """The runnable SHELL command with ``{test_root}`` / ``{report}`` substituted.

        Single-step convenience (legacy call sites) — delegates to the first
        resolved step, which for a legacy (non-``steps``) campaign is the only one.
        """
        return self.resolved_steps()[0].resolve_command(test_root=test_root, report_path=report_path)

    def resolve_argv(self, *, test_root: str, report_path: str) -> tuple[str, ...]:
        """The runnable ARGV with ``{test_root}`` / ``{report}`` substituted per element.

        Single-step convenience (legacy call sites) — delegates to the first
        resolved step, which for a legacy (non-``steps``) campaign is the only one.
        """
        return self.resolved_steps()[0].resolve_argv(test_root=test_root, report_path=report_path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_template": self.command_template,
            "command_argv": list(self.command_argv) if self.command_argv is not None else None,
            "report_relpath": self.report_relpath,
            "report_format": self.report_format,
            "requires_node_install": self.requires_node_install,
            "steps": [s.to_dict() for s in self.steps],
        }


@dataclass(frozen=True)
class OracleScopeSpec:
    """The file scope an implement-time oracle MUST be certified to cover.

    Anti-false-green (the #1 failure mode in the design memo): a native oracle
    "proves" nothing about files it never looked at. Before trusting a green
    ``tsc`` we certify its config (e.g. ``tsconfig.json`` ``include``/``files``)
    actually covers source + tests + e2e + helpers — otherwise an incoherent
    test/helper would pass UNSEEN. The scope is expressed as the
    :class:`LayoutProfile` ROOTS that must be inside the oracle's view; the
    per-stack certifier (see ``codd.implement_oracle``) resolves them against the
    project's real config. ``require_test_root`` is the load-bearing flag: the
    whole reason to move the gate to implement-time is to catch test/helper
    incoherence, so a config that excludes the test tree is a HARD FAIL, not a
    silent pass.
    """

    require_source_root: bool = True
    require_test_root: bool = True


@dataclass(frozen=True)
class ImplementOracleSpec:
    """A stack's implement-time native-oracle (profile-driven, not hardcoded).

    * ``command`` — the native coherence oracle, run from the project root during
      the IMPLEMENT stage (TS: ``npx --no-install tsc --noEmit`` — a pure
      typecheck, no emit). It must exit non-zero on a symbol/module incoherence.
    * ``kind`` — ``"compiler"`` (a static all-paths checker: tsc/go build/cargo
      check) or ``"composite"`` (a stack of weaker oracles unioned for a
      no-compiler language; DEFERRED — see the Python extension point in
      ``codd.implement_oracle``).
    * ``scope`` — the :class:`OracleScopeSpec` the gate certifies BEFORE trusting
      a pass (anti-false-green).
    * ``requires_node_install`` — whether the blocking dependency-install
      preflight must run first (TS: yes — ``tsc``/deps must be materialized).

    The COMMAND is per-language (unavoidable: each toolchain has its own CLI
    surface); the MEANING (run a coherence oracle at implement-time, certify its
    scope, normalize failures, retry) is core. That split is the whole point of
    the Artifact-Contract-Graph backbone.
    """

    command: str
    kind: str = "compiler"
    scope: OracleScopeSpec = field(default_factory=OracleScopeSpec)
    requires_node_install: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "kind": self.kind,
            "requires_node_install": self.requires_node_install,
            "scope": {
                "require_source_root": self.scope.require_source_root,
                "require_test_root": self.scope.require_test_root,
            },
        }


# ═══════════════════════════════════════════════════════════
# Manifest↔lock coherence: the harness OWNS the test-toolchain dependency
# versions + the lock-finalization contract (design: /tmp/gpt_result_dep.txt,
# GPT-5.5 Pro consult 2026-06-15; verdict (b) primary + (a) finalization +
# (c) forbidden).
# ═══════════════════════════════════════════════════════════
#
# THE BUG (observed greenfield codex9/codex10): verify's frozen install
# preflight (``npm ci``) hard-fails because the SUT wrote ``package.json`` with
# an OLD test-toolchain dep (``"vitest": "^1.6.0"``) while the scaffold/gate
# install had already produced ``package-lock.json`` with the LATEST resolution
# (``@vitest/expect@3.2.6`` from vitest@3.x). ``npm ci`` requires lock↔manifest
# agreement → "lock's @vitest/expect@3.2.6 does not satisfy ^1.6.0" → the run
# never reaches a green typecheck/test.
#
# OWNERSHIP (the load-bearing decision — design verdict (b)): the test-toolchain
# deps (vitest, typescript, tsx/ts-node, @types/node, coverage, the e2e runner)
# are NOT the generated app's business dependencies — they are the HARNESS's
# verification tooling. The split:
#
#   * SUT owns:     app runtime deps, domain libraries, source + test CONTENT.
#   * harness owns: the test runner, the compiler/typechecker, coverage, the e2e
#                   runner, the verify scripts, collection + module-resolution
#                   config — and THE VERSIONS of those toolchain deps.
#   * owner owns:   an explicit stack choice (a requirement saying "use Jest"
#                   makes the PROFILE Jest — see the runner field on
#                   :class:`LayoutProfile`; a future owner override could pin
#                   toolchain versions the same way the package name is pinnable).
#
# So when the SUT's ``package.json`` sets a DIFFERENT version for a harness-owned
# toolchain dep, the harness RECONCILES it back to the profile's version. This is
# NOT vandalizing the SUT's output — it is "recovering the verifier's own
# property" (the design's exact phrase). App/domain deps the SUT declared are
# never touched.
#
# GENERAL CONTRACT (design section D — language-independent): this is the
# manifest↔lock coherence contract, not an npm quirk. Every ecosystem has it:
# package-lock.json / uv.lock / poetry.lock / Cargo.lock (and Go's go.sum, which
# is a checksum-hygiene variant — ``go.mod ↔ go.sum``). The profile below is the
# per-stack declaration so Python/Rust/Go become PROFILE + ADAPTER entries, not
# core edits. TS/npm is implemented now; the others are documented extension
# points (see the registry + ``codd.dependency_lock_coherence``).


@dataclass(frozen=True)
class ToolchainDependency:
    """One harness-owned toolchain dependency: its name + the version the
    profile pins.

    ``dev`` (default True) declares the dep belongs in the manifest's
    development-dependency section (npm ``devDependencies``) — true for every
    test-toolchain dep (vitest/typescript/@types/node are not shipped with the
    app). The version SPEC is a normal range string (``"^3.2.6"``); the harness
    writes EXACTLY this spec, so a SUT that pinned an incompatible range is
    reconciled to the profile's range (which the refreshed lock then resolves).
    """

    name: str
    version: str
    dev: bool = True


@dataclass(frozen=True)
class ToolchainDependencyProfile:
    """A stack's harness-owned toolchain deps + the lock-finalization commands.

    Profile-driven so the manifest↔lock coherence contract generalizes across
    ecosystems with NO core edits (design section D). Fields:

    * ``deps`` — the toolchain dependencies the harness OWNS the versions of
      (vitest, typescript, @types/node, …). At implement-end the SUT's manifest
      is reconciled so each of these declares the profile's version; an app/
      domain dep the SUT added is NEVER in this set and is left untouched.
    * ``manifest_filename`` — the dependency manifest the deps live in
      (``package.json`` / ``pyproject.toml`` / ``Cargo.toml``).
    * ``lock_filenames`` — the lock/checksum file(s) this contract finalizes
      (``package-lock.json``; later ``uv.lock``/``poetry.lock``; ``Cargo.lock``;
      ``go.sum``). The first present one (or the first listed) is the lock the
      refresh produces.
    * ``lock_refresh_command`` — the DETERMINISTIC command that updates ONLY the
      lock to match the reconciled manifest, WITHOUT a frozen check
      (``npm install --package-lock-only``; later ``uv lock``; ``cargo
      generate-lockfile``; ``go mod tidy``). This is a harness FINALIZATION, not
      a SUT repair loop — it runs once at implement-end.
    * ``materialize_command`` — optional: after the lock is coherent, install
      node_modules so the SAME-PROCESS implement-oracle typecheck has its deps
      (``npm ci``). ``None`` skips materialization (the verify-stage install
      preflight will materialize later). Kept FROZEN (``npm ci``) so even the
      materialization honors the freshly-coherent lock rather than re-resolving.
    * ``frozen_install_command`` — the FROZEN install the verify campaign consumes
      (``npm ci``). It NEVER re-resolves; it verifies that the current lock
      reproduces the current manifest. The lock-freshness barrier validates the
      refreshed lock with exactly this command BEFORE verify, so verify's own
      frozen install (and the campaign's) passes honestly. (For npm it is the same
      string as ``materialize_command``; kept a SEPARATE field because the two are
      semantically distinct seams — materialize is an implement-end convenience,
      the frozen install is the verify-time reproducibility check the barrier must
      pass through.)
    * ``completeness_refresh_command`` — the FULL refresh used as the barrier's
      completeness FALLBACK (``npm install``) when the deterministic
      ``lock_refresh_command`` (``--package-lock-only``) leaves the lock/manifest
      still incoherent (a transitive omission like ufo/path-key). It re-resolves
      the WHOLE tree, then the frozen install is retried. ``None`` disables the
      fallback (the primary refresh is then the only path). See
      :func:`codd.dependency_lock_coherence.ensure_lock_freshness_barrier`.
    * ``workspace_manifest_globs`` — project-relative globs that match the stack's
      SECONDARY/WORKSPACE manifests beyond the root one (npm workspaces:
      ``packages/*/package.json`` etc.). They feed the manifest DIGEST so a
      workspace-only dependency change still re-freezes the lock. Empty by default
      (a single-package project); a workspace project declares its layout here.
    * ``config_filenames`` — project-relative dependency-resolution CONFIG files
      whose content changes the resolved tree (``.npmrc`` — ``legacy-peer-deps``,
      registries, ``shamefully-hoist``). They feed the digest so a flag change
      re-freezes. The barrier ALSO holds these constant between the refresh and the
      frozen install (lock generation + npm ci must see the SAME flags).
    * ``package_manager_version_command`` — the command whose stdout identifies the
      package-manager VERSION/config (``npm --version``). Folded into the digest so
      a manager upgrade (which can change lock format/resolution) re-freezes.
      ``None`` omits it (best-effort; a manager with no stable version probe).

    The MEANING (reconcile harness-owned deps → refresh the lock deterministically
    at implement-end → keep verify's install frozen → re-freeze before verify when
    the manifest set changed) is core; only these COMMANDS + FILE SETS are
    per-ecosystem. ``None`` (the default, and Python's value today) makes the
    finalization a strict NO-OP for that stack.
    """

    deps: tuple[ToolchainDependency, ...] = ()
    manifest_filename: str = "package.json"
    lock_filenames: tuple[str, ...] = ("package-lock.json",)
    lock_refresh_command: str = "npm install --package-lock-only"
    materialize_command: str | None = "npm ci"
    frozen_install_command: str = "npm ci"
    completeness_refresh_command: str | None = "npm install"
    workspace_manifest_globs: tuple[str, ...] = ()
    config_filenames: tuple[str, ...] = (".npmrc",)
    package_manager_version_command: str | None = "npm --version"

    def to_dict(self) -> dict[str, Any]:
        return {
            "deps": [{"name": d.name, "version": d.version, "dev": d.dev} for d in self.deps],
            "manifest_filename": self.manifest_filename,
            "lock_filenames": list(self.lock_filenames),
            "lock_refresh_command": self.lock_refresh_command,
            "materialize_command": self.materialize_command,
            "frozen_install_command": self.frozen_install_command,
            "completeness_refresh_command": self.completeness_refresh_command,
            "workspace_manifest_globs": list(self.workspace_manifest_globs),
            "config_filenames": list(self.config_filenames),
            "package_manager_version_command": self.package_manager_version_command,
        }


# ── The TS/npm toolchain profile (the only ecosystem implemented today) ──
#
# Versions are PINNED to current-major ranges so the scaffold/gate install and
# the SUT-reconciled manifest agree on the SAME resolution the lock holds. These
# are the toolchain deps the TS scaffold's ``test``/``build`` scripts need:
#   * ``vitest``      — the test runner the profile declares (``runner=vitest``).
#   * ``typescript``  — the ``tsc`` compiler the implement-oracle + build run.
#   * ``@types/node`` — Node type declarations (a strict ``tsc`` over CLI/fs code
#                       needs them; without it ``tsc`` errors on ``process``/etc).
# A project that legitimately wants a DIFFERENT major (e.g. pinned vitest 1.x for
# a plugin) is an OWNER stack choice; the future owner-override hook (mirroring
# ``project.package_name``) is the place for that — NOT a SUT-authored downgrade,
# which is exactly the incoherence this contract recovers from.
_TYPESCRIPT_TOOLCHAIN_PROFILE = ToolchainDependencyProfile(
    deps=(
        ToolchainDependency(name="vitest", version="^3.2.4"),
        ToolchainDependency(name="typescript", version="^5.9.2"),
        ToolchainDependency(name="@types/node", version="^24.3.0"),
    ),
    manifest_filename="package.json",
    lock_filenames=("package-lock.json",),
    lock_refresh_command="npm install --package-lock-only",
    materialize_command="npm ci",
    frozen_install_command="npm ci",
    # Completeness fallback: when ``--package-lock-only`` leaves a transitive
    # omission (ufo/path-key) so the frozen ``npm ci`` still reports "Missing
    # from lock file", a FULL ``npm install`` re-resolves the whole tree, then the
    # barrier retries the frozen install. Both stay inside the barrier — verify is
    # never the place a lock gets repaired (reproducibility).
    completeness_refresh_command="npm install",
    # npm workspaces conventions — secondary manifests whose dep changes must also
    # re-freeze the lock. Harmless for a single-package project (matches nothing);
    # a workspaces monorepo is covered without a core edit.
    workspace_manifest_globs=("packages/*/package.json", "apps/*/package.json"),
    # ``.npmrc`` changes the resolved tree (legacy-peer-deps, registries) → digest.
    config_filenames=(".npmrc",),
    package_manager_version_command="npm --version",
)


@dataclass(frozen=True)
class SourcePlacementSpec:
    """One declared source-placement root for a stack's harness-owned layout.

    A stack MAY own MORE than one source root — C++ owns ``src/`` (translation
    units) AND ``include/`` (public headers) — which the single ``package_root``
    field cannot express. ``root`` is the normalized project-relative directory;
    ``file_globs`` are that set's OWN globs (verbatim from the declarative profile,
    used to make a placement rule concrete WITHOUT a language-name literal);
    ``reference_base`` is ``True`` iff the stack's first-party import rule resolves
    a reference by that root as a path prefix (``imports.first_party.rule ==
    "include_path_prefix"`` with ``base == root``) — i.e. a file under ``root`` is
    referenced by its path RELATIVE TO ``root`` from every other file, never by a
    bare same-directory filename.

    ``LayoutProfile.source_placements`` defaults to ``()`` so every legacy-built
    profile (Python/TypeScript) is untouched BY CONSTRUCTION; only the generic
    synthesizer populates it, and the layout-placement contract renders the
    multi-root rule ONLY when it holds >1 DISTINCT normalized root.
    """

    root: str
    file_globs: tuple[str, ...] = ()
    reference_base: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "file_globs": list(self.file_globs),
            "reference_base": self.reference_base,
        }


@dataclass(frozen=True)
class SurfaceSpec:
    """One OPTIONAL deliverable surface a stack CAN emit but a project MAY exclude.

    The harness scaffolds a surface's ``paths`` (e.g. a runnable console entry point)
    by default; a project whose requirements exclude it (deterministic plan-stage
    intake) drops it entirely — the scaffold does not create it, the owned-scaffold
    authority no longer lists it, and any implement task that declares it is
    fence-rejected. ``LayoutProfile.optional_surfaces`` defaults to ``()`` so every
    stack that declares none is byte-identical BY CONSTRUCTION; ids/descriptions are
    profile DATA (no surface semantics in shared core).
    """
    id: str
    description: str
    paths: tuple[str, ...] = ()
    # DATA JOIN KEY — names the ``ProjectCapabilities.e2e_modality`` this surface
    # BACKS (e.g. the runnable console entry point backs the ``"cli"`` modality).
    # ``None`` (default) = this surface backs no modality. When such a surface is
    # EXCLUDED, :func:`effective_e2e_modality` downgrades the matching modality to
    # ``"none"`` so a pure library is not prompted for a subprocess e2e it cannot
    # satisfy — a pure DATA join (no language/framework literal in shared core).
    backs_e2e_modality: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "paths": list(self.paths),
            "backs_e2e_modality": self.backs_e2e_modality,
        }


# The CLI-backing optional surface EVERY stack shares: a runnable command-line
# entry point. Its ``backs_e2e_modality="cli"`` is the DATA join key
# :func:`effective_e2e_modality` reads to downgrade a pure library's e2e cli→none
# (no entry point ⇒ no CLI to subprocess ⇒ a CLI e2e suite that cannot pass). This
# is the SINGLE constructor for that surface, so every layout profile — Python's +
# TypeScript's per-stack builders AND the generic synthesizer (C#/C++/Java/JS) —
# carries the IDENTICAL id + backing flag by CONSTRUCTION. A new stack physically
# cannot ship a runnable-entrypoint that forgets the flag (the flag is baked in
# here, not restated per builder); the registry-driven guard
# ``TestRunnableEntrypointFlagGuard`` additionally fails if a stack forgets the
# whole surface. ``paths`` vary per stack: Python scaffolds
# ``src/<pkg>/__main__.py`` (so exclusion TRUE-SUBTRACTS it); a stack whose
# scaffolder materializes no single canonical entry file passes ``()`` — the
# surface is still the e2e-modality marker + plan-intake classification target, it
# simply subtracts no scaffold path. No language-name literal appears here: id +
# flag are INVARIANT, only the caller-supplied paths/description differ.
RUNNABLE_ENTRYPOINT_SURFACE_ID = "runnable-entrypoint"


def _runnable_entrypoint_surface(
    *, paths: tuple[str, ...] = (), description: str | None = None
) -> SurfaceSpec:
    """The shared CLI-backing ``runnable-entrypoint`` surface (backs the "cli" e2e modality)."""
    return SurfaceSpec(
        id=RUNNABLE_ENTRYPOINT_SURFACE_ID,
        description=(
            description
            or "an executable command-line entry point a user invokes directly"
        ),
        paths=paths,
        backs_e2e_modality="cli",
    )


@dataclass(frozen=True)
class LayoutProfile:
    """Harness-owned repository topology + module-resolution contract for a stack.

    Every path is DERIVED (from the project name + ``scan.source_dirs`` /
    ``scan.test_dirs``), never hardcoded. The generation pipeline writes INTO
    ``package_root`` so source lands inside the package; the coherence gate
    enforces ``test_import_policy`` (Python: ``package_absolute`` — a test must
    import a generated source module as ``from <package_name>.<mod> import ...``,
    never by bare basename); the test-runner ensurer + scaffold realize
    ``runner`` / ``install_mode`` so tests run against the REAL installed package
    (anti-false-green: an accidental flat import no longer resolves).

    ``implement_oracle`` (optional) declares a stack's IMPLEMENT-TIME native
    coherence oracle (TS: ``tsc --noEmit``). When present, the greenfield
    implement stage runs it after all units are generated and BEFORE verify, so
    symbol/module incoherence is fixed while the SUT can still edit every file.
    ``None`` (the default, and Python's value today) makes the gate a strict
    NO-OP for that stack — its coherence backstop stays the existing verify-stage
    gates. This is the single registration point for a new compiler stack.
    """

    language: str
    package_name: str
    source_root: str
    package_root: str
    test_root: str
    runner: str = "pytest"
    install_mode: str = "editable"  # "editable" | "none"
    test_import_policy: str = "package_absolute"  # "package_absolute" | "flat"
    requires_package_init: bool = True
    requires_test_init: bool = True
    implement_oracle: ImplementOracleSpec | None = None
    toolchain_dependencies: ToolchainDependencyProfile | None = None
    verify_campaign: VerifyCampaignSpec | None = None
    # AMBIENT MODULES — opt-in sentinel (e.g. "python-stdlib") naming the set of
    # runtime/stdlib-provided module names for this stack. The import-coherence
    # gate exempts a first-party source module whose bare name collides with an
    # ambient module (a first-party ``ast.py`` shadowing Python's stdlib ``ast``)
    # from its bare-import check — such a bare import is not a CONFIRMED first-party
    # reference (anti-false-red). ``None`` (default) = no exemption, unchanged.
    ambient_modules: str | None = None
    # SOURCE PLACEMENTS — the FULL set of harness-owned source roots (a stack may
    # own >1, e.g. C++ ``src/`` + ``include/``). Empty by default so a legacy-built
    # profile (Python/TS) is byte-identical BY CONSTRUCTION; populated ONLY by
    # :func:`_synthesize_layout_profile_from_language` from the declarative
    # profile's ``source_sets``. :func:`render_layout_placement_contract` projects a
    # multi-root SOURCE LOCATION rule (plus a SOURCE REFERENCE FORM rule for any
    # ``reference_base`` set) ONLY when this holds >1 DISTINCT normalized root —
    # otherwise it falls through to the single-root rule, unchanged.
    source_placements: tuple[SourcePlacementSpec, ...] = ()
    # OPTIONAL DELIVERABLE SURFACES — surfaces the stack CAN emit but a project MAY
    # exclude (``optional_surfaces``), plus the set actually excluded for THIS
    # project (``excluded_surface_ids``, threaded once in
    # :func:`resolve_layout_profile` from config). Both EMPTY by default so a
    # profile that declares none is byte-identical BY CONSTRUCTION.
    optional_surfaces: tuple[SurfaceSpec, ...] = ()
    excluded_surface_ids: frozenset[str] = frozenset()

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "package_name": self.package_name,
            "source_root": self.source_root,
            "package_root": self.package_root,
            "test_root": self.test_root,
            "runner": self.runner,
            "install_mode": self.install_mode,
            "test_import_policy": self.test_import_policy,
            "ambient_modules": self.ambient_modules,
            "source_placements": [p.to_dict() for p in self.source_placements],
            "optional_surfaces": [s.to_dict() for s in self.optional_surfaces],
            "excluded_surface_ids": sorted(self.excluded_surface_ids),
            "implement_oracle": (
                self.implement_oracle.to_dict() if self.implement_oracle is not None else None
            ),
            "toolchain_dependencies": (
                self.toolchain_dependencies.to_dict()
                if self.toolchain_dependencies is not None
                else None
            ),
            "verify_campaign": (
                self.verify_campaign.to_dict() if self.verify_campaign is not None else None
            ),
        }

    def harness_owned_scaffold_paths(self) -> tuple[str, ...]:
        """Project-relative files the harness SCAFFOLD owns (the contract escape hatch).

        These are the files :func:`scaffold_layout` creates for this stack —
        topology + config the SUT never "owns" via a task, yet which are
        legitimate generated artifacts (the orphan-artifact invariant's "owned by
        a task OR an explicit harness/profile contract" branch). The orphan-gate
        and the scoped-rerun write-fence consult this list so a scaffold file
        (e.g. TS ``vitest.config.ts`` / ``tsconfig.json``) is never mis-flagged as
        an unowned orphan and never reverted by the fence.

        PROFILE-DRIVEN (Contract Kernel v2.71): the stack-specific scaffold files
        are selected by the resolved :class:`LanguageProfile`'s legacy-bridge
        ``scaffolder`` realizer id (a harness-policy capability name — Python's
        package-topology scaffolder vs TS's config scaffolder), NOT a
        ``self.language ==`` literal. Each path is still DERIVED from this
        ``LayoutProfile``'s own fields (``package_root`` / ``test_root`` /
        ``requires_*`` — the same values the scaffolder uses) + the toolchain
        manifest/lock filenames, so a new stack inherits the contract by declaring
        its ``legacy_project_types`` bridge, with no language-name logic in the gate.
        A stack with no legacy bridge (Go) or an unknown/unaccepted language declares
        no stack-specific scaffold files (only the toolchain manifest/lock, if any).
        The list is a STATIC declaration of what the scaffolder *can* create (not
        what is present on disk); callers that need only existing files filter by
        ``is_file()``.
        """
        paths: list[str] = []

        def _add(rel: str) -> None:
            norm = _norm_rel(rel)
            if norm and norm not in paths:
                paths.append(norm)

        # Dependency manifest + lockfile(s) the toolchain contract owns
        # (package.json / package-lock.json; pyproject.toml / uv.lock; …).
        toolchain = self.toolchain_dependencies
        if toolchain is not None:
            _add(toolchain.manifest_filename)
            for lock in toolchain.lock_filenames:
                _add(lock)

        # Route the stack-scaffold files by the legacy-bridge scaffolder realizer id
        # (a harness-policy capability name from the profile), never a language name.
        scaffolder_id = _legacy_realizer_id(self.language, "scaffolder")
        if scaffolder_id == _SCAFFOLDER_PY_SRC_PACKAGE:
            # _scaffold_python: pyproject + package <__init__>/<__main__> + test <__init__>.
            _add(_PYPROJECT_FILENAME)
            if self.requires_package_init:
                _add(f"{self.package_root}/__init__.py")
                _add(f"{self.package_root}/__main__.py")
            if self.requires_test_init:
                _add(f"{self.test_root}/__init__.py")
        elif scaffolder_id == _SCAFFOLDER_TS_NPM:
            # _scaffold_typescript: tsconfig + vitest config + package.json.
            _add(_TSCONFIG_FILENAME)
            _add(_VITEST_CONFIG_FILENAME)
            _add(_PACKAGE_JSON_FILENAME)
        else:
            # Generic-template stack (opt-in; csharp): the scaffold files are the profile's
            # ``scaffold.owned_files`` + each ``scaffold.templates[].path``, with
            # ``{package_name}`` + ``scaffold.defaults`` substituted (the SAME values
            # :func:`_scaffold_generic_template` writes — so the orphan-gate/write-fence
            # recognise the scaffolded ``<pkg>.csproj`` as harness-owned, not an orphan).
            # Go is excluded (no opt-in key → spec is None) so this stays empty for it.
            spec = _generic_template_scaffold_spec(self.language)
            if spec is not None:
                templates, defaults, owned = spec
                subst = _generic_template_substitutions(self, defaults)
                for rel in owned:
                    _add(_apply_template_substitutions(str(rel), subst))
                for tmpl in templates:
                    if not hasattr(tmpl, "get"):
                        continue
                    template_path = tmpl.get("path")
                    if template_path:
                        _add(_apply_template_substitutions(str(template_path), subst))

        excluded = {p for rel in self.excluded_surface_paths() if (p := _norm_rel(rel))}
        return tuple(p for p in paths if p not in excluded)

    def facade_output_paths(self) -> tuple[str, ...]:
        """Project-relative package-FACADE file(s) whose TOPOLOGY the harness owns
        but whose CONTENT the SUT/AI authors (the ownership carve-out).

        A named-package stack scaffolds an empty package-root facade file (a
        docstring placeholder) as harness TOPOLOGY, yet its public-API re-exports
        are genuine SUT-authored content: a downstream module importing the
        package's public symbols resolves them THROUGH this file, so leaving it
        empty is a real cross-artifact incoherence the implement-oracle rightly
        rejects. This accessor names the file(s) the harness creates but must NOT
        claim as an AI-produced obligation — the deriver keeps them in a task's
        declared outputs, the completeness/kind contract imposes the source-kind
        obligation on them, and the layout prompt stops forbidding them.

        STRICT NO-OP for every stack whose scaffolder does not create a
        content-bearing package facade: the routing mirrors
        :meth:`harness_owned_scaffold_paths` (the same legacy-bridge scaffolder
        realizer id), and any other scaffolder returns ``()`` — so this carve-out
        never widens beyond the one stack whose scaffolder emits such a placeholder,
        and it introduces NO path prefix and NO ``language ==`` literal. It is a
        strict SUBSET of :meth:`harness_owned_scaffold_paths` (the scaffold still
        creates the file and the orphan-gate/write-fence still exempt it); only the
        module-level obligation authority subtracts it.
        """
        scaffolder_id = _legacy_realizer_id(self.language, "scaffolder")
        if scaffolder_id == _SCAFFOLDER_PY_SRC_PACKAGE and self.requires_package_init:
            norm = _norm_rel(f"{self.package_root}/__init__.py")
            return (norm,) if norm else ()
        return ()

    def excluded_surface_paths(self) -> tuple[str, ...]:
        """Normalized paths of this profile's optional surfaces that are EXCLUDED.

        STRICT no-op (``()``) unless the profile declares optional surfaces AND some
        are excluded (``excluded_surface_ids``). A true subtraction target: the
        scaffold skips these, ``harness_owned_scaffold_paths`` drops them, and the
        derive fence rejects any task declaring them.
        """
        if not self.excluded_surface_ids:
            return ()
        out: list[str] = []
        for surface in self.optional_surfaces:
            if surface.id in self.excluded_surface_ids:
                for rel in surface.paths:
                    norm = _norm_rel(rel)
                    if norm and norm not in out:
                        out.append(norm)
        return tuple(out)

    def test_block_profile(self) -> Any:
        """Resolve this stack's test-structure adapter for the VB authenticity gate.

        Returns a ``codd.vb_marker_authenticity.TestBlockProfile`` (a per-language
        parser that locates executable test blocks and resolves skip/assertion
        facts) or ``None`` for a stack with no adapter — in which case the
        authenticity gate gracefully degrades to its language-agnostic stage 1
        (orphan-marker) check only.

        PROFILE-DRIVEN (Contract Kernel v2.71): the parser is selected by the
        resolved :class:`LanguageProfile`'s ``tests.semantics_adapter`` CAPABILITY
        ID (``python-test-semantics`` / ``typescript-test-semantics`` /
        ``go-test-semantics``), NOT a ``self.language ==`` literal. The id is a
        capability name declared in the language YAML, so a new stack registers its
        parser by declaring its semantics adapter id + adding one entry to the
        adapter-id→parser table below — no language-name logic in the gate. An
        unknown language / a profile with no semantics adapter ⇒ ``None`` (the
        authenticity gate degrades to its stage-1 check). Imports are lazy so the
        authenticity module (which imports the VB audit) is never pulled in at
        ``project_types`` import time.
        """

        profile = _resolve_kernel_language_profile(self.language)
        tests = getattr(profile, "tests", None) if profile is not None else None
        adapter_id = getattr(tests, "semantics_adapter", None) if tests is not None else None
        if not adapter_id:
            return None

        try:
            from codd.vb_marker_authenticity import (
                CppTestBlockProfile,
                CSharpTestBlockProfile,
                GoTestBlockProfile,
                JavaTestBlockProfile,
                PythonTestBlockProfile,
                TypeScriptTestBlockProfile,
            )
        except Exception:  # noqa: BLE001 — adapter is optional; degrade if unavailable.
            return None

        # adapter-id → test-block parser. The keys are CAPABILITY ids from the
        # language profiles (never language names), so this is language-free: a new
        # compiler stack registers its parser by declaring its semantics adapter id
        # in its YAML + adding ONE entry here — no language-name logic in the gate.
        builders = {
            "python-test-semantics": PythonTestBlockProfile,
            "typescript-test-semantics": TypeScriptTestBlockProfile,
            "go-test-semantics": GoTestBlockProfile,
            "java-junit-semantics": JavaTestBlockProfile,
            "csharp-test-semantics": CSharpTestBlockProfile,
            "cpp-test-semantics": CppTestBlockProfile,
        }
        builder = builders.get(str(adapter_id))
        return builder() if builder is not None else None

    def runner_report_adapter(self) -> Any:
        """Resolve this stack's SINGLE runner-report adapter, iff one adapter reads
        EVERY report the campaign declares.

        Returns a ``codd.coverage_execution_coherence.RunnerReportAdapter`` when
        every :class:`~codd.project_types.CampaignReportSpec` across every resolved
        step (:meth:`VerifyCampaignSpec.resolved_steps`) shares ONE distinct
        ``format`` — true for every single-report campaign (TS/C#/C++/Go's
        extension point) and for Java (both ``surefire-xml``) — else ``None``.
        Selection is data-driven on the report ``format`` string(s) — never a
        language-name branch — so a new runner is one adapter + one profile
        ``format`` value, never a core gate edit.

        This is DELIBERATELY narrower than "can every report be read" (that
        question is :func:`~codd.coverage_execution_coherence.coherence_gate_applies`
        / ``certify_verify_campaign_observable``, which check EACH report's format
        resolves independently): this method answers "is there one adapter whose
        ``produces_test_case_identity()`` capability applies uniformly to this
        campaign's WHOLE evidence" — the question ``_authentic_cover_case_keys``
        needs to decide per-case vs. file-level reconciliation. A campaign whose
        reports use DIFFERENT formats degrades to ``None`` here (safe: the caller
        then falls back to file-level reconciliation), even though each report may
        individually be perfectly readable. The import is lazy so the coherence
        module is not pulled in at ``project_types`` import time.

        ``None`` when the profile declares no ``verify_campaign`` (Python today),
        the campaign's reports mix formats, or a declared format has no registered
        adapter yet.
        """

        campaign = self.verify_campaign
        if campaign is None:
            return None
        try:
            from codd.coverage_execution_coherence import resolve_runner_report_adapter
        except Exception:  # noqa: BLE001 — adapter is optional; degrade if unavailable.
            return None
        try:
            formats = {report.format for step in campaign.resolved_steps() for report in step.reports}
        except Exception:  # noqa: BLE001 — a malformed campaign resolves to no adapter.
            return None
        if len(formats) != 1:
            return None
        return resolve_runner_report_adapter(next(iter(formats)))


def _placement_reference_ext(file_globs: tuple[str, ...]) -> str:
    """The dotted file extension a placement's globs match (``.hpp`` from
    ``include/**/*.hpp``), or ``""`` when none is derivable.

    Keeps the SOURCE REFERENCE FORM sample path concrete WITHOUT a hardcoded
    suffix or a language-specific noun — the extension is read from the profile's
    OWN globs. Uses the first glob whose leaf is a ``*.<ext>`` wildcard.
    """
    for glob in file_globs:
        leaf = str(glob).rsplit("/", 1)[-1]
        if leaf.startswith("*.") and len(leaf) > 2:
            return leaf[1:]
    return ""


def render_layout_placement_contract(profile: "LayoutProfile | None") -> str:
    """Project the harness-owned repository LAYOUT (test root, source root, and the
    harness-owned config files) onto a generation/implement prompt, DATA-DRIVEN
    from the resolved :class:`LayoutProfile` — the SAME topology the scaffold
    creates and the output-path fence enforces.

    The generation prompt otherwise never conveys WHERE test files live, so the
    model freelances a sibling test directory (the JS greenfield wrote unit specs
    under ``test/`` while the harness owns ``tests/``); the output-path fence then
    drops the misplaced file, and its declared 'test' deliverable reads as "not
    produced" → a hard ``StageError``. Rendering the profile the scaffold realises
    makes the prompt-side layout contract unable to drift from the harness-side
    one — the same same-truth-source principle behind
    :func:`~codd.import_coherence.render_import_coherence_contract` /
    ``resolve_test_framework_guidance`` / :func:`~codd.verifiable_behavior_audit.render_vb_contract`.

    Language-free: the test root, source root, and harness-owned scaffold paths are
    all read from ``profile`` — there is NO language-name branch and NO hardcoded
    owned path (the owned roots come from ``profile.test_root`` /
    ``profile.package_root``). Each rule is gated on the exact profile fields that
    decide it:

    * the TEST ROOT rule is emitted whenever the profile declares a ``test_root``
      (every supported stack does) — the owned root is rendered verbatim, so a
      brownfield stack whose real test dir is ``test`` renders ``test/`` with no
      ``tests`` literal anywhere;
    * the SOURCE ROOT rule iff ``not requires_package_init`` — a named-package
      stack (Python) already states the source-root rule through its import
      contract (:func:`render_import_coherence_contract` rule 1), so re-emitting it
      here would DUPLICATE it; a path-relative stack (TypeScript/JS) has no import
      contract, so this is the ONLY place its source root is stated;
    * the harness-owned config-file rule iff the profile declares any
      ``harness_owned_scaffold_paths`` (else omitted).

    Returns ``""`` when ``profile`` is ``None`` (a stack with no resolved layout,
    e.g. Go) — there is nothing to project.
    """
    if profile is None:
        return ""

    rules: list[str] = []

    test_root = str(profile.test_root or "").strip().replace("\\", "/").strip("/")
    if test_root:
        # Common sibling test-dir names the model may freelance, MINUS the owned
        # root — so a stack whose owned root IS ``test`` is never told to avoid it
        # (the illustration is derived, never a hardcoded ``tests``-vs-``test``
        # assumption). Purely an example set to make the rule concrete.
        siblings = [d for d in ("test", "tests", "spec", "specs") if d != test_root]
        example = (
            f" (do NOT invent a sibling test directory such as "
            f"{', '.join(f'`{d}/`' for d in siblings)})"
            if siblings
            else ""
        )
        rules.append(
            f"{len(rules) + 1}. TEST LOCATION — the harness OWNS the test root "
            f"`{test_root}/`, and the verify runner discovers test files ONLY under "
            f"`{test_root}/`. Put EVERY test file you author — and every test-file "
            f"path this document references — UNDER `{test_root}/`{example}. A test "
            f"file placed outside `{test_root}/` is dropped by the output-path fence, "
            f"so its declared 'test' deliverable reads as never produced and fails "
            f"the build."
        )

    if not profile.requires_package_init:
        # A stack may OWN more than one source root (C++ ``src/`` + ``include/``).
        # ``source_placements`` is EMPTY for every legacy-built profile, so the
        # multi-root path below is unreachable for them and the single-root rule in
        # the ``else`` renders byte-for-byte as before. Collapse to the DISTINCT
        # normalized roots (order-preserving) — a header-set whose root equals the
        # source-set root dedupes back to single-root.
        ordered_placements: list[SourcePlacementSpec] = []
        seen_roots: list[str] = []
        for placement in profile.source_placements:
            root = _norm_rel(placement.root)
            if root and root not in seen_roots:
                seen_roots.append(root)
                ordered_placements.append(placement)
        if len(seen_roots) > 1:
            # MULTI-ROOT: the single ``package_root`` rule can describe only ONE root
            # and would (wrongly) tell the model that files outside it are dropped —
            # so a file authored under a second owned root would read as contract-
            # non-compliant. Emit one SOURCE LOCATION bullet per owned root, each
            # listing that set's OWN file globs verbatim (placement made concrete with
            # NO language noun — the globs are profile DATA).
            bullets: list[str] = []
            for placement in ordered_placements:
                root = _norm_rel(placement.root)
                globs = ", ".join(f"`{g}`" for g in placement.file_globs)
                where = f"the files matching {globs}" if globs else "its files"
                bullets.append(f"     - {where} belong under `{root}/`")
            rules.append(
                f"{len(rules) + 1}. SOURCE LOCATION — this project owns MORE than one "
                f"source root; author each file under the owned root whose file-glob "
                f"set it matches (a file placed outside EVERY owned root is dropped by "
                f"the output-path fence):\n" + "\n".join(bullets)
            )
            # SOURCE REFERENCE FORM — for any owned root whose first-party rule
            # resolves a reference by path RELATIVE TO that root (``reference_base``).
            # Placement alone does not close the bug: a file at ``<root>/<dir>/<name>``
            # referenced by a bare same-directory filename recreates the identical
            # failure. The sample path's extension is derived from the set's OWN globs.
            for placement in ordered_placements:
                if not placement.reference_base:
                    continue
                root = _norm_rel(placement.root)
                ext = _placement_reference_ext(placement.file_globs)
                rules.append(
                    f"{len(rules) + 1}. SOURCE REFERENCE FORM — a file at "
                    f"`{root}/<dir>/<name>{ext}` is referenced as `<dir>/<name>{ext}` "
                    f"from every other file wherever it lives, never by a bare "
                    f"filename that only resolves from the referencing file's own "
                    f"directory."
                )
        else:
            source_root = str(profile.package_root or "").strip().replace("\\", "/").strip("/")
            if source_root:
                rules.append(
                    f"{len(rules) + 1}. SOURCE LOCATION — put EVERY source module you "
                    f"author UNDER `{source_root}/`. A source file placed outside "
                    f"`{source_root}/` is dropped by the output-path fence."
                )

    try:
        # The FACADE file is carved out of the harness obligation (its content is
        # SUT-authored), so it must NOT appear in the "do not author these" list —
        # the model is now expected to populate it. Subtract it here (this rule
        # reads the profile method directly, not the obligation authority).
        facade = {_norm_rel(p) for p in profile.facade_output_paths()}
        scaffold_paths = tuple(
            p for p in profile.harness_owned_scaffold_paths() if _norm_rel(p) not in facade
        )
    except Exception:  # noqa: BLE001 — an ownership-resolution failure must never break the prompt.
        scaffold_paths = ()
    if scaffold_paths:
        listed = ", ".join(f"`{p}`" for p in scaffold_paths)
        rules.append(
            f"{len(rules) + 1}. HARNESS-OWNED SCAFFOLD — the dependency manifest, the "
            f"lockfile, and the test-runner / toolchain config files are created by "
            f"the harness scaffold, and the verify command is fixed. Do NOT author or "
            f"declare a runner/tool config file among your outputs — these are already "
            f"provided: {listed}. A config file you emit is dropped by the output-path "
            f"fence (and never changes how verify runs)."
        )

    try:
        excluded_surfaces = tuple(profile.excluded_surface_paths())
    except Exception:  # noqa: BLE001 — projection must never break the prompt.
        excluded_surfaces = ()
    if excluded_surfaces:
        listed = ", ".join(f"`{p}`" for p in excluded_surfaces)
        rules.append(
            f"{len(rules) + 1}. EXCLUDED DELIVERABLE SURFACE — the requirements exclude these "
            f"surface(s), so the harness does NOT create them and you must NOT author or declare "
            f"them among your outputs: {listed}. A file you emit here is an unowned orphan and "
            f"fails the build. Do NOT author tests that invoke or exercise an excluded surface "
            f"either (e.g. no subprocess/CLI test that runs an excluded entry point) — an excluded "
            f"surface does not exist to be tested; cover the behavior in-process instead."
        )

    if not rules:
        return ""

    header = (
        "Repository LAYOUT CONTRACT (release-blocking — the harness scaffold owns "
        "this topology and the output-path fence enforces it at implement; a file "
        "placed outside the owned roots is dropped, so a declared deliverable then "
        "reads as never produced and fails the build — get the placement right the "
        "first time):"
    )
    return "\n".join([header, "", *rules])


def _sanitize_package_identifier(
    raw_input: str, *, fallback: str, preserve_case: bool, leading_upper: bool
) -> str:
    """Shared identifier sanitizer behind :func:`normalize_package_name`.

    ``preserve_case=False`` force-lowers first (the historical default). Any run of
    non-``[A-Za-z0-9_]`` chars collapses to a single ``_``; leading/trailing ``_`` are
    stripped; an empty result → ``fallback``; a leading digit is prefixed with ``_``.
    ``leading_upper`` then upper-cases the first char when it is a letter (the PascalCase
    guarantee). Pure + deterministic — the SAME inputs always yield the SAME identifier.
    """
    raw = raw_input if preserve_case else raw_input.lower()
    chars: list[str] = []
    for ch in raw:
        chars.append(ch if (ch.isalnum() or ch == "_") else "_")
    collapsed = "".join(chars).strip("_")
    while "__" in collapsed:
        collapsed = collapsed.replace("__", "_")
    if not collapsed:
        return fallback
    if collapsed[0].isdigit():
        collapsed = "_" + collapsed
    if leading_upper and collapsed[0].isalpha():
        collapsed = collapsed[0].upper() + collapsed[1:]
    return collapsed


def normalize_package_name(
    project_name: str | None, *, fallback: str = "app", package_case: str = "lower"
) -> str:
    """Derive a valid package identifier from a project name (casing is DATA-driven).

    ``package_case`` is the casing discipline a LanguageProfile declares via
    ``naming.package_case`` — the harness branches on this VALUE, NEVER on a language
    name, so a stack opts into a casing by data alone:

    * ``"lower"`` (DEFAULT — Python/TS/Go) → force a lower-case identifier:
      ``todo-cli`` → ``todo_cli``; ``2048 Game`` → ``_2048_game``. BYTE-FOR-BYTE the
      historical behavior (every caller that omits ``package_case`` is unchanged).
    * ``"pascal"`` (case-PRESERVING — C#) → keep the author's casing + guarantee a
      leading uppercase: ``TextKit`` → ``TextKit`` (the ``--project-name TextKit`` a
      force-``.lower()`` used to defeat); a bare lower word ``textkit`` → ``Textkit``
      (no over-guessing of word splits). An invalid char still sanitizes to ``_``.

    Empty/garbage → ``fallback`` under either discipline. Deterministic and pure so the
    same ``(project name, package_case)`` always yields the same package, which is what
    makes source + tests + manifest agree on ONE cased name.
    """
    raw_input = str(project_name or "").strip()
    if str(package_case).strip().lower() == "pascal":
        return _sanitize_package_identifier(
            raw_input, fallback=fallback, preserve_case=True, leading_upper=True
        )
    # "lower" (and any non-"pascal" value) → the historical force-lower discipline.
    return _sanitize_package_identifier(
        raw_input, fallback=fallback, preserve_case=False, leading_upper=False
    )


def _config_package_name_override(config: Any, *, package_case: str = "lower") -> str | None:
    """Read an explicit ``project.package_name`` override from project config.

    The harness OWNS the package name; an owner may pin it explicitly via
    ``project.package_name`` in ``codd.yaml`` (highest precedence — design-doc
    PROSE is never the topology authority). Returns the normalized identifier
    (honoring the profile's ``package_case`` so a pinned ``TextKit`` stays cased on a
    pascal stack), or ``None`` when unset/blank/invalid so resolution falls through to
    the next tier.
    """
    if not isinstance(config, Mapping):
        return None
    project_section = config.get("project")
    if not isinstance(project_section, Mapping):
        return None
    raw = project_section.get("package_name")
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    # Normalize so an owner who writes ``calc-lib`` still gets a valid identifier;
    # a value that normalizes to the bare fallback (garbage) is treated as unset.
    normalized = normalize_package_name(text, package_case=package_case)
    return normalized if normalized != "app" or text.strip().lower() in {"app"} else None


def _detect_single_top_level_package(
    project_root: Path | None,
    source_root: str,
) -> str | None:
    """Deterministically detect the model's single top-level src package, if unambiguous.

    A greenfield SUT often authors its own package name internally coherently
    (source uses ``from .mod import``, tests import ``from <pkg> import``,
    ``[tool.coverage] source = ['<pkg>']`` — all agreeing on ``<pkg>``). When the
    GENERATED structure has EXACTLY ONE top-level package directory under
    ``source_root`` (a dir with an ``__init__.py`` and at least one other module),
    that name is a deterministic ARTIFACT (not prose) and is the safest canonical:
    adopting it keeps source/tests/imports/coverage byte-for-byte coherent instead
    of rewriting every ``from <pkg> import`` in the model's tests.

    Returns the package name only when the choice is UNAMBIGUOUS (exactly one such
    top-level package); ``None`` otherwise (zero, or two+ — fall back to the
    project-name default and let the deterministic scaffold/merge own topology).
    """
    if project_root is None:
        return None
    src_dir = Path(project_root) / _norm_rel(source_root)
    if not src_dir.is_dir():
        return None
    candidates: list[str] = []
    for child in sorted(src_dir.iterdir()):
        if not child.is_dir():
            continue
        name = child.name
        if name in {"__pycache__"} or name.startswith("."):
            continue
        if not name.isidentifier():
            continue
        if not (child / "__init__.py").exists():
            continue
        candidates.append(name)
    return candidates[0] if len(candidates) == 1 else None


def _norm_rel(rel: str) -> str:
    return str(rel).strip().replace("\\", "/").strip("/")


def resolve_canonical_package_name(
    project_name: str | None,
    *,
    config: Any = None,
    project_root: Path | None = None,
    source_root: str = "src",
    package_case: str = "lower",
) -> str:
    """Resolve the ONE canonical Python package name the harness owns (deterministic).

    Resolution order (highest precedence first):

    1. **Explicit config override** — ``project.package_name`` in ``codd.yaml``.
       The owner pinned it; honor it exactly. Design-doc prose is NEVER the
       authority.
    2. **Derive-from-actual** — the GENERATED structure's single unambiguous
       top-level src package (see :func:`_detect_single_top_level_package`). The
       structure is a deterministic artifact; adopting it keeps the model's
       internally-coherent source/tests/imports/coverage byte-for-byte coherent
       (no test-rewrite churn), which is both safer and cleaner than forcing a
       name and rewriting imports.
    3. **Project-name default** — ``normalize_package_name(project_name)``.
       Deterministic and pure; the fallback when there is no override and no
       single unambiguous existing package.

    Every tier is deterministic and model-independent: the same inputs (config,
    on-disk structure, project name) always yield the same canonical name, which
    is what makes the reconciled source/pyproject/imports all agree.

    ``package_case`` (DATA the LanguageProfile declares — never a language branch) is
    threaded to the config-override and project-name tiers so a ``pascal`` stack PRESERVES
    case (``TextKit`` → ``TextKit``); ``lower`` (the default) is the legacy behavior. The
    derive-from-actual tier already preserves the on-disk dir's case verbatim.
    """
    override = _config_package_name_override(config, package_case=package_case)
    if override is not None:
        return override
    detected = _detect_single_top_level_package(project_root, source_root)
    if detected is not None:
        return detected
    return normalize_package_name(project_name, package_case=package_case)


def _first_clean_dir(dirs: Any, default: str) -> str:
    """First normalized (slash-free) root from a ``scan.*_dirs`` value, or default."""
    normalized = _normalize_dirs(dirs)
    return normalized[0] if normalized else default


def _python_layout_profile(
    *,
    project_name: str | None,
    source_dirs: Any,
    test_dirs: Any,
    config: Any = None,
    project_root: Path | None = None,
) -> LayoutProfile:
    """Python ``python_src_package`` profile: a src-layout, installed package.

    * ``package_name`` is the harness-owned CANONICAL name
      (:func:`resolve_canonical_package_name`): explicit ``project.package_name``
      override > the generated structure's single unambiguous top-level package >
      ``normalize_package_name(project_name)``. Deterministic and model-independent.
    * ``source_root`` from ``scan.source_dirs`` (default ``src``).
    * ``package_root`` = ``<source_root>/<package_name>`` — source lives in a
      named package, so package-absolute imports work both in tests (installed)
      and at runtime (``python -m <package_name>``).
    * ``test_root`` from ``scan.test_dirs`` (default ``tests``).
    * runner=pytest, install_mode=editable, policy=package_absolute.
    """
    source_root = _first_clean_dir(source_dirs, "src")
    package_name = resolve_canonical_package_name(
        project_name, config=config, project_root=project_root, source_root=source_root
    )
    test_root = _first_clean_dir(test_dirs, "tests")
    return LayoutProfile(
        language="python",
        package_name=package_name,
        source_root=source_root,
        package_root=f"{source_root}/{package_name}",
        test_root=test_root,
        runner="pytest",
        install_mode="editable",
        test_import_policy="package_absolute",
        requires_package_init=True,
        requires_test_init=True,
        # This surface BACKS the "cli" e2e modality: the CLI-subprocess e2e tests
        # (run_cli → `python -m <pkg>`) exist only because this entry point does.
        # Excluding it (a pure library) downgrades e2e to "none". Built through the
        # SHARED constructor so the CLI-backing flag is identical to every other
        # stack's runnable-entrypoint (see :func:`_runnable_entrypoint_surface`).
        optional_surfaces=(
            _runnable_entrypoint_surface(
                paths=(f"{source_root}/{package_name}/__main__.py",),
                description=(
                    "an executable command-line entry point a user invokes directly "
                    "(e.g. `python -m <package>`)"
                ),
            ),
        ),
        # A first-party module may legitimately share a bare name with the Python
        # stdlib (e.g. a domain ``ast.py``); resolved at runtime from the running
        # interpreter so the core hardcodes no module list. Exempts such a name
        # from the import-coherence bare-import check (anti-false-red).
        ambient_modules="python-stdlib",
        # IMPLEMENT-TIME ORACLE — COMPOSITE (Python has no single compiler that
        # proves all-paths symbol coherence). ``kind="composite"`` routes the gate
        # to the in-process multi-tool executor in ``codd.implement_oracle``
        # (``_run_python_composite_oracle``), which unions THREE hard layers run
        # BEFORE pytest at implement-time, each with an observability gate that
        # HARD-FAILS if a required tool did not see every source+test .py:
        #     1. in-process compile() over every source+test .py  (syntax/encoding)
        #   + 2. a first-party import/symbol resolver over every source+test .py
        #        (the KEYSTONE: catches ``src/app/hidden.py: from .missing import
        #        X`` that no test imports — invisible to py_compile + collect-only)
        #   + 3. pytest --collect-only  (test↔helper symbol mismatch surfaces as an
        #        ImportError at COLLECTION — the test-surface importability layer)
        # ``command`` is a SENTINEL ("python-composite"); the kind dispatch runs
        # the executor, not a shell command. ruff/pyflakes undefined-name lint is
        # an OPTIONAL enhancement (``implement.python_name_lint: off|optional|
        # required``, default optional → skip if absent) and a SEPARATE registry
        # contract — when skipped the oracle does NOT claim undefined-local-name
        # coverage. The existing verify-stage gates (import_coherence /
        # test_import_coherence / e2e_contract_coherence) remain the backstop.
        implement_oracle=ImplementOracleSpec(
            command="python-composite",  # sentinel; executed by kind dispatch, not a shell
            kind="composite",
            scope=OracleScopeSpec(require_source_root=True, require_test_root=True),
            requires_node_install=False,
        ),
        # MANIFEST↔LOCK COHERENCE — DEFERRED for Python (separate task). The same
        # contract applies (pyproject.toml ↔ uv.lock / poetry.lock: ``uv lock``
        # /``poetry lock`` refresh the lock to the manifest, and ``--locked`` /
        # ``--frozen`` is the equivalent of npm ci). But today's Python path does
        # NOT pre-build a lock at scaffold time, and ``pip install -e .`` is not a
        # frozen-lock install, so there is no manifest↔lock divergence to recover —
        # making this a true NO-OP, not a gap. To wire it later: pin the Python
        # test-toolchain deps (pytest, the typechecker) in a
        # ToolchainDependencyProfile(manifest_filename="pyproject.toml",
        # lock_filenames=("uv.lock",), lock_refresh_command="uv lock",
        # materialize_command=...) and add the pyproject reconcile adapter in
        # codd.dependency_lock_coherence. Until then None ⇒ the finalization is a
        # strict NO-OP for Python (today's behaviour, unchanged).
        toolchain_dependencies=None,
        # VERIFY CAMPAIGN — DEFERRED for Python (separate task). The MEANING is the
        # same (run the whole VB-bearing test surface under one harness-owned
        # command + a machine-readable report, then reconcile executed+passed with
        # static VB coverage). To wire it: set verify_campaign=VerifyCampaignSpec(
        # command_template="python -m pytest {test_root} --junitxml={report} ...",
        # report_relpath=".codd/verify/pytest-junit.xml",
        # report_format="pytest-junit-xml") and register a ``pytest-junit-xml``
        # adapter in codd.coverage_execution_coherence (parse <testcase
        # classname=.. name=..> + <skipped>/<failure>/<error> children → executed +
        # passed files/cases). Python's pytest collects unit AND e2e in ONE run by
        # default (no e2e-only-script split like npm), so today's verify path
        # already executes the whole VB surface — making this a coherence-hardening
        # ENHANCEMENT, not a false-green gap. Until wired, None ⇒ the coherence gate
        # is a strict NO-OP for Python and the EXISTING verify-stage gates
        # (import_coherence / e2e_contract_coherence + the VB coverage/authenticity
        # gates) remain its backstop, UNCHANGED.
        verify_campaign=None,
    )


# ── Python test-execution environment provisioner (realizer, Python zone) ──
#
# The Python射影 of "toolchain materialization": realize the ``install_mode:
# editable`` layout the profile DECLARES but no realizer previously EXECUTED — a
# project-local venv with a verifier-pinned pytest + ``pip install -e .``. Unlike
# the npm/cmake channels (whose materialized deps are found via cwd inheritance —
# node_modules / build/), Python's channel is ENVIRONMENT inheritance: the
# interpreter identity IS the dependency store. So the realizer additionally
# records a state artifact naming the REAL absolute bin dir; the language-free
# verify executor prepends it to PATH (its ``exec_path_prepend`` seam) so an
# UNCHANGED ``python``/``pytest`` argv resolves to this venv.
#
# The pytest pin is the VERIFIER's own toolchain (the SUT's pyproject is NEVER
# edited — same principle as the TS ``_TYPESCRIPT_TOOLCHAIN_PROFILE`` vitest pin),
# a bounded current-major range so a hypothetical breaking pytest major cannot
# silently change the harness verifier.
_PYTHON_TEST_TOOLCHAIN_PIN = "pytest>=8,<10"

#: Where the provisioner records its realized environment. ``.codd/**`` is
#: harness_owned (python.yaml), so this never trips the orphan/propagate gates. The
#: NAME + SHAPE are language-neutral on purpose (a plain list of dirs) — the verify
#: runner that reads it stays free of any venv/interpreter knowledge.
_ENV_PROVISION_STATE_RELPATH = ".codd/verify/exec_env.json"

#: Bounded budget for the venv build + editable install (a cold pip resolve can be
#: slow but must not hang the gate forever). Mirrors the node install preflight cap.
_ENV_PROVISION_TIMEOUT_SECONDS = 900.0


@dataclass(frozen=True)
class EnvProvisionResult:
    """Outcome of realizing a stack's test-execution environment.

    ``ok`` is False ONLY on a genuine environment/toolchain build failure (an
    ``environment_build_error`` — NOT a code defect, so the repair loop must never
    edit source over it). A stack with no provisioner realizer is ``ok=True,
    action="unsupported"`` (a benign NO-OP, never a failure). ``state_path`` is the
    project-relative path of the recorded state artifact when one was written.
    """

    ok: bool
    action: str  # "provisioned" | "up_to_date" | "unsupported" | "failed"
    detail: str
    state_path: str | None = None


def _venv_bin_dir(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts" if os.name == "nt" else "bin")


def _venv_interpreter(venv_dir: Path) -> Path:
    return _venv_bin_dir(venv_dir) / ("python.exe" if os.name == "nt" else "python")


def _env_probe_ok(interpreter: Path, package_name: str) -> bool:
    """True iff the venv interpreter can import BOTH the project package and pytest.

    The idempotency probe: a run that passes it needs no reinstall. Package name is
    the harness-owned CANONICAL name (from :func:`resolve_canonical_package_name`,
    carried on the resolved :class:`LayoutProfile`), never a hardcoded literal.
    """
    if not interpreter.exists() or not package_name:
        return False
    try:
        completed = subprocess.run(  # noqa: S603 — trusted argv (harness), shell=False
            [str(interpreter), "-c", f"import {package_name}, pytest"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _record_env_provision_state(
    project_root: Path, venv_dir: Path, *, action: str, detail: str
) -> EnvProvisionResult:
    """Write the REAL absolute bin dir + interpreter path to the state artifact."""
    bin_dir = _venv_bin_dir(venv_dir)
    interpreter = _venv_interpreter(venv_dir)
    state = {
        # A list of REAL absolute dirs for the verify executor to prepend to PATH.
        "path_prepend_dirs": [str(bin_dir.resolve())],
        "interpreter": str(interpreter.resolve()) if interpreter.exists() else str(interpreter),
    }
    state_file = project_root / _ENV_PROVISION_STATE_RELPATH
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return EnvProvisionResult(
        ok=True, action=action, detail=detail, state_path=_ENV_PROVISION_STATE_RELPATH
    )


def _env_build_error(detail: str) -> EnvProvisionResult:
    """A code-NON-addressable environment_build_error (never a repair-loop target)."""
    return EnvProvisionResult(ok=False, action="failed", detail=detail)


def _provision_python_env(
    project_root: Path, *, profile: LayoutProfile
) -> EnvProvisionResult:
    """Realize a project-local editable venv so verify can run unattended.

    Steps (idempotent):
      (i)   create ``<project>/.venv`` with THIS harness interpreter
            (``sys.executable -m venv`` — never assumes a ``python`` on PATH; a venv
            always provides ``bin/python`` even on a python3-only host, which is
            what closes 根因1);
      (ii)  install the verifier-pinned pytest + ``pip install -e .`` INTO the venv
            (the SUT manifest is untouched; the pin is the verifier's own toolchain);
      (iii) a probe short-circuit: if the venv already imports the package + pytest,
            skip the install (re-record the state artifact and return);
      (iv)  record the state artifact (real absolute bin dir + interpreter) under
            ``.codd/`` so the verify executor can PATH-prepend it.

    A create/install/probe failure is an honest ``environment_build_error`` (the
    barrier maps it to a StageError; the repair loop never edits code over it).
    """
    venv_dir = project_root / ".venv"
    interpreter = _venv_interpreter(venv_dir)
    package_name = profile.package_name

    # (iii) idempotent probe: an already-usable env is a no-op (still re-record state).
    if _env_probe_ok(interpreter, package_name):
        return _record_env_provision_state(
            project_root, venv_dir, action="up_to_date",
            detail="existing venv already imports the package + pytest",
        )

    # (i) create the venv with the harness interpreter (no PATH `python` assumed).
    if not interpreter.exists():
        try:
            created = subprocess.run(  # noqa: S603 — trusted argv, shell=False
                [sys.executable, "-m", "venv", str(venv_dir)],
                capture_output=True,
                text=True,
                timeout=_ENV_PROVISION_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return _env_build_error(f"could not create virtualenv at {venv_dir}: {exc}")
        if created.returncode != 0:
            return _env_build_error(
                f"virtualenv creation exited {created.returncode}: "
                f"{(created.stderr or created.stdout or '').strip()[-2000:]}"
            )

    # (ii) install the verifier-pinned pytest + the editable project package.
    try:
        installed = subprocess.run(  # noqa: S603 — trusted argv, shell=False
            [
                str(interpreter), "-m", "pip", "install",
                "--disable-pip-version-check",
                _PYTHON_TEST_TOOLCHAIN_PIN, "-e", ".",
            ],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=_ENV_PROVISION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return _env_build_error(
            f"editable install exceeded {_ENV_PROVISION_TIMEOUT_SECONDS:g}s"
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _env_build_error(f"editable install could not run: {exc}")
    if installed.returncode != 0:
        return _env_build_error(
            f"editable install exited {installed.returncode}: "
            f"{(installed.stderr or installed.stdout or '').strip()[-2000:]}"
        )

    # (iv) prove the built env before recording it green.
    if not _env_probe_ok(interpreter, package_name):
        return _env_build_error(
            "provisioned venv cannot import the project package + pytest after install "
            "(the environment build did not produce a usable interpreter)"
        )
    return _record_env_provision_state(
        project_root, venv_dir, action="provisioned",
        detail=f"provisioned .venv (editable install + pinned pytest) for {package_name}",
    )


# Realizer capability id → env provisioner (Contract Kernel v2.71 seam). The dispatch
# key is the profile's ``legacy_project_types.env_provisioner`` realizer id — a
# harness-policy capability name, NOT a language name. A stack with no such key (every
# other language + Go) resolves to no provisioner = a strict NO-OP.
_EnvProvisioner = Callable[..., EnvProvisionResult]
#: Realizer capability id (must match the profile YAML ``legacy_project_types``).
_ENV_PROVISIONER_PY_VENV = "venv-editable-pip-provisioner-v1"
_ENV_PROVISIONERS_BY_REALIZER: dict[str, _EnvProvisioner] = {
    _ENV_PROVISIONER_PY_VENV: _provision_python_env,
}


def provision_project_env(
    project_root: Path | str,
    *,
    language: str | None,
    project_name: str | None,
    source_dirs: Any = None,
    test_dirs: Any = None,
    config: Any = None,
) -> EnvProvisionResult:
    """Realize the stack's test-execution environment, or a NO-OP if unsupported.

    PROFILE-DRIVEN dispatch (Contract Kernel v2.71): the runtime ``language`` is
    gated by the profile's ``legacy_project_types`` bridge and the provisioner is
    selected by the bridge's ``env_provisioner`` realizer id — never a ``language ==``
    literal. A stack that declares no ``env_provisioner`` (every other language + Go),
    an unknown/unaccepted language, or a stack with no resolvable layout profile is a
    strict NO-OP (``ok=True, action="unsupported"`` — never a failure, never a venv).
    All paths derive from the resolved :class:`LayoutProfile` (canonical package name
    + roots); nothing is hardcoded.
    """
    root = Path(project_root)
    realizer_id = _legacy_realizer_id(language, "env_provisioner")
    provisioner = _ENV_PROVISIONERS_BY_REALIZER.get(realizer_id) if realizer_id else None
    if provisioner is None:
        return EnvProvisionResult(
            ok=True,
            action="unsupported",
            detail=f"no env provisioner for language {language!r}",
        )
    profile = resolve_layout_profile(
        language=language,
        project_name=project_name,
        source_dirs=source_dirs,
        test_dirs=test_dirs,
        config=config,
        project_root=root,
    )
    if profile is None:
        return EnvProvisionResult(
            ok=True,
            action="unsupported",
            detail=f"no layout profile resolved for language {language!r}",
        )
    return provisioner(root, profile=profile)


def _typescript_layout_profile(
    *,
    project_name: str | None,
    source_dirs: Any,
    test_dirs: Any,
    config: Any = None,  # noqa: ARG001 — accepted for builder-signature parity; TS uses path resolution, not a named package.
    project_root: Path | None = None,  # noqa: ARG001 — parity (see above).
) -> LayoutProfile:
    """TypeScript (node) profile: a path-relative ``src`` layout, npm-installed.

    Unlike Python's named-package layout, TypeScript modules resolve by PATH
    (``import { x } from "./foo"`` / ``from "../src/foo"``), so there is no
    ``<source_root>/<package_name>`` subdir — ``package_root == source_root``
    and the test import policy is ``relative`` (path imports, not a bare
    basename and not a Python-style package namespace). The runner is vitest by
    default (the generated stack's choice; the ensurer respects an author's jest
    setup). ``install_mode="node"`` selects the BLOCKING dependency-install
    preflight (npm/pnpm/yarn/bun) rather than Python's editable install.
    """
    package_name = normalize_package_name(project_name)
    source_root = _first_clean_dir(source_dirs, "src")
    test_root = _first_clean_dir(test_dirs, "tests")
    return LayoutProfile(
        language="typescript",
        package_name=package_name,
        source_root=source_root,
        package_root=source_root,
        test_root=test_root,
        runner="vitest",
        install_mode="node",
        test_import_policy="relative",
        requires_package_init=False,
        requires_test_init=False,
        # OPTIONAL CLI SURFACE — the runnable command-line entry point that BACKS the
        # "cli" e2e modality (SHARED constructor: identical id + flag as Python's).
        # ``paths=()``: TS resolves by PATH and the scaffolder materializes no single
        # canonical entry file to subtract — the surface exists so a pure-library TS
        # project (CLI excluded at plan intake) downgrades e2e cli→none via
        # :func:`effective_e2e_modality` instead of generating an invokeCli/
        # tempWorkspace CLI e2e suite for an entry point that never exists.
        optional_surfaces=(_runnable_entrypoint_surface(),),
        # IMPLEMENT-TIME ORACLE (TS) — ``tsc --noEmit`` is a compiler-class
        # coherence oracle: a pure typecheck (no emit) that statically proves
        # every ``import``/symbol across src + tests + e2e + helpers resolves. Run
        # at implement-time (after all units exist, before verify) it catches the
        # src↔src and test↔helper symbol incoherence (TS2305/2724/2459) while the
        # SUT can still edit test files — BEFORE verify's auto-repair is
        # scope-blocked from doing so. ``--no-install`` keeps it offline-honest:
        # the blocking node-install preflight (``requires_node_install``) is what
        # materializes ``tsc`` + deps; a missing install must surface as an
        # environment error, never an implicit network fetch. Scope is certified
        # against ``tsconfig.json`` before a green result is trusted.
        implement_oracle=ImplementOracleSpec(
            command="npx --no-install tsc --noEmit",
            kind="compiler",
            scope=OracleScopeSpec(require_source_root=True, require_test_root=True),
            requires_node_install=True,
        ),
        # MANIFEST↔LOCK COHERENCE (TS/npm) — the harness owns the test-toolchain
        # dep VERSIONS (vitest/typescript/@types/node). At implement-end the SUT's
        # package.json is reconciled to these, then ``npm install
        # --package-lock-only`` refreshes the lock to match, so verify's frozen
        # ``npm ci`` passes honestly (it never re-resolves; it just verifies). See
        # :func:`codd.dependency_lock_coherence.finalize_dependency_lock_coherence`.
        toolchain_dependencies=_TYPESCRIPT_TOOLCHAIN_PROFILE,
        # VERIFY CAMPAIGN (TS/vitest) — the harness-owned canonical verification
        # command. It runs the WHOLE ``{test_root}`` (unit AND e2e — NOT a SUT
        # ``test:unit`` script) under ``vitest run`` with the JSON reporter, so the
        # coverage-execution coherence gate can reconcile which VB-covering test
        # FILES actually executed + passed against the static VB coverage map. This
        # is what closes the codex14 false-green (28 e2e-only VBs "covered" but the
        # detected ``test:unit`` never ran them). ``--no-install`` keeps it offline-
        # honest (the blocking node-install preflight materializes vitest). vitest
        # COLLECTION (incl. the ``.e2e.*`` convention) is owned by the scaffolded
        # ``vitest.config.ts`` ``test.include``; the positional ``{test_root}``
        # filters to the project's test tree. The report lands under ``.codd/`` —
        # a harness artifact, never the SUT's. See
        # :mod:`codd.coverage_execution_coherence`.
        verify_campaign=VerifyCampaignSpec(
            command_template=(
                "npx --no-install vitest run {test_root} "
                "--reporter=json --outputFile={report}"
            ),
            report_relpath=".codd/verify/vitest-report.json",
            report_format="vitest-json",
            requires_node_install=True,
        ),
    )


# Realizer capability id → legacy layout-profile builder (Contract Kernel v2.71).
# The dispatch key is the HARNESS-POLICY capability id a profile declares in its
# ``legacy_project_types.layout_builder`` — NOT a language name. The builder carries
# the runner/install/oracle/toolchain/verify POLICY the declarative profile does not
# yet model (full externalization = the v3.0.0 gate; this increment removes only the
# language-name DISPATCH). A new stack maps to an existing builder by declaring that
# realizer id, or registers a new builder here under a new id.
_LayoutProfileBuilder = Callable[..., LayoutProfile]
#: Realizer capability ids (must match the profile YAML ``legacy_project_types``).
_LAYOUT_BUILDER_PY_SRC_PACKAGE = "src-package-pytest-editable-layout-v1"
_LAYOUT_BUILDER_TS_NPM = "npm-vitest-tsc-layout-v1"
_LAYOUT_BUILDERS_BY_REALIZER: dict[str, _LayoutProfileBuilder] = {
    _LAYOUT_BUILDER_PY_SRC_PACKAGE: _python_layout_profile,
    _LAYOUT_BUILDER_TS_NPM: _typescript_layout_profile,
}


def _legacy_bridged_names(field: str) -> list[str]:
    """Sorted runtime names whose profile bridges ``field`` to a known realizer.

    Walks the language registry and, for every profile declaring a
    ``legacy_project_types`` block whose ``field`` realizer id is registered,
    contributes that block's ``accepted_names`` (the EXACT historical names — NOT
    the wider registry aliases). This is the PROFILE-DRIVEN, byte-identical
    replacement for the old hardcoded ``sorted(_DICT)`` over language-name keys.
    """
    realizers = {
        "layout_builder": _LAYOUT_BUILDERS_BY_REALIZER,
        "test_runner_ensurer": _TEST_RUNNER_ENSURERS_BY_REALIZER,
    }.get(field, {})
    names: set[str] = set()
    try:
        from codd.languages.registry import default_registry

        for profile in default_registry.all_profiles():
            extra = getattr(profile, "extra", None)
            block = extra.get(_LEGACY_BRIDGE_KEY) if isinstance(extra, Mapping) else None
            if not isinstance(block, Mapping):
                continue
            realizer = block.get(field)
            if not realizer or str(realizer) not in realizers:
                continue
            for n in block.get("accepted_names") or ():
                if str(n).strip():
                    names.add(str(n).strip().lower())
    except Exception:  # noqa: BLE001 — registry optional; empty list is the safe degrade.
        return []
    return sorted(names)


def supported_layout_profile_languages() -> list[str]:
    """Runtime names with a harness-owned layout profile (deterministic topology).

    PROFILE-DRIVEN (Contract Kernel v2.71): the union of every profile's
    ``legacy_project_types.accepted_names`` whose ``layout_builder`` realizer is
    registered — never a hardcoded language-name table. Byte-identical to the legacy
    dict's key set (``node``/``python``/``typescript``): only the EXPLICITLY accepted
    names, not the wider registry aliases, and not Go (no legacy bridge).
    """
    return _legacy_bridged_names("layout_builder")


# ═══════════════════════════════════════════════════════════
# Generic LayoutProfile synthesizer (greenfield ② — opt-in, language-free)
# ═══════════════════════════════════════════════════════════
#
# A stack with NO per-language legacy builder (Python/TS each have one) can still get a
# harness-owned LayoutProfile WITHOUT adding a per-language builder: when its declarative
# LanguageProfile OPTS IN (``greenfield_synthesis: true``), the roots + implement-oracle +
# verify-campaign are SYNTHESIZED directly from the YAML. This mirrors the implement-oracle's
# ``_resolve_registry_oracle`` (legacy None → YAML-direct synthesis) — the same "型紙".
# Anti-false-green: a non-opted-in / unknown / data-incomplete stack → ``None`` (the
# conservative NO-OP, never a wrong-layout default).
#
# OPT-IN GATE (data-driven; the core NEVER branches on a language NAME): presence of the
# ``greenfield_synthesis`` key (a top-level YAML key → preserved in LanguageProfile.extra)
# authorizes BOTH this synthesizer AND the generic-template scaffolder. A profile WITHOUT it
# keeps its prior behaviour — crucially Go (whose ``scaffold.adapter`` is ``generic-template``
# too, but which has no legacy builder) stays a strict NO-OP, preserving
# ``test_unknown_stack_is_noop``. Added ONLY to csharp.yaml this increment.
_GREENFIELD_SYNTHESIS_KEY = "greenfield_synthesis"


def _greenfield_synthesis_opted_in(lang_profile: Any) -> bool:
    """True IFF the resolved LanguageProfile declares the opt-in synthesis key truthy."""
    extra = getattr(lang_profile, "extra", None)
    if not isinstance(extra, Mapping):
        return False
    return bool(extra.get(_GREENFIELD_SYNTHESIS_KEY))


def synthesize_implement_oracle_spec(lang_profile: Any) -> ImplementOracleSpec | None:
    """Build the gate's :class:`ImplementOracleSpec` from a LanguageProfile's modeled
    ``implement_oracle`` declaration.

    SHARED by BOTH the synthesized :class:`LayoutProfile` (below) AND
    ``implement_oracle._resolve_registry_oracle`` so the two cannot DRIFT: a synthesized
    profile whose ``implement_oracle`` differed from the registry path would silently
    change — or stop — the oracle for that stack (the exact regression this extraction
    prevents). Mirrors the declaration's ``kind`` so the SAME kind-routed dispatch in
    ``implement_oracle._run_oracle_command`` runs the registered adapter; the ``command``
    is a SENTINEL (the real argv/cwd come from ``lang_profile.layout`` / ``.commands`` at
    run time). Scope policy matches the registry path exactly: an ``adapter`` kind
    certifies BOTH roots; a ``command``/``composite`` kind certifies the source root (the
    adapter owns its own test-scope certification). ``None`` when no oracle is declared.
    """
    oracle_decl = getattr(lang_profile, "implement_oracle", None)
    if oracle_decl is None:
        return None
    lang_id = str(getattr(lang_profile, "id", "") or "")
    kind = getattr(oracle_decl, "kind", None)
    if kind == "adapter":
        return ImplementOracleSpec(
            command=f"{lang_id}-adapter",  # sentinel; kind dispatch runs the contract path
            kind="adapter",
            scope=OracleScopeSpec(require_source_root=True, require_test_root=True),
            requires_node_install=False,
        )
    return ImplementOracleSpec(
        command=f"{lang_id}-{kind}",  # sentinel; kind dispatch runs the contract path
        kind=str(kind or "composite"),
        scope=OracleScopeSpec(require_source_root=True, require_test_root=False),
        requires_node_install=False,
    )


def _synthesize_verify_campaign(lang_profile: Any) -> VerifyCampaignSpec | None:
    """Synthesize a :class:`VerifyCampaignSpec` from the profile's verify command +
    report(s) (additively generalized 2026-07-02 to N reports per command).

    Design A (argv form + report(s)): the campaign ARGV is the resolved
    ``commands[<verify.command>].argv``; the report path(s) + adapter(s) come from
    the top-level ``verify.reports`` (plural) or ``verify.report`` (singular) —
    ``format`` = ``report.adapter`` (the id the runner-report registry resolves on,
    e.g. ``dotnet-trx``) or ``report.format`` as a fallback. Exactly ONE report
    synthesizes the legacy flat-field form (byte-identical to every profile before
    this generalization — TS/C#/C++/Go's extension point all declare one report);
    TWO OR MORE synthesize the general ``steps`` form as ONE step carrying every
    declared report (Java: one ``mvn verify`` invocation, two report roots).
    ``capture`` and ``optional`` are carried through in BOTH cases (the legacy path
    previously silently dropped ``capture`` — harmless while every synthesized
    campaign was file-written, but would have silently broken a future stdout-
    reporting language the day it opts in). ``None`` when the profile declares no
    verify block / reports / adapter / argv, or a declared report is incomplete
    (missing path or format) — the coverage gate then stays a strict NO-OP for the
    stack — never a silent green for an unreadable campaign.
    """
    verify = getattr(lang_profile, "verify", None)
    if verify is None:
        return None
    resolver = getattr(verify, "resolved_reports", None)
    reports = tuple(resolver()) if callable(resolver) else ()
    if not reports:
        return None
    command_id = getattr(verify, "command", None)
    commands = getattr(lang_profile, "commands", {}) or {}
    cmd = commands.get(command_id) if command_id else None
    argv = tuple(str(a) for a in (getattr(cmd, "argv", ()) or ())) if cmd is not None else ()
    if not argv:
        return None

    campaign_reports: list[CampaignReportSpec] = []
    for report in reports:
        relpath = _norm_rel(getattr(report, "path", "") or "")
        report_format = getattr(report, "adapter", None) or getattr(report, "format", None)
        if not relpath or not report_format:
            return None  # an incomplete report declaration voids the whole campaign
        campaign_reports.append(
            CampaignReportSpec(
                relpath=relpath,
                format=str(report_format),
                capture=getattr(report, "capture", None),
                optional=bool(getattr(report, "optional", False)),
            )
        )

    # A single, fully-vanilla report (no capture, not optional — every profile
    # today) synthesizes the legacy flat-field form, byte-identical to every
    # profile before this generalization. A report that declares ``capture`` or
    # ``optional`` — which the flat fields have no slot for — or two-or-more
    # reports (Java: one ``mvn verify`` invocation, two report roots) synthesizes
    # the general ``steps`` form instead, so neither flag is ever silently dropped.
    only = campaign_reports[0]
    if len(campaign_reports) == 1 and only.capture is None and not only.optional:
        return VerifyCampaignSpec(
            report_relpath=only.relpath,
            report_format=only.format,
            command_argv=argv,
            requires_node_install=False,
        )
    return VerifyCampaignSpec(
        steps=(VerifyCampaignStep(reports=tuple(campaign_reports), command_argv=argv),),
        requires_node_install=False,
    )


def _synthesize_toolchain_dependencies(lang_profile: Any) -> ToolchainDependencyProfile | None:
    """Synthesize a :class:`ToolchainDependencyProfile` IFF the stack declares a LOCKFILE.

    DATA-DRIVEN (no language name): only a stack whose
    ``toolchain.dependency_integrity_files`` lists a ``kind: lock`` entry has a
    manifest↔lock coherence contract. C# declares NONE (like Python) → ``None`` — an
    HONEST NO-OP, not a gap (there is no frozen-lock install to diverge). A lock-bearing
    stack synthesizes its profile from the declared manifest + lock filename(s) +
    package-manager reconcile/materialize commands; ecosystem-specific digest inputs
    (workspace globs / config files / manager-version probe) default EMPTY (single
    package, no extra inputs) so the contract generalizes without npm-specific literals.
    """
    toolchain = getattr(lang_profile, "toolchain", None)
    if toolchain is None:
        return None
    dep_files = tuple(getattr(toolchain, "dependency_integrity_files", ()) or ())
    lock_filenames = tuple(
        _norm_rel(getattr(f, "path", "") or "")
        for f in dep_files
        if str(getattr(f, "kind", "")).strip().lower() == "lock" and getattr(f, "path", None)
    )
    if not lock_filenames:
        return None  # C#: no lockfile → honest NO-OP (Python-equivalent).
    manifest = getattr(toolchain, "manifest", None)
    manifest_filename = _norm_rel(getattr(manifest, "path", "") or "") if manifest is not None else ""
    pm = getattr(toolchain, "package_manager", None)

    def _argv_command(key: str) -> str | None:
        raw = pm.get(key) if hasattr(pm, "get") else None
        argv = list(raw.get("argv") or []) if hasattr(raw, "get") else []
        return " ".join(str(a) for a in argv) if argv else None

    refresh = _argv_command("reconcile_command")
    materialize = _argv_command("materialize_command")
    if not manifest_filename or not refresh:
        return None  # incomplete declaration — decline (never a half-specified contract).
    return ToolchainDependencyProfile(
        deps=(),
        manifest_filename=manifest_filename,
        lock_filenames=lock_filenames,
        lock_refresh_command=refresh,
        materialize_command=materialize,
        frozen_install_command=materialize or refresh,
        completeness_refresh_command=None,
        workspace_manifest_globs=(),
        config_filenames=(),
        package_manager_version_command=None,
    )


def _synthesize_layout_profile_from_language(
    *,
    language: str | None,
    project_name: str | None,
    source_dirs: Any = None,  # noqa: ARG001 — parity; a synthesized stack reads its roots from the declarative profile.
    test_dirs: Any = None,  # noqa: ARG001 — parity (see above).
    config: Any = None,  # noqa: ARG001 — parity; the package name is project-derived (no named-package config tier here).
    project_root: Path | None = None,  # noqa: ARG001 — parity.
) -> LayoutProfile | None:
    """Synthesize a :class:`LayoutProfile` from a declarative LanguageProfile (opt-in).

    The general, per-language-builder-FREE fallback ``resolve_layout_profile`` uses when a
    stack has no legacy builder: when the profile OPTS IN (``greenfield_synthesis: true``)
    the harness-owned topology + verify-campaign + implement-oracle are built DIRECTLY from
    the YAML (``layout`` / ``commands`` / ``verify`` / ``implement_oracle``). ``None`` when
    the language is unknown, does not opt in, or lacks the data the synthesis needs (a
    conservative NO-OP — never a wrong-layout default).

    Field derivation (design): ``source_root`` = ``layout.source_sets[0].root``;
    ``test_root`` = ``layout.test_sets[0].root``; ``package_root`` from
    ``layout.package_root.kind``: ``none`` / ``path_root`` (FLAT) → ``= source_root``;
    ``named_package`` (Python) and ``path_package`` (C#) → the declared ``path`` sub-dir
    (``src/<pkg>``, with ``{package_name}`` substituted). Only ``named_package`` carries the
    Python ``__init__`` + package-absolute import contract; EVERY other kind (including the
    nested ``path_package``) sets ``requires_*_init=False`` + ``test_import_policy !=
    "package_absolute"`` so the Python-specific import-coherence checks are strict NO-OPs
    (anti-false-RED). The dispatch is on the data ``kind`` only — never a language name.
    """
    lang_profile = _resolve_kernel_language_profile(language)
    if lang_profile is None or not _greenfield_synthesis_opted_in(lang_profile):
        return None
    layout = getattr(lang_profile, "layout", None)
    if layout is None:
        return None
    source_sets = tuple(getattr(layout, "source_sets", ()) or ())
    test_sets = tuple(getattr(layout, "test_sets", ()) or ())
    if not source_sets or not test_sets:
        return None
    source_root = _norm_rel(getattr(source_sets[0], "root", "") or "")
    test_root = _norm_rel(getattr(test_sets[0], "root", "") or "")
    if not source_root or not test_root:
        return None

    # Casing is DATA the profile declares (``naming.package_case``: lower default | pascal
    # case-preserving) — the core branches on the VALUE inside normalize_package_name, NEVER
    # on the language name here. C# declares ``pascal`` so ``TextKit`` survives end-to-end
    # (package_name → the nested package_root → scaffold/routing/harness_owned all cased);
    # Python/TS/Go declare nothing → lower (unchanged).
    package_name = normalize_package_name(
        project_name, package_case=lang_profile.package_case
    )
    pkg = getattr(layout, "package_root", None)
    pkg_kind = str(getattr(pkg, "kind", "none") or "none")
    # A declared ``{package_name}``-bearing path nests the package (``src/<pkg>``); absent a
    # path the package IS the source root (a flat layout). Computed once for the two nesting
    # kinds below so they cannot drift.
    raw_pkg_path = _norm_rel(getattr(pkg, "path", "") or "")
    nested_pkg_root = (
        raw_pkg_path.replace("{package_name}", package_name) if raw_pkg_path else source_root
    )
    if pkg_kind == "named_package":
        # PYTHON: a nested package dir (``src/<pkg>``) that ALSO carries the ``__init__.py`` +
        # package-absolute import contract.
        package_root = nested_pkg_root
        requires_package_init = True
        requires_test_init = True
        test_import_policy = "package_absolute"
    elif pkg_kind == "path_package":
        # A nested package dir (``src/<pkg>``, e.g. C#'s library-project dir) with NO language
        # package-import contract: the toolchain compiles by DIRECTORY (the .NET SDK's project-
        # dir-relative ``**/*.cs`` glob), there is no ``__init__.py`` and no package-absolute
        # import rule. package_root is the declared sub-path — so the routing accept-list +
        # the scaffold agree on the SAME lib dir — while source_root stays the source-set root
        # (the parent ``src`` the glob + scaffold use). The ONLY difference from the flat
        # ``none``/``path_root`` case below is the nested package_root: the Python import-
        # coherence init/policy checks MUST stay strict NO-OPs (anti-false-RED).
        package_root = nested_pkg_root
        requires_package_init = False
        requires_test_init = False
        test_import_policy = "relative"
    else:
        # ``none`` / ``path_root``: FLAT — no nested package subdir → package_root ==
        # source_root. The Python import-coherence init/policy checks MUST NOT engage
        # (a flat/root layout has no __init__/package-absolute contract):
        #   * requires_*_init=False  → _check_missing_init / _check_source_outside_package
        #     are strict NO-OPs (no false-RED on a missing __init__.py).
        #   * test_import_policy != "package_absolute" → the bare-basename check skips.
        package_root = source_root
        requires_package_init = False
        requires_test_init = False
        test_import_policy = "relative"

    pm = getattr(getattr(lang_profile, "toolchain", None), "package_manager", None)
    runner = str(pm.get("id")) if hasattr(pm, "get") and pm.get("id") else "generic"

    # SOURCE PLACEMENTS: one spec per declared source set, carrying its OWN
    # normalized root + globs. ``reference_base`` is decided by the first-party
    # import rule DATA (never a language name): True iff the rule is
    # ``include_path_prefix`` and its ``base`` equals this set's root — i.e. a
    # reference is the file's path relative to that root (C++ ``include/``). Every
    # other stack's rule (java ``source_root_package`` / csharp
    # ``root_namespace_prefix`` / js/ts ``path_alias`` / python ``package_name``)
    # fails the ``include_path_prefix`` guard, so only C++ can set it today.
    imports_spec = getattr(lang_profile, "imports", None)
    imports_data = getattr(imports_spec, "data", None)
    first_party: Mapping[str, Any] = {}
    if isinstance(imports_data, Mapping):
        fp = imports_data.get("first_party")
        if isinstance(fp, Mapping):
            first_party = fp
    fp_rule = str(first_party.get("rule") or "")
    fp_base = _norm_rel(str(first_party.get("base") or ""))
    source_placements = tuple(
        SourcePlacementSpec(
            root=set_root,
            file_globs=tuple(getattr(s, "file_globs", ()) or ()),
            reference_base=(
                fp_rule == "include_path_prefix" and bool(fp_base) and fp_base == set_root
            ),
        )
        for s in source_sets
        if (set_root := _norm_rel(getattr(s, "root", "") or ""))
    )

    return LayoutProfile(
        language=str(getattr(lang_profile, "id", "") or language or ""),
        package_name=package_name,
        source_root=source_root,
        package_root=package_root,
        test_root=test_root,
        runner=runner,
        install_mode="none",
        test_import_policy=test_import_policy,
        requires_package_init=requires_package_init,
        requires_test_init=requires_test_init,
        implement_oracle=synthesize_implement_oracle_spec(lang_profile),
        toolchain_dependencies=_synthesize_toolchain_dependencies(lang_profile),
        verify_campaign=_synthesize_verify_campaign(lang_profile),
        source_placements=source_placements,
        # OPTIONAL CLI SURFACE — the shared CLI-backing runnable-entrypoint, so a
        # synthesized stack (C#/C++/Java/JS) that EXCLUDES the CLI at plan intake
        # downgrades its e2e cli→none exactly like Python/TS — the SAME per-profile
        # DATA flag, in ONE locus covering every greenfield-synthesis stack (no
        # language-name branch). ``paths=()``: the generic-template scaffolder
        # materializes no single canonical entry file to subtract; the surface is
        # the e2e-modality marker + intake classification target. Inert when the
        # project type's e2e modality is not "cli" (the join never matches).
        optional_surfaces=(_runnable_entrypoint_surface(),),
    )


def _read_excluded_surface_ids(config: Any) -> frozenset[str]:
    """The plan-persisted set of excluded deliverable-surface ids (or empty)."""
    try:
        section = config.get("deliverable") if isinstance(config, Mapping) else None
        raw = section.get("excluded_surfaces") if isinstance(section, Mapping) else None
        if isinstance(raw, (list, tuple)):
            return frozenset(str(x).strip() for x in raw if str(x).strip())
    except Exception:  # noqa: BLE001 — no/garbage config ⇒ nothing excluded (legacy).
        pass
    return frozenset()


def resolve_layout_profile(
    *,
    language: str | None,
    project_name: str | None,
    source_dirs: Any = None,
    test_dirs: Any = None,
    config: Any = None,
    project_root: Path | None = None,
) -> LayoutProfile | None:
    """Resolve the :class:`LayoutProfile` for a stack, or ``None`` if unsupported.

    PROFILE-DRIVEN dispatch (Contract Kernel v2.71): the runtime ``language`` value is
    resolved to its :class:`LanguageProfile`, the profile's ``legacy_project_types``
    bridge gates it (``language`` must be in ``accepted_names`` — so support stays
    byte-identical: ``node`` resolves to the canonical TypeScript profile, but the
    wider aliases ``ts``/``js``/``py`` do NOT), then the builder is selected by the
    bridge's ``layout_builder`` realizer id — NOT a ``language ==``/dict-keyed-by-name
    literal. A blank/unknown/unaccepted language, or a profile with no bridge (Go),
    resolves to ``None`` (the conservative degradation — the caller stays on its
    no-profile path, never a wrong-layout default).

    Every path the builder produces is derived from ``project_name`` + the configured
    ``scan.*_dirs`` — there are NO hardcoded ``src``/``tests``/``<package>`` literals
    outside the per-stack builder's documented defaults.

    ``config`` (the loaded project config) and ``project_root`` are optional and feed
    the harness-owned CANONICAL package-name resolution (config override >
    derive-from-actual single package > project-name default) for stacks that use a
    named package (Python). Stacks that resolve by path (TypeScript) ignore them.
    Omitting both preserves the pure project-name default (back-compat).
    """
    realizer_id = _legacy_realizer_id(language, "layout_builder")
    builder = _LAYOUT_BUILDERS_BY_REALIZER.get(realizer_id) if realizer_id else None
    if builder is None:
        # No per-language legacy builder → try the OPT-IN generic synthesizer (a stack
        # whose declarative profile declares ``greenfield_synthesis: true``; csharp this
        # increment). A non-opted-in / unknown language (Go) → None (conservative NO-OP).
        profile = _synthesize_layout_profile_from_language(
            language=language,
            project_name=project_name,
            source_dirs=source_dirs,
            test_dirs=test_dirs,
            config=config,
            project_root=project_root,
        )
    else:
        profile = builder(
            project_name=project_name,
            source_dirs=source_dirs,
            test_dirs=test_dirs,
            config=config,
            project_root=project_root,
        )
    # Thread the project's excluded optional-surface ids onto the profile in ONE
    # place (all consumers then read ``profile.excluded_surface_paths()`` — no
    # signature changes). Intersect with what the profile actually declares so an
    # unknown/foreign id is ignored; empty (default) leaves the profile untouched.
    if profile is not None and profile.optional_surfaces:
        excluded = _read_excluded_surface_ids(config) & frozenset(
            s.id for s in profile.optional_surfaces
        )
        if excluded:
            profile = dataclasses.replace(profile, excluded_surface_ids=excluded)
    return profile


def harness_owned_output_paths(
    config: Any = None,
    *,
    project_root: Path | None = None,
) -> frozenset[str]:
    """The CLOSED, profile-declared set of harness-owned scaffold paths for a config.

    Resolves the active stack's :class:`LayoutProfile` from the loaded project
    ``config`` (``project.language`` / ``project.name`` / ``scan.*_dirs``) and
    returns its :meth:`LayoutProfile.harness_owned_scaffold_paths` as a normalized
    frozenset. These are the files the harness SCAFFOLD owns — created by the
    harness, NEVER authored by the SUT/AI (e.g. a C# ``src/<Pkg>/<Pkg>.csproj``
    whose dependency manifest lives UNDER ``src/``, the first stack whose manifest
    is classified SOURCE by the kind gate).

    This is the SINGLE authority both the task deriver
    (:func:`codd.llm.plan_deriver.exclude_harness_owned_outputs`) and the implement
    kind/completeness contract (greenfield pipeline) consult to decide which
    declared outputs are scaffold-satisfied and therefore impose NO AI-produced
    deliverable obligation. It is a CLOSED set keyed on the profile's OWN ownership
    DECLARATION — never a path prefix, never a ``language ==`` literal — so an
    arbitrary path can never claim exemption, and a real SOURCE file the profile
    does not own (``src/<Pkg>/Foo.cs``) is never in it (anti-false-green).

    Fail-closed: any resolution failure (no/unknown/unaccepted language, no
    profile, a builder error) returns the EMPTY set — i.e. NO exemption, the
    strict gate stays in force — never a wrong / over-broad set.
    """
    try:
        section = config.get("project") if isinstance(config, Mapping) else None
        language = section.get("language") if isinstance(section, Mapping) else None
        project_name = section.get("name") if isinstance(section, Mapping) else None
        scan = config.get("scan") if isinstance(config, Mapping) else None
        source_dirs = scan.get("source_dirs") if isinstance(scan, Mapping) else None
        test_dirs = scan.get("test_dirs") if isinstance(scan, Mapping) else None
        profile = resolve_layout_profile(
            language=language,
            project_name=project_name,
            source_dirs=source_dirs,
            test_dirs=test_dirs,
            config=config,
            project_root=project_root,
        )
        if profile is None:
            return frozenset()
        owned = frozenset(
            norm for rel in profile.harness_owned_scaffold_paths() if (norm := _norm_rel(rel))
        )
        # Ownership carve-out: the package FACADE file's topology is harness-owned
        # (the scaffold creates it) but its CONTENT is SUT-authored (public-API
        # re-exports). Subtract it from the OBLIGATION authority so the deriver
        # keeps it in a task's declared outputs and the kind/completeness contract
        # imposes the source obligation on it — while the fence/orphan authority
        # (harness_owned_scaffold_paths) still lists it, so the scaffold still
        # creates it and it is never flagged an orphan. STRICT SUBSET: a stack
        # whose profile declares no facade (facade_output_paths() == ()) is
        # unchanged.
        facade = frozenset(
            norm for rel in profile.facade_output_paths() if (norm := _norm_rel(rel))
        )
        return owned - facade
    except Exception:  # noqa: BLE001 — fail-closed: no exemption, strict gate.
        return frozenset()


# ═══════════════════════════════════════════════════════════
# Deterministic scaffold (harness creates topology; model fills contents)
# ═══════════════════════════════════════════════════════════
#
# The scaffold realizes a :class:`LayoutProfile` on disk: pyproject (package
# metadata + pytest config, NO ``pythonpath="."``), ``<package_root>/__init__``,
# ``<package_root>/__main__``, and the test ``__init__`` the profile requires.
# It is CREATE-ONLY and IDEMPOTENT: it never moves or rewrites model-authored
# files (that would violate "harness owns structure, not contents" and could
# corrupt author intent — an EXISTING incoherent build must instead FAIL the
# coherence gate honestly and be REGENERATED, not silently healed). A valid
# Claude-consistent layout is therefore left byte-for-byte alone; a second call
# is a no-op.

_PYTEST_INI_SECTION = "[tool.pytest.ini_options]"
_PYPROJECT_FILENAME = "pyproject.toml"

#: Package-init marker so a created ``__init__.py`` is recognised as scaffold
#: (idempotent) and never an author file we might clobber on re-augment.
_SCAFFOLD_INIT_DOC = '"""Package root (scaffolded by codd greenfield)."""\n'


@dataclass(frozen=True)
class ScaffoldResult:
    """Outcome of realizing a layout profile on disk."""

    language: str
    created: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    detail: str = ""


#: Scaffolder realizer capability ids (Contract Kernel v2.71 — must match the
#: profile YAML ``legacy_project_types.scaffolder``). Harness-policy names, NOT
#: language names: the dispatch key for which legacy scaffolder realizes a profile.
_SCAFFOLDER_PY_SRC_PACKAGE = "pyproject-src-package-scaffold-v1"
_SCAFFOLDER_TS_NPM = "npm-tsconfig-vitest-scaffold-v1"

#: The scaffold ADAPTER id (a ``LanguageProfile.scaffold.adapter`` value, NOT a legacy
#: realizer id) that selects the GENERIC-TEMPLATE scaffolder (design (c)). Gated by the
#: opt-in key so Go (adapter is generic-template too, but no opt-in) stays a NO-OP.
_SCAFFOLD_ADAPTER_GENERIC_TEMPLATE = "generic-template"


def _generic_template_scaffold_spec(
    language: str | None,
) -> tuple[tuple[Mapping[str, Any], ...], dict[str, str], tuple[str, ...]] | None:
    """The ``(templates, defaults, owned_files)`` for an OPTED-IN generic-template stack.

    ``None`` unless the resolved profile (a) opts in (``greenfield_synthesis``) AND
    (b) declares ``scaffold.adapter == "generic-template"`` AND (c) declares ≥1 template.
    This is what keeps Go — whose ``scaffold.adapter`` is generic-template too but which
    does NOT opt in — a strict NO-OP (preserving ``test_unknown_stack_is_noop``). Selection
    is by the adapter id + the opt-in key, NEVER a language-name literal. ``defaults``
    (``scaffold.defaults`` — the per-stack substitution-variable defaults) is read from the
    profile's ``raw`` view (the ScaffoldSpec dataclass models only adapter/owned_files/
    templates; ``raw`` is the documented later-phase escape hatch).
    """
    lang_profile = _resolve_kernel_language_profile(language)
    if lang_profile is None or not _greenfield_synthesis_opted_in(lang_profile):
        return None
    scaffold = getattr(lang_profile, "scaffold", None)
    if scaffold is None:
        return None
    adapter = str(getattr(scaffold, "adapter", "") or "").strip().lower()
    if adapter != _SCAFFOLD_ADAPTER_GENERIC_TEMPLATE:
        return None
    templates = tuple(getattr(scaffold, "templates", ()) or ())
    if not templates:
        return None
    owned = tuple(str(p) for p in (getattr(scaffold, "owned_files", ()) or ()))
    defaults: dict[str, str] = {}
    raw = getattr(lang_profile, "raw", None)
    if isinstance(raw, Mapping):
        raw_scaffold = raw.get("scaffold")
        if isinstance(raw_scaffold, Mapping):
            raw_defaults = raw_scaffold.get("defaults")
            if isinstance(raw_defaults, Mapping):
                defaults = {str(k): str(v) for k, v in raw_defaults.items()}
    return templates, defaults, owned


def _generic_template_substitutions(
    profile: LayoutProfile, defaults: Mapping[str, str]
) -> dict[str, str]:
    """``{var}`` → value: ``scaffold.defaults`` overlaid with the resolved
    ``package_name`` (the harness-owned name always wins over a default)."""
    subst = {str(k): str(v) for k, v in defaults.items()}
    subst["package_name"] = profile.package_name
    return subst


def _probe_host_toolchain_version(argv: tuple[str, ...], pattern: str) -> int | None:
    """Best-effort host-toolchain MAJOR-version probe (``javac -version`` → ``17``).

    Runs the profile-declared probe argv (10s timeout), searches ``pattern``
    (first group = the major) across stdout+stderr (``javac -version`` printed to
    stderr before JDK 9, stdout after), and returns the integer major — or
    ``None`` on ANY failure (missing binary, timeout, no match, non-integer
    group). The caller treats ``None`` as "keep the declared default"
    (fail-open): the probe can only ever LOWER a doomed default, never introduce
    a new failure mode.
    """
    try:
        proc = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(pattern, f"{proc.stdout}\n{proc.stderr}")
    if match is None:
        return None
    try:
        return int(match.group(1))
    except (IndexError, ValueError):
        return None


def _generic_template_host_clamps(
    language: str | None,
) -> dict[str, Mapping[str, Any]]:
    """The profile's ``scaffold.host_version_clamps`` mapping (raw view), or ``{}``.

    Read from ``raw`` like ``scaffold.defaults`` (the ScaffoldSpec dataclass
    models only adapter/owned_files/templates; ``raw`` is the documented
    later-phase escape hatch).
    """
    lang_profile = _resolve_kernel_language_profile(language)
    raw = getattr(lang_profile, "raw", None) if lang_profile is not None else None
    if isinstance(raw, Mapping):
        raw_scaffold = raw.get("scaffold")
        if isinstance(raw_scaffold, Mapping):
            clamps = raw_scaffold.get("host_version_clamps")
            if isinstance(clamps, Mapping):
                return {
                    str(k): v for k, v in clamps.items() if isinstance(v, Mapping)
                }
    return {}


def _resolve_host_clamped_defaults(
    defaults: Mapping[str, str], clamps: Mapping[str, Mapping[str, Any]]
) -> tuple[dict[str, str], list[str]]:
    """Clamp scaffold DEFAULTS to the host toolchain — the environment-continuation
    projection of toolchain materialization (v3.15.0 lineage) applied to the
    generic-template scaffold's substitution variables.

    For each profile-declared clamp key: probe the host (profile-declared
    argv+pattern); when the probed major is LOWER than the declared default's
    leading integer, replace the default with the probed major (min semantics —
    a declared ``21`` on a JDK-17 host scaffolds ``17``; a declared ``11`` on a
    JDK-17 host stays ``11``). A probe failure or a non-numeric declared value
    keeps the declared default (fail-open). Returns ``(resolved defaults,
    human-readable clamp notes)`` — the notes ride the scaffold detail so a
    clamp is never silent. CONTENT-only: the harness-owned PATH declaration
    (:meth:`LayoutProfile.harness_owned_scaffold_paths`) substitutes UNCLAMPED
    defaults, so a clamped variable must never appear in a template ``path``.
    """
    out = dict(defaults)
    notes: list[str] = []
    for key, spec in (clamps or {}).items():
        declared = out.get(key)
        if declared is None:
            continue
        declared_match = re.match(r"\s*(\d+)", str(declared))
        if declared_match is None:
            continue  # non-numeric default → this clamp cannot reason about it
        declared_major = int(declared_match.group(1))
        argv = tuple(str(a) for a in (spec.get("probe_argv") or ()) if str(a).strip())
        if not argv:
            continue
        pattern = str(spec.get("version_pattern") or r"(\d+)")
        probed = _probe_host_toolchain_version(argv, pattern)
        if probed is None or probed >= declared_major:
            continue
        out[key] = str(probed)
        notes.append(
            f"host-clamped {key} {declared}→{probed} (probe: {' '.join(argv)})"
        )
    return out, notes


def _apply_template_substitutions(text: str, subst: Mapping[str, str]) -> str:
    """Replace each ``{key}`` token with its value (replace-based, NOT ``str.format`` — so
    a template's literal braces never raise; mirrors the oracle gate's ``{package_name}``
    replace)."""
    out = str(text)
    for key, value in subst.items():
        out = out.replace("{" + key + "}", str(value))
    return out


def scaffold_layout(
    project_root: Path | str,
    profile: LayoutProfile,
) -> ScaffoldResult:
    """Create the profile's topology (create-only, idempotent, non-clobbering).

    Returns the relative paths created vs. skipped (already present).

    PROFILE-DRIVEN (Contract Kernel v2.71): the scaffolder is selected by the
    resolved :class:`LanguageProfile`'s legacy-bridge ``scaffolder`` realizer id (a
    harness-policy capability name — Python package-topology vs TS config), NOT a
    ``profile.language ==`` literal. A stack with no legacy bridge but an OPT-IN
    ``generic-template`` scaffold (csharp) routes to :func:`_scaffold_generic_template`.
    A stack with neither (Go — generic-template adapter but no opt-in; or an
    unknown/unaccepted language) is a strict no-op (the conservative degradation —
    never a wrong scaffolder writing a wrong layout).
    """
    scaffolder_id = _legacy_realizer_id(profile.language, "scaffolder")
    if scaffolder_id == _SCAFFOLDER_PY_SRC_PACKAGE:
        return _scaffold_python(Path(project_root), profile)
    if scaffolder_id == _SCAFFOLDER_TS_NPM:
        return _scaffold_typescript(Path(project_root), profile)
    # Generic-template scaffolder (opt-in, data-driven): no legacy scaffolder, but the
    # profile opts in AND declares ``scaffold.adapter == "generic-template"``. Go is
    # excluded (no opt-in key → spec is None → strict NO-OP).
    spec = _generic_template_scaffold_spec(profile.language)
    if spec is not None:
        return _scaffold_generic_template(Path(project_root), profile, spec)
    return ScaffoldResult(language=profile.language, detail="no scaffolder for stack")


def _scaffold_generic_template(
    project_root: Path,
    profile: LayoutProfile,
    spec: tuple[tuple[Mapping[str, Any], ...], dict[str, str], tuple[str, ...]],
) -> ScaffoldResult:
    """Realize a profile's ``scaffold.templates`` on disk (create-only / idempotent /
    non-clobber — the SAME contract as ``_scaffold_python``'s ``_ensure_file``).

    Each template ``{path, content_template}`` has ``{package_name}`` + the per-stack
    ``scaffold.defaults`` substituted (replace-based), then is written IFF absent. The
    template SET + the substitution variables come from the YAML, never a per-language
    code branch — a new generic-template stack is one YAML profile + the opt-in key.
    """
    templates, defaults, _owned = spec
    defaults, clamp_notes = _resolve_host_clamped_defaults(
        defaults, _generic_template_host_clamps(profile.language)
    )
    subst = _generic_template_substitutions(profile, defaults)
    created: list[str] = []
    skipped: list[str] = []

    def _ensure_file(rel: str, content: str) -> None:
        norm = _norm_rel(rel)
        if not norm:
            return
        target = project_root / norm
        if target.exists():
            skipped.append(norm)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        created.append(norm)

    for tmpl in templates:
        if not hasattr(tmpl, "get"):
            continue
        path = _apply_template_substitutions(str(tmpl.get("path", "") or ""), subst)
        if not path:
            continue
        content = _apply_template_substitutions(str(tmpl.get("content_template", "") or ""), subst)
        _ensure_file(path, content)

    detail = (
        f"generic-template: {len(created)} created, {len(skipped)} skipped "
        f"(package={profile.package_name})"
    )
    if clamp_notes:
        detail += "; " + "; ".join(clamp_notes)
    return ScaffoldResult(
        language=profile.language,
        created=tuple(created),
        skipped=tuple(skipped),
        detail=detail,
    )


def _scaffold_python(project_root: Path, profile: LayoutProfile) -> ScaffoldResult:
    created: list[str] = []
    skipped: list[str] = []

    def _ensure_file(rel: str, content: str) -> None:
        target = project_root / rel
        if target.exists():
            skipped.append(rel)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        created.append(rel)

    package_dir = profile.package_root
    # __init__ makes <source_root>/<package_name> an importable package; __main__
    # gives ``python -m <package_name>`` an entry point. Both package-relative.
    if profile.requires_package_init:
        _ensure_file(f"{package_dir}/__init__.py", _SCAFFOLD_INIT_DOC)
        _excluded = {p for rel in profile.excluded_surface_paths() if (p := _norm_rel(rel))}
        if _norm_rel(f"{package_dir}/__main__.py") not in _excluded:
            _ensure_file(
                f"{package_dir}/__main__.py",
                (
                    '"""Console entry point (scaffolded by codd greenfield)."""\n\n'
                    "def main() -> int:\n"
                    "    raise NotImplementedError\n\n\n"
                    'if __name__ == "__main__":\n'
                    "    raise SystemExit(main())\n"
                ),
            )
    if profile.requires_test_init:
        _ensure_file(f"{profile.test_root}/__init__.py", "")

    runner_result = _ensure_python_test_runner(
        project_root,
        profile=profile,
    )
    if runner_result.action in ("created", "augmented") and runner_result.path is not None:
        created.append(_PYPROJECT_FILENAME)
    elif runner_result.action == "present":
        skipped.append(_PYPROJECT_FILENAME)

    detail = (
        f"package={profile.package_root}, test_root={profile.test_root}, "
        f"runner={runner_result.action}"
    )
    return ScaffoldResult(
        language="python",
        created=tuple(created),
        skipped=tuple(skipped),
        detail=detail,
    )


# ── TypeScript (node) scaffold ───────────────────────────────
#
# Realizes the TS profile on disk: a strict ``tsconfig.json`` and the
# ``test``/``build`` package.json scripts, both CREATE-ONLY / non-clobbering.
# The single hard contract is MODULE-SYSTEM COHERENCE: the scaffolded tsconfig
# (``NodeNext`` resolution), package.json (``"type": "module"`` when we create
# it), and the vitest runner must agree so the model-generated ``import``
# statements resolve at typecheck AND at runtime. A package.json the model
# already authored is the authority for ``type``/module system — we only ADD
# missing scripts there, never rewrite its module config.

_TSCONFIG_FILENAME = "tsconfig.json"
_PACKAGE_JSON_FILENAME = "package.json"
_VITEST_CONFIG_FILENAME = "vitest.config.ts"

#: A strict, NodeNext tsconfig. ``noEmit`` keeps ``tsc`` a pure typechecker
#: (the executed ``tsc --noEmit`` gate); NodeNext module+resolution makes ESM
#: ``import "./mod.js"`` specifiers resolve consistently under node + vitest.
_SCAFFOLD_TSCONFIG: dict[str, Any] = {
    "//": "Scaffolded by codd greenfield (create-only). Strict + NodeNext for module coherence.",
    "compilerOptions": {
        "target": "ES2022",
        "module": "NodeNext",
        "moduleResolution": "NodeNext",
        "strict": True,
        "esModuleInterop": True,
        "skipLibCheck": True,
        "forceConsistentCasingInFileNames": True,
        "noEmit": True,
        "resolveJsonModule": True,
    },
}

#: Scaffolded ``vitest.config.ts`` (create-only). vitest's DEFAULT
#: ``test.include`` is ``**/*.{test,spec}.?(c|m)[jt]s?(x)`` — it does NOT match
#: the ``.e2e.*`` e2e convention codex emits and this harness ROUTES to verify
#: nodes (see ``find_spec_files`` in the vitest provider). Declaring the include
#: here — the IDIOMATIC vitest mechanism; the CLI has no ``--include`` flag —
#: makes FIND and RUN agree so a routed ``.e2e.ts`` is actually collected. Kept a
#: strict superset of vitest's default so nothing already collected is excluded.
_SCAFFOLD_VITEST_CONFIG = (
    "// Scaffolded by codd greenfield (create-only). Collection include must\n"
    "// cover the .e2e.* e2e convention, not just vitest's default .test/.spec.\n"
    'import { defineConfig } from "vitest/config";\n'
    "\n"
    "export default defineConfig({\n"
    "  test: {\n"
    "    include: [\n"
    '      "**/*.{test,spec}.{ts,tsx,cts,mts,js,jsx,cjs,mjs}",\n'
    '      "**/*.e2e.{ts,tsx,cts,mts,js,jsx,cjs,mjs}",\n'
    "    ],\n"
    "  },\n"
    "});\n"
)


def _scaffold_typescript(project_root: Path, profile: LayoutProfile) -> ScaffoldResult:
    created: list[str] = []
    skipped: list[str] = []

    tsconfig = project_root / _TSCONFIG_FILENAME
    if tsconfig.exists():
        skipped.append(_TSCONFIG_FILENAME)
    else:
        source_glob = f"{profile.source_root}/**/*"
        test_glob = f"{profile.test_root}/**/*"
        payload = dict(_SCAFFOLD_TSCONFIG)
        payload["include"] = [source_glob, test_glob]
        tsconfig.parent.mkdir(parents=True, exist_ok=True)
        tsconfig.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        created.append(_TSCONFIG_FILENAME)

    # vitest.config.ts owns COLLECTION (test.include): without it vitest's default
    # include skips the routed ``.e2e.*`` convention → 0-collected hard fail.
    vitest_config = project_root / _VITEST_CONFIG_FILENAME
    if vitest_config.exists():
        skipped.append(_VITEST_CONFIG_FILENAME)
    else:
        vitest_config.parent.mkdir(parents=True, exist_ok=True)
        vitest_config.write_text(_SCAFFOLD_VITEST_CONFIG, encoding="utf-8")
        created.append(_VITEST_CONFIG_FILENAME)

    runner_result = _ensure_typescript_test_runner(project_root, profile=profile)
    if runner_result.action in ("created", "augmented"):
        created.append(_PACKAGE_JSON_FILENAME)
    elif runner_result.action == "present":
        skipped.append(_PACKAGE_JSON_FILENAME)

    detail = (
        f"source_root={profile.source_root}, test_root={profile.test_root}, "
        f"runner={runner_result.action} ({runner_result.detail})"
    )
    return ScaffoldResult(
        language=profile.language,
        created=tuple(created),
        skipped=tuple(skipped),
        detail=detail,
    )


def _read_json_or_none(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def detect_node_package_manager(project_root: Path) -> str:
    """Detect the node package manager from the present lockfile.

    Returns one of ``pnpm`` / ``yarn`` / ``bun`` / ``npm``. ``npm`` is the
    default when no lockfile is present (an ``npm install`` then CREATES
    ``package-lock.json``). Lockfile presence — not a global tool guess —
    drives this so the BLOCKING install preflight uses the project's own
    declared manager and never reaches for an implicit global ``npx``.
    """
    root = Path(project_root)
    if (root / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (root / "yarn.lock").is_file():
        return "yarn"
    if (root / "bun.lockb").is_file() or (root / "bun.lock").is_file():
        return "bun"
    return "npm"


def node_install_command(project_root: Path) -> str:
    """The BLOCKING dependency-install command for the detected manager.

    Uses the reproducible ``ci``/``--frozen-lockfile`` form when a lockfile
    exists, else the plain install (which creates the lock). This is run as a
    verify PREFLIGHT — NOT as the advisory ``_ensure_test_runner`` — so an
    install failure becomes an honest ``environment_build_error`` rather than a
    swallowed warning.
    """
    root = Path(project_root)
    manager = detect_node_package_manager(root)
    has_lock = {
        "pnpm": (root / "pnpm-lock.yaml").is_file(),
        "yarn": (root / "yarn.lock").is_file(),
        "bun": (root / "bun.lockb").is_file() or (root / "bun.lock").is_file(),
        "npm": (root / "package-lock.json").is_file(),
    }[manager]
    if manager == "pnpm":
        return "pnpm install --frozen-lockfile" if has_lock else "pnpm install"
    if manager == "yarn":
        return "yarn install --frozen-lockfile" if has_lock else "yarn install"
    if manager == "bun":
        return "bun install --frozen-lockfile" if has_lock else "bun install"
    return "npm ci" if has_lock else "npm install"


@dataclass(frozen=True)
class EnsureTestRunnerResult:
    """Outcome of ensuring a stack's test-runner config exists.

    ``action`` is one of:
      * ``"created"``   — a new config file was written.
      * ``"augmented"`` — an existing config file gained the test-runner section.
      * ``"present"``   — a runnable test setup already existed (left untouched).
      * ``"unsupported"`` — no ensurer for this language/stack (no-op).
    """

    language: str
    action: str
    path: Path | None = None
    detail: str = ""


def _normalize_dirs(dirs: Any) -> list[str]:
    """Normalize a ``scan.*_dirs`` value to clean, in-root relative roots.

    RC-6 path-escape jail: ``scan.source_dirs`` / ``scan.test_dirs`` are
    user-controllable (codd.yaml) and feed the layout ``source_root`` /
    ``test_root`` (via :func:`_first_clean_dir`). A ``..`` segment or an absolute
    path that escapes the (relative) layout root must NOT survive — otherwise the
    generated package, the pytest ``pythonpath``, and the scaffold writes resolve
    OUTSIDE the project tree (the upstream root cause behind RC-1/2/3).

    Each entry is resolved *purely* (lexical ``..`` collapse against a virtual
    root — no filesystem touch, so this stays a pure-path normalizer usable
    without a real project_root) and kept only when it stays at or below that
    virtual root. ``.`` / ``./x`` and interior ``a/b/../c`` that stay in-root are
    normalized to their clean relative form; ``../x``, ``..``, ``a/../../b`` and
    absolute out-of-root paths are dropped (silently excluded, never crash).
    """
    if not isinstance(dirs, (list, tuple)):
        return []
    roots: list[str] = []
    for item in dirs:
        text = str(item).strip().replace("\\", "/")
        if not text:
            continue
        normalized = _confine_relative_dir(text)
        if normalized and normalized not in roots:
            roots.append(normalized)
    return roots


def _confine_relative_dir(text: str) -> str | None:
    """Lexically resolve ``text`` and return it only if it stays in-root.

    Pure path logic (no filesystem access): collapses ``.``/``..`` segments and
    returns the clean POSIX relative path, or ``None`` when the path is absolute
    out-of-root or climbs above the root via ``..``. ``"."``/empty collapse to
    ``None`` (no usable root). Mirrors the resolve-and-confine rule of
    ``path_safety.resolve_project_path`` but without needing a concrete root on
    disk (these values name layout roots, not yet-existing files).
    """
    is_absolute = text.startswith("/")
    parts: list[str] = []
    for segment in text.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not parts:
                # Climbs above the virtual root → escape.
                return None
            parts.pop()
            continue
        parts.append(segment)
    if not parts:
        return None
    rel = "/".join(parts)
    if is_absolute:
        # An absolute path is in-root only if, treated as root-relative, it does
        # not escape — but an absolute ``scan.*_dirs`` root names a location
        # OUTSIDE the (relative) layout tree, so it is never a valid layout root.
        return None
    return rel


def _toml_str_array(values: list[str]) -> str:
    """Render a list of strings as a TOML inline array (deterministic order)."""
    inner = ", ".join('"' + value.replace('"', '\\"') + '"' for value in values)
    return "[" + inner + "]"


def _render_pytest_ini_section(*, testpaths: list[str], source_root: str) -> str:
    """Build a minimal, valid ``[tool.pytest.ini_options]`` TOML block.

    ANTI-FALSE-GREEN (A-core): ``pythonpath`` is the SOURCE ROOT ONLY — never
    ``"."``. The prior fix put ``pythonpath = [<src>, "."]`` so tests ran without
    an installed package, but ``"."`` (plus a flat ``src`` layout) let a test
    resolve a source module by BARE BASENAME (``import todo_store``) even when the
    source uses package-relative imports — an environment-dependent FALSE GREEN.
    With the harness-owned src-layout PACKAGE (``<source_root>/<package_name>/``),
    a source-root-only ``pythonpath`` makes the package-absolute import
    ``from <package_name>.<mod> import ...`` resolve while a bare ``import <mod>``
    does NOT (there is no top-level ``<source_root>/<mod>.py``). Combined with
    ``--import-mode=importlib`` (no ``sys.path[0]`` insertion of the test's own
    dir), an accidental flat import stays a real failure. The package metadata
    (see :func:`_python_editable_metadata`) additionally makes ``pip install -e .``
    work for real deployment, but is not required for tests to run. ``addopts``
    also disables the cache plugin so a read-only checkout never fails on
    ``.pytest_cache``.
    """
    lines = [_PYTEST_INI_SECTION]
    if testpaths:
        lines.append(f"testpaths = {_toml_str_array(testpaths)}")
    clean_root = source_root.strip().replace("\\", "/").strip("/")
    if clean_root:
        lines.append(f"pythonpath = {_toml_str_array([clean_root])}")
    lines.append('addopts = "-p no:cacheprovider --import-mode=importlib"')
    return "\n".join(lines) + "\n"


# ── Python build-backend awareness (the harness OWNS packaging topology) ──
#
# The harness owns the repository TOPOLOGY and the PACKAGING manifest fields that
# realize it (where the wheel/editable install finds the package). It does NOT own
# the model's domain intent — ``[project]``, dependencies, ``[tool.pytest]``, the
# library logic. So packaging reconciliation is BACKEND-DETECTED and edits ONLY
# the backend's packaging sub-table: never a setuptools table in a hatch project
# or vice-versa (that would produce an incoherent manifest the build can't honor).

_BACKEND_SETUPTOOLS = "setuptools"
_BACKEND_HATCHLING = "hatchling"


def _detect_build_backend(text: str) -> str | None:
    """Classify ``[build-system] build-backend`` → ``setuptools`` / ``hatchling`` / None.

    ``None`` means "no build-system declared yet" (a fresh file we will create
    with a default backend). An UNKNOWN/unrecognized backend string returns the
    raw token so the caller can decline to edit (the manifest gate then fails
    honestly rather than the harness guessing a packaging table for a backend it
    doesn't understand — anti-false-green).
    """
    parsed = _parse_pyproject_toml(text)
    if not isinstance(parsed, dict):
        # Unparseable-but-nonempty: surface as an opaque non-None so the caller
        # leaves it for the parse/manifest gates rather than editing blind.
        return "" if text.strip() else None
    build_system = parsed.get("build-system")
    backend = build_system.get("build-backend") if isinstance(build_system, dict) else None
    if not isinstance(backend, str) or not backend.strip():
        # A pyproject without build-system but with content: unknown backend.
        return None if not text.strip() else ""
    token = backend.strip().lower()
    if token.startswith("setuptools"):
        return _BACKEND_SETUPTOOLS
    if token.startswith("hatchling") or token.startswith("hatch"):
        return _BACKEND_HATCHLING
    return token  # a real but unsupported backend (flit/pdm/poetry/…) — decline.


def _parse_pyproject_toml(text: str) -> dict[str, Any] | None:
    if not text.strip():
        return {}
    try:  # tomllib is stdlib from 3.11.
        import tomllib as parser  # type: ignore[import-not-found]
    except ModuleNotFoundError:  # pragma: no cover - py<3.11
        try:
            import tomli as parser  # type: ignore[import-not-found, no-redef]
        except ModuleNotFoundError:
            return None
    try:
        loaded = parser.loads(text)
    except Exception:  # noqa: BLE001 - a broken pyproject is the parse gate's job.
        return None
    return loaded if isinstance(loaded, dict) else None


def _python_packaging_metadata(profile: LayoutProfile, *, backend: str) -> str:
    """Full ``[build-system]`` + ``[project]`` + packaging table for a NEW file.

    Backend-correct: setuptools gets ``[tool.setuptools.packages.find] where``;
    hatchling gets ``[tool.hatch.build.targets.wheel] packages``. Used ONLY when
    no pyproject exists — never to clobber author metadata.
    """
    pkg = profile.package_name
    src = profile.source_root
    if backend == _BACKEND_HATCHLING:
        return (
            "[build-system]\n"
            'requires = ["hatchling"]\n'
            'build-backend = "hatchling.build"\n\n'
            "[project]\n"
            f'name = "{pkg}"\n'
            'version = "0.0.0"\n\n'
            "[tool.hatch.build.targets.wheel]\n"
            f'packages = ["{src}/{pkg}"]\n'
        )
    return (
        "[build-system]\n"
        'requires = ["setuptools>=61"]\n'
        'build-backend = "setuptools.build_meta"\n\n'
        "[project]\n"
        f'name = "{pkg}"\n'
        'version = "0.0.0"\n\n'
        "[tool.setuptools]\n"
        f'package-dir = {{"" = "{src}"}}\n\n'
        "[tool.setuptools.packages.find]\n"
        f'where = ["{src}"]\n'
    )


# Back-compat alias: the prior name for the setuptools-only metadata builder.
def _python_editable_metadata(profile: LayoutProfile) -> str:  # pragma: no cover - thin alias
    return _python_packaging_metadata(profile, backend=_BACKEND_SETUPTOOLS)


def _upsert_toml_table(text: str, header: str, body_lines: list[str]) -> str:
    """Replace (or append) a single TOML table by HEADER, preserving all other text.

    Surgical and byte-faithful for everything OUTSIDE ``[header]``: finds the
    table that starts with ``[header]`` and rewrites only its non-blank body up to
    the next table header (a line starting with ``[``), leaving ``[project]``,
    deps, ``[tool.pytest]``, comments, ordering, AND blank-line separators
    untouched. Appends a fresh table when the header is absent.

    IDEMPOTENT: when the existing table's non-blank body already equals
    ``body_lines``, the text is returned unchanged (so a second ensure is a true
    no-op). ``body_lines`` are the lines UNDER the header (no header line).
    """
    lines = text.splitlines()
    header_norm = header.strip()
    out: list[str] = []
    i = 0
    replaced = False
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        if not replaced and stripped == header_norm:
            # Capture the existing table body (until the next header / EOF),
            # separating meaningful lines from a trailing blank-line block so we
            # can preserve the original spacing to the next table.
            i += 1
            existing_body: list[str] = []
            while i < n and not lines[i].lstrip().startswith("["):
                existing_body.append(lines[i])
                i += 1
            # Trailing blank lines that separate this table from the next one.
            trailing_blanks: list[str] = []
            while existing_body and existing_body[-1].strip() == "":
                trailing_blanks.insert(0, existing_body.pop())
            if [ln.strip() for ln in existing_body if ln.strip()] == [
                ln.strip() for ln in body_lines
            ]:
                # Already coherent — re-emit verbatim (idempotent no-op).
                out.append(header)
                out.extend(existing_body)
                out.extend(trailing_blanks)
            else:
                out.append(header)
                out.extend(body_lines)
                out.extend(trailing_blanks)
            replaced = True
            continue
        out.append(lines[i])
        i += 1
    if not replaced:
        if out and out[-1].strip() != "":
            out.append("")
        out.append(header)
        out.extend(body_lines)
    rendered = "\n".join(out)
    if not rendered.endswith("\n"):
        rendered += "\n"
    return rendered


def _ensure_python_packaging(
    project_root: Path,
    *,
    profile: LayoutProfile,
) -> EnsureTestRunnerResult:
    """ALWAYS reconcile the harness-owned PACKAGING fields, backend-correctly.

    This is split from the pytest ensurer so that "the model owns test config"
    (``[tool.pytest]``) NEVER suppresses "the harness owns packaging coherence".
    Behavior, by build-backend detected in an EXISTING pyproject:

      * **setuptools** → force ``[tool.setuptools] package-dir = {"" = "<src>"}``
        and ``[tool.setuptools.packages.find] where = ["<src>"]`` so the package
        at ``<package_root>`` is the installed package. Edits ONLY those two
        sub-tables; ``[project]``, deps, ``[tool.pytest]`` are byte-for-byte
        preserved.
      * **hatchling** → force ``[tool.hatch.build.targets.wheel] packages =
        ["<src>/<pkg>"]``. Never writes a setuptools table into a hatch project.
      * **unknown/unsupported backend** (flit/pdm/poetry/…) → DECLINE to edit
        (return ``present``); the manifest gate fails honestly rather than the
        harness guessing a packaging table it cannot reason about (anti-false-
        green). A NEW file (no pyproject) is created with a default setuptools
        backend + coherent packaging.
    """
    pyproject = project_root / _PYPROJECT_FILENAME
    src = profile.source_root
    pkg = profile.package_name

    if not pyproject.exists():
        return EnsureTestRunnerResult(
            language="python",
            action="present",
            detail="no pyproject.toml yet; packaging written by the runner-ensure step",
        )

    text = _read_text_or_empty(pyproject)
    backend = _detect_build_backend(text)

    if backend == _BACKEND_SETUPTOOLS:
        new_text = _upsert_toml_table(text, "[tool.setuptools]", [f'package-dir = {{"" = "{src}"}}'])
        new_text = _upsert_toml_table(
            new_text, "[tool.setuptools.packages.find]", [f'where = ["{src}"]']
        )
        if new_text != text:
            pyproject.write_text(new_text, encoding="utf-8")
            return EnsureTestRunnerResult(
                language="python",
                action="augmented",
                path=pyproject,
                detail=(
                    f"reconciled setuptools packaging (package-dir/where = ['{src}'], "
                    f"package={profile.package_root})"
                ),
            )
        return EnsureTestRunnerResult(
            language="python", action="present", detail="setuptools packaging already coherent"
        )

    if backend == _BACKEND_HATCHLING:
        new_text = _upsert_toml_table(
            text, "[tool.hatch.build.targets.wheel]", [f'packages = ["{src}/{pkg}"]']
        )
        if new_text != text:
            pyproject.write_text(new_text, encoding="utf-8")
            return EnsureTestRunnerResult(
                language="python",
                action="augmented",
                path=pyproject,
                detail=f"reconciled hatchling packaging ([tool.hatch...wheel] packages = ['{src}/{pkg}'])",
            )
        return EnsureTestRunnerResult(
            language="python", action="present", detail="hatchling packaging already coherent"
        )

    # Unknown/unsupported backend (a non-empty token) OR an unparseable file:
    # DECLINE to edit. The manifest gate is the honest backstop; the harness
    # never writes a packaging table for a backend it cannot reason about.
    return EnsureTestRunnerResult(
        language="python",
        action="present",
        detail=(
            f"build-backend {backend!r} is not setuptools/hatchling; packaging left "
            "untouched (manifest gate is the backstop)"
        ),
    )


def _ensure_python_test_runner(
    project_root: Path,
    *,
    profile: LayoutProfile,
) -> EnsureTestRunnerResult:
    """Ensure a RUNNABLE pyproject: harness-owned PACKAGING + a pytest section.

    Two SEPARATE concerns, so the model owning ``[tool.pytest]`` never suppresses
    harness-owned packaging coherence (the prior all-or-nothing bug):

      1. **Packaging** (:func:`_ensure_python_packaging`) — ALWAYS reconciled,
         backend-correctly (setuptools ``where``/``package-dir`` or hatchling
         ``[tool.hatch...wheel] packages``), even when a strong pytest config
         exists. Edits ONLY the harness-owned packaging sub-tables.
      2. **Pytest section** — appended ONLY when no strong pytest config
         (pytest.ini / setup.cfg / ``[tool.pytest]``) and no other test command
         is wired up. A strong/AI-authored pytest config is authoritative and
         left byte-for-byte.

    A brand-new file (no pyproject, no other runner) is created with both a
    backend-correct packaging block and a pytest section.
    """
    from codd.test_detection import _has_strong_pytest_config, detect_test_command

    pyproject = project_root / _PYPROJECT_FILENAME

    # ── Concern 1: packaging coherence — runs UNCONDITIONALLY on an existing file.
    packaging = _ensure_python_packaging(project_root, profile=profile)

    # ── Concern 2: pytest section.
    # Strong pytest config present → model owns test config; do NOT append a pytest
    # section. Packaging was STILL reconciled above (the split). Report the
    # packaging outcome so the harness still owns topology.
    if _has_strong_pytest_config(project_root):
        if packaging.action == "augmented":
            return packaging
        return EnsureTestRunnerResult(
            language="python",
            action="present",
            detail="a strong pytest config already exists; packaging checked, both coherent",
        )

    detected = detect_test_command(project_root)
    pyproject_text = _read_text_or_empty(pyproject) if pyproject.exists() else ""
    bare_pyproject_only = pyproject.exists() and "[tool.pytest" not in pyproject_text
    if detected is not None and not bare_pyproject_only:
        # A non-pytest runner is the author's choice; respect it. Packaging was
        # still reconciled above when a pyproject existed.
        if packaging.action == "augmented":
            return packaging
        return EnsureTestRunnerResult(
            language="python",
            action="present",
            detail=f"a non-pytest test command is already detectable ({detected}); left untouched",
        )

    section = _render_pytest_ini_section(
        testpaths=[profile.test_root], source_root=profile.source_root
    )

    if pyproject.exists():
        # Packaging already reconciled in-place above; re-read so we append the
        # pytest section onto the reconciled text.
        existing = _read_text_or_empty(pyproject)
        addition = section
        # Add packaging metadata only when the file declared NO build/project at
        # all (an exotic bare file); the backend-aware packaging ensurer already
        # handled the normal case in-place. Default backend = setuptools.
        if "[project]" not in existing and "[build-system]" not in existing:
            addition = _python_packaging_metadata(profile, backend=_BACKEND_SETUPTOOLS) + "\n" + section
        separator = "" if existing.endswith("\n") or not existing else "\n"
        pyproject.write_text(existing + separator + "\n" + addition, encoding="utf-8")
        return EnsureTestRunnerResult(
            language="python",
            action="augmented",
            path=pyproject,
            detail=f"appended {_PYTEST_INI_SECTION} (importlib mode); packaging reconciled",
        )

    pyproject.write_text(
        _python_packaging_metadata(profile, backend=_BACKEND_SETUPTOOLS) + "\n" + section,
        encoding="utf-8",
    )
    return EnsureTestRunnerResult(
        language="python",
        action="created",
        path=pyproject,
        detail=f"wrote pyproject.toml (setuptools package + {_PYTEST_INI_SECTION}, importlib mode)",
    )


def _read_text_or_empty(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _ensure_typescript_test_runner(
    project_root: Path,
    *,
    profile: LayoutProfile,
) -> EnsureTestRunnerResult:
    """Ensure a RUNNABLE node test setup (a ``test`` script in package.json).

    CREATE-ONLY / non-clobbering, mirroring the Python ensurer's discipline:

      * an existing ``test`` script (any runner: vitest, jest, mocha, …) is
        author intent → left untouched (``present``);
      * a ``package.json`` WITHOUT a ``test`` script gains ``test`` (and
        ``build`` when absent) while every other field is preserved
        byte-for-faithfully (re-serialized JSON) → ``augmented``;
      * no ``package.json`` → a minimal one is created with ``"type": "module"``
        (coherent with the scaffolded NodeNext tsconfig), ``test`` + ``build``
        scripts, and the package ``name`` derived from the project → ``created``.

    The scripts use the runner the profile declares (vitest) and ``tsc`` for the
    build; dependency INSTALL is handled by the blocking verify preflight
    (:func:`node_install_command`), NEVER here.
    """
    runner = profile.runner or "vitest"
    test_cmd = "vitest run" if runner == "vitest" else ("jest" if runner == "jest" else f"{runner}")
    build_cmd = "tsc -p tsconfig.json"
    package_json = project_root / _PACKAGE_JSON_FILENAME

    if package_json.exists():
        payload = _read_json_or_none(package_json)
        if payload is None:
            # Present but unparseable: do not clobber author content; the verify
            # honesty/typecheck gates remain the authority.
            return EnsureTestRunnerResult(
                language=profile.language,
                action="present",
                detail="package.json exists but is not valid JSON; left untouched",
            )
        scripts = payload.get("scripts")
        if not isinstance(scripts, dict):
            scripts = {}
        existing_test = str(scripts.get("test") or "").strip()
        # A real test script (anything other than the npm-init placeholder) is
        # author intent → leave the whole file untouched.
        placeholder = "echo" in existing_test and "exit 1" in existing_test
        if existing_test and not placeholder:
            return EnsureTestRunnerResult(
                language=profile.language,
                action="present",
                path=package_json,
                detail=f"package.json already declares a test script ({existing_test}); left untouched",
            )
        added: list[str] = []
        if not existing_test or placeholder:
            scripts["test"] = test_cmd
            added.append("test")
        if "build" not in scripts:
            scripts["build"] = build_cmd
            added.append("build")
        payload["scripts"] = scripts
        package_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return EnsureTestRunnerResult(
            language=profile.language,
            action="augmented",
            path=package_json,
            detail=f"added package.json script(s): {', '.join(added)}",
        )

    payload = {
        "name": profile.package_name,
        "version": "0.0.0",
        "private": True,
        "type": "module",
        "scripts": {"test": test_cmd, "build": build_cmd},
    }
    package_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return EnsureTestRunnerResult(
        language=profile.language,
        action="created",
        path=package_json,
        detail=f"wrote package.json (type=module, test={test_cmd!r}, build={build_cmd!r})",
    )


# Realizer capability id → legacy test-runner ensurer (Contract Kernel v2.71). The
# dispatch key is the profile's ``legacy_project_types.test_runner_ensurer`` realizer
# id — a harness-policy capability name, NOT a language name. A stack with no legacy
# bridge (Go's profile declares none) or an unknown/unaccepted language is the
# "unsupported" advisory no-op in :func:`ensure_test_runner_config`. Each ensurer
# drives off the resolved :class:`LayoutProfile`, so topology lives in ONE place.
_TestRunnerEnsurer = Callable[..., EnsureTestRunnerResult]
#: Realizer capability ids (must match the profile YAML ``legacy_project_types``).
_ENSURER_PY_PYPROJECT = "pyproject-pytest-runner-v1"
_ENSURER_TS_NPM = "npm-vitest-runner-v1"
_TEST_RUNNER_ENSURERS_BY_REALIZER: dict[str, _TestRunnerEnsurer] = {
    _ENSURER_PY_PYPROJECT: _ensure_python_test_runner,
    _ENSURER_TS_NPM: _ensure_typescript_test_runner,
}


def supported_test_runner_languages() -> list[str]:
    """Runtime names for which greenfield can deterministically ensure a test runner.

    PROFILE-DRIVEN (Contract Kernel v2.71): the union of every profile's
    ``legacy_project_types.accepted_names`` whose ``test_runner_ensurer`` realizer is
    registered — never a hardcoded language-name table. Byte-identical to the legacy
    dict's key set (``node``/``python``/``typescript``): only the explicitly accepted
    names, not the wider registry aliases, and not Go (no legacy bridge).
    """
    return _legacy_bridged_names("test_runner_ensurer")


def ensure_test_runner_config(
    project_root: Path | str,
    *,
    language: str | None,
    project_name: str | None = None,
    source_dirs: Any = None,
    test_dirs: Any = None,
) -> EnsureTestRunnerResult:
    """Guarantee a RUNNABLE, detectable test setup for ``language``'s stack.

    Stack-general entry point. PROFILE-DRIVEN dispatch (Contract Kernel v2.71): the
    runtime ``language`` is resolved to its :class:`LanguageProfile`, gated by the
    profile's ``legacy_project_types`` bridge (``accepted_names`` — so support stays
    byte-identical), and the ensurer is selected by the bridge's ``test_runner_ensurer``
    realizer id — NOT a ``language ==``/dict-keyed-by-name literal. For a stack WITH a
    registered ensurer, the ensurer owns the present/augment/create decision (it checks
    its own STRONG marker — e.g. a ``[tool.pytest`` section, not mere ``pyproject.toml``
    presence — so it can upgrade a bare config that is detectable but not runnable). For
    a stack WITHOUT an ensurer (Go's profile declares no bridge, or an unknown/unaccepted
    language), this returns an ``"unsupported"`` no-op UNLESS a test command is already
    detectable, in which case a provided (possibly non-native) setup is respected.

    Either way an AI/user-provided setup is never clobbered, and the verify layer
    remains the authority that refuses to certify an unexecuted build. All paths
    derive from the resolved :class:`LayoutProfile` (``project_name`` +
    ``scan.source_dirs`` / ``scan.test_dirs``); nothing is hardcoded.
    """
    root = Path(project_root)
    lang = (language or "").strip().lower()

    ensurer_id = _legacy_realizer_id(lang, "test_runner_ensurer")
    ensurer = _TEST_RUNNER_ENSURERS_BY_REALIZER.get(ensurer_id) if ensurer_id else None
    if ensurer is None:
        # No native ensurer for this stack. Respect any test command an AI/user
        # already wired up (stack-agnostic), otherwise it is an advisory no-op:
        # the verify honesty gate still refuses to certify an unexecuted build.
        from codd.test_detection import detect_test_command

        if detect_test_command(root) is not None:
            return EnsureTestRunnerResult(
                language=lang or "unknown",
                action="present",
                detail="a test command is already detectable; left untouched",
            )
        return EnsureTestRunnerResult(
            language=lang or "unknown",
            action="unsupported",
            detail=(
                f"no deterministic test-runner ensurer for language {lang!r}; "
                "relying on the generated project to provide a detectable setup"
            ),
        )

    profile = resolve_layout_profile(
        language=lang,
        project_name=project_name,
        source_dirs=source_dirs,
        test_dirs=test_dirs,
    )
    if profile is not None:
        return ensurer(root, profile=profile)

    # Fallback (no LayoutProfile for an ensurer-having shape — shouldn't happen, but
    # keep the path total): synthesize a minimal profile from dirs.
    source_root = _first_clean_dir(source_dirs, "src")
    test_root = _first_clean_dir(test_dirs, "tests")
    package_name = normalize_package_name(project_name)
    fallback_profile = LayoutProfile(
        language=lang,
        package_name=package_name,
        source_root=source_root,
        package_root=f"{source_root}/{package_name}",
        test_root=test_root,
    )
    return ensurer(root, profile=fallback_profile)
