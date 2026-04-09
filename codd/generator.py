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
    "test": ["Overview", "Acceptance Criteria", "Failure Criteria", "E2E Test Generation Meta-Prompt"],
    "operations": ["Overview", "Runbook", "Monitoring", "CI/CD Pipeline Generation Meta-Prompt"],
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
    # For test code files, use comment-style headers instead of YAML frontmatter
    if _is_test_code_output(artifact.output):
        dep_paths = [d.get("id", "") for d in artifact.depends_on]
        header_lines = [f"// @generated-from: {path}" for path in dep_paths]
        header_lines.append("// @generated-by: codd generate")
        header_lines.append(f"// @codd-node-id: {artifact.node_id}")
        header = "\n".join(header_lines)
        # Strip any markdown fences the AI might have wrapped the code in
        cleaned = body.strip()
        if cleaned.startswith("```"):
            first_newline = cleaned.index("\n") if "\n" in cleaned else len(cleaned)
            cleaned = cleaned[first_newline + 1:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        return f"{header}\n\n{cleaned.strip()}\n"

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


def _is_test_code_output(output_path: str) -> bool:
    """Check if the output target is an executable test file (not a design doc)."""
    return output_path.endswith(('.spec.ts', '.test.ts', '.spec.js', '.test.js', '.spec.py', '.test.py'))


def _build_generation_prompt(
    artifact: WaveArtifact,
    dependency_documents: list[DependencyDocument],
    conventions: list[dict[str, Any]],
    feedback: str | None = None,
) -> str:
    # Test code generation mode: output executable test code, not a Markdown document
    if _is_test_code_output(artifact.output):
        return _build_test_code_prompt(artifact, dependency_documents, conventions, feedback=feedback)

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

    if doc_type == "test":
        lines.extend(
            [
                "",
                "E2E Test Generation Meta-Prompt section rules:",
                "- The final section '## E2E Test Generation Meta-Prompt' serves as a machine-readable instruction for `codd propagate` to auto-generate E2E tests.",
                "- MECE domain decomposition: Split E2E tests into non-overlapping domain files (e.g. auth, rbac, tenant-isolation, core-features, integrations). Each file owns exactly one domain.",
                "- Scenario derivation: For each domain, derive test scenarios from acceptance criteria (positive + negative) and failure criteria (inverted to assertions).",
                "- Architecture adaptation: Include a rule that test generation must scan the actual route/endpoint structure and mark unimplemented endpoints with `test.fixme()` instead of skipping.",
                "- Quality gate: Define pass criteria — all PASS, zero SKIP, acceptance criteria coverage, and any release-blocking constraints from conventions.",
                "- Output file mapping: Specify a table mapping each domain to its output file path under `tests/e2e/`.",
                "- Shared helpers: Mandate a `tests/e2e/helpers/` directory for auth flows, test data setup, and common assertions to avoid duplication across spec files.",
                "- Generation markers: All generated files must include `// @generated-from:` and `// @generated-by: codd propagate` headers. Manual tests marked with `// @manual` must be preserved on regeneration.",
                "",
                "E2E Test Level Separation (CRITICAL):",
                "- E2E tests MUST be split into two distinct levels: API integration tests and browser tests. These are NOT interchangeable.",
                "- API integration tests use HTTP client mode (e.g. Playwright `request` context, `supertest`, `fetch`) to verify endpoint responses, status codes, and data contracts. These test the server, not the user experience.",
                "- Browser tests use real browser automation (e.g. Playwright `page`, Cypress `cy`) to simulate actual user interactions: clicking buttons, filling forms, navigating pages, and verifying visible UI state.",
                "- For web applications with authentication, browser tests MUST include a login-redirect-render flow: (1) navigate to login page, (2) fill credentials and submit, (3) assert redirect to the correct post-login URL, (4) assert the target page renders expected content. This catches redirect misconfigurations and route mismatches that API tests cannot detect.",
                "- For any page transition triggered by a user action (form submit, link click, button click), browser tests MUST verify both the resulting URL (via URL assertion) and at least one visible content element on the destination page. Checking only the HTTP status is insufficient — a 200 with wrong content or a silent redirect to a 404 page will be missed.",
                "- Server health baseline: Every HTTP request assertion MUST first verify the response status is < 500 before checking business-logic status codes (200, 302, 401, etc.). A 5xx is a server error (unhandled exception, DB down) — categorically different from a 4xx (auth failure, not found). Without this, a DB failure silently passes when tests only check for specific success codes.",
                "- Output file naming: API integration tests → `tests/e2e/<domain>.spec.ts`, browser tests → `tests/e2e/<domain>.browser.spec.ts`. This makes the test level immediately visible from the filename.",
                "",
                "E2E Runtime Environment rules:",
                "- E2E tests for web applications require a running server. The meta-prompt MUST specify how to start the application under test before running E2E tests.",
                "- Detect the project type from package.json scripts, framework config, or entry points. Include the appropriate startup sequence (e.g., build → start → wait-for-ready) in the E2E instructions.",
                "- For CI environments, specify that the server must run in the background with a health-check wait before test execution begins.",
                "- Browser tests require a headed or headless browser. Specify the browser launch configuration (e.g. `use: { headless: true }`) in the test config.",
            ]
        )

    if doc_type == "operations":
        lines.extend(
            [
                "",
                "CI/CD Pipeline Generation Meta-Prompt section rules:",
                "- The final section '## CI/CD Pipeline Generation Meta-Prompt' serves as a machine-readable instruction for generating `.github/workflows/ci.yml`.",
                "- Derive CI jobs from the test strategy document: for each test level (unit, integration, E2E, performance), create a corresponding CI job.",
                "- Include build verification: `npm run build` (or equivalent) must pass before tests run.",
                "- Database setup: If the project uses a database, include a service container (e.g. PostgreSQL) with seed step.",
                "- Environment variables: List required env vars from the project config (e.g. NEXTAUTH_SECRET, DATABASE_URL) and mark which should be GitHub Secrets.",
                "- Merge gate: All test jobs must pass before PR merge is allowed. Specify branch protection rule recommendations.",
                "- Output file: `.github/workflows/ci.yml`. Include `// @generated-by: codd propagate` as a YAML comment.",
                "- Trigger: `on: pull_request` to main/develop branches.",
                "- Caching: Include dependency caching (node_modules, pip cache, etc.) for faster CI runs.",
                "- Failure notification: Recommend but do not require Slack/email notification on failure.",
                "",
                "Prerequisite Validation rules:",
                "- Before referencing any tool or package in a CI step (e.g., a linter, test runner, build tool), verify it exists in the project's dependency manifest (package.json, requirements.txt, pyproject.toml, etc.).",
                "- If a required tool is missing, either add an install step in CI or note it as a prerequisite that must be added to the project's dev dependencies.",
                "- Do not generate CI steps that invoke tools the project has not installed.",
                "",
                "Runtime Compatibility rules:",
                "- When generating configuration files or CI steps, detect the project's existing tool versions (framework, linter, test runner) and produce version-compatible output.",
                "- Avoid generating config formats or flags that require a newer version than what the project uses (e.g., flat config for ESLint <9, or module syntax for older Node.js).",
                "- If version information is available in package.json, requirements.txt, or lock files, use it to guide config format choices.",
                "",
                "E2E Job Server Startup rules:",
                "- If the CI includes E2E tests for a web application, the E2E job MUST include steps to build and start the application server before running tests.",
                "- Detect the project type (web app, CLI, library) from the project structure and only add server startup for web applications.",
                "- Include a readiness check (e.g., wait-on, curl health endpoint) between server start and test execution to avoid race conditions.",
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


def _build_test_code_prompt(
    artifact: WaveArtifact,
    dependency_documents: list[DependencyDocument],
    conventions: list[dict[str, Any]],
    feedback: str | None = None,
) -> str:
    """Build a prompt that generates executable test code (not a Markdown document)."""
    # Detect test framework from output filename
    ext = PurePosixPath(artifact.output).suffix
    if ext in ('.ts', '.js'):
        framework = "Playwright"
        lang = "TypeScript"
    else:
        framework = "pytest"
        lang = "Python"

    conv_text = ""
    for c in conventions:
        targets = ", ".join(c.get("targets", []))
        reason = c.get("reason", "")
        conv_text += f"  - [{targets}]: {reason}\n"

    lines = [
        f"You are generating executable {framework} test code in {lang}.",
        f"Node ID: {artifact.node_id}",
        f"Title: {artifact.title}",
        f"Output file: {artifact.output}",
        "",
        "CRITICAL: Output ONLY executable test code. Do NOT output Markdown, frontmatter, design prose, or commentary.",
        "The output must be a valid, runnable test file that can be executed directly by the test runner.",
        "",
        "Conventions to enforce in tests:",
        conv_text,
        "",
        "Test separation rules:",
        "- Tests that can run in CI (headless browser + test DB) must NOT be tagged.",
        "- Tests that require a deployed environment (VPS, staging) must be tagged with @cdp-only in the describe block name.",
        "  Example: test.describe('Deploy Smoke @cdp-only', () => { ... })",
        "- The Playwright config uses `grepInvert: /@cdp-only/` in CI to exclude deploy-only tests.",
        "- CI tests: login flow, redirect checks, route protection, role-based access.",
        "- CDP-only tests: visual layout checks, mobile viewport, deployed URL smoke tests.",
        "",
        "Server health baseline (CRITICAL):",
        "- Every test that makes an HTTP request MUST assert the response status is < 500 BEFORE any business-logic assertions.",
        "  Example: expect(response.status()).toBeLessThan(500);",
        "- 5xx = server broke (unhandled exception, DB down). 4xx = business logic rejection (auth failure, not found). These are categorically different.",
        "- Without this assertion, a DB connection failure silently passes when the test only checks for specific success codes like [200, 302].",
        "- For browser tests after page.goto() or form submission, check response?.status() < 500 before asserting page content.",
        "- For API tests, assert < 500 first, then assert the specific expected status code.",
        "",
    ]

    if framework == "Playwright":
        lines.extend([
            "Playwright-specific rules:",
            "- Import from '@playwright/test': test, expect, Page",
            "- Use page object for browser tests, NOT playwrightRequest for API tests.",
            "- For login forms: detect the actual form structure from dependency documents.",
            "  Look for input labels, button text, tab switching if the form has multiple modes.",
            "- Use getByRole, getByLabel, getByText for selectors (accessibility-first).",
            "- For redirects: use page.waitForURL() with regex pattern and reasonable timeout.",
            "- Assert both URL and visible content after navigation.",
            "- Use process.env.BASE_URL for the server URL.",
            "",
            "File header format:",
            f"// @generated-from: <dependency doc paths>",
            "// @generated-by: codd generate",
            "",
        ])

    lines.append("Use the following dependency documents as context for what to test:")
    lines.append("")
    for doc in dependency_documents:
        lines.append(f"--- {doc.node_id} ({doc.path.as_posix()}) ---")
        lines.append(doc.content[:8000])
        lines.append("--- END ---")
        lines.append("")

    if feedback:
        lines.extend([
            "--- REVIEW FEEDBACK ---",
            feedback.rstrip(),
            "--- END REVIEW FEEDBACK ---",
            "",
        ])

    lines.append(f"Output the complete {lang} test file now. No markdown fences. No prose. Just code.")
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
    # For test code output, skip Markdown-specific sanitization
    if output_path and _is_test_code_output(output_path):
        cleaned = body.strip()
        if not cleaned:
            raise ValueError("AI command returned empty output")
        return cleaned + "\n"

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
