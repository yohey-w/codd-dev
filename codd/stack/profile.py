"""Frozen dataclasses modeling the FrameworkProfile / AddonProfile taxonomy.

This mirrors §1 (層一般化) and §3 (FrameworkProfile taxonomy) of
``dogfood/gpt_composable_profile_design.md``. It is the framework half of the
v3.0 composable-Profile generality (owner 2026-06-20: "v3.0 は言語とフレームワーク
を profile にする — 言語だけじゃない"). The language half lives in
``codd/languages/profile.py``; this module is **additive** and does not touch it.

Design model (§1): every stack layer shares the same SHAPE — declaration +
adapter bindings + conformance — but languages and frameworks have very
different RESPONSIBILITIES:

* ``LanguageProfile`` (codd/languages): syntax, import resolution, manifest/lock,
  unit test semantics, coverage, artifact.
* ``FrameworkProfile`` (here, ``kind="framework"``): project layout convention,
  file roles, route/operation discovery, framework build/e2e, framework
  obligations. e.g. Next.js, Rails, LangChain/LangGraph.
* ``AddonProfile`` (here, ``kind="addon"``): a cross-cutting capability that is
  NOT the application framework but still shapes the stack contract — ORM,
  e2e harness, test runner, migration. e.g. Prisma, Playwright, RSpec, Vitest.
  Keeping these distinct from frameworks stops Next.js + Prisma + Playwright
  from all claiming "framework" and blurring the ownership hierarchy (§1).

Rather than frozen-dataclass inheritance (whose default-ordering rules make a
shared mutable base brittle), both profiles are standalone frozen dataclasses
that share the same primitive types and satisfy the structural
:class:`LayerProfile` Protocol — which is all the composition layer needs.

All dataclasses are ``frozen=True``: a loaded profile is an immutable contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, ClassVar, Literal, Mapping, Protocol, runtime_checkable

# Reuse the proven primitives + freeze helpers from the language profile — they
# are layer-agnostic (a command is a command, an adapter ref is an adapter ref).
from codd.languages.profile import (
    AdapterRef,
    CommandSpec,
    ReportSpec,
    SourceSet,
    Strictness,
)

LayerKind = Literal["language", "framework", "addon", "runtime", "platform"]


# ---------------------------------------------------------------------------
# identity / requirements (shared LayerProfileBase fields, §1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LayerIdentity:
    """Layer identity & matching keys (design §1, §3 common taxonomy)."""

    id: str
    kind: LayerKind
    display_name: str = ""
    aliases: tuple[str, ...] = ()
    schema_version: str = "1"
    profile_version: str = "0.1.0"
    strictness: Strictness = "strict"


@dataclass(frozen=True)
class LanguageRequirement:
    """One acceptable language a framework/addon can sit on (design §1 dependency)."""

    id: str
    version: str | None = None


@dataclass(frozen=True)
class LayerRequirements:
    """What a framework/addon REQUIRES underneath it (design §1).

    ``any_language`` lists acceptable host languages (a framework requires at
    least one); ``runtime`` / ``addons`` are kept loose for Phase 2.
    """

    any_language: tuple[LanguageRequirement, ...] = ()
    runtime: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    addons: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# detection / variants (§3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DetectionManifest:
    """A manifest signal that a framework/addon is in use (design §3 detection).

    e.g. ``package.json`` carrying a ``next`` dependency.
    """

    file: str
    dependency: str | None = None


@dataclass(frozen=True)
class Detection:
    """How to auto-detect this layer (design §3, §2 codd.stack.lock).

    Auto-detect is for ``local init`` convenience only; in CI the
    ``codd.stack.lock`` is the source of truth (§2).
    """

    manifests: tuple[DetectionManifest, ...] = ()
    files: tuple[str, ...] = ()


@dataclass(frozen=True)
class Variant:
    """A mutually-exclusive framework variant (design §3, §1 exclusive_select).

    e.g. Next.js ``app_router`` vs ``pages_router``. ``detect`` is a loose
    mapping (adapter-facing globs). When a profile declares variants they are
    ``exclusive_select`` by default — exactly one is chosen.
    """

    id: str
    detect: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


# ---------------------------------------------------------------------------
# layout / file roles / operations / assertions / artifacts (§3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LayerLayoutSpec:
    """Framework/addon layout contributions (design §3 layout).

    A framework CONTRIBUTES layout (e.g. Next.js ``app/`` routes) on top of the
    language's ``source_sets``; it does not replace them. ``generated`` /
    ``ignored`` are framework-owned output dirs (e.g. ``.next/``).
    """

    source_sets: tuple[SourceSet, ...] = ()
    generated: tuple[str, ...] = ()
    ignored: tuple[str, ...] = ()


@dataclass(frozen=True)
class FileRole:
    """Maps a path pattern to a framework file ROLE (design §3 file_roles).

    e.g. ``app/**/page.tsx`` → ``route_page``, ``app/**/route.ts`` →
    ``route_handler``. The classifier adapter consumes these.
    """

    pattern: str
    role: str


@dataclass(frozen=True)
class OperationsSpec:
    """Route/operation discovery (design §3 operations).

    For a web framework an OPERATION is a route; for LangChain it is an
    agent/tool/eval. Both are kept loose (adapter-facing) — the
    ``route_resolver`` / ``operation_flow`` adapters give them meaning.
    """

    route_discovery: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    operation_flow: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class AssertionsSpec:
    """Framework-specific assertion/eval idioms (design §3 assertions)."""

    idioms: tuple[str, ...] = ()
    extra: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class LayerArtifactsSpec:
    """Framework build outputs + reports (design §3 artifacts)."""

    build_outputs: tuple[str, ...] = ()
    reports: tuple[ReportSpec, ...] = ()


# ---------------------------------------------------------------------------
# obligations (§3, §1 composition — anti-false-green)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Obligation:
    """A framework obligation the resolved contract must enforce (design §3, §1).

    e.g. Next.js: "a route handler must be exercised by an e2e test", or the
    anti-false-green guard "``next build`` may NOT substitute for typecheck when
    ``next.config.*`` sets ``typescript.ignoreBuildErrors: true``". ``checker``
    names the adapter that enforces it; ``severity`` is ``error`` (release-
    blocking) or ``warn``. ``data`` is the adapter-facing detail.
    """

    id: str
    description: str = ""
    checker: str | None = None
    severity: Literal["error", "warn"] = "error"
    data: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class ConformanceSpec:
    """Anti-false-green conformance fixtures the layer must pass (design §3, §0).

    The extensibility-safety contract: a user-added framework/addon profile is
    only TRUSTED once its fixtures prove its obligation checkers reject the
    seeded false-greens (mirrors the language conformance suite).
    """

    fixtures: tuple[Mapping[str, Any], ...] = ()
    adapter: str | None = None


# ---------------------------------------------------------------------------
# structural LayerProfile Protocol (what the composition layer depends on)
# ---------------------------------------------------------------------------


@runtime_checkable
class LayerProfile(Protocol):
    """Structural type shared by Language/Framework/Addon profiles (design §1).

    The composition layer (``ResolvedStackContract``) depends only on this —
    not on a concrete base class — so the harness body never branches on a
    layer's concrete type, only on the resolved contract.
    """

    @property
    def identity(self) -> LayerIdentity | Any: ...

    @property
    def id(self) -> str: ...

    @property
    def kind(self) -> str: ...

    def matches(self, name: str) -> bool: ...


# ---------------------------------------------------------------------------
# top-level profiles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrameworkProfile:
    """The full declarative framework contract (design §3, ``kind="framework"``).

    A framework REQUIRES a host language (``requires.any_language``) and
    CONTRIBUTES layout/operations/commands/obligations to the resolved stack
    contract. Adapter-facing sub-structures are kept loose (Phase-1 philosophy);
    unknown top-level keys are preserved in ``extra`` / ``raw``.
    """

    identity: LayerIdentity
    requires: LayerRequirements = field(default_factory=LayerRequirements)
    detection: Detection = field(default_factory=Detection)
    variants: tuple[Variant, ...] = ()
    exclusive_variants: bool = True
    layout: LayerLayoutSpec = field(default_factory=LayerLayoutSpec)
    file_roles: tuple[FileRole, ...] = ()
    operations: OperationsSpec = field(default_factory=OperationsSpec)
    commands: Mapping[str, CommandSpec] = field(default_factory=lambda: MappingProxyType({}))
    assertions: AssertionsSpec = field(default_factory=AssertionsSpec)
    artifacts: LayerArtifactsSpec = field(default_factory=LayerArtifactsSpec)
    obligations: tuple[Obligation, ...] = ()
    adapters: Mapping[str, AdapterRef] = field(default_factory=lambda: MappingProxyType({}))
    conformance: ConformanceSpec | None = None
    provides: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    trust: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    extra: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    raw: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def id(self) -> str:
        return self.identity.id

    @property
    def kind(self) -> str:
        return self.identity.kind

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
        return any(needle == a.strip().lower() for a in self.identity.aliases)


@dataclass(frozen=True)
class AddonProfile:
    """A cross-cutting addon contract (design §1, ``kind="addon"``).

    An addon (Prisma/Playwright/Vitest/RSpec) adds a capability — ORM, e2e,
    test runner — without being the application framework. It can contribute
    commands, obligations, artifacts and adapters, but owns a NARROWER namespace
    than a framework (it must not claim the application-build slot). Same shape,
    smaller surface.
    """

    identity: LayerIdentity
    requires: LayerRequirements = field(default_factory=LayerRequirements)
    detection: Detection = field(default_factory=Detection)
    capability: str = ""  # e.g. "orm" / "e2e" / "test_runner" / "migration"
    commands: Mapping[str, CommandSpec] = field(default_factory=lambda: MappingProxyType({}))
    obligations: tuple[Obligation, ...] = ()
    artifacts: LayerArtifactsSpec = field(default_factory=LayerArtifactsSpec)
    adapters: Mapping[str, AdapterRef] = field(default_factory=lambda: MappingProxyType({}))
    conformance: ConformanceSpec | None = None
    provides: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    trust: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    extra: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    raw: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def id(self) -> str:
        return self.identity.id

    @property
    def kind(self) -> str:
        return self.identity.kind

    @property
    def aliases(self) -> tuple[str, ...]:
        return self.identity.aliases

    @property
    def strictness(self) -> Strictness:
        return self.identity.strictness

    def matches(self, name: str) -> bool:
        needle = name.strip().lower()
        if needle == self.identity.id.strip().lower():
            return True
        return any(needle == a.strip().lower() for a in self.identity.aliases)
