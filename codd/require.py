"""CoDD require — infer requirement documents from extracted facts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Any

import yaml

from codd.config import load_project_config
from codd.generator import _invoke_ai_command, _resolve_ai_command, _sanitize_generated_body
from codd.planner import ExtractedDocument, _load_extracted_documents
from codd.restore import INFERRED_REQUIREMENT_SECTIONS


CROSS_CUTTING_CLUSTER = "cross-cutting"
_CROSS_CUTTING_MARKERS = ("system-context", "architecture-overview")
_GENERIC_PATH_TOKENS = {
    "app",
    "apps",
    "codd",
    "doc",
    "docs",
    "extracted",
    "lib",
    "libs",
    "module",
    "modules",
    "package",
    "packages",
    "service",
    "services",
    "source",
    "sources",
    "src",
}


@dataclass(frozen=True)
class RequireResult:
    """Result of generating one inferred requirements document."""

    node_id: str
    path: Path
    status: str


def cluster_extracted_docs(
    docs: list[ExtractedDocument],
    config: dict[str, Any],
) -> dict[str, list[ExtractedDocument]]:
    """Group extracted docs into per-boundary clusters plus cross-cutting context."""
    clusters: dict[str, list[ExtractedDocument]] = {}
    cross_cutting_docs = [doc for doc in docs if _is_cross_cutting_doc(doc)]
    module_docs = [doc for doc in docs if not _is_cross_cutting_doc(doc)]

    service_boundaries = _normalize_service_boundaries(config.get("service_boundaries"))
    assigned_doc_keys: set[str] = set()

    for boundary_name, boundary_tokens in service_boundaries.items():
        matched = [
            doc for doc in module_docs
            if _doc_key(doc) not in assigned_doc_keys and _doc_matches_boundary(doc, boundary_tokens)
        ]
        if matched:
            clusters[boundary_name] = sorted(matched, key=_doc_key)
            assigned_doc_keys.update(_doc_key(doc) for doc in matched)

    for doc in module_docs:
        if _doc_key(doc) in assigned_doc_keys:
            continue
        cluster_name = _infer_doc_cluster(doc)
        clusters.setdefault(cluster_name, []).append(doc)

    for cluster_name, cluster_docs in list(clusters.items()):
        clusters[cluster_name] = sorted(cluster_docs, key=_doc_key)

    clusters[CROSS_CUTTING_CLUSTER] = sorted(cross_cutting_docs, key=_doc_key)
    return clusters


def _build_require_header(cluster_name: str) -> list[str]:
    """Build the brownfield requirement-inference prompt header."""
    scope_name = "system-wide cross-cutting behavior" if cluster_name == CROSS_CUTTING_CLUSTER else cluster_name
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
        "- Mark any non-obvious inference with [inferred] or [speculative] so humans can verify later.",
        "",
        "Inference scope:",
        f"  Cluster: {scope_name}",
    ]


def build_require_prompt(
    cluster_name: str,
    cluster_docs: list[ExtractedDocument],
    cross_cutting_docs: list[ExtractedDocument],
    feedback: str | None = None,
) -> str:
    """Build the AI prompt for one requirements cluster."""
    required_headings = [
        f"## {index}. {section_name}"
        for index, section_name in enumerate(INFERRED_REQUIREMENT_SECTIONS, start=1)
    ]
    unique_cluster_docs = _unique_docs(cluster_docs)
    context_docs = [
        doc for doc in _unique_docs(cross_cutting_docs)
        if _doc_key(doc) not in {_doc_key(cluster_doc) for cluster_doc in unique_cluster_docs}
    ]

    lines = _build_require_header(cluster_name)
    lines.extend(
        [
            "",
            "ABSOLUTE PROHIBITION: Do not emit YAML frontmatter, TODO placeholders, or meta-commentary about the writing process.",
            "Start directly with the document content.",
            "",
            "Inference guidelines:",
            "- Tag each inferred requirement with one of [observed], [inferred], or [speculative].",
            "- Cite concrete evidence for every requirement using extracted file paths, symbols, routes, schemas, services, or document references.",
            "- Functional Requirements: derive capabilities from modules, APIs, classes, functions, schemas, and integrations.",
            "- Non-Functional Requirements: infer quality attributes from patterns such as auth, retries, caching, async execution, observability, and deployment setup.",
            "- Constraints: capture concrete frameworks, data stores, protocols, architectural boundaries, and technology choices that the code imposes.",
            "- Open Questions: call out ambiguities that need human confirmation.",
            "- Do not invent features that are not evidenced in the extracted facts.",
            "- Do not assume standard features exist unless the extracted facts show them.",
            "- Do not write aspirational requirements or recommendations.",
            "- Include explicit review-needed notes for [speculative] or weakly supported items.",
            "",
            "Output contract:",
            "- Write the finished Markdown requirements document body now.",
            "- The first content line after the title must be the first required section heading below.",
            "- Use these section headings exactly once and in this order:",
        ]
    )
    lines.extend(required_headings)
    lines.extend(
        [
            "",
            "Primary extracted documents for this cluster:",
        ]
    )

    for doc in unique_cluster_docs:
        lines.extend(
            [
                f"--- BEGIN CLUSTER DOC {doc.path} ({doc.node_id}) ---",
                doc.content.rstrip(),
                f"--- END CLUSTER DOC {doc.path} ---",
                "",
            ]
        )

    if context_docs:
        lines.append("Cross-cutting context documents:")
        for doc in context_docs:
            lines.extend(
                [
                    f"--- BEGIN CONTEXT DOC {doc.path} ({doc.node_id}) ---",
                    doc.content.rstrip(),
                    f"--- END CONTEXT DOC {doc.path} ---",
                    "",
                ]
            )

    if feedback:
        lines.extend(
            [
                "--- REVIEW FEEDBACK (from previous generation attempt) ---",
                "A reviewer found issues with a previous version of this requirements document.",
                "You MUST address ALL of the following feedback in this generation:",
                feedback.rstrip(),
                "--- END REVIEW FEEDBACK ---",
                "",
            ]
        )

    lines.append(
        "Final instruction: infer the requirements from the extracted facts above. "
        "These are INFERRED requirements describing what was built, not hidden intent. "
        "Output the Markdown body now."
    )

    return "\n".join(lines).rstrip() + "\n"


def _build_frontmatter(cluster_name: str) -> str:
    """Build the generated requirements frontmatter."""
    node_id = _cluster_node_id(cluster_name)
    payload = {
        "codd": {
            "node_id": node_id,
            "type": "requirement",
            "depends_on": [],
            "confidence": 0.65,
            "source": "codd-require",
        }
    }
    return f"---\n{yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)}---\n\n"


def run_require(
    project_root: Path,
    output_dir: str = "docs/requirements/",
    scope: str | None = None,
    ai_command: str | None = None,
    force: bool = False,
    feedback: str | None = None,
) -> list[RequireResult]:
    """Infer requirements documents from extracted code facts."""
    project_root = project_root.resolve()
    config = load_project_config(project_root)
    extracted_documents = _load_extracted_documents(project_root, config)
    if not extracted_documents:
        raise ValueError("Run 'codd extract' first")

    clusters = cluster_extracted_docs(extracted_documents, config)
    cross_cutting_docs = clusters.get(CROSS_CUTTING_CLUSTER, [])
    target_clusters = _select_clusters(clusters, scope)
    resolved_ai_command = _resolve_ai_command(config, ai_command, command_name="require")
    base_output_dir = Path(output_dir)
    if not base_output_dir.is_absolute():
        base_output_dir = project_root / base_output_dir

    results: list[RequireResult] = []
    for cluster_name in target_clusters:
        output_path = base_output_dir / _cluster_output_name(cluster_name)
        node_id = _cluster_node_id(cluster_name)
        if output_path.exists() and not force:
            results.append(RequireResult(node_id=node_id, path=output_path, status="skipped"))
            continue

        output_path.parent.mkdir(parents=True, exist_ok=True)
        prompt = build_require_prompt(
            cluster_name,
            clusters.get(cluster_name, []),
            cross_cutting_docs,
            feedback=feedback,
        )
        title = _cluster_title(cluster_name)
        raw_body = _invoke_ai_command(resolved_ai_command, prompt)
        body = _sanitize_generated_body(title, raw_body, output_path=output_path.as_posix())
        output_path.write_text(_build_frontmatter(cluster_name) + body.rstrip() + "\n", encoding="utf-8")
        results.append(RequireResult(node_id=node_id, path=output_path, status="generated"))

    return results


def _select_clusters(clusters: dict[str, list[ExtractedDocument]], scope: str | None) -> list[str]:
    if scope:
        requested = scope.strip().lower()
        for cluster_name in clusters:
            if cluster_name.lower() == requested:
                return [cluster_name]
        available = ", ".join(sorted(name for name in clusters if clusters[name]))
        raise ValueError(f"unknown scope {scope!r}. Available scopes: {available or '(none)'}")

    cluster_names = [
        cluster_name
        for cluster_name, cluster_docs in clusters.items()
        if cluster_docs
    ]
    if CROSS_CUTTING_CLUSTER in cluster_names:
        cluster_names.remove(CROSS_CUTTING_CLUSTER)
        return [CROSS_CUTTING_CLUSTER, *sorted(cluster_names)]
    return sorted(cluster_names)


def _normalize_service_boundaries(boundaries: Any) -> dict[str, set[str]]:
    if not isinstance(boundaries, list):
        return {}

    normalized: dict[str, set[str]] = {}
    for entry in boundaries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            continue

        tokens = {_normalize_token(name)}
        modules = entry.get("modules")
        if isinstance(modules, list):
            for module in modules:
                if isinstance(module, str):
                    tokens.update(_extract_module_tokens(module))

        normalized[name.strip()] = {token for token in tokens if token}
    return normalized


def _extract_module_tokens(module_spec: str) -> set[str]:
    raw = module_spec.strip().replace("\\", "/")
    if not raw:
        return set()

    path = PurePosixPath(raw)
    parts = [part for part in path.parts if part not in {".", ".."}]
    candidates = [
        raw,
        path.name,
        path.stem,
        path.parent.name,
        *parts,
    ]

    tokens: set[str] = set()
    for candidate in candidates:
        for piece in re.split(r"[^a-zA-Z0-9]+", candidate):
            token = _normalize_token(piece)
            if token and token not in _GENERIC_PATH_TOKENS:
                tokens.add(token)
    return tokens


def _doc_matches_boundary(doc: ExtractedDocument, boundary_tokens: set[str]) -> bool:
    return bool(_extract_doc_tokens(doc) & boundary_tokens)


def _extract_doc_tokens(doc: ExtractedDocument) -> set[str]:
    path = PurePosixPath(doc.path.replace("\\", "/"))
    pieces = [
        _infer_doc_cluster(doc),
        doc.node_id,
        path.name,
        path.stem,
        path.parent.name,
        *path.parts,
    ]
    tokens: set[str] = set()
    for piece in pieces:
        for chunk in re.split(r"[^a-zA-Z0-9]+", piece):
            token = _normalize_token(chunk)
            if token and token not in _GENERIC_PATH_TOKENS:
                tokens.add(token)
    return tokens


def _infer_doc_cluster(doc: ExtractedDocument) -> str:
    if _is_cross_cutting_doc(doc):
        return CROSS_CUTTING_CLUSTER

    path = PurePosixPath(doc.path.replace("\\", "/"))
    if path.parent.name == "modules":
        return path.stem

    node_suffix = doc.node_id.split(":")[-1].strip()
    if node_suffix:
        return node_suffix

    return path.stem


def _is_cross_cutting_doc(doc: ExtractedDocument) -> bool:
    lowered_path = doc.path.lower()
    lowered_node_id = doc.node_id.lower()
    return any(marker in lowered_path or marker in lowered_node_id for marker in _CROSS_CUTTING_MARKERS)


def _cluster_output_name(cluster_name: str) -> str:
    if cluster_name == CROSS_CUTTING_CLUSTER:
        return "system-requirements.md"
    return f"{_cluster_slug(cluster_name)}-requirements.md"


def _cluster_node_id(cluster_name: str) -> str:
    if cluster_name == CROSS_CUTTING_CLUSTER:
        return "req:system"
    return f"req:{_cluster_slug(cluster_name)}"


def _cluster_title(cluster_name: str) -> str:
    if cluster_name == CROSS_CUTTING_CLUSTER:
        return "System Requirements"

    words = [
        word for word in re.split(r"[-_]+", cluster_name)
        if word
    ]
    pretty_words = [word.upper() if len(word) <= 3 else word.capitalize() for word in words]
    return f"{' '.join(pretty_words)} Requirements"


def _cluster_slug(cluster_name: str) -> str:
    return _normalize_token(cluster_name) or "requirements"


def _normalize_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _doc_key(doc: ExtractedDocument) -> str:
    return f"{doc.path}::{doc.node_id}"


def _unique_docs(docs: list[ExtractedDocument]) -> list[ExtractedDocument]:
    seen: set[str] = set()
    unique_docs: list[ExtractedDocument] = []
    for doc in docs:
        key = _doc_key(doc)
        if key in seen:
            continue
        seen.add(key)
        unique_docs.append(doc)
    return unique_docs
