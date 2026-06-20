"""Language-generality subsystem (Phase 1, additive).

This package introduces the declarative ``LanguageProfile`` model plus a
registry/loader, per GPT-5.5 Pro's language-generality redesign
(``dogfood/gpt_language_generality_design.md``).

**Phase 1 is purely ADDITIVE.** Nothing here is wired into the existing
gates yet. The goal is to model the taxonomy and prove the data (Python,
TypeScript, Go profiles) loads and round-trips, with the load-bearing
invariant that source locations are a SET (``source_sets``) — there is NO
single ``source_root`` field — so a language like Go is never forced under
``src/``.
"""

from __future__ import annotations

from .profile import (
    AdapterRef,
    ArtifactsSpec,
    CommandSpec,
    DependencyIntegrityFile,
    Identity,
    ImportsSpec,
    LanguageProfile,
    LayoutSpec,
    ManifestSpec,
    PackageRoot,
    ReportSpec,
    ScaffoldSpec,
    ScopeSpec,
    SourceSet,
    TestSet,
    TestsSpec,
    ToolchainSpec,
    VerifySpec,
)
from .loader import LanguageProfileError, load_language_profile
from .registry import (
    AdapterRegistry,
    LanguageRegistry,
    UnknownLanguageError,
    default_registry,
)

__all__ = [
    # model
    "AdapterRef",
    "ArtifactsSpec",
    "CommandSpec",
    "DependencyIntegrityFile",
    "Identity",
    "ImportsSpec",
    "LanguageProfile",
    "LayoutSpec",
    "ManifestSpec",
    "PackageRoot",
    "ReportSpec",
    "ScaffoldSpec",
    "ScopeSpec",
    "SourceSet",
    "TestSet",
    "TestsSpec",
    "ToolchainSpec",
    "VerifySpec",
    # loader
    "LanguageProfileError",
    "load_language_profile",
    # registry
    "AdapterRegistry",
    "LanguageRegistry",
    "UnknownLanguageError",
    "default_registry",
]
