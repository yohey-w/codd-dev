"""CoDD AI Extract — AI-powered design document recovery from codebases.

Uses _invoke_ai_command() (subprocess + stdin) to pass project context
to an AI CLI (default: claude --print) and generate 6-layer design docs.

Two-phase approach:
  Phase 1: Deterministic pre-scan (Python) — discover files, read key contents
  Phase 2: AI synthesis — categorize into 6 layers, generate structured docs
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# ═══════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════

PROMPT_TEMPLATE_FILE = Path(__file__).parent / "templates" / "extract_ai_prompt_baseline.md"

# Files to always read if they exist (framework detection)
FRAMEWORK_FILES = [
    "package.json", "requirements.txt", "Pipfile", "pyproject.toml",
    "composer.json", "Gemfile", "go.mod", "pom.xml", "build.gradle",
    "Cargo.toml", "mix.exs",
]

# Config files to always read
CONFIG_FILES = [
    "tsconfig.json", "next.config.mjs", "next.config.js", "next.config.ts",
    "vite.config.ts", "vite.config.js", "webpack.config.js",
    "tailwind.config.js", "tailwind.config.ts",
    ".env.example", ".env.local.example",
    "docker-compose.yml", "docker-compose.yaml", "Dockerfile",
]

# IaC patterns
IAC_EXTENSIONS = {".bicep", ".bicepparam", ".tf", ".tfvars", ".hcl"}

# Schema file patterns
SCHEMA_PATTERNS = [
    "prisma/schema.prisma", "db/schema.rb", "alembic/versions",
]

# Max file size to include in prompt (bytes)
MAX_FILE_SIZE = 50_000

# Max total context size (chars) — leave room for instructions
MAX_CONTEXT_CHARS = 400_000

# Directories to always skip
SKIP_DIRS = {
    "node_modules", ".next", "dist", "build", "coverage",
    ".turbo", ".cache", "vendor", "tmp", "__pycache__",
    ".git", ".venv", "venv", "env",
}


# ═══════════════════════════════════════════════════════════
# Data structures
# ═══════════════════════════════════════════════════════════

@dataclass
class PreScanResult:
    """Collected project context before AI invocation."""
    project_root: Path
    directory_tree: str
    framework_files: dict[str, str]   # path -> content
    source_files: dict[str, str]      # path -> content
    config_files: dict[str, str]      # path -> content
    iac_files: dict[str, str]         # path -> content
    test_files: list[str]             # path list (not contents — too large)
    total_files: int = 0
    total_chars: int = 0


@dataclass
class ExtractAIResult:
    """Result of AI-powered extraction."""
    output_dir: Path
    generated_files: list[Path] = field(default_factory=list)
    ai_raw_output: str = ""
    module_count: int = 0


# ═══════════════════════════════════════════════════════════
# Phase 1: Deterministic pre-scan
# ═══════════════════════════════════════════════════════════

def _build_directory_tree(root: Path, max_depth: int = 4) -> str:
    """Build a directory listing similar to `find . -maxdepth N -type d`."""
    lines: list[str] = []

    def _walk(current: Path, depth: int, prefix: str = ""):
        if depth > max_depth:
            return
        try:
            entries = sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            return

        dirs = [e for e in entries if e.is_dir() and e.name not in SKIP_DIRS and not e.name.startswith(".")]
        files = [e for e in entries if e.is_file()]

        for f in files:
            rel = f.relative_to(root)
            size = f.stat().st_size
            lines.append(f"{prefix}{rel}  ({size:,} bytes)")

        for d in dirs:
            rel = d.relative_to(root)
            lines.append(f"{prefix}{rel}/")
            _walk(d, depth + 1, prefix)

    _walk(root, 0)
    return "\n".join(lines[:500])  # cap at 500 lines


def _read_file_safe(path: Path, max_size: int = MAX_FILE_SIZE) -> str | None:
    """Read a file, returning None if too large or binary."""
    if not path.exists() or not path.is_file():
        return None
    if path.stat().st_size > max_size:
        return f"[FILE TOO LARGE: {path.stat().st_size:,} bytes — skipped]"
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def _find_files_by_pattern(root: Path, patterns: list[str]) -> list[Path]:
    """Find files matching glob patterns relative to root."""
    results: list[Path] = []
    for pattern in patterns:
        results.extend(root.glob(pattern))
    return sorted(set(results))


def _find_source_files(root: Path) -> list[Path]:
    """Find key source files for each layer."""
    patterns = [
        # L1: Data models
        "prisma/schema.prisma", "db/schema.rb", "**/models/*.py",
        "**/models/*.ts", "**/entity/*.ts", "**/entity/*.java",
        # L2: API routes
        "app/api/**/route.ts", "app/api/**/route.js",
        "pages/api/**/*.ts", "pages/api/**/*.js",
        "src/routes/**/*.ts", "src/routes/**/*.py",
        "app/Http/Controllers/**/*.php",
        "routes/**/*.php", "routes/**/*.rb",
        # L3: UI pages
        "app/**/page.tsx", "app/**/page.jsx",
        "src/pages/**/*.tsx", "src/pages/**/*.vue",
        "resources/views/**/*.blade.php",
        # L4: Business logic
        "src/lib/**/*.ts", "src/services/**/*.ts",
        "app/Services/**/*.php", "app/UseCases/**/*.py",
        # L5: Infra (handled separately via IAC_EXTENSIONS)
        # L6: Tests (list only, don't read)
    ]
    files: list[Path] = []
    for pattern in patterns:
        found = list(root.glob(pattern))
        # Filter out files in skip dirs
        found = [f for f in found if not any(s in f.parts for s in SKIP_DIRS)]
        files.extend(found)
    return sorted(set(files))


def _find_iac_files(root: Path) -> list[Path]:
    """Find IaC and DevOps files."""
    iac: list[Path] = []
    # IaC directories
    for dirname in ["infra", "deploy", "terraform", "cdk", "k8s", "helm", ".github/workflows"]:
        d = root / dirname
        if d.is_dir():
            for f in d.rglob("*"):
                if f.is_file() and f.stat().st_size < MAX_FILE_SIZE:
                    iac.append(f)
    # Root-level IaC files
    for f in root.iterdir():
        if f.is_file() and f.suffix in IAC_EXTENSIONS:
            iac.append(f)
    # CI/CD files
    for pattern in [".github/workflows/*.yml", ".github/workflows/*.yaml",
                    ".gitlab-ci.yml", "Jenkinsfile", "azure-pipelines.yml"]:
        iac.extend(root.glob(pattern))
    return sorted(set(iac))


def _find_test_files(root: Path) -> list[Path]:
    """Find test files (paths only, not contents)."""
    patterns = [
        "tests/**/*.test.*", "tests/**/*.spec.*",
        "test/**/*.test.*", "test/**/*.spec.*",
        "src/**/*.test.*", "src/**/*.spec.*",
        "spec/**/*_spec.*",
    ]
    tests: list[Path] = []
    for pattern in patterns:
        found = list(root.glob(pattern))
        found = [f for f in found if not any(s in f.parts for s in SKIP_DIRS)]
        tests.extend(found)
    return sorted(set(tests))


def pre_scan(project_root: Path) -> PreScanResult:
    """Phase 1: Deterministic pre-scan of the project."""
    result = PreScanResult(
        project_root=project_root,
        directory_tree="",
        framework_files={},
        source_files={},
        config_files={},
        iac_files={},
        test_files=[],
    )

    # Directory tree
    result.directory_tree = _build_directory_tree(project_root)

    # Framework detection files
    for name in FRAMEWORK_FILES:
        content = _read_file_safe(project_root / name)
        if content:
            result.framework_files[name] = content

    # Config files
    for name in CONFIG_FILES:
        content = _read_file_safe(project_root / name)
        if content:
            result.config_files[name] = content

    # Source files (key files per layer)
    total_chars = 0
    for f in _find_source_files(project_root):
        if total_chars > MAX_CONTEXT_CHARS:
            break
        content = _read_file_safe(f)
        if content:
            rel = str(f.relative_to(project_root))
            result.source_files[rel] = content
            total_chars += len(content)

    # IaC files
    for f in _find_iac_files(project_root):
        if total_chars > MAX_CONTEXT_CHARS:
            break
        content = _read_file_safe(f)
        if content:
            rel = str(f.relative_to(project_root))
            result.iac_files[rel] = content
            total_chars += len(content)

    # Test files (paths only)
    result.test_files = [
        str(f.relative_to(project_root)) for f in _find_test_files(project_root)
    ]

    result.total_files = (
        len(result.framework_files) + len(result.source_files)
        + len(result.config_files) + len(result.iac_files)
    )
    result.total_chars = total_chars

    return result


# ═══════════════════════════════════════════════════════════
# Phase 2: AI prompt building
# ═══════════════════════════════════════════════════════════

def _build_prompt(scan: PreScanResult) -> str:
    """Build the AI prompt from pre-scan results + extract template."""
    sections: list[str] = []

    # Load prompt template
    if PROMPT_TEMPLATE_FILE.exists():
        template = PROMPT_TEMPLATE_FILE.read_text(encoding="utf-8")
    else:
        template = _fallback_prompt_template()

    sections.append(template)
    sections.append("\n\n# ═══ PROJECT CONTEXT (pre-scanned) ═══\n")

    # Directory tree
    sections.append("## Directory Structure\n```")
    sections.append(scan.directory_tree)
    sections.append("```\n")

    # Framework files
    if scan.framework_files:
        sections.append("## Framework Detection Files\n")
        for name, content in scan.framework_files.items():
            sections.append(f"### {name}\n```\n{content}\n```\n")

    # Config files
    if scan.config_files:
        sections.append("## Configuration Files\n")
        for name, content in scan.config_files.items():
            sections.append(f"### {name}\n```\n{content}\n```\n")

    # Source files
    if scan.source_files:
        sections.append("## Source Files\n")
        for path, content in scan.source_files.items():
            sections.append(f"### {path}\n```\n{content}\n```\n")

    # IaC files
    if scan.iac_files:
        sections.append("## Infrastructure / IaC Files\n")
        for path, content in scan.iac_files.items():
            sections.append(f"### {path}\n```\n{content}\n```\n")

    # Test file list
    if scan.test_files:
        sections.append("## Test Files (paths only)\n```")
        sections.append("\n".join(scan.test_files))
        sections.append("```\n")

    # Final instruction
    sections.append("""
# ═══ TASK ═══

Using the project context above and the extract prompt instructions,
generate a complete 6-layer design document inventory.

Output format: One YAML document (extract_result.yaml schema from the prompt),
followed by 6 markdown documents (L1 through L6), each with CoDD frontmatter.

Separate each document with a line: `--- FILE: <filename> ---`

Example:
--- FILE: extract_result.yaml ---
meta:
  generated_at: ...
...

--- FILE: L1_data_models.md ---
---
id: L1_data_models
...
---
# L1: Data Models
...
""")

    return "\n".join(sections)


def _fallback_prompt_template() -> str:
    """Minimal prompt if template file is missing."""
    return """# CoDD Extract v3

Extract design documents from the provided codebase context.
Categorize all artifacts into 6 MECE layers:
- L1: Data Models (DB schema, entities)
- L2: API Endpoints (request handlers)
- L3: UI Pages (route-owning screens)
- L4: Business Logic (services, domain)
- L5: Infrastructure / Config (deploy, runtime, IaC, env)
- L6: Tests (automated verification)

Rules:
1. Source of truth is the provided file contents only. Never infer.
2. Every artifact gets one canonical layer. No duplicates.
3. All counts must be exact. No estimates.
4. IaC files belong to L5.
5. Test files belong to L6.
"""


# ═══════════════════════════════════════════════════════════
# Phase 3: AI invocation + output parsing
# ═══════════════════════════════════════════════════════════

def _invoke_ai_command(ai_command: str, prompt: str) -> str:
    """Invoke AI CLI via subprocess with prompt on stdin.

    Mirrors generator._invoke_ai_command() pattern.
    """
    import shlex
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


def _parse_ai_output(raw: str, output_dir: Path) -> list[Path]:
    """Parse AI output separated by `--- FILE: <name> ---` markers."""
    files: list[Path] = []
    current_name: str | None = None
    current_lines: list[str] = []

    for line in raw.split("\n"):
        stripped = line.strip()
        if stripped.startswith("--- FILE:") and stripped.endswith("---"):
            # Flush previous file
            if current_name:
                out_path = output_dir / current_name
                out_path.write_text("\n".join(current_lines), encoding="utf-8")
                files.append(out_path)
            # Start new file
            current_name = stripped.replace("--- FILE:", "").replace("---", "").strip()
            current_lines = []
        else:
            current_lines.append(line)

    # Flush last file
    if current_name:
        out_path = output_dir / current_name
        out_path.write_text("\n".join(current_lines), encoding="utf-8")
        files.append(out_path)

    return files


# ═══════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════

def run_extract_ai(
    project_root: Path,
    ai_command: str,
    output_dir: str | None = None,
    prompt_file: str | None = None,
) -> ExtractAIResult:
    """Run AI-powered design document extraction.

    Args:
        project_root: Path to the project to extract from.
        ai_command: AI CLI command (e.g. 'claude --print --model claude-opus-4-6').
        output_dir: Output directory (default: {project_root}/codd/extracted/).
        prompt_file: Path to a custom prompt file. Overrides the built-in baseline preset.
    """
    project_root = project_root.resolve()
    out = Path(output_dir) if output_dir else project_root / "codd" / "extracted"
    out.mkdir(parents=True, exist_ok=True)

    # Phase 1: Pre-scan
    scan = pre_scan(project_root)

    # Phase 2: Build prompt (custom or baseline)
    if prompt_file:
        custom_path = Path(prompt_file)
        if not custom_path.exists():
            raise FileNotFoundError(f"Prompt file not found: {prompt_file}")
        original_template = PROMPT_TEMPLATE_FILE
        # Temporarily swap template for custom prompt
        import codd.extract_ai as _self
        _self.PROMPT_TEMPLATE_FILE = custom_path
        prompt = _build_prompt(scan)
        _self.PROMPT_TEMPLATE_FILE = original_template
    else:
        prompt = _build_prompt(scan)

    # Phase 3: AI invocation
    raw_output = _invoke_ai_command(ai_command, prompt)

    # Phase 4: Parse and write
    generated = _parse_ai_output(raw_output, out)

    # Also save raw output
    raw_path = out / "_raw_ai_output.txt"
    raw_path.write_text(raw_output, encoding="utf-8")

    return ExtractAIResult(
        output_dir=out,
        generated_files=generated,
        ai_raw_output=raw_output,
        module_count=len(scan.source_files),
    )
