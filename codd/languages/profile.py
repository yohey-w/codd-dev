"""Frozen dataclasses modeling the ``LanguageProfile`` taxonomy.

This mirrors §1 (LanguageProfile taxonomy) of
``dogfood/gpt_language_generality_design.md``.

Design notes (Phase 1, additive only):

* **The load-bearing point**: there is NO single ``source_root`` field.
  Source locations are a SET — ``LayoutSpec.source_sets`` — so a language
  whose sources are spread across multiple roots (e.g. Go's ``cmd/`` +
  ``internal/``) is representable and is *never* forced under ``src/``.
* Placeholders such as ``{package_name}`` / ``{module_path}`` /
  ``{module_root}`` are kept as **literal template strings**. They are NOT
  substituted here — substitution is PathPlanner's job in a later phase.
* Adapter-only / opaque sub-structures (``assertion_hints``, the internals
  of ``imports``, ``package_manager``) are kept typed-but-loose
  (``Mapping[str, Any]``) on purpose. Over-modeling them is out of scope
  for Phase 1, since their meaning only crystallizes once the adapters that
  consume them exist.

All dataclasses are ``frozen=True`` so a loaded profile is an immutable
contract, per design §8.1 ("LanguageProfile は '設定' ではなく '契約'").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, ClassVar, Literal, Mapping

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

#: Strictness vocabulary (design §1.1).
Strictness = Literal["strict", "legacy_compatible"]


def _freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Return an immutable, recursively-frozen view of a mapping.

    Keeps opaque sub-structures hashable-friendly and read-only so a frozen
    profile cannot be mutated through one of its loose ``Mapping`` fields.
    """
    if not value:
        return MappingProxyType({})
    return MappingProxyType({k: _freeze_value(v) for k, v in value.items()})


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze_value(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(v) for v in value)
    return value


# ---------------------------------------------------------------------------
# 1.1 Identity / aliases
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    """Language identity & matching keys (design §1.1)."""

    id: str
    display_name: str
    aliases: tuple[str, ...] = ()
    file_extensions: tuple[str, ...] = ()
    strictness: Strictness = "strict"


# ---------------------------------------------------------------------------
# 1.2 Layout / topology
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceSet:
    """One declared source location (design §1.2).

    There can be MANY of these. This is the structure that replaces the
    Python/TS-centric single ``source_root``.
    """

    id: str
    root: str
    file_globs: tuple[str, ...] = ()
    role: str | None = None


@dataclass(frozen=True)
class TestSet:
    """One declared test location (design §1.2)."""

    # Not a pytest test class — silence PytestCollectionWarning when imported.
    __test__: ClassVar[bool] = False

    id: str
    root: str
    file_globs: tuple[str, ...] = ()
    role: str | None = None
    colocated: bool = False
    optional: bool = False


@dataclass(frozen=True)
class PackageRoot:
    """Package-root descriptor (design §1.2).

    ``kind`` is e.g. ``named_package`` (Python), ``path_root`` (TS) or
    ``none`` (Go — Go has no single package root). ``path`` may be ``None``
    (when ``kind == "none"``) or a literal template like ``src/{package_name}``.
    """

    kind: str
    path: str | None = None


@dataclass(frozen=True)
class LayoutSpec:
    """Repository topology (design §1.2).

    NOTE: there is intentionally **no** ``source_root`` field. Source
    locations live in ``source_sets`` (a set). This is the central fix
    that makes the layout language-free.
    """

    repo_root: str = "."
    module_root: str = "."
    manifest_root: str = "."
    default_command_cwd: str | None = None
    source_sets: tuple[SourceSet, ...] = ()
    test_sets: tuple[TestSet, ...] = ()
    package_root: PackageRoot = field(default_factory=lambda: PackageRoot(kind="none"))


# ---------------------------------------------------------------------------
# 1.3 Manifest / module / dependency files
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManifestSpec:
    """Dependency manifest descriptor (design §1.3).

    e.g. ``pyproject.toml`` / ``package.json`` / ``go.mod``.
    """

    path: str
    format: str
    required: bool = True


@dataclass(frozen=True)
class DependencyIntegrityFile:
    """A lock OR checksum artifact (design §1.3).

    ``kind`` distinguishes a true lock file (``lock``: package-lock.json,
    uv.lock) from a checksum file (``checksum``: go.sum). Per design §1.3,
    Go's ``go.sum`` is a **checksum** artifact, NOT a lock file.
    """

    path: str
    kind: str = "lock"
    required: bool = False
    generated_when: str | None = None


@dataclass(frozen=True)
class ToolchainSpec:
    """Toolchain / dependency wiring (design §1.3).

    ``package_manager``, ``module_identity``, ``materialize`` and
    ``reconcile`` are kept as loose mappings: they are adapter-facing and
    not load-bearing for Phase 1.
    """

    manifest: ManifestSpec
    dependency_integrity_files: tuple[DependencyIntegrityFile, ...] = ()
    package_manager: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    module_identity: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    extra: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


# ---------------------------------------------------------------------------
# 1.4 Commands
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReportSpec:
    """Machine-readable report descriptor for a command/verify (design §1.4/1.8)."""

    path: str | None = None
    format: str | None = None
    adapter: str | None = None
    capture: str | None = None


@dataclass(frozen=True)
class ScopeSpec:
    """Scope requirement for a command (design §1.4).

    Declares which source/test sets a command MUST cover, by id.
    """

    must_include_source_sets: tuple[str, ...] = ()
    must_include_test_sets: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerifyObservationPolicy:
    """Unweakenable anti-false-green observation policy for a verify command.

    The DEFAULTS are the invariant: a verify run is green ONLY if it observed at
    least ``min_collected_tests`` real tests, produced a parseable report, and
    had no failed/skipped tests. A profile may STRENGTHEN (raise
    ``min_collected_tests``) but NEVER weaken — every weakening declaration
    (``allow_zero_tests``, ``zero_tests: warn``, ``report_missing: pass``,
    ``min_collected_tests: 0``, an unknown key, …) is rejected by
    :meth:`from_mapping` at load time, so a profile cannot silently turn a
    not-green verify outcome green (a profile is a contract, not a way to
    disable the invariant).
    """

    min_collected_tests: int = 1
    zero_tests: Literal["red"] = "red"
    report_missing: Literal["red"] = "red"
    report_parse_error: Literal["red"] = "red"
    failed_tests: Literal["red"] = "red"
    skipped_tests: Literal["red"] = "red"

    #: Fields that may ONLY ever be "red" — they cannot be weakened by a profile.
    RED_ONLY: ClassVar[tuple[str, ...]] = (
        "zero_tests",
        "report_missing",
        "report_parse_error",
        "failed_tests",
        "skipped_tests",
    )

    @classmethod
    def from_mapping(
        cls, raw: Any, *, where: str = "commands.verify.observation"
    ) -> "VerifyObservationPolicy":
        """Parse + STRICTLY validate an ``observation:`` block; raise on weakening.

        ``None`` → the strict defaults. Any weakening (a RED_ONLY field set to
        anything but "red", ``min_collected_tests < 1``, or an unknown key such
        as ``allow_zero_tests``) raises :class:`ValueError`.
        """
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise ValueError(f"{where}: expected a mapping, got {type(raw).__name__}")
        known = {"min_collected_tests", *cls.RED_ONLY}
        unknown = sorted(k for k in raw if k not in known)
        if unknown:
            raise ValueError(
                f"{where}: unknown observation key(s) {unknown} — the anti-false-green "
                f"observation policy cannot be extended with weakening flags "
                f"(e.g. allow_zero_tests). Permitted keys: {sorted(known)}."
            )
        for key in cls.RED_ONLY:
            if key in raw and str(raw[key]).strip().lower() != "red":
                raise ValueError(
                    f"{where}: {key}={raw[key]!r} weakens an anti-false-green invariant; "
                    f"only 'red' is permitted (a profile cannot turn a not-green verify "
                    f"outcome green)."
                )
        mct_raw = raw.get("min_collected_tests", 1)
        try:
            mct = int(mct_raw)
        except (TypeError, ValueError):
            raise ValueError(
                f"{where}: min_collected_tests must be an integer, got {mct_raw!r}"
            )
        if mct < 1:
            raise ValueError(
                f"{where}: min_collected_tests={mct} weakens the invariant; it must be "
                f">= 1 (a verify that observed zero tests is never green). Profiles may "
                f"raise it (stricter), never lower it."
            )
        return cls(min_collected_tests=mct)


@dataclass(frozen=True)
class CommandSpec:
    """A single runnable command (design §1.4).

    ``argv`` is the canonical form (not a shell string). ``cwd`` and the
    members of ``env``/``mutates`` may contain literal placeholder templates
    (e.g. ``{module_root}``) — they are NOT substituted in Phase 1.
    """

    id: str
    argv: tuple[str, ...]
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    mutates: tuple[str, ...] = ()
    requires_materialized_deps: bool = False
    report: ReportSpec | None = None
    scope: ScopeSpec | None = None
    #: Unweakenable anti-false-green observation policy (verify commands). ``None``
    #: ⇒ the strict defaults apply (see :meth:`VerifyObservationPolicy.from_mapping`).
    observation: VerifyObservationPolicy | None = None
    #: Free-form leftover keys (e.g. ``pass_condition``) preserved for later phases.
    extra: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


# ---------------------------------------------------------------------------
# 1.5 Module / import resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportsSpec:
    """Import-resolution config (design §1.5).

    Half data, half adapter. ``resolver_adapter`` names the adapter that
    actually parses/resolves imports in a later phase. Everything else is
    kept as a loose mapping (``data``) because its shape is adapter-defined.
    """

    resolver_adapter: str | None = None
    data: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


# ---------------------------------------------------------------------------
# 1.7 Assertion / test semantics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TestsSpec:
    """Test-semantics config (design §1.7).

    ``assertion_hints`` and ``authenticity_policy`` are adapter-facing and
    intentionally loose. ``test_file_globs`` is load-bearing (used to find
    candidate test files) and is kept typed.
    """

    # Not a pytest test class — silence PytestCollectionWarning when imported.
    __test__: ClassVar[bool] = False

    semantics_adapter: str | None = None
    runner_report_adapter: str | None = None
    test_file_globs: tuple[str, ...] = ()
    assertion_hints: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    authenticity_policy: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    test_block_kinds: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# 1.8 Runner report / execution coherence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifySpec:
    """Verify campaign descriptor (design §1.8).

    ``command`` references a command id in ``LanguageProfile.commands``.
    ``execution_policy`` is the generic anti-false-green policy and is kept
    loose (adapter/gate-facing).
    """

    command: str | None = None
    report: ReportSpec | None = None
    execution_policy: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )


# ---------------------------------------------------------------------------
# 1.9 Artifact placement / ownership
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArtifactsSpec:
    """Artifact ownership policy (design §1.9)."""

    harness_root: str | None = None
    harness_owned: tuple[str, ...] = ()
    ignored: tuple[str, ...] = ()
    #: e.g. ``build_outputs`` / ``generated_binaries`` — kept loose for now.
    extra: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


# ---------------------------------------------------------------------------
# scaffold (design §4 Phase 3 / §5.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScaffoldSpec:
    """Scaffold ownership (design Phase 3 / §5.1).

    ``owned_files`` may contain literal placeholder templates such as
    ``src/{package_name}/__init__.py`` — NOT substituted in Phase 1.
    """

    adapter: str | None = None
    owned_files: tuple[str, ...] = ()
    templates: tuple[Mapping[str, Any], ...] = ()


# ---------------------------------------------------------------------------
# ci scaffold (design §v2.70)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CiSpec:
    """CI scaffold contract — the toolchain bootstrap a generated CI workflow
    needs before it can run the project's verify/test command.

    Declarative GitHub-Actions step mappings (``{uses: ..., with: {...}}`` /
    ``{run: ...}``) so greenfield's ``ci_scaffold`` reads them from the language
    profile instead of a hardcoded per-marker table in the pipeline core (the
    Contract Kernel: the core never branches on a language name).
    """

    setup_steps: tuple[Mapping[str, Any], ...] = ()
    runs_on: str = "ubuntu-latest"


# ---------------------------------------------------------------------------
# generic adapter reference
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdapterRef:
    """A (kind, id) reference to a pluggable adapter (design §0).

    Phase 1 only records the reference; no adapters exist yet.
    """

    kind: str
    id: str


# ---------------------------------------------------------------------------
# implement_oracle declaration (Contract Kernel oracle dispatch §1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImplementOracleStepSpec:
    """One step of a composite implement-oracle (Contract Kernel §1).

    ``command`` is an id that MUST reference an entry in
    ``LanguageProfile.commands`` — the step carries NO argv/cwd/env of its own
    (those live on the referenced :class:`CommandSpec`). The loader fail-closes
    if the referenced command id is not declared.
    """

    command: str


@dataclass(frozen=True)
class ImplementOracleProfileSpec:
    """The PROFILE's declaration of its implement-time oracle (Contract Kernel §1).

    This is the *declaration* the language profile carries in YAML — NOT the
    gate's runtime spec (:class:`codd.project_types.ImplementOracleSpec`, which
    the dispatch still synthesizes). Distinct on purpose: this model only names
    *what kind* of oracle the language has and *which command ids / adapter* it
    references; it never duplicates argv/cwd/env.

    * ``kind`` — one of:
        - ``"command"``   — a single static checker (TS: ``tsc --noEmit``),
          named by ``command`` (a command id). ``steps`` MUST be empty.
        - ``"composite"`` — a sequence of weaker checkers unioned (Go:
          ``typecheck`` + ``vet``), named by ``steps``. ``command`` MUST be
          unset.
        - ``"adapter"``   — an in-process custom executor (Python's compile +
          import-resolver + collect-only composite). Neither ``command`` nor
          ``steps`` is set; the ``adapter`` id does all the work.
    * ``adapter`` — the (always-required) adapter id that resolves the oracle's
      tool semantics (scope certify / output normalize, and execute for the
      ``adapter`` kind). The profile declares it EXPLICITLY — it is never
      inferred from the language id (an implicit naming convention would create
      silent-green on a declaration typo).
    * ``command`` / ``steps`` — command-id references only (see above).
    """

    kind: Literal["command", "composite", "adapter"]
    adapter: str
    command: str | None = None
    steps: tuple[ImplementOracleStepSpec, ...] = ()


# ---------------------------------------------------------------------------
# top-level profile
# ---------------------------------------------------------------------------

#: Valid ``naming.package_case`` values (design fork C). ``lower`` = the historical
#: force-lower identifier discipline (Python snake_case); ``pascal`` = case-PRESERVING
#: (C# idiomatic PascalCase). ``snake`` etc. are a documented future extension. This is
#: per-profile DATA the core branches on — NEVER a language-name branch.
_VALID_PACKAGE_CASES: frozenset[str] = frozenset({"lower", "pascal"})


@dataclass(frozen=True)
class LanguageProfile:
    """The full declarative language contract (design §1).

    Sub-structures that are not yet load-bearing for Phase 1 are optional.
    ``path_rules`` and any unknown top-level keys are preserved verbatim in
    ``raw`` / ``extra`` so a later phase can consume them without re-parsing the
    YAML. ``implement_oracle`` is now modeled as a first-class field (Contract
    Kernel §1) — no longer left in ``extra``.
    """

    identity: Identity
    layout: LayoutSpec
    toolchain: ToolchainSpec | None = None
    commands: Mapping[str, CommandSpec] = field(
        default_factory=lambda: MappingProxyType({})
    )
    imports: ImportsSpec | None = None
    tests: TestsSpec | None = None
    verify: VerifySpec | None = None
    artifacts: ArtifactsSpec | None = None
    scaffold: ScaffoldSpec | None = None
    ci: CiSpec | None = None
    #: The implement-time oracle declaration (Contract Kernel §1). ``None`` when
    #: the profile declares no ``implement_oracle:`` block.
    implement_oracle: "ImplementOracleProfileSpec | None" = None
    #: ``path_rules`` etc., preserved but not modeled in Phase 1.
    extra: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    #: The full parsed YAML mapping, for round-trip / debugging.
    raw: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    # -- convenience accessors (read-only, language-free) --

    @property
    def id(self) -> str:
        return self.identity.id

    @property
    def aliases(self) -> tuple[str, ...]:
        return self.identity.aliases

    @property
    def strictness(self) -> Strictness:
        return self.identity.strictness

    def matches(self, name: str) -> bool:
        """True if *name* matches this profile's id or any alias (case-insensitive)."""
        needle = name.strip().lower()
        if needle == self.identity.id.strip().lower():
            return True
        return any(needle == alias.strip().lower() for alias in self.identity.aliases)

    @property
    def package_case(self) -> str:
        """The declared package-name casing discipline (design fork C — DATA, not a branch).

        Read from the profile's ``naming.package_case`` block: ``lower`` (the historical
        default — Python/TS/Go force a lower-case identifier) or ``pascal`` (case-PRESERVING
        — C#'s idiomatic PascalCase). ABSENT → ``lower``, so EVERY existing profile (none
        declare ``naming``) is byte-for-byte unchanged. A DECLARED-but-unknown value RAISES
        (anti-false-green: a misdeclared casing must surface loudly, never silently degrade
        to lower and re-break a case-sensitive build). The harness branches on this VALUE,
        so a new casing is one profile key — never a language-name code branch.
        """
        naming = self.raw.get("naming") if isinstance(self.raw, Mapping) else None
        if not isinstance(naming, Mapping):
            return "lower"
        raw_value = naming.get("package_case")
        if raw_value is None:
            return "lower"
        value = str(raw_value).strip().lower()
        if value not in _VALID_PACKAGE_CASES:
            raise ValueError(
                f"naming.package_case must be one of {sorted(_VALID_PACKAGE_CASES)}, "
                f"got {raw_value!r}"
            )
        return value
