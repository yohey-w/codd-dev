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

import json
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
_VALID_RUNNER_REPORT_FORMATS = {"vitest-json", "pytest-junit-xml", "go-test-json"}


@dataclass(frozen=True)
class VerifyCampaignSpec:
    """A stack's harness-owned greenfield verification campaign (profile-driven).

    * ``command_template`` — the canonical verify command, run from the project
      root during the greenfield VERIFY stage. It MUST run the WHOLE VB-bearing
      test surface (unit AND e2e — never one SUT script) and write a
      machine-readable report. ``{test_root}`` and ``{report}`` are substituted
      with the profile's test root and the resolved report path (POSIX, project-
      relative) before execution. TS: ``npx --no-install vitest run {test_root}
      --reporter=json --outputFile={report}``.
    * ``report_relpath`` — where the runner writes its machine-readable report
      (project-relative; under ``.codd/`` so it is a harness artifact, never the
      SUT's). The coherence gate reads + parses this.
    * ``report_format`` — which per-profile adapter parses the report
      (``vitest-json`` now; ``pytest-junit-xml`` / ``go-test-json`` are the
      documented extension points). Selects the
      :class:`~codd.coverage_execution_coherence.RunnerReportAdapter`.
    * ``requires_node_install`` — whether the BLOCKING dependency-install
      preflight must run first (TS: yes — vitest/deps must be materialized).

    The COMMAND + FORMAT are per-language (each runner has its own CLI + report);
    the MEANING (run the whole VB surface, reconcile executed+passed with static
    coverage) is core. ``None`` on a profile makes the coherence gate a strict
    NO-OP for that stack.
    """

    # A campaign declares its command in ONE of two forms (design A — argv support):
    #   * ``command_template`` — a SHELL string (vitest/go), run with ``shell=True``;
    #     ``{test_root}`` / ``{report}`` are substituted before execution.
    #   * ``command_argv``     — an ARGV list (C#/dotnet), run with ``shell=False`` so
    #     an argument containing shell metacharacters (``trx;LogFileName=test.trx``)
    #     is passed VERBATIM, never split/interpreted by a shell. ``{test_root}`` /
    #     ``{report}`` are substituted per element (only elements that contain a
    #     ``{`` placeholder are formatted, so a literal ``;`` argv element is safe).
    # Exactly one must be set (validated in ``__post_init__``). The report path is
    # INDEPENDENT of the command (a single-file report at ``report_relpath`` — C#'s
    # ``.trx`` is written by the runner to a fixed path, not via a ``{report}`` arg).
    report_relpath: str
    report_format: str
    command_template: str | None = None
    command_argv: tuple[str, ...] | None = None
    requires_node_install: bool = False

    def __post_init__(self) -> None:
        if not self.command_template and not self.command_argv:
            raise ValueError(
                "VerifyCampaignSpec requires a command_template (shell) OR a "
                "command_argv (argv); a campaign with no command cannot run."
            )

    def resolve_command(self, *, test_root: str, report_path: str) -> str:
        """The runnable SHELL command with ``{test_root}`` / ``{report}`` substituted."""
        if self.command_template is None:
            raise ValueError(
                "this campaign is argv-based (no command_template); use resolve_argv()"
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
                "this campaign is shell-based (no command_argv); use resolve_command()"
            )
        return tuple(
            (a.format(test_root=test_root, report=report_path) if "{" in a else a)
            for a in self.command_argv
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_template": self.command_template,
            "command_argv": list(self.command_argv) if self.command_argv is not None else None,
            "report_relpath": self.report_relpath,
            "report_format": self.report_format,
            "requires_node_install": self.requires_node_install,
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

        return tuple(paths)

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
        """Resolve this stack's runner-report adapter for the coherence gate.

        Returns a ``codd.coverage_execution_coherence.RunnerReportAdapter`` that
        normalizes the :class:`VerifyCampaignSpec` report into the set of executed
        + passed test FILES (and, when available, test cases), or ``None`` for a
        stack with no adapter. Selection keys off ``verify_campaign.report_format``
        (``vitest-json`` now; ``pytest-junit-xml`` / ``go-test-json`` are the
        documented extension points), so a new runner is one adapter + one profile
        ``report_format`` value — never a core gate edit. The import is lazy so the
        coherence module is not pulled in at ``project_types`` import time.

        ``None`` when the profile declares no ``verify_campaign`` (Python today) or
        the declared ``report_format`` has no registered adapter yet — the
        coherence gate then degrades EXPLICITLY (it never silently passes a build
        whose campaign report it cannot read; see the gate's observability rule).
        """

        campaign = self.verify_campaign
        if campaign is None:
            return None
        try:
            from codd.coverage_execution_coherence import resolve_runner_report_adapter
        except Exception:  # noqa: BLE001 — adapter is optional; degrade if unavailable.
            return None
        return resolve_runner_report_adapter(campaign.report_format)


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
    """Synthesize a :class:`VerifyCampaignSpec` from the profile's verify command + report.

    Design A (argv form + single-file report): the campaign ARGV is the resolved
    ``commands[<verify.command>].argv``; the report path + adapter come from the top-level
    ``verify.report`` (``report_format`` = ``verify.report.adapter`` — the id the
    runner-report registry resolves on, e.g. ``dotnet-trx``). ``None`` when the profile
    declares no verify block / report / adapter / argv (the coverage gate then stays a
    strict NO-OP for the stack — never a silent green for an unreadable campaign).
    """
    verify = getattr(lang_profile, "verify", None)
    if verify is None:
        return None
    report = getattr(verify, "report", None)
    if report is None:
        return None
    report_relpath = _norm_rel(getattr(report, "path", "") or "")
    report_format = getattr(report, "adapter", None) or getattr(report, "format", None)
    if not report_relpath or not report_format:
        return None
    command_id = getattr(verify, "command", None)
    commands = getattr(lang_profile, "commands", {}) or {}
    cmd = commands.get(command_id) if command_id else None
    argv = tuple(str(a) for a in (getattr(cmd, "argv", ()) or ())) if cmd is not None else ()
    if not argv:
        return None
    return VerifyCampaignSpec(
        report_relpath=report_relpath,
        report_format=str(report_format),
        command_argv=argv,
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
    )


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
        return _synthesize_layout_profile_from_language(
            language=language,
            project_name=project_name,
            source_dirs=source_dirs,
            test_dirs=test_dirs,
            config=config,
            project_root=project_root,
        )
    return builder(
        project_name=project_name,
        source_dirs=source_dirs,
        test_dirs=test_dirs,
        config=config,
        project_root=project_root,
    )


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
        _ensure_file(
            f"{package_dir}/__init__.py",
            _SCAFFOLD_INIT_DOC,
        )
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
