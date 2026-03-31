"""CoDD template generator driven by wave_config."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
from typing import Any

import yaml

from codd.config import load_project_config


DEFAULT_AI_COMMAND = 'claude --print --model claude-opus-4-6 --tools ""'
DEFAULT_RELATION = "depends_on"
DEFAULT_SEMANTIC = "governance"
DOC_TYPE_BY_DIR = {
    "requirements": "requirement",
    "design": "design",
    "detailed_design": "design",
    "plan": "plan",
    "governance": "governance",
    "test": "test",
    "operations": "operations",
}
TYPE_SECTIONS = {
    "requirement": ["Overview", "Scope", "Open Questions"],
    "design": ["Overview", "Architecture", "Open Questions"],
    "plan": ["Overview", "Milestones", "Risks"],
    "governance": ["Overview", "Decision Log", "Follow-ups"],
    "test": ["Overview", "Acceptance Criteria", "Failure Criteria"],
    "operations": ["Overview", "Runbook", "Monitoring"],
    "document": ["Overview", "Details", "Open Questions"],
}
DETAILED_DESIGN_SECTIONS = [
    "Overview",
    "Mermaid Diagrams",
    "Ownership Boundaries",
    "Implementation Implications",
    "Open Questions",
]
MARKDOWN_FENCE_RE = re.compile(r"^\s*```(?:markdown|md)?\s*\n(?P<body>.*)\n```\s*$", re.IGNORECASE | re.DOTALL)
FENCE_LINE_RE = re.compile(r"^\s*```(?:[a-zA-Z0-9_-]+)?\s*$")
TITLE_HEADING_RE = re.compile(r"^\s*#\s+(?P<title>.+?)\s*$")
SECTION_HEADING_RE = re.compile(r"^##\s+.+$", re.MULTILINE)
MERMAID_FENCE_RE = re.compile(r"```mermaid\b", re.IGNORECASE)
H1_HEADING_RE = re.compile(r"^#\s+(.+)$")
H3_HEADING_RE = re.compile(r"^###\s+(.+)$")
BOLD_HEADING_RE = re.compile(r"^\*\*(\d+\.\s+.+?)\*\*\s*$")
META_PREAMBLE_PATTERNS = (
    re.compile(r"^\s*the\s+docs?(?:/[a-z0-9._-]+)*\s+directory\b.*$", re.IGNORECASE),
    re.compile(r"^\s*the\s+dependency\s+documents\s+provided\s+inline\b.*$", re.IGNORECASE),
    re.compile(r"^\s*the\s+existing\s+(?:file|document|content)\b.*$", re.IGNORECASE),
    re.compile(r"^\s*now\s+i\s+have\s+enough\s+context\b.*$", re.IGNORECASE),
    re.compile(r"^\s*no\s+existing\s+file\s+found\b.*$", re.IGNORECASE),
    re.compile(r"^\s*since the user\b.*$", re.IGNORECASE),
    re.compile(r"^\s*i\s+need\s+to\s+write\s+just\s+the\s+document\s+body\b.*$", re.IGNORECASE),
    re.compile(
        r"^\s*.*\b(?:i(?:'|’)ll\s+(?:now\s+)?(?:output|write|create)|let me(?:\s+now)?\s+write)\b.*$",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*let me(?:\s+(?:review|verify|check|compare))\b.*$", re.IGNORECASE),
    re.compile(
        r"^\s*(?:here is|here(?:'|’)s)\b.*\b(?:document|markdown|body|content)\b.*$",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*[-*]\s+.+→\s+covered\b.*$", re.IGNORECASE),
    re.compile(r"^\s*`[^`]+`\s+を(?:作成|生成)しました。?\s*$"),
    re.compile(r"^\s*(?:主要|主な)な?構成[:：]\s*$"),
    re.compile(r"^\s*(?:以下|上記)の(?:内容|構成|設計)で(?:作成|生成)しました。?\s*$"),
)


@dataclass(frozen=True)
class WaveArtifact:
    """Normalized wave_config entry."""

    wave: int
    node_id: str
    output: str
    title: str
    depends_on: list[dict[str, Any]]
    conventions: list[dict[str, Any]]
    modules: list[str] = ()


@dataclass(frozen=True)
class GenerationResult:
    """Result of rendering one artifact."""

    node_id: str
    path: Path
    status: str


@dataclass(frozen=True)
class DependencyDocument:
    """Resolved dependency document used as AI context."""

    node_id: str
    path: Path
    content: str


def generate_wave(
    project_root: Path,
    wave: int,
    force: bool = False,
    ai_command: str | None = None,
    feedback: str | None = None,
) -> list[GenerationResult]:
    """Generate or skip all documents configured for a wave."""
    from codd.scanner import build_document_node_path_map

    config = _load_project_config(project_root)
    artifacts = _load_wave_artifacts(config)
    selected = [artifact for artifact in artifacts if artifact.wave == wave]
    if not selected:
        raise ValueError(f"wave_config has no entries for wave {wave}")

    resolved_ai_command = _resolve_ai_command(config, ai_command, command_name="generate")
    global_conventions = _normalize_conventions(config.get("conventions", []))
    depended_by_map = _build_depended_by_map(artifacts)
    document_node_paths = build_document_node_path_map(project_root, config)

    results: list[GenerationResult] = []
    for artifact in selected:
        output_path = project_root / artifact.output
        if output_path.exists() and not force:
            results.append(GenerationResult(node_id=artifact.node_id, path=output_path, status="skipped"))
            continue

        dependency_documents = _load_dependency_documents(project_root, artifact.depends_on, document_node_paths)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        combined_conventions = deepcopy(global_conventions) + deepcopy(artifact.conventions)
        content = _render_document(
            artifact=artifact,
            global_conventions=global_conventions,
            depended_by=depended_by_map.get(artifact.node_id, []),
            body=_generate_document_body(
                artifact=artifact,
                dependency_documents=dependency_documents,
                conventions=combined_conventions,
                ai_command=resolved_ai_command,
                feedback=feedback,
            ),
        )
        output_path.write_text(content, encoding="utf-8")
        results.append(GenerationResult(node_id=artifact.node_id, path=output_path, status="generated"))

    return results


def _load_project_config(project_root: Path) -> dict[str, Any]:
    return load_project_config(project_root)


def _load_wave_artifacts(config: dict[str, Any]) -> list[WaveArtifact]:
    wave_config = config.get("wave_config")
    if not isinstance(wave_config, dict) or not wave_config:
        raise ValueError(
            "codd.yaml is missing wave_config. "
            "Run 'codd plan --init' to generate it from your requirements, "
            "or 'codd generate' will auto-generate it for you."
        )

    artifacts: list[WaveArtifact] = []
    for wave_key, entries in wave_config.items():
        try:
            wave = int(wave_key)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"wave_config key must be an integer wave number, got {wave_key!r}") from exc

        if not isinstance(entries, list):
            raise ValueError(f"wave_config[{wave_key!r}] must be a list of artifacts")

        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"wave_config[{wave_key!r}] entries must be mappings")

            missing = [field for field in ("node_id", "output", "title") if not entry.get(field)]
            if missing:
                raise ValueError(
                    f"wave_config[{wave_key!r}] entry is missing required fields: {', '.join(missing)}"
                )

            artifacts.append(
                WaveArtifact(
                    wave=wave,
                    node_id=str(entry["node_id"]),
                    output=str(entry["output"]),
                    title=str(entry["title"]),
                    depends_on=_normalize_dependencies(entry.get("depends_on", [])),
                    conventions=_normalize_conventions(entry.get("conventions", [])),
                    modules=_normalize_modules(entry.get("modules", [])),
                )
            )

    return artifacts


def _normalize_dependencies(entries: Any) -> list[dict[str, Any]]:
    if not entries:
        return []
    if not isinstance(entries, list):
        raise ValueError("depends_on must be a list")

    normalized: list[dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, str):
            data: dict[str, Any] = {"id": entry}
        elif isinstance(entry, dict):
            data = deepcopy(entry)
        else:
            raise ValueError(f"depends_on entries must be strings or mappings, got {type(entry).__name__}")

        node_id = data.get("id") or data.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("depends_on entries require a non-empty id")

        data["id"] = node_id
        data.setdefault("relation", DEFAULT_RELATION)
        data.setdefault("semantic", DEFAULT_SEMANTIC)
        normalized.append(data)

    return normalized


def _normalize_conventions(entries: Any) -> list[dict[str, Any]]:
    if not entries:
        return []
    if not isinstance(entries, list):
        raise ValueError("conventions must be a list")

    normalized: list[dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, str):
            normalized.append({"targets": [entry], "reason": ""})
            continue
        if not isinstance(entry, dict):
            raise ValueError(f"conventions entries must be strings or mappings, got {type(entry).__name__}")

        data = deepcopy(entry)
        targets = data.get("targets", [])
        if isinstance(targets, str):
            data["targets"] = [targets]
        elif isinstance(targets, list):
            data["targets"] = [target for target in targets if isinstance(target, str)]
        else:
            raise ValueError("convention targets must be a string or list of strings")
        data.setdefault("reason", "")
        normalized.append(data)

    return normalized


def _normalize_modules(entries: Any) -> list[str]:
    if not entries:
        return []
    if not isinstance(entries, list):
        raise ValueError("modules must be a list of strings")
    return [str(m) for m in entries if isinstance(m, str) and m.strip()]


def _build_depended_by_map(artifacts: list[WaveArtifact]) -> dict[str, list[dict[str, Any]]]:
    depended_by: dict[str, list[dict[str, Any]]] = {artifact.node_id: [] for artifact in artifacts}

    for artifact in artifacts:
        for dependent in artifacts:
            if dependent.wave <= artifact.wave:
                continue

            for dependency in dependent.depends_on:
                if dependency["id"] != artifact.node_id:
                    continue

                reverse = {"id": dependent.node_id}
                for key, value in dependency.items():
                    if key == "id":
                        continue
                    reverse[key] = deepcopy(value)
                depended_by[artifact.node_id].append(reverse)

    return depended_by


def _render_document(
    artifact: WaveArtifact,
    global_conventions: list[dict[str, Any]],
    depended_by: list[dict[str, Any]],
    body: str,
) -> str:
    doc_type = _infer_doc_type(artifact.output)
    codd_block = {
        "node_id": artifact.node_id,
        "type": doc_type,
        "depends_on": deepcopy(artifact.depends_on),
        "depended_by": deepcopy(depended_by),
        "conventions": deepcopy(global_conventions) + deepcopy(artifact.conventions),
    }
    if artifact.modules:
        codd_block["modules"] = list(artifact.modules)
    frontmatter = yaml.safe_dump(
        {"codd": codd_block},
        allow_unicode=True,
        sort_keys=False,
    )
    return f"---\n{frontmatter}---\n\n{body.rstrip()}\n"


def _infer_doc_type(output_path: str) -> str:
    parts = PurePosixPath(output_path).parts
    if len(parts) >= 3 and parts[0] == "docs":
        return DOC_TYPE_BY_DIR.get(parts[1], "document")
    return "document"


def _resolve_ai_command(
    config: dict[str, Any],
    override: str | None,
    command_name: str | None = None,
) -> str:
    if override is not None:
        raw_command = override
    elif command_name and isinstance(config.get("ai_commands"), dict):
        raw_command = config["ai_commands"].get(command_name, config.get("ai_command", DEFAULT_AI_COMMAND))
    else:
        raw_command = config.get("ai_command", DEFAULT_AI_COMMAND)
    if not isinstance(raw_command, str) or not raw_command.strip():
        raise ValueError("ai_command must be a non-empty string")
    return raw_command.strip()


def _load_dependency_documents(
    project_root: Path,
    dependencies: list[dict[str, Any]],
    document_node_paths: dict[str, Path],
) -> list[DependencyDocument]:
    documents: list[DependencyDocument] = []
    missing_node_ids: list[str] = []
    seen_node_ids: set[str] = set()

    for dependency in dependencies:
        node_id = dependency["id"]
        if node_id in seen_node_ids:
            continue
        seen_node_ids.add(node_id)

        rel_path = document_node_paths.get(node_id)
        if rel_path is None:
            missing_node_ids.append(node_id)
            continue

        file_path = project_root / rel_path
        if not file_path.exists():
            raise ValueError(
                f"dependency document {node_id!r} maps to {rel_path.as_posix()}, but the file does not exist"
            )

        documents.append(
            DependencyDocument(
                node_id=node_id,
                path=rel_path,
                content=file_path.read_text(encoding="utf-8"),
            )
        )

    if missing_node_ids:
        raise ValueError(f"unable to resolve dependency document paths for: {', '.join(missing_node_ids)}")

    return documents


def _generate_document_body(
    artifact: WaveArtifact,
    dependency_documents: list[DependencyDocument],
    conventions: list[dict[str, Any]],
    ai_command: str,
    feedback: str | None = None,
) -> str:
    prompt = _build_generation_prompt(artifact, dependency_documents, conventions, feedback=feedback)
    return _sanitize_generated_body(
        artifact.title,
        _invoke_ai_command(ai_command, prompt),
        output_path=artifact.output,
    )


def _build_generation_prompt(
    artifact: WaveArtifact,
    dependency_documents: list[DependencyDocument],
    conventions: list[dict[str, Any]],
    feedback: str | None = None,
) -> str:
    doc_type = _infer_doc_type(artifact.output)
    is_detailed_design = _is_detailed_design_output(artifact.output)
    section_names = DETAILED_DESIGN_SECTIONS if is_detailed_design else TYPE_SECTIONS.get(doc_type, TYPE_SECTIONS["document"])
    preferred_sections = ", ".join(section_names)
    required_section_headings = [f"## {index}. {name}" for index, name in enumerate(section_names, start=1)]

    lines = [
        f"You are writing a CoDD {doc_type} document.",
        f"Node ID: {artifact.node_id}",
        f"Title: {artifact.title}",
        "Use the dependency documents below as the primary context, synthesize them, and write a complete Markdown document body.",
        (
            "ABSOLUTE PROHIBITION: **Do not emit** YAML frontmatter, implementation notes, "
            "TODO placeholders, or any meta-commentary about the writing process "
            "(e.g. 'I'll write...', 'No existing file found...', 'Here is...', "
            "'Let me...', 'Now I have enough context...'). **Start directly with the document content.** "
            "Violating this instruction is a **CRITICAL ERROR** and breaks a release-blocking constraint."
        ),
        "Treat requirement documents as the source of truth and reflect every feature, screen, workflow, API, integration, and operational rule they describe.",
        "Before finalizing, self-check that every capability and constraint mentioned in the depends_on documents is represented in the document body.",
        "Use concrete tool names, framework names, services, table names, endpoints, thresholds, counts, and timelines wherever applicable.",
        "Never use vague placeholders such as '推奨なし', '要検討', or 'TBD'.",
        f"Prefer a structure that covers: {preferred_sections}.",
        "After the title, immediately continue with section headings such as '## Overview' or '## 1. Overview'; do not acknowledge that you created the file.",
        "Do not write summary phrases like '`docs/...` を作成しました。', '本設計書は以下を網羅しています:', or '主な構成:'. Write the actual sections instead.",
    ]

    if is_detailed_design:
        lines.extend(
            [
                "This artifact lives under docs/detailed_design/ and must serve as a downstream-ready detailed design document.",
                "Use Mermaid diagrams when they clarify ownership, dependencies, sequences, states, CRUD boundaries, or module/component structure.",
                "Choose only the diagram types justified by the dependency documents; do not force every possible diagram.",
                "For every diagram, add concise prose that explains canonical ownership, reuse/import expectations, and implementation boundaries.",
                "If a shared type, module, or workflow should have a single owner, state that ownership explicitly to prevent reimplementation drift.",
                "Include at least one Mermaid diagram and at least three section headings in the final document body.",
            ]
        )

    lines.extend(
        [
            "",
            "Output contract:",
            "- Write the finished document body now, not a summary of what it would contain.",
            "- The first content line after the title must be the first required section heading below.",
            "- Use these section headings exactly once and in this order:",
        ]
    )
    lines.extend(required_section_headings)
    if is_detailed_design:
        lines.extend(
            [
                "- Under '## 2. Mermaid Diagrams', include at least one ```mermaid``` fenced block.",
                "- Use prose after each Mermaid block to explain ownership boundaries and implementation consequences.",
            ]
        )

    if conventions:
        lines.extend(
            [
                "",
                "Non-negotiable conventions:",
                "- These are release-blocking constraints. Reflect them explicitly in the document body.",
                "- Explicitly state how the document complies with each convention and invariant listed below.",
                "- For security or access-control constraints, state the concrete controls in architecture, security, data, or workflow sections.",
                "- For legal/privacy constraints, add explicit compliance or data-handling requirements.",
                "- For SLA/performance constraints, include measurable thresholds in non-functional sections.",
            ]
        )
        for index, convention in enumerate(conventions, start=1):
            targets = ", ".join(str(target) for target in convention.get("targets", []) if isinstance(target, str))
            reason = str(convention.get("reason") or "").strip() or "(no reason provided)"
            lines.append(f"{index}. Targets: {targets or '(no explicit targets)'}")
            lines.append(f"   Reason: {reason}")

        lines.extend(
            [
                "- Example reflections: tenant isolation in security/data model sections, auth requirements in access control, privacy rules in compliance, performance thresholds in non-functional requirements.",
            ]
        )

    lines.extend(
        [
            "",
            "Dependency documents:",
        ]
    )

    for document in dependency_documents:
        lines.extend(
            [
                f"--- BEGIN DEPENDENCY {document.path.as_posix()} ({document.node_id}) ---",
                document.content.rstrip(),
                f"--- END DEPENDENCY {document.path.as_posix()} ---",
                "",
            ]
        )

    if feedback:
        lines.extend([
            "",
            "--- REVIEW FEEDBACK (from previous generation attempt) ---",
            "A reviewer found issues with a previous version of this document.",
            "You MUST address ALL of the following feedback in this generation:",
            feedback.rstrip(),
            "--- END REVIEW FEEDBACK ---",
            "",
        ])

    lines.extend(
        [
            "Final instruction: output the real Markdown document body now using the required section headings above. "
            "Do not describe the document. Do not announce completion. Do not provide a summary list.",
        ]
    )

    return "\n".join(lines).rstrip() + "\n"


def _is_detailed_design_output(output_path: str) -> bool:
    parts = PurePosixPath(output_path).parts
    return len(parts) >= 2 and parts[0] == "docs" and parts[1] == "detailed_design"


def _invoke_ai_command(ai_command: str, prompt: str) -> str:
    command = shlex.split(ai_command)
    if not command:
        raise ValueError("ai_command must not be empty")

    try:
        result = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ValueError(f"AI command not found: {command[0]}") from exc

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise ValueError(f"AI command failed: {detail}")

    if not result.stdout.strip():
        raise ValueError("AI command returned empty output")

    return result.stdout


def _sanitize_generated_body(title: str, body: str, *, output_path: str | None = None) -> str:
    normalized = body.lstrip()
    if normalized.startswith("---"):
        match = re.match(r"^---\s*\n.*?\n---\s*\n?", normalized, re.DOTALL)
        if match:
            normalized = normalized[match.end():]

    normalized = _strip_meta_preamble(normalized)
    normalized = normalized.strip()
    if not normalized:
        raise ValueError("AI command returned empty output")
    if re.search(r"\bTODO\b", normalized):
        raise ValueError("AI command returned scaffold content containing TODO")
    if not normalized.startswith("# "):
        normalized = f"# {title}\n\n{normalized}"
    normalized = _normalize_title_heading_block(title, normalized)
    normalized = _normalize_section_headings(normalized)
    normalized = _collapse_blank_line_runs(normalized)
    _validate_generated_body(title, normalized, output_path=output_path)

    return normalized.rstrip() + "\n"


def _strip_meta_preamble(body: str) -> str:
    fenced = MARKDOWN_FENCE_RE.match(body)
    if fenced:
        body = fenced.group("body")

    lines = [line for line in body.splitlines() if not _is_meta_preamble_line(line)]
    _trim_outer_non_content_lines(lines)

    return "\n".join(lines)


def _is_meta_preamble_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return False
    return any(pattern.match(stripped) for pattern in META_PREAMBLE_PATTERNS)


def _trim_outer_non_content_lines(lines: list[str]) -> None:
    while lines:
        stripped = lines[0].strip()
        if not stripped or stripped == "---":
            lines.pop(0)
            continue
        break

    while lines:
        stripped = lines[-1].strip()
        if not stripped or stripped == "---":
            lines.pop()
            continue
        break


def _collapse_blank_line_runs(body: str) -> str:
    lines = body.splitlines()
    collapsed: list[str] = []
    in_fence = False
    blank_run = 0

    for line in lines:
        if FENCE_LINE_RE.match(line.strip()):
            in_fence = not in_fence
            blank_run = 0
            collapsed.append(line)
            continue

        if not in_fence and not line.strip():
            blank_run += 1
            if blank_run > 1:
                continue
        else:
            blank_run = 0

        collapsed.append(line)

    return "\n".join(collapsed)


def _normalize_title_heading_block(title: str, body: str) -> str:
    lines = body.splitlines()
    if not lines:
        return body

    expected = re.sub(r"\s+", " ", title).strip().casefold()
    if _normalize_heading_text(lines[0]) != expected:
        return body

    retained: list[str] = [lines[0]]
    index = 1
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped or stripped == "---" or FENCE_LINE_RE.match(stripped):
            index += 1
            continue
        if _is_meta_preamble_line(lines[index]):
            index += 1
            continue
        if _normalize_heading_text(lines[index]) == expected:
            index += 1
            continue
        break

    if index < len(lines):
        retained.extend(["", *lines[index:]])

    return "\n".join(retained)


def _normalize_heading_text(line: str) -> str | None:
    match = TITLE_HEADING_RE.match(line)
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group("title")).strip().casefold()


def _normalize_section_headings(body: str) -> str:
    """Promote or demote misleveled headings so ``## `` section headings exist.

    AI models sometimes emit ``###`` or bare ``#`` (non-title) headings instead
    of the required ``## `` level.  This function detects the mismatch and
    adjusts heading levels *outside* fenced code blocks.  Bold pseudo-headings
    (``**1. Name**``) are also promoted.

    If ``## `` headings already exist the body is returned unchanged.
    """
    if SECTION_HEADING_RE.search(body):
        return body

    lines = body.splitlines()
    has_title = bool(lines and TITLE_HEADING_RE.match(lines[0]))

    # Tally heading-like patterns (outside fences) to decide the strategy.
    h1_non_title = 0
    h3_count = 0
    bold_count = 0
    in_fence = False
    for idx, line in enumerate(lines):
        if FENCE_LINE_RE.match(line.strip()):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if idx == 0 and has_title:
            continue
        if H3_HEADING_RE.match(line):
            h3_count += 1
        elif H1_HEADING_RE.match(line):
            h1_non_title += 1
        elif BOLD_HEADING_RE.match(line):
            bold_count += 1

    if h3_count == 0 and h1_non_title == 0 and bold_count == 0:
        return body  # Nothing we can safely fix

    result: list[str] = []
    in_fence = False
    for idx, line in enumerate(lines):
        if FENCE_LINE_RE.match(line.strip()):
            in_fence = not in_fence
            result.append(line)
            continue
        if in_fence:
            result.append(line)
            continue

        # Skip the title heading
        if idx == 0 and has_title:
            result.append(line)
            continue

        # Strategy: promote/demote to ##
        if h3_count > 0 and H3_HEADING_RE.match(line):
            result.append(re.sub(r"^###", "##", line))
        elif h1_non_title > 0 and H1_HEADING_RE.match(line) and not (idx == 0 and has_title):
            result.append(re.sub(r"^#\s+", "## ", line))
        elif bold_count > 0:
            m = BOLD_HEADING_RE.match(line)
            if m:
                result.append(f"## {m.group(1)}")
            else:
                result.append(line)
        else:
            result.append(line)

    return "\n".join(result)


def _validate_generated_body(title: str, body: str, *, output_path: str | None = None) -> None:
    if not SECTION_HEADING_RE.search(body):
        raise ValueError(f"AI command returned unstructured summary for {title!r}; missing section headings")

    first_content_line = _first_content_line_after_title(body)
    if first_content_line and any(pattern.match(first_content_line) for pattern in META_PREAMBLE_PATTERNS):
        raise ValueError(f"AI command returned meta commentary instead of document content for {title!r}")

    if output_path and _is_detailed_design_output(output_path):
        if not MERMAID_FENCE_RE.search(body):
            raise ValueError(f"AI command returned detailed design without Mermaid diagrams for {title!r}")


def _first_content_line_after_title(body: str) -> str | None:
    lines = body.splitlines()
    start_index = 1 if lines and TITLE_HEADING_RE.match(lines[0]) else 0
    for line in lines[start_index:]:
        stripped = line.strip()
        if stripped:
            return stripped
    return None
