"""ResolvedLanguageContract — the language-free view the kernel consumes (v2.68).

The Contract Kernel goal: the harness core never branches on a language name; it
resolves a project's language to a :class:`LanguageProfile`, resolves the
adapters that profile *names* against the :class:`AdapterRegistry`, and operates
only on the resulting :class:`ResolvedLanguageContract` (profile + resolved
adapter capabilities + a content hash).

Anti-false-green seam: a profile that NAMES an adapter the registry cannot
supply is an INCOMPLETE contract. :meth:`ResolvedLanguageContract.require_complete`
raises (RED) rather than letting a gate run with a missing observation
capability — a missing adapter must never become a silent green. (Legacy mode
may instead inspect :attr:`~ResolvedLanguageContract.is_complete` and degrade to
a warning; that choice belongs to the caller, not to this module.)

This module is ADDITIVE in v2.68: it establishes the resolution seam and is
exercised by tests, but is not yet wired into the live verify/greenfield gates
(that switch is v2.69+). Adapter *implementations* are registered in v2.72; until
then every declared adapter resolves as missing, which is exactly what the
incomplete-contract test asserts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping

from .profile import LanguageProfile
from .registry import (
    AdapterRegistry,
    LanguageRegistry,
    default_adapter_registry,
    default_registry,
)

# Adapter "kind" is implied by WHERE in the profile the adapter id is declared.
# These kind names are the contract between profiles (which name bare ids) and
# the AdapterRegistry (keyed by (kind, id)); v2.72 registers adapters under them.
KIND_TEST_SEMANTICS = "test_semantics"
KIND_RUNNER_REPORT = "runner_report"
KIND_IMPORT_RESOLVER = "import_resolver"
KIND_SCAFFOLD = "scaffold"
#: The implement-time oracle tool-semantics adapter kind (Contract Kernel oracle
#: dispatch §3). A profile names its oracle adapter id in
#: ``implement_oracle.adapter``; the registry keys it under this kind. The concrete
#: oracle_go / oracle_python / oracle_typescript adapters register under it WITH
#: their dispatch switch steps — none is registered yet (the plumbing is additive).
KIND_IMPLEMENT_ORACLE = "implement_oracle"


@dataclass(frozen=True)
class AdapterRequirement:
    """An adapter a profile declares it needs, with where it was declared."""

    kind: str
    id: str
    source: str  # e.g. "tests.semantics_adapter" — for honest diagnostics

    @property
    def ref(self) -> str:
        return f"{self.kind}:{self.id}"


class IncompleteLanguageContractError(RuntimeError):
    """A profile names adapters the registry cannot supply (RED, not silent green)."""

    def __init__(self, language_id: str, missing: tuple[AdapterRequirement, ...]) -> None:
        self.language_id = language_id
        self.missing = tuple(missing)
        detail = ", ".join(f"{r.ref} (from {r.source})" for r in self.missing)
        super().__init__(
            f"incomplete language contract for {language_id!r}: no adapter registered "
            f"for {detail}. A profile that names an adapter the registry cannot supply "
            f"is an incomplete contract (RED), never a silent green."
        )


def _config_language(config: Mapping[str, Any] | None) -> str | None:
    """Read ``project.language`` from a codd.yaml mapping (None if absent)."""
    if not isinstance(config, Mapping):
        return None
    project = config.get("project")
    if isinstance(project, Mapping):
        language = project.get("language")
        if isinstance(language, str) and language.strip():
            return language.strip()
    return None


def _declared_adapter_requirements(
    profile: LanguageProfile,
) -> tuple[AdapterRequirement, ...]:
    """Collect every adapter id the profile declares, as (kind, id) requirements.

    Walks the MODELED profile fields only (commands/imports/tests/verify/scaffold).
    ``implement_oracle`` / ``path_rules`` adapters live in ``profile.extra`` and
    are not modeled until v2.73/v2.74, so they are intentionally not collected yet.
    """
    reqs: list[AdapterRequirement] = []

    tests = profile.tests
    if tests is not None:
        if tests.semantics_adapter:
            reqs.append(
                AdapterRequirement(KIND_TEST_SEMANTICS, tests.semantics_adapter, "tests.semantics_adapter")
            )
        if tests.runner_report_adapter:
            reqs.append(
                AdapterRequirement(KIND_RUNNER_REPORT, tests.runner_report_adapter, "tests.runner_report_adapter")
            )

    imports = profile.imports
    if imports is not None and imports.resolver_adapter:
        reqs.append(
            AdapterRequirement(KIND_IMPORT_RESOLVER, imports.resolver_adapter, "imports.resolver_adapter")
        )

    scaffold = profile.scaffold
    if scaffold is not None and scaffold.adapter:
        reqs.append(AdapterRequirement(KIND_SCAFFOLD, scaffold.adapter, "scaffold.adapter"))

    verify = profile.verify
    if verify is not None and verify.report is not None and verify.report.adapter:
        reqs.append(
            AdapterRequirement(KIND_RUNNER_REPORT, verify.report.adapter, "verify.report.adapter")
        )

    for cmd_id, cmd in profile.commands.items():
        if cmd.report is not None and cmd.report.adapter:
            reqs.append(
                AdapterRequirement(KIND_RUNNER_REPORT, cmd.report.adapter, f"commands.{cmd_id}.report.adapter")
            )

    # Dedupe by (kind, id), keeping the first declaration site for diagnostics.
    seen: set[tuple[str, str]] = set()
    unique: list[AdapterRequirement] = []
    for req in reqs:
        key = (req.kind, req.id)
        if key not in seen:
            seen.add(key)
            unique.append(req)
    # Deterministic order so the content hash is stable.
    unique.sort(key=lambda r: (r.kind, r.id))
    return tuple(unique)


def _contract_hash(
    profile: LanguageProfile, required: tuple[AdapterRequirement, ...]
) -> str:
    payload = json.dumps(
        {
            "language": profile.identity.id,
            "adapters": [r.ref for r in required],
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class ResolvedLanguageContract:
    """Profile + resolved adapter capabilities + content hash (kernel input)."""

    language_id: str
    profile: LanguageProfile
    required_adapters: tuple[AdapterRequirement, ...]
    resolved_adapter_refs: tuple[str, ...]
    missing_adapters: tuple[AdapterRequirement, ...]
    content_hash: str

    @property
    def is_complete(self) -> bool:
        return not self.missing_adapters

    @property
    def adapter_ids(self) -> tuple[str, ...]:
        """All declared adapter refs (``kind:id``), resolved or not."""
        return tuple(r.ref for r in self.required_adapters)

    def require_complete(self) -> "ResolvedLanguageContract":
        """Return self if every declared adapter resolved; else raise (RED)."""
        if self.missing_adapters:
            raise IncompleteLanguageContractError(self.language_id, self.missing_adapters)
        return self

    def to_trace(self) -> dict[str, Any]:
        """Run-trace fields proving the run went through the contract seam."""
        return {
            "resolved_language_profile_id": self.language_id,
            "language_contract_hash": self.content_hash,
            "adapter_ids": list(self.adapter_ids),
            "missing_adapters": [r.ref for r in self.missing_adapters],
        }


def build_language_contract(
    profile: LanguageProfile,
    *,
    adapter_registry: AdapterRegistry | None = None,
) -> ResolvedLanguageContract:
    """Resolve a profile's declared adapters into a contract.

    When the caller passes NO ``adapter_registry`` (so resolution falls back to the
    process-wide :data:`default_adapter_registry`), the built-in adapters are
    LAZILY registered first — so a default-registry contract for go/typescript
    resolves the ``runner_report`` adapter instead of reporting it missing. An
    EXPLICIT ``adapter_registry`` (e.g. a test's empty ``AdapterRegistry()``) is
    left untouched: it keeps the prior "names an unregistered adapter ⇒ missing"
    behavior, so callers can still exercise the incomplete-contract path. The
    builtin import is done INSIDE the function to avoid an import cycle at module
    load (registration is lazy by construction).
    """
    if adapter_registry is None:
        from .builtin_adapters import ensure_builtin_adapters_registered

        ensure_builtin_adapters_registered(default_adapter_registry)
    registry = adapter_registry if adapter_registry is not None else default_adapter_registry
    required = _declared_adapter_requirements(profile)
    resolved: list[str] = []
    missing: list[AdapterRequirement] = []
    for req in required:
        if (req.kind, req.id) in registry:
            resolved.append(req.ref)
        else:
            missing.append(req)
    return ResolvedLanguageContract(
        language_id=profile.identity.id,
        profile=profile,
        required_adapters=required,
        resolved_adapter_refs=tuple(resolved),
        missing_adapters=tuple(missing),
        content_hash=_contract_hash(profile, required),
    )


def resolve_language_profile(
    config: Mapping[str, Any] | None,
    *,
    registry: LanguageRegistry | None = None,
) -> LanguageProfile | None:
    """Resolve the project's declared language to a profile.

    Returns ``None`` when no ``project.language`` is declared (the caller stays
    on the legacy path). Raises ``UnknownLanguageError`` when a language IS
    declared but no profile matches — a declared-unknown language is an honest
    error, never silently ignored. This is the single language-resolution seam;
    new code MUST use it instead of a fixed ``registry.resolve("go")``.
    """
    lang_registry = registry if registry is not None else default_registry
    language = _config_language(config)
    if not language:
        return None
    return lang_registry.resolve(language)


def resolve_language_contract(
    config: Mapping[str, Any] | None,
    *,
    language_registry: LanguageRegistry | None = None,
    adapter_registry: AdapterRegistry | None = None,
) -> ResolvedLanguageContract | None:
    """``config`` -> resolved language contract, or ``None`` if no language."""
    profile = resolve_language_profile(config, registry=language_registry)
    if profile is None:
        return None
    return build_language_contract(profile, adapter_registry=adapter_registry)
