"""CoDD assembler — integrate generated sprint fragments into a working project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import warnings

import codd.generator as generator_module
from codd.generator import _load_project_config, _normalize_conventions
from codd.implementer import get_task_slugs_by_sprint
from codd.scanner import _extract_frontmatter, build_document_node_path_map


@dataclass(frozen=True)
class AssembleResult:
    """Result of the assemble command."""

    output_dir: Path
    files_written: int
    ai_output: str


def assemble_project(
    project_root: Path,
    *,
    output_dir: str | None = None,
    ai_command: str | None = None,
) -> AssembleResult:
    """Read generated fragments + design docs, invoke AI to assemble a working project."""
    project_root = project_root.resolve()
    config = _load_project_config(project_root)
    resolved_ai_command = generator_module._resolve_ai_command(config, ai_command, command_name="assemble")

    # Collect design documents
    design_docs = _collect_design_documents(project_root, config)

    # Collect generated fragments
    fragments = _collect_generated_fragments(project_root, config)

    if not fragments:
        raise ValueError("No generated fragments found in src/generated/. Run 'codd implement' first.")

    # Determine output directory
    dest = output_dir or "src"
    dest_path = project_root / dest

    # Build the prompt
    prompt = _build_assemble_prompt(config, design_docs, fragments, dest)

    # Invoke AI
    raw_output = generator_module._invoke_ai_command(resolved_ai_command, prompt)

    # Parse and write files
    files_written = _write_assembled_files(project_root, dest_path, raw_output)

    return AssembleResult(
        output_dir=dest_path,
        files_written=files_written,
        ai_output=raw_output,
    )


def _collect_design_documents(project_root: Path, config: dict[str, Any]) -> list[dict[str, str]]:
    """Collect all design documents with their node_ids and content."""
    node_paths = build_document_node_path_map(project_root, config)
    docs = []
    for node_id, rel_path in sorted(node_paths.items()):
        full_path = project_root / rel_path
        if full_path.exists():
            content = full_path.read_text(encoding="utf-8")
            # Strip frontmatter for the prompt
            stripped = _strip_frontmatter(content)
            docs.append({
                "node_id": node_id,
                "path": str(rel_path),
                "content": stripped,
            })
    return docs


def _collect_generated_fragments(project_root: Path, config: dict[str, Any]) -> list[dict[str, str]]:
    """Collect all generated code fragments from src/generated/sprint_N/.

    Cross-references against the implementation plan to detect orphan fragments
    from renamed or deleted tasks. Orphans are excluded with a warning.
    """
    source_dirs = config.get("scan", {}).get("source_dirs", ["src/"])
    generated_base = None
    for src_dir in source_dirs:
        candidate = project_root / src_dir / "generated"
        if candidate.is_dir():
            generated_base = candidate
            break

    if generated_base is None:
        generated_base = project_root / "src" / "generated"

    if not generated_base.is_dir():
        return []

    # Load valid task slugs from implementation plan for orphan detection
    valid_slugs = get_task_slugs_by_sprint(project_root)

    code_extensions = (".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".java", ".css")
    fragments = []
    for sprint_dir in sorted(generated_base.iterdir()):
        if not sprint_dir.is_dir() or not sprint_dir.name.startswith("sprint_"):
            continue

        # Identify orphan task directories
        orphan_dirs: set[str] = set()
        if valid_slugs and sprint_dir.name in valid_slugs:
            expected = valid_slugs[sprint_dir.name]
            for child in sprint_dir.iterdir():
                if child.is_dir() and child.name not in expected:
                    orphan_dirs.add(child.name)
                    warnings.warn(
                        f"Orphan fragment directory '{sprint_dir.name}/{child.name}' "
                        f"does not match any task in the implementation plan. Skipping.",
                        stacklevel=2,
                    )

        for code_file in sorted(sprint_dir.rglob("*")):
            if not code_file.is_file() or code_file.suffix not in code_extensions:
                continue

            # Skip files under orphan task directories
            rel_to_sprint = code_file.relative_to(sprint_dir)
            if rel_to_sprint.parts and rel_to_sprint.parts[0] in orphan_dirs:
                continue

            rel_path = code_file.relative_to(project_root)
            content = code_file.read_text(encoding="utf-8")
            fragments.append({
                "sprint_dir": sprint_dir.name,
                "path": str(rel_path),
                "content": content,
            })

    return fragments


def _strip_frontmatter(content: str) -> str:
    """Remove YAML frontmatter from markdown content."""
    import re
    stripped = content.lstrip()
    if stripped.startswith("---"):
        match = re.match(r"^---\s*\n.*?\n---\s*\n?", stripped, re.DOTALL)
        if match:
            return stripped[match.end():]
    return content


def _build_assemble_prompt(
    config: dict[str, Any],
    design_docs: list[dict[str, str]],
    fragments: list[dict[str, str]],
    output_dir: str,
) -> str:
    """Build the prompt for the AI to assemble fragments into a working project."""
    language = config.get("project", {}).get("language", "typescript")
    project_name = config.get("project", {}).get("name", "project")

    parts = []
    parts.append(f"""You are a code assembler. Your job is to integrate generated code fragments into a single working {language} project.

## Project: {project_name}

## Instructions

1. Read the design documents below to understand the architecture, component tree, data model, and state management.
2. Read all generated code fragments — they contain implementation pieces organized by sprint.
3. Produce a COMPLETE, BUILDABLE project. This includes:
   - **Project configuration files** at the project root: package.json, tsconfig.json, next.config.*, tailwind.config.*, postcss.config.*, etc. — whatever the tech stack requires to build and run.
   - **Entry point / scaffold files**: app/layout.tsx, app/page.tsx (for Next.js), index.html, main.py, etc. — the files that wire the application together.
   - **Source code** under `{output_dir}/`: components, utilities, types, styles, hooks, reducers.
   - **Style entry points**: globals.css or equivalent with framework imports (e.g. @import "tailwindcss").
4. Resolve conflicts between sprint fragments: later sprints may refine or replace earlier ones.
5. Ensure all imports resolve correctly between files.
6. Do NOT add features beyond what the design documents specify.
7. Preserve traceability comments (@generated-by, @generated-from) where practical.

## Output Format

Output ONLY file contents in this exact format, one per file:

=== FILE: path/to/file.ts ===
<file content>

=== FILE: path/to/another.tsx ===
<file content>

File paths are relative to the project root. Examples:
- `package.json` for the project manifest
- `tsconfig.json` for TypeScript config
- `{output_dir}/app/layout.tsx` for Next.js root layout
- `{output_dir}/components/MyComponent.tsx` for source code

Do not include explanations outside of the === FILE blocks.
""")

    # Add design documents
    parts.append("## Design Documents\n")
    for doc in design_docs:
        parts.append(f"### {doc['node_id']} ({doc['path']})\n")
        parts.append(doc["content"])
        parts.append("\n---\n")

    # Add generated fragments
    parts.append("## Generated Code Fragments\n")
    current_sprint = None
    for frag in fragments:
        if frag["sprint_dir"] != current_sprint:
            current_sprint = frag["sprint_dir"]
            parts.append(f"\n### {current_sprint}\n")
        parts.append(f"#### {frag['path']}\n```\n{frag['content']}\n```\n")

    return "\n".join(parts)


def _write_assembled_files(project_root: Path, dest_path: Path, raw_output: str) -> int:
    """Parse === FILE: ... === blocks and write files."""
    import re

    file_block_re = re.compile(r"^=== FILE: (?P<path>.+?) ===\s*$", re.MULTILINE)
    matches = list(file_block_re.finditer(raw_output))

    if not matches:
        raise ValueError(
            "AI output did not contain any === FILE: ... === blocks. "
            "The assemble command expects the AI to output files in the === FILE: path === format."
        )

    files_written = 0
    for i, match in enumerate(matches):
        file_path_str = match.group("path").strip()
        content_start = match.end()
        content_end = matches[i + 1].start() if i + 1 < len(matches) else len(raw_output)
        content = raw_output[content_start:content_end].strip()

        # Remove leading/trailing code fence if present
        if content.startswith("```"):
            first_newline = content.index("\n") if "\n" in content else len(content)
            content = content[first_newline + 1:]
        if content.endswith("```"):
            content = content[:-3].rstrip()

        # Write the file
        out_path = project_root / file_path_str
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content + "\n", encoding="utf-8")
        files_written += 1

    return files_written
