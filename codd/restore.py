"""CoDD restore — reconstruct design documents from extracted facts."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from codd.config import load_project_config
from codd.generator import (
    DOC_TYPE_BY_DIR,
    DETAILED_DESIGN_SECTIONS,
    TYPE_SECTIONS,
    GenerationResult,
    WaveArtifact,
    _build_depended_by_map,
    _invoke_ai_command,
    _is_detailed_design_output,
    _load_wave_artifacts,
    _render_document,
    _resolve_ai_command,
    _sanitize_generated_body,
)
from codd.planner import ExtractedDocument, _load_extracted_documents

INFERRED_REQUIREMENT_SECTIONS = [
    "Overview",
    "Functional Requirements",
    "Non-Functional Requirements",
    "Constraints",
    "Open Questions",
]


def restore_wave(
    project_root: Path,
    wave: int,
    force: bool = False,
    ai_command: str | None = None,
) -> list[GenerationResult]:
    """Restore design documents for a wave from extracted facts."""
    config = load_project_config(project_root)
    artifacts = _load_wave_artifacts(config)
    selected = [a for a in artifacts if a.wave == wave]
    if not selected:
        raise ValueError(f"wave_config has no entries for wave {wave}")

    extracted_documents = _load_extracted_documents(project_root, config)
    if not extracted_documents:
        raise ValueError(
            "no extracted documents found in codd/extracted/. "
            "Run 'codd extract' first to generate them from source code."
        )

    resolved_ai_command = _resolve_ai_command(config, ai_command, command_name="restore")
    depended_by_map = _build_depended_by_map(artifacts)

    results: list[GenerationResult] = []
    for artifact in selected:
        output_path = project_root / artifact.output
        if output_path.exists() and not force:
            results.append(GenerationResult(node_id=artifact.node_id, path=output_path, status="skipped"))
            continue

        output_path.parent.mkdir(parents=True, exist_ok=True)
        prompt = _build_restoration_prompt(artifact, extracted_documents)
        raw_body = _invoke_ai_command(resolved_ai_command, prompt)
        body = _sanitize_generated_body(artifact.title, raw_body, output_path=artifact.output)
        content = _render_document(
            artifact=artifact,
            global_conventions=[],
            depended_by=depended_by_map.get(artifact.node_id, []),
            body=body,
        )
        output_path.write_text(content, encoding="utf-8")
        results.append(GenerationResult(node_id=artifact.node_id, path=output_path, status="restored"))

    return results


def _build_restoration_prompt(
    artifact: WaveArtifact,
    extracted_documents: list[ExtractedDocument],
) -> str:
    """Build a prompt that asks AI to reconstruct design intent from extracted facts."""
    # Determine doc type and sections
    parts = PurePosixPath(artifact.output).parts
    doc_type = "document"
    if len(parts) >= 3 and parts[0] == "docs":
        doc_type = DOC_TYPE_BY_DIR.get(parts[1], "document")

    is_detailed = _is_detailed_design_output(artifact.output)
    is_requirement = doc_type == "requirement"

    if is_requirement:
        section_names = INFERRED_REQUIREMENT_SECTIONS
    elif is_detailed:
        section_names = DETAILED_DESIGN_SECTIONS
    else:
        section_names = TYPE_SECTIONS.get(doc_type, TYPE_SECTIONS["document"])
    required_headings = [f"## {i}. {name}" for i, name in enumerate(section_names, start=1)]

    # Filter extracted docs relevant to this artifact's modules
    relevant_docs = extracted_documents
    if artifact.modules:
        module_set = set(artifact.modules)
        relevant_docs = [
            doc for doc in extracted_documents
            if _is_relevant_extracted_doc(doc, module_set)
        ]
        # If filtering left nothing, use all docs
        if not relevant_docs:
            relevant_docs = extracted_documents

    if is_requirement:
        lines = _build_requirement_inference_header(artifact)
    else:
        lines = [
            f"You are RESTORING a {doc_type} document for a brownfield project.",
            "This project already has working code. The extracted documents below describe the actual codebase — its modules, symbols, dependencies, patterns, and architecture.",
            "",
            "Your task is to RECONSTRUCT the design intent behind the existing code.",
            "Do NOT write aspirational or forward-looking design. Write what the system IS, not what it should be.",
            "The extracted facts are ground truth. Your job is to elevate them into a coherent design narrative.",
            "",
            "Document to restore:",
            f"  Node ID: {artifact.node_id}",
            f"  Title: {artifact.title}",
            f"  Output: {artifact.output}",
        ]

    if artifact.modules:
        lines.append(f"  Covers modules: {', '.join(artifact.modules)}")

    lines.extend([
        "",
        "ABSOLUTE PROHIBITION: Do not emit YAML frontmatter, implementation notes, "
        "TODO placeholders, or meta-commentary about the writing process. "
        "Start directly with the document content.",
        "",
    ])

    if is_requirement:
        lines.extend([
            "Inference guidelines:",
            "- INFER what the original requirements were from what the code actually does.",
            "- Functional Requirements: list every capability the system provides, derived from modules, classes, API routes, and function signatures.",
            "- Non-Functional Requirements: infer from code patterns (e.g., async = performance concern, rate limiting = scalability, RLS = security, caching = latency).",
            "- Constraints: infer technology choices and architectural constraints from frameworks, libraries, and patterns used.",
            "- Where intent is ambiguous, state the most likely interpretation and mark it as [inferred].",
            "- Use concrete names from the extracted docs: module names, class names, function signatures, route paths.",
            f"- Use this section structure: {', '.join(section_names)}.",
            "- After the title, continue directly with section headings.",
        ])
    else:
        lines.extend([
            "Restoration guidelines:",
            "- Describe the system as it exists, based on the extracted facts.",
            "- Explain design decisions that can be inferred from the code structure (e.g., why certain modules depend on others, why certain patterns were chosen).",
            "- Use concrete names from the extracted docs: module names, class names, function signatures, route paths, schema tables.",
            "- Where design intent is ambiguous from code alone, state the most likely interpretation and flag it as inferred.",
            f"- Use this section structure: {', '.join(section_names)}.",
            "- After the title, continue directly with section headings.",
        ])

    if is_detailed:
        lines.extend([
            "",
            "This is a detailed design document under docs/detailed_design/.",
            "Include Mermaid diagrams to visualize ownership, dependencies, sequences, or state machines based on the extracted facts.",
            "Add prose after each diagram explaining what the code does and why.",
        ])

    if artifact.conventions:
        lines.extend([
            "",
            "Conventions (release-blocking constraints) detected for this artifact:",
        ])
        for i, conv in enumerate(artifact.conventions, 1):
            targets = ", ".join(str(t) for t in conv.get("targets", []))
            reason = str(conv.get("reason", "")).strip() or "(no reason)"
            lines.append(f"  {i}. Targets: {targets} — {reason}")

    lines.extend([
        "",
        "Output contract:",
        "- Write the finished document body now.",
        "- The first content line after the title must be the first required section heading below.",
        "- Use these section headings exactly once and in this order:",
    ])
    lines.extend(required_headings)

    if is_detailed:
        lines.extend([
            "- Under the Mermaid Diagrams section, include at least one ```mermaid``` fenced block.",
        ])

    lines.extend([
        "",
        "Extracted documents (ground truth about the existing codebase):",
    ])

    for doc in relevant_docs:
        lines.extend([
            f"--- BEGIN EXTRACTED {doc.path} ({doc.node_id}) ---",
            doc.content.rstrip(),
            f"--- END EXTRACTED {doc.path} ---",
            "",
        ])

    if is_requirement:
        lines.append(
            "Final instruction: infer the requirements from the extracted facts above. "
            "These are INFERRED requirements — describing what was built, not original intent. "
            "Output the Markdown body now."
        )
    else:
        lines.append(
            "Final instruction: reconstruct the design document from the extracted facts above. "
            "Describe what exists, not what should exist. Output the Markdown body now."
        )

    return "\n".join(lines).rstrip() + "\n"


def _build_requirement_inference_header(artifact: WaveArtifact) -> list[str]:
    """Build the header lines for requirements inference prompt."""
    return [
        "You are INFERRING REQUIREMENTS from an existing codebase (brownfield project).",
        "The extracted documents below describe the actual code — modules, symbols, dependencies, patterns, and architecture.",
        "",
        "Your task is to REVERSE-ENGINEER what the original requirements were, based on what the code does.",
        "These are INFERRED requirements — they describe what WAS built, not what SHOULD be built.",
        "The code is ground truth. Derive the requirements from the structural facts.",
        "",
        "Important distinctions:",
        "- You CANNOT know features that were planned but never implemented.",
        "- You CANNOT distinguish bugs from intentional behavior — describe observed behavior.",
        "- You CANNOT know business context that isn't reflected in code (stakeholder decisions, trade-off reasoning).",
        "- Mark any non-obvious inference with [inferred] so humans can verify later.",
        "",
        "Document to restore:",
        f"  Node ID: {artifact.node_id}",
        f"  Title: {artifact.title}",
        f"  Output: {artifact.output}",
    ]


def _is_relevant_extracted_doc(doc: ExtractedDocument, module_set: set[str]) -> bool:
    """Check if an extracted doc is relevant to the given module set."""
    # Always include system-context and architecture-overview
    if "system-context" in doc.node_id or "architecture-overview" in doc.node_id:
        return True
    # Include module docs that match
    for module_name in module_set:
        if module_name in doc.node_id or module_name in doc.path:
            return True
    return False
