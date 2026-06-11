"""CoDD restore — reconstruct design documents from extracted facts.

Brownfield reverse-restoration aims for the *principled ceiling*: recover
everything the available evidence (source + tests + IaC + docs) can yield, have
every restored statement carry provenance + a confidence band, and surface the
irreducible residue (rationale/intent that is irrecoverable in principle) as
explicit machine-readable open-questions instead of fabricating.

This module assembles a structured :class:`EvidenceBundle` from ALL available
deterministic sources and feeds it into the restoration prompt:

* **Tests as functional-requirements evidence** — the deterministic
  ``ModuleInfo.test_details`` (test names / fixtures, surfaced from the extractor)
  are the richest source of acceptance criteria / verifiable behaviors.
* **IaC → NFR/ops evidence** — :func:`codd.iac_nfr.derive_iac_nfrs` turns the
  structured ``ProjectFacts.infra_config`` into NFR candidates + operational
  facts, each already carrying ``source`` provenance + a ``confidence`` level.
* **Docs/ADR/README/comments as rationale evidence** — an in-repo source of
  the *why*; ingested (bounded) so the model can cite them rather than invent.
* **Git history as testimony** — :mod:`codd.git_evidence` separates fact (the
  diff) from testimony (the commit message — a claim about the *why*).
  Testimony attaches only where its changes survive into HEAD (``git blame``),
  is capped at the amber band, and may only feed ``candidate_answer`` entries
  inside open questions or corroborate amber statements — never green facts.

The prompt then demands a machine-readable provenance / confidence-band /
open-questions block which :func:`codd.generator.extract_restoration_meta` lifts
into the restored document's ``codd:`` frontmatter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from codd.config import load_project_config
from codd.generator import (
    DOC_TYPE_BY_DIR,
    DETAILED_DESIGN_SECTIONS,
    OPERATIONAL_BEHAVIOR_MODEL_BLOCK,
    TYPE_SECTIONS,
    GenerationResult,
    WaveArtifact,
    _build_depended_by_map,
    _invoke_ai_command,
    _is_detailed_design_output,
    _load_wave_artifacts,
    _render_document,
    _resolve_ai_command,
    _resolve_generation_capabilities,
    _sanitize_generated_body,
    extract_restoration_meta,
)
from codd.git_evidence import (
    GitTestimony,
    SupersessionChain,
    collect_git_testimony,
    detect_supersession_chains,
)
from codd.iac_nfr import NfrCandidate, derive_iac_nfrs
from codd.planner import ExtractedDocument, _load_extracted_documents
from codd.project_types import ProjectCapabilities

INFERRED_REQUIREMENT_SECTIONS = [
    "Overview",
    "Functional Requirements",
    "Non-Functional Requirements",
    "Constraints",
    "Open Questions",
    "Human Review Issues",
]

# Doc-type families that the IaC/NFR evidence is directly relevant to. Design
# docs also receive it (system design references infra/topology), but the
# dedicated infra/ops/NFR artifacts are where it is load-bearing.
_NFR_DOC_TYPES = {"requirement", "operations", "design"}

# Output-path tokens that mark an artifact as an infrastructure / deployment /
# operations / NFR artifact (capability-gated, consistent with R1 profiles).
_INFRA_OPS_PATH_TOKENS = (
    "infra",
    "infrastructure",
    "deploy",
    "deployment",
    "operations",
    "ops",
    "runbook",
)
_NFR_PATH_TOKENS = ("non_functional", "nfr")

# Bounded ingestion limits for supplementary rationale evidence.
_MAX_RATIONALE_FILES = 12
_MAX_RATIONALE_FILE_CHARS = 8_000
_MAX_RATIONALE_TOTAL_CHARS = 60_000

# Documentation / rationale sources (the only in-repo "why").
_RATIONALE_GLOBS = (
    "README*",
    "readme*",
    "docs/adr/**/*.md",
    "docs/adr/**/*.markdown",
    "docs/decisions/**/*.md",
    "docs/decisions/**/*.markdown",
    "CHANGELOG*",
    "changelog*",
    "docs/CHANGELOG*",
    "ADR*",
)

_RATIONALE_SKIP_DIR_PARTS = {
    "node_modules",
    ".git",
    "dist",
    "build",
    "__pycache__",
    ".venv",
    "venv",
    "vendor",
}


@dataclass
class EvidenceBundle:
    """All deterministic evidence assembled for a restoration run.

    Every field is additive and degrades gracefully to empty: a project with no
    IaC, no tests, and no docs yields an empty bundle, which steers the prompt
    toward emitting open_questions rather than fabricating.
    """

    # category -> list of test evidence dicts {name, source, fixtures}
    test_evidence: list[dict[str, Any]] = field(default_factory=list)
    # IaC-derived NFR candidates + operational facts (each carries source+confidence)
    nfr_candidates: list[NfrCandidate] = field(default_factory=list)
    # structured infra facts, as serialized config summaries
    infra_facts: list[dict[str, Any]] = field(default_factory=list)
    # supplementary rationale evidence {path, content}
    rationale_docs: list[dict[str, str]] = field(default_factory=list)
    # git-history testimony (blame-anchored, amber-capped, kind=testimony)
    git_testimony: list[GitTestimony] = field(default_factory=list)
    # deterministic supersession chains (rejected-alternatives evidence)
    supersession_chains: list[SupersessionChain] = field(default_factory=list)

    def has_any(self) -> bool:
        return bool(
            self.test_evidence
            or self.nfr_candidates
            or self.infra_facts
            or self.rationale_docs
            or self.git_testimony
            or self.supersession_chains
        )


def restore_wave(
    project_root: Path,
    wave: int,
    force: bool = False,
    ai_command: str | None = None,
    feedback: str | None = None,
) -> list[GenerationResult]:
    """Restore design documents for a wave from extracted facts + evidence."""
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
    capabilities = _resolve_generation_capabilities(config, project_root)
    bands = _load_bands_config(config)
    # Deterministic, all-source evidence bundle (best-effort; degrades to empty).
    evidence = _assemble_evidence_bundle(project_root, config)

    results: list[GenerationResult] = []
    for artifact in selected:
        output_path = project_root / artifact.output
        if output_path.exists() and not force:
            results.append(GenerationResult(node_id=artifact.node_id, path=output_path, status="skipped"))
            continue

        output_path.parent.mkdir(parents=True, exist_ok=True)
        prompt = _build_restoration_prompt(
            artifact,
            extracted_documents,
            feedback=feedback,
            evidence=evidence,
            capabilities=capabilities,
            bands=bands,
        )
        raw_body = _invoke_ai_command(resolved_ai_command, prompt)
        restoration_meta = extract_restoration_meta(raw_body)
        body = _sanitize_generated_body(artifact.title, raw_body, output_path=artifact.output)
        content = _render_document(
            artifact=artifact,
            global_conventions=[],
            depended_by=depended_by_map.get(artifact.node_id, []),
            body=body,
            restoration_meta=restoration_meta,
        )
        output_path.write_text(content, encoding="utf-8")
        results.append(GenerationResult(node_id=artifact.node_id, path=output_path, status="restored"))

    return results


# ---------------------------------------------------------------------------
# Evidence bundle assembly (deterministic, all-source)
# ---------------------------------------------------------------------------
def _assemble_evidence_bundle(project_root: Path, config: dict[str, Any]) -> EvidenceBundle:
    """Assemble the deterministic evidence bundle from ALL available sources.

    Best-effort and fail-open: any source that cannot be read contributes
    nothing rather than aborting restoration (graceful degradation → more
    open_questions, never fabrication).
    """

    bundle = EvidenceBundle()

    # Tests + IaC come from the same deterministic extractor that `codd extract`
    # uses, so restore consumes the exact ground-truth facts (no re-derivation).
    facts = _safe_extract_facts(project_root, config)
    if facts is not None:
        bundle.test_evidence = _collect_test_evidence(facts)
        try:
            bundle.nfr_candidates = derive_iac_nfrs(facts.infra_config)
        except Exception:
            bundle.nfr_candidates = []
        bundle.infra_facts = _summarize_infra_facts(facts.infra_config)

    bundle.rationale_docs = _collect_rationale_docs(project_root)

    # Git-history testimony — blame-anchored to the files the extractor proved
    # exist (the same anchors provenance locators use). Skippable via
    # restore.git_evidence.enabled; degrades to empty for non-repos.
    if facts is not None and _git_evidence_enabled(config):
        file_paths = _collect_evidence_file_paths(facts)
        if file_paths:
            try:
                bundle.git_testimony = collect_git_testimony(project_root, file_paths)
            except Exception:
                bundle.git_testimony = []
            try:
                bundle.supersession_chains = detect_supersession_chains(
                    project_root, file_paths
                )
            except Exception:
                bundle.supersession_chains = []
    return bundle


def _git_evidence_enabled(config: dict[str, Any]) -> bool:
    """Read restore.git_evidence.enabled (default true) safely."""
    restore_section = config.get("restore") if isinstance(config, dict) else None
    if not isinstance(restore_section, dict):
        return True
    git_section = restore_section.get("git_evidence")
    if not isinstance(git_section, dict):
        return True
    return bool(git_section.get("enabled", True))


def _collect_evidence_file_paths(facts: Any) -> list[str]:
    """Source-file anchors for git testimony: the extracted modules' files.

    Deterministic and bounded (collect_git_testimony re-caps at its own
    max_locators); ordering is stable (sorted module names, file order as
    extracted, de-duplicated).
    """

    paths: list[str] = []
    seen: set[str] = set()
    modules = getattr(facts, "modules", {}) or {}
    for module_name in sorted(modules):
        module = modules[module_name]
        for rel in getattr(module, "files", []) or []:
            value = str(rel)
            if value and value not in seen:
                seen.add(value)
                paths.append(value)
    return paths


def _safe_extract_facts(project_root: Path, config: dict[str, Any]):
    """Run the deterministic fact extractor, returning None on any failure."""
    try:
        from codd.extractor import extract_facts
    except Exception:
        return None

    project_section = config.get("project") if isinstance(config, dict) else None
    language = None
    if isinstance(project_section, dict):
        language = project_section.get("language") or None
    scan_section = config.get("scan") if isinstance(config, dict) else None
    source_dirs = None
    exclude = None
    if isinstance(scan_section, dict):
        raw_dirs = scan_section.get("source_dirs")
        if isinstance(raw_dirs, list) and raw_dirs:
            source_dirs = [str(d) for d in raw_dirs]
        raw_exclude = scan_section.get("exclude")
        if isinstance(raw_exclude, list) and raw_exclude:
            exclude = [str(e) for e in raw_exclude]

    try:
        return extract_facts(
            project_root,
            language=language,
            source_dirs=source_dirs,
            exclude_patterns=exclude,
        )
    except Exception:
        return None


def _collect_test_evidence(facts: Any) -> list[dict[str, Any]]:
    """Surface deterministic per-test metadata as functional-requirements evidence.

    Tests are the richest verifiable-behavior source: each test name and its
    owning source module is a candidate acceptance criterion. We carry the test
    file path as provenance (``file::test_name``) so the model can cite it.
    """

    evidence: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    modules = getattr(facts, "modules", {}) or {}
    for module_name in sorted(modules):
        module = modules[module_name]
        for test_info in getattr(module, "test_details", []) or []:
            file_path = getattr(test_info, "file_path", "") or ""
            source_module = getattr(test_info, "source_module", None) or module_name
            for test_name in getattr(test_info, "test_functions", []) or []:
                key = (file_path, str(test_name))
                if key in seen:
                    continue
                seen.add(key)
                evidence.append(
                    {
                        "test": str(test_name),
                        "module": source_module,
                        "source": f"{file_path}::{test_name}" if file_path else str(test_name),
                        "fixtures": list(getattr(test_info, "fixtures", []) or []),
                    }
                )
    return evidence


def _summarize_infra_facts(infra_config: Any) -> list[dict[str, Any]]:
    """Compact, deterministic summary of structured IaC facts for the prompt."""
    summaries: list[dict[str, Any]] = []
    if not isinstance(infra_config, dict):
        return summaries
    for rel_path in sorted(infra_config):
        config = infra_config[rel_path]
        services = getattr(config, "services", []) or []
        resources = getattr(config, "resources", []) or []
        pipelines = getattr(config, "pipelines", []) or []
        images = getattr(config, "images", []) or []
        summary: dict[str, Any] = {
            "file": getattr(config, "file_path", rel_path),
            "format": getattr(config, "format", ""),
        }
        if services:
            summary["services"] = [s.get("name") for s in services if isinstance(s, dict)]
        if resources:
            summary["resources"] = [
                f"{r.get('kind') or r.get('type') or '?'}:{r.get('name') or ''}".strip(":")
                for r in resources
                if isinstance(r, dict)
            ]
        if pipelines:
            summary["pipelines"] = [p.get("name") for p in pipelines if isinstance(p, dict)]
        if images:
            summary["images"] = len(images)
        recognized = getattr(config, "recognized_kind", "")
        if recognized:
            summary["recognized_kind"] = recognized
        summaries.append(summary)
    return summaries


def _collect_rationale_docs(project_root: Path) -> list[dict[str, str]]:
    """Ingest bounded README/ADR/CHANGELOG/decision docs — the in-repo 'why'.

    These are the only in-repo source of *rationale*. They are tagged as evidence
    so the model can cite them rather than invent rationale. Git history is out of
    scope.
    """

    found: list[Path] = []
    seen: set[Path] = set()
    for pattern in _RATIONALE_GLOBS:
        try:
            matches = sorted(project_root.glob(pattern))
        except Exception:
            continue
        for path in matches:
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            parts = path.relative_to(project_root).parts if _is_under(path, project_root) else path.parts
            if any(part in _RATIONALE_SKIP_DIR_PARTS for part in parts):
                continue
            seen.add(resolved)
            found.append(path)

    found.sort(key=lambda p: p.as_posix())
    docs: list[dict[str, str]] = []
    total = 0
    for path in found:
        if len(docs) >= _MAX_RATIONALE_FILES or total >= _MAX_RATIONALE_TOTAL_CHARS:
            break
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if not text.strip():
            continue
        snippet = text[:_MAX_RATIONALE_FILE_CHARS]
        try:
            rel = path.relative_to(project_root).as_posix()
        except ValueError:
            rel = path.as_posix()
        docs.append({"path": rel, "content": snippet.rstrip()})
        total += len(snippet)
    return docs


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _load_bands_config(config: dict[str, Any]) -> dict[str, Any]:
    """Read the project's confidence-band thresholds, with safe defaults."""
    bands = config.get("bands") if isinstance(config, dict) else None
    if not isinstance(bands, dict):
        return {}
    return bands


def _is_infra_ops_artifact(output: str) -> bool:
    lowered = PurePosixPath(output).as_posix().lower()
    return any(token in lowered for token in _INFRA_OPS_PATH_TOKENS)


def _is_nfr_artifact(output: str) -> bool:
    lowered = PurePosixPath(output).as_posix().lower()
    return any(token in lowered for token in _NFR_PATH_TOKENS)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------
def _build_restoration_prompt(
    artifact: WaveArtifact,
    extracted_documents: list[ExtractedDocument],
    feedback: str | None = None,
    evidence: EvidenceBundle | None = None,
    capabilities: ProjectCapabilities | None = None,
    bands: dict[str, Any] | None = None,
) -> str:
    """Build a prompt that asks AI to reconstruct design intent from evidence."""
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
            "- Non-Functional Requirements: infer from code patterns (e.g., async = performance concern, rate limiting = scalability, RLS = security, caching = latency) AND from the deterministic IaC-derived NFR evidence below.",
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

    # Design docs (restored) must carry the same operation_flow / operational-behavior
    # structure as greenfield-generated design docs — shared single source of truth.
    if doc_type == "design":
        lines.extend(OPERATIONAL_BEHAVIOR_MODEL_BLOCK)

    # Evidence bundle — tests, IaC/NFR, rationale docs — capability-aware.
    lines.extend(
        _build_evidence_blocks(
            artifact,
            doc_type,
            is_requirement,
            evidence,
            capabilities,
        )
    )

    if artifact.conventions:
        lines.extend([
            "",
            "Conventions (release-blocking constraints) detected for this artifact:",
        ])
        for i, conv in enumerate(artifact.conventions, 1):
            targets = ", ".join(str(t) for t in conv.get("targets", []))
            reason = str(conv.get("reason", "")).strip() or "(no reason)"
            lines.append(f"  {i}. Targets: {targets} — {reason}")

    # Provenance + confidence-band + open-questions contract (the principled core).
    lines.extend(_build_provenance_contract_block(bands))

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

    if feedback:
        lines.extend([
            "--- REVIEW FEEDBACK (from previous restoration attempt) ---",
            "A reviewer found issues with a previous version of this document.",
            "You MUST address ALL of the following feedback in this restoration:",
            feedback.rstrip(),
            "--- END REVIEW FEEDBACK ---",
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


def _build_evidence_blocks(
    artifact: WaveArtifact,
    doc_type: str,
    is_requirement: bool,
    evidence: EvidenceBundle | None,
    capabilities: ProjectCapabilities | None,
) -> list[str]:
    """Render the structured evidence bundle into prompt lines.

    Test evidence reaches functional-requirements and test/design docs. IaC/NFR
    evidence + infra facts reach NFR/operations/infrastructure/deployment and
    design docs, and is GATED on capabilities: a pure library/CLI with no
    long-running service and no network surface is NOT pushed to invent an
    operations runbook or infrastructure NFRs (consistent with R1's conditional
    profiles). Absence of evidence is stated explicitly so the model emits
    open_questions rather than fabricating.
    """

    if evidence is None:
        evidence = EvidenceBundle()

    lines: list[str] = []

    # --- Tests as functional-requirements / verifiable-behavior evidence -----
    wants_tests = is_requirement or doc_type in {"test", "design"}
    if wants_tests:
        lines.extend([
            "",
            "Test evidence (deterministic — the richest source of verifiable behaviors / "
            "acceptance criteria). Each test name is a candidate acceptance criterion; "
            "cite its source (file::test_name) as provenance:",
        ])
        if evidence.test_evidence:
            for item in evidence.test_evidence[:200]:
                fixtures = item.get("fixtures") or []
                fixture_note = f" (fixtures: {', '.join(fixtures)})" if fixtures else ""
                lines.append(
                    f"  - module={item.get('module')} test={item.get('test')} "
                    f"source={item.get('source')}{fixture_note}"
                )
        else:
            lines.append(
                "  (No automated test evidence was found. Do NOT invent acceptance "
                "criteria; emit an open_question noting that verifiable behaviors are "
                "unconfirmed without tests.)"
            )

    # --- IaC → NFR / operational evidence (capability-gated) -----------------
    infra_relevant = _infra_ops_evidence_relevant(artifact, doc_type, is_requirement, capabilities)
    if infra_relevant:
        lines.extend([
            "",
            "Infrastructure / NFR evidence (deterministically derived from "
            "Infrastructure-as-Code). These are DATA-DRIVEN candidates: each carries a "
            "provenance source and a confidence level. Use them to ground "
            "non-functional / infrastructure / deployment / operations statements. "
            "Direct facts (a replica count, a route, a retention value) are HIGH "
            "confidence; mere-presence inferences are MEDIUM:",
        ])
        if evidence.nfr_candidates:
            for cand in evidence.nfr_candidates[:200]:
                lines.append(
                    f"  - [{cand.confidence}/{cand.kind}] ({cand.category}) "
                    f"{cand.statement}  [source: {cand.source}]"
                )
        else:
            lines.append(
                "  (No Infrastructure-as-Code evidence was found. Do NOT invent "
                "availability/scalability/DR/operations requirements; emit "
                "open_questions for any NFR/ops topic the audience needs, naming that "
                "no IaC evidence exists to ground it.)"
            )
        if evidence.infra_facts:
            lines.append("")
            lines.append("Structured infrastructure facts (raw, for grounding/topology):")
            for fact in evidence.infra_facts[:80]:
                lines.append(f"  - {fact}")

    # --- Rationale evidence (README / ADR / decisions / CHANGELOG) -----------
    lines.extend([
        "",
        "Rationale evidence (README / ADR / decision records / CHANGELOG — the ONLY "
        "in-repo source of the 'why'). Cite these for any rationale/intent statement. "
        "Rationale NOT grounded here is irrecoverable from code and MUST become an "
        "open_question, never an assertion:",
    ])
    if evidence.rationale_docs:
        for rdoc in evidence.rationale_docs:
            lines.extend([
                f"--- BEGIN RATIONALE {rdoc['path']} ---",
                rdoc["content"],
                f"--- END RATIONALE {rdoc['path']} ---",
                "",
            ])
    else:
        lines.append(
            "  (No README/ADR/decision/CHANGELOG documents were found. ALL rationale, "
            "intent, rejected alternatives, and priority weights are therefore "
            "irrecoverable — emit them as open_questions, do NOT fabricate the 'why'.)"
        )

    # --- Git history testimony (blame-anchored, amber-capped) ---------------
    lines.extend(_build_git_testimony_block(evidence))

    return lines


def _build_git_testimony_block(evidence: EvidenceBundle) -> list[str]:
    """Render git-history testimony + supersession chains into prompt lines.

    Rendered only when present: a project without git history simply has no
    testimony, and absence of evidence stays absent (open questions stay
    blank rather than being padded with fake leads).
    """

    lines: list[str] = []
    if not evidence.git_testimony and not evidence.supersession_chains:
        return lines

    lines.extend([
        "",
        "Git history testimony (UNVERIFIED — testimony, not fact). Each entry "
        "below is a commit whose changes SURVIVE into the current code (anchored "
        "via git blame, so superseded/stale intent is already excluded). The "
        "DIFF behind each commit is fact, but the MESSAGE is a CLAIM by its "
        "author about the 'why'. Testimony is capped at the amber band. You must "
        "NOT assert testimony as fact and must NOT use it to justify a green "
        "statement. Its REQUIRED use: attach it to a matching open_questions "
        "entry as a candidate_answer (keeping needs_human_confirmation: true), "
        "or cite it as corroboration for an amber statement:",
    ])

    if evidence.git_testimony:
        for item in evidence.git_testimony[:100]:
            corroboration = "[corroborated]" if item.corroborated else "[uncorroborated]"
            day = item.date[:10] if item.date else "unknown date"
            lines.append(
                f"  - commit:{item.commit} ({day}) {corroboration} "
                f"{item.subject} — {item.survival_note}"
            )
            if item.body_excerpt:
                lines.append(f"      body: {item.body_excerpt}")
    else:
        lines.append("  (no surviving-commit testimony passed the noise filter)")

    if evidence.supersession_chains:
        lines.extend([
            "",
            "Supersession chains (deterministic rejected-alternatives evidence): "
            "the history below shows an implementation being replaced or reverted "
            "over time. That an alternative existed and was rejected is FACT; the "
            "WHY of the rejection is testimony (amber). Use these to fill "
            "candidate_answer leads on 'rejected alternatives' open questions — "
            "never to assert the rejection rationale as fact:",
        ])
        for chain in evidence.supersession_chains[:40]:
            trail = " -> ".join(
                f"commit:{sha} ({date}) \"{subject}\"" for sha, date, subject in chain.commits
            )
            lines.append(f"  - {chain.file} decision trail: {trail}")
            if chain.note:
                lines.append(f"      note: {chain.note}")

    return lines


def _infra_ops_evidence_relevant(
    artifact: WaveArtifact,
    doc_type: str,
    is_requirement: bool,
    capabilities: ProjectCapabilities | None,
) -> bool:
    """Decide whether IaC/NFR evidence should be injected for this artifact.

    Always relevant for explicit infrastructure/deployment/operations/NFR
    artifacts. For generic requirement/design docs it is relevant only when the
    project actually has an operational surface (``long_running_service`` or a
    non-``none`` ``network_surface``) — a pure library/CLI is not pushed to
    invent infra/ops content.
    """

    if _is_infra_ops_artifact(artifact.output) or _is_nfr_artifact(artifact.output):
        return True
    if doc_type not in _NFR_DOC_TYPES and not is_requirement:
        return False
    if capabilities is None:
        # Untyped/legacy project: keep historical behavior (web fallback has a
        # network surface), so include the evidence.
        return True
    return bool(
        capabilities.long_running_service
        or (capabilities.network_surface and capabilities.network_surface != "none")
    )


def _build_provenance_contract_block(bands: dict[str, Any] | None) -> list[str]:
    """The principled output contract: provenance, confidence bands, open-questions.

    This is what makes "how far restoration got" machine-inspectable and what
    enforces never-fabricate: irrecoverable items become structured
    open_questions with ``needs_human_confirmation: true`` instead of invented
    facts.
    """

    green = {}
    amber = {}
    if isinstance(bands, dict):
        green = bands.get("green") if isinstance(bands.get("green"), dict) else {}
        amber = bands.get("amber") if isinstance(bands.get("amber"), dict) else {}
    green_min_conf = green.get("min_confidence", 0.9)
    green_min_count = green.get("min_evidence_count", 2)
    amber_min_conf = amber.get("min_confidence", 0.5)

    return [
        "",
        "PROVENANCE + CONFIDENCE + OPEN-QUESTIONS CONTRACT (CRITICAL — machine-read):",
        "After the prose document body, append EXACTLY ONE fenced ```yaml``` block "
        "whose single top-level key is `codd_restoration:`. CoDD lifts this block "
        "into the document's machine-readable frontmatter. It MUST contain:",
        "  provenance: a list mapping restored statements/sections to the evidence "
        "that backs them. Each item: {statement: <short claim or section name>, "
        "evidence: [<provenance strings>], band: green|amber}. Every provenance "
        "string MUST be a REAL evidence locator copied from the material above — a "
        "source file path, an IaC `file::Kind::name` source, or a test "
        "`file::test_name`. NEVER invent a provenance string.",
        "  confidence_bands: optional rollup, e.g. {green: <count>, amber: <count>}.",
        "  open_questions: a list of the irreducible residue — things irrecoverable "
        "IN PRINCIPLE from code/tests/IaC (business rationale, rejected alternatives, "
        "priority weights among NFRs, threshold justifications, unbuilt/planned "
        "intent). Each item: {question: <what is missing>, why_unrecoverable: <why "
        "code cannot answer it>, needs_human_confirmation: true}.",
        "    An open_questions entry MAY additionally carry candidate_answer: "
        "{text: <the lead suggested by git-history testimony>, provenance: "
        "\"commit:<short-sha> (<date>)\", corroborated: true|false} — ONLY when "
        "the git history testimony above actually suggests an answer. A "
        "candidate_answer is a LEAD for the human reviewer, not an answer: "
        "needs_human_confirmation MUST remain true on that entry, the question "
        "stays open, and commit testimony must NEVER be promoted into a green "
        "statement or appear as evidence for a green provenance item. If no "
        "testimony matches, leave the entry without candidate_answer — absence "
        "of evidence stays absent.",
        "  assumptions: optional list of inferences you made that a human should "
        "confirm; each {assumption: <text>, basis: <evidence or 'none'>, "
        "needs_human_confirmation: true}.",
        "",
        "Confidence-band rule (from this project's bands config):",
        f"  - green = high confidence: derived DIRECTLY from a deterministic fact "
        f"(a replica count, a route, a test assertion) AND corroborated by "
        f">= {green_min_count} evidence sources (min_confidence {green_min_conf}).",
        f"  - amber = lower / single-source / inferred intent (min_confidence "
        f"{amber_min_conf}).",
        "  - Statements that infer INTENT are at most amber. Statements with NO "
        "evidence are NOT statements at all — they are open_questions.",
        "  - Git-history testimony (a `commit:<short-sha> (<date>)` locator) is "
        "capped at amber: it may corroborate an amber statement, but a statement "
        "whose only evidence is commit testimony can NEVER be green.",
        "",
        "NEVER-FABRICATE RULE (overrides everything): if the evidence above does not "
        "support a rationale/intent/NFR claim, you MUST NOT assert it. Emit an "
        "open_question naming what is missing and why it cannot be inferred. A "
        "restored document that invents rationale is a FAILED restoration.",
    ]


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
