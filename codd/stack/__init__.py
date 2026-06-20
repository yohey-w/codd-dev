"""Stack-composition subsystem — the FRAMEWORK half of v3.0 composable Profile.

Owner 2026-06-20: "v3.0 は言語とフレームワークを profile にする — 言語だけじゃない".
The language half lives in :mod:`codd.languages`; this package adds the
declarative ``FrameworkProfile`` / ``AddonProfile`` model (per
``dogfood/gpt_composable_profile_design.md`` §1, §3) and — in later phases — the
loader/registry, the curated framework/addon profiles (Next.js, Prisma,
Playwright), and the ``ResolvedStackContract`` composition that merges
language ⊕ framework ⊕ addons into the single contract the harness consumes.

**Additive**: nothing here changes ``codd.languages`` or the existing gates yet.
"""

from __future__ import annotations

from .profile import (
    AddonProfile,
    AdapterRef,
    AssertionsSpec,
    CommandSpec,
    ConformanceSpec,
    Detection,
    DetectionManifest,
    FileRole,
    FrameworkProfile,
    LanguageRequirement,
    LayerArtifactsSpec,
    LayerIdentity,
    LayerKind,
    LayerLayoutSpec,
    LayerProfile,
    LayerRequirements,
    Obligation,
    OperationsSpec,
    ReportSpec,
    SourceSet,
    Variant,
)

__all__ = [
    "AddonProfile",
    "AdapterRef",
    "AssertionsSpec",
    "CommandSpec",
    "ConformanceSpec",
    "Detection",
    "DetectionManifest",
    "FileRole",
    "FrameworkProfile",
    "LanguageRequirement",
    "LayerArtifactsSpec",
    "LayerIdentity",
    "LayerKind",
    "LayerLayoutSpec",
    "LayerProfile",
    "LayerRequirements",
    "Obligation",
    "OperationsSpec",
    "ReportSpec",
    "SourceSet",
    "Variant",
]
