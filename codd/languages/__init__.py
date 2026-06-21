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
    CiSpec,
    CommandSpec,
    DependencyIntegrityFile,
    Identity,
    ImplementOracleProfileSpec,
    ImplementOracleStepSpec,
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
    VerifyObservationPolicy,
    VerifySpec,
)
from .loader import LanguageProfileError, load_language_profile
from .registry import (
    AdapterRegistry,
    LanguageRegistry,
    UnknownLanguageError,
    default_adapter_registry,
    default_registry,
)
from .contract import (
    AdapterRequirement,
    IncompleteLanguageContractError,
    ResolvedLanguageContract,
    build_language_contract,
    resolve_language_contract,
    resolve_language_profile,
)
from .verify_plan import (
    ShadowComparison,
    VerifyClass,
    VerifyOutcome,
    VerifyRunPlan,
    build_verify_plan,
    classify_verify_outcome,
    shadow_compare,
)
from .compat import (
    UnsupportedLayoutShape,
    layout_profile_from_language_profile,
)
from .path_planner import OutputPlan, PathPlanError, PathPlanner

__all__ = [
    # model
    "AdapterRef",
    "ArtifactsSpec",
    "CiSpec",
    "CommandSpec",
    "DependencyIntegrityFile",
    "Identity",
    "ImplementOracleProfileSpec",
    "ImplementOracleStepSpec",
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
    "VerifyObservationPolicy",
    "VerifySpec",
    # loader
    "LanguageProfileError",
    "load_language_profile",
    # registry
    "AdapterRegistry",
    "LanguageRegistry",
    "UnknownLanguageError",
    "default_adapter_registry",
    "default_registry",
    # resolved language contract (the language-free kernel seam, v2.68)
    "AdapterRequirement",
    "IncompleteLanguageContractError",
    "ResolvedLanguageContract",
    "build_language_contract",
    "resolve_language_contract",
    "resolve_language_profile",
    # verify run plan + semantic classifier (v2.69a, shadow)
    "ShadowComparison",
    "VerifyClass",
    "VerifyOutcome",
    "VerifyRunPlan",
    "build_verify_plan",
    "classify_verify_outcome",
    "shadow_compare",
    # compat shim (LanguageProfile -> legacy LayoutProfile)
    "UnsupportedLayoutShape",
    "layout_profile_from_language_profile",
    # path planner (single declared-output authority)
    "OutputPlan",
    "PathPlanError",
    "PathPlanner",
]
