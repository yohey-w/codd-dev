"""CoDD require plugins — extension point for governance/calibration features.

The plugin system allows require prompts to be enhanced with additional
inference guidelines, tag systems, and output contracts. Plugins extend
the built-in defaults with organisation-specific governance rules,
calibration datasets, and approval workflows.

Plugin resolution order:
1. Project-local: {codd_dir}/plugins/require.py
2. Site-wide: ~/.codd/plugins/require.py
3. Built-in: default guidelines (this module)

Each plugin module may define:
    INFERENCE_TAGS: list[dict]     — tag definitions (name, description)
    EVIDENCE_FORMAT: str | None    — evidence citation format (overrides builtin)
    OUTPUT_SECTIONS: list[str]     — additional output contract sections
    INFERENCE_GUIDELINES: list[str] — additional inference guidelines
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RequirePlugin:
    """Loaded plugin configuration for require prompts."""

    name: str = "builtin"
    inference_tags: list[dict] = field(default_factory=list)
    evidence_format: str | None = None
    output_sections: list[str] = field(default_factory=list)
    inference_guidelines: list[str] = field(default_factory=list)


# ── Built-in (OSS) defaults ──────────────────────────────────────

_BUILTIN_TAGS = [
    {"name": "[observed]", "description": "directly evidenced in code (explicit route, exported function, DB table, test assertion)"},
    {"name": "[inferred]", "description": "reasonable inference from patterns (e.g., retry logic implies reliability requirement)"},
    {"name": "[speculative]", "description": "weak evidence, needs human validation (commented-out code, naming conventions only)"},
    {"name": "[unknown]", "description": "no evidence found in extracted facts — gap that requires human investigation"},
    {"name": "[contradictory]", "description": "conflicting evidence across modules (e.g., two auth strategies, inconsistent schema versions)"},
]

_BUILTIN_GUIDELINES = [
    "- Functional Requirements: derive capabilities from modules, APIs, classes, functions, schemas, and integrations.",
    "- Non-Functional Requirements: infer quality attributes from patterns such as auth, retries, caching, async execution, observability, and deployment setup.",
    "- Constraints: capture concrete frameworks, data stores, protocols, architectural boundaries, and technology choices that the code imposes.",
    "- Open Questions: call out ambiguities that need human confirmation.",
    "- Do not invent features that are not evidenced in the extracted facts.",
    "- Do not assume standard features exist unless the extracted facts show them.",
    "- Do not write aspirational requirements or recommendations.",
    "- Include explicit review-needed notes for [speculative] items.",
    "- Use [unknown] to flag gaps where code evidence is absent but requirements likely exist.",
    "- Use [contradictory] when extracted facts conflict — describe both sides and flag for human resolution.",
]

_BUILTIN_EVIDENCE_FORMAT = "Evidence: src/module.py:function_name() + tests/test_module.py"

_BUILTIN_OUTPUT_SECTIONS = [
    "- Human Review Issues: prioritized list of items requiring human judgment (contradictions, gaps, ambiguous intent).",
]

BUILTIN_PLUGIN = RequirePlugin(
    name="builtin",
    inference_tags=_BUILTIN_TAGS,
    evidence_format=_BUILTIN_EVIDENCE_FORMAT,
    output_sections=_BUILTIN_OUTPUT_SECTIONS,
    inference_guidelines=_BUILTIN_GUIDELINES,
)


def load_require_plugin(project_root: Path | None = None) -> RequirePlugin:
    """Load the require plugin, checking project-local and site-wide locations.

    Falls back to built-in OSS defaults if no plugin is found.
    """
    candidates: list[Path] = []

    # Project-local
    if project_root:
        from codd.config import find_codd_dir

        codd_dir = find_codd_dir(project_root)
        if codd_dir:
            candidates.append(codd_dir / "plugins" / "require.py")

    # Site-wide
    site_dir = Path.home() / ".codd" / "plugins"
    candidates.append(site_dir / "require.py")

    for path in candidates:
        if path.is_file():
            plugin = _load_plugin_from_file(path)
            if plugin is not None:
                return plugin

    return BUILTIN_PLUGIN


def _load_plugin_from_file(path: Path) -> RequirePlugin | None:
    """Load a plugin module from a file path."""
    try:
        spec = importlib.util.spec_from_file_location("codd_require_plugin", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:
        return None

    return RequirePlugin(
        name=getattr(module, "PLUGIN_NAME", path.stem),
        inference_tags=getattr(module, "INFERENCE_TAGS", _BUILTIN_TAGS),
        evidence_format=getattr(module, "EVIDENCE_FORMAT", None),
        output_sections=getattr(module, "OUTPUT_SECTIONS", []),
        inference_guidelines=getattr(module, "INFERENCE_GUIDELINES", _BUILTIN_GUIDELINES),
    )


def build_tag_instructions(plugin: RequirePlugin) -> list[str]:
    """Build the tag instruction lines for the prompt."""
    tag_names = ", ".join(t["name"] for t in plugin.inference_tags)
    lines = [
        f"- Tag each inferred requirement with one of {tag_names}.",
    ]
    for tag in plugin.inference_tags:
        lines.append(f"  - {tag['name']}: {tag['description']}.")
    return lines


def build_evidence_instructions(plugin: RequirePlugin) -> list[str]:
    """Build evidence format instructions if the plugin defines them."""
    if not plugin.evidence_format:
        return []
    return [
        "- Cite concrete evidence for every requirement: source file path, symbol name, related test file, and git-traceable module references.",
        f"  Example: `{plugin.evidence_format}`",
    ]


def build_output_contract(plugin: RequirePlugin) -> list[str]:
    """Build additional output contract lines from the plugin."""
    return plugin.output_sections
