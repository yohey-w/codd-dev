"""CoDD fix — detect test/build failures, map to design docs, and auto-fix via AI."""

from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codd.config import find_codd_dir, load_project_config
from codd.generator import _invoke_ai_command, _resolve_ai_command
from codd.scanner import _extract_frontmatter

logger = logging.getLogger("codd.fixer")


@dataclass
class FailureInfo:
    """A single test or build failure."""

    source: str  # "ci", "local", "file"
    category: str  # "test", "build", "lint", "typecheck"
    summary: str  # human-readable summary
    log: str  # full error log
    failed_files: list[str] = field(default_factory=list)  # files mentioned in errors


@dataclass
class FixAttempt:
    """Result of a single fix attempt."""

    attempt: int
    failures: list[FailureInfo]
    fixed: bool
    ai_output: str = ""


@dataclass
class FixResult:
    """Result of the entire fix process."""

    source: str  # "ci", "local", "file"
    attempts: list[FixAttempt]
    fixed: bool
    pushed: bool = False
    ci_passed: bool | None = None  # None = not checked


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_fix(
    project_root: Path,
    *,
    ai_command: str | None = None,
    max_attempts: int = 3,
    test_results: str | None = None,
    ci_log: str | None = None,
    ci_only: bool = False,
    local_only: bool = False,
    push: bool = True,
    dry_run: bool = False,
) -> FixResult:
    """Main entry point for codd fix.

    Auto-detects failure source:
    1. Explicit --test-results / --ci-log files
    2. CI failures via `gh run view`
    3. Local test execution

    Then maps failures to design docs, invokes AI to fix, and verifies.
    """
    config = load_project_config(project_root)
    resolved_ai = _resolve_ai_command(config, ai_command, command_name="fix")

    # Step 1: Detect failures
    failures: list[FailureInfo] = []
    source = "unknown"

    if test_results or ci_log:
        # Explicit files provided (e.g., from CI artifact download)
        source = "file"
        failures = _parse_failure_files(test_results, ci_log)
    elif not local_only:
        # Try CI first
        ci_failures = _detect_ci_failures(project_root)
        if ci_failures:
            source = "ci"
            failures = ci_failures

    if not failures and not ci_only:
        # Run tests locally
        source = "local"
        failures = _run_local_tests(project_root, config)

    if not failures:
        return FixResult(source=source, attempts=[], fixed=True)

    if dry_run:
        return FixResult(
            source=source,
            attempts=[FixAttempt(attempt=0, failures=failures, fixed=False)],
            fixed=False,
        )

    # Step 2: Fix loop
    attempts: list[FixAttempt] = []
    for attempt_num in range(1, max_attempts + 1):
        # Map failures to design context
        context = _build_fix_context(project_root, config, failures)

        # Build prompt and invoke AI (fix mode: returns fixed source, writes to files)
        prompt = _build_fix_prompt(project_root, failures, context, config)
        ai_output = _invoke_fix_ai(resolved_ai, prompt, project_root)

        # Re-run tests to verify
        new_failures = _run_local_tests(project_root, config)

        if new_failures is None:
            # Tests could not run — mark as unverified, not fixed
            logger.warning("Local tests could not run. Fix is unverified.")
            attempts.append(FixAttempt(
                attempt=attempt_num,
                failures=failures,
                fixed=False,
                ai_output=ai_output,
            ))
            break

        fixed = len(new_failures) == 0

        attempts.append(FixAttempt(
            attempt=attempt_num,
            failures=failures,
            fixed=fixed,
            ai_output=ai_output,
        ))

        if fixed:
            break

        # Next iteration uses new failures
        failures = new_failures

    all_fixed = attempts[-1].fixed if attempts else False

    # Step 3: Push and watch CI if fixed
    pushed = False
    ci_passed = None
    if all_fixed and push and not dry_run:
        pushed = _git_push(project_root)
        if pushed:
            ci_passed = _watch_ci(project_root)

    return FixResult(
        source=source,
        attempts=attempts,
        fixed=all_fixed,
        pushed=pushed,
        ci_passed=ci_passed,
    )


# ---------------------------------------------------------------------------
# AI invocation for fix (source-in → fixed-source-out → write back)
# ---------------------------------------------------------------------------

# Regex patterns to extract fenced code blocks tagged with file paths.

# Primary: ```language path/to/file.py
_FIX_BLOCK_RE = re.compile(
    r"```[a-zA-Z]*\s+([\w./_-]+)\s*\n(.*?)```",
    re.DOTALL,
)

# Fallback 1: **path/to/file.py** or `path/to/file.py` on preceding line
# followed by a code block
_FIX_BLOCK_PRECEDED_RE = re.compile(
    r"(?:\*\*|`)([\w./_-]+\.\w+)(?:\*\*|`)\s*:?\s*\n```[a-zA-Z]*\s*\n(.*?)```",
    re.DOTALL,
)

# Fallback 2: // filepath: path/to/file.py as first line inside code block
_FIX_BLOCK_COMMENT_RE = re.compile(
    r"```[a-zA-Z]*\s*\n\s*(?://|#)\s*(?:filepath|file):\s*([\w./_-]+)\s*\n(.*?)```",
    re.DOTALL,
)

# System prompt optimized for code fix (not document generation)
_FIX_SYSTEM_PROMPT = (
    "You are a code repair assistant. You receive error logs, current source code, "
    "and design documents. Output the complete fixed source for each file in fenced "
    "code blocks tagged with the file path. Do not output explanations before the "
    "code blocks. Fix implementation to match the design specification."
)


def _prepare_fix_ai_command(ai_command: str) -> str:
    """Adapt an AI command for fix mode.

    If the command contains a --system-prompt intended for document generation,
    replace it with the fix-optimized system prompt.
    """
    parts = shlex.split(ai_command)
    cleaned: list[str] = []
    skip_next = False
    has_system_prompt = False

    for tok in parts:
        if skip_next:
            skip_next = False
            continue
        if tok == "--system-prompt":
            skip_next = True
            has_system_prompt = True
            continue
        cleaned.append(tok)

    # Add fix-specific system prompt
    if has_system_prompt or "--print" in cleaned:
        cleaned.extend(["--system-prompt", _FIX_SYSTEM_PROMPT])

    return shlex.join(cleaned)


def _invoke_fix_ai(ai_command: str, prompt: str, project_root: Path) -> str:
    """Invoke AI in --print mode and apply returned code blocks to files.

    The prompt includes the original source and error log.  The AI returns
    the **complete fixed source** for each file, wrapped in fenced code
    blocks tagged with file paths::

        ```typescript src/app/api/enrollments/route.ts
        // ... fixed code ...
        ```

    This function parses those blocks and writes them back to disk.
    Uses multiple regex patterns for robustness (primary + 2 fallbacks).
    """
    fixed_command = _prepare_fix_ai_command(ai_command)
    ai_output = _invoke_ai_command(fixed_command, prompt)

    # Parse fenced code blocks with file paths and write them back
    applied: list[str] = []
    seen_paths: set[str] = set()

    # Try all patterns, primary first
    for pattern in (_FIX_BLOCK_RE, _FIX_BLOCK_PRECEDED_RE, _FIX_BLOCK_COMMENT_RE):
        for match in pattern.finditer(ai_output):
            file_path_str = match.group(1)
            fixed_code = match.group(2)

            if file_path_str in seen_paths:
                continue  # Already applied by a higher-priority pattern

            target = project_root / file_path_str
            if not target.resolve().is_relative_to(project_root.resolve()):
                logger.warning("Skipping file outside project: %s", file_path_str)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(fixed_code, encoding="utf-8")
            seen_paths.add(file_path_str)
            applied.append(file_path_str)
            logger.info("Applied fix to: %s", file_path_str)

    if not applied:
        logger.warning("AI output contained no parseable file blocks to apply")

    return ai_output


# ---------------------------------------------------------------------------
# Failure detection
# ---------------------------------------------------------------------------


def _detect_ci_failures(project_root: Path) -> list[FailureInfo]:
    """Check for CI failures via `gh run view`."""
    if not _has_gh_cli():
        return []

    try:
        # Get latest run status
        result = subprocess.run(
            ["gh", "run", "list", "--limit", "1", "--json",
             "status,conclusion,databaseId,headBranch"],
            capture_output=True, text=True, cwd=str(project_root),
        )
        if result.returncode != 0:
            return []

        runs = json.loads(result.stdout)
        if not runs:
            return []

        latest = runs[0]
        if latest.get("conclusion") != "failure":
            return []

        run_id = latest["databaseId"]

        # Get failed job logs
        log_result = subprocess.run(
            ["gh", "run", "view", str(run_id), "--log-failed"],
            capture_output=True, text=True, cwd=str(project_root),
        )
        if log_result.returncode != 0:
            return []

        log_text = log_result.stdout
        if not log_text.strip():
            return []

        # Parse log into failure categories
        return _parse_ci_log(log_text)

    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _parse_ci_log(log_text: str) -> list[FailureInfo]:
    """Parse GitHub Actions failed log into structured failures."""
    failures: list[FailureInfo] = []

    # Split by job steps
    sections = log_text.split("\n")
    current_category = "test"

    # Detect category from step names
    for line in sections:
        lower = line.lower()
        if "lint" in lower or "eslint" in lower:
            current_category = "lint"
        elif "typecheck" in lower or "tsc" in lower:
            current_category = "typecheck"
        elif "build" in lower and "npm run build" in lower:
            current_category = "build"
        elif "test" in lower:
            current_category = "test"

    failures.append(FailureInfo(
        source="ci",
        category=current_category,
        summary=f"CI failure ({current_category})",
        log=log_text[:15000],  # Truncate to avoid token overflow
        failed_files=_extract_file_paths_from_log(log_text),
    ))

    return failures


def _parse_failure_files(
    test_results: str | None,
    ci_log: str | None,
) -> list[FailureInfo]:
    """Parse explicit failure files."""
    failures: list[FailureInfo] = []

    if test_results:
        path = Path(test_results)
        if path.is_dir():
            # Playwright-style: look for test results
            for f in path.rglob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    failures.extend(_parse_playwright_results(data))
                except (json.JSONDecodeError, KeyError):
                    continue
            if not failures:
                # Fall back to reading all text files
                for f in path.rglob("*"):
                    if f.is_file() and f.suffix in (".txt", ".log"):
                        failures.append(FailureInfo(
                            source="file",
                            category="test",
                            summary=f"Test failure from {f.name}",
                            log=f.read_text(encoding="utf-8")[:10000],
                            failed_files=_extract_file_paths_from_log(
                                f.read_text(encoding="utf-8")
                            ),
                        ))
        elif path.is_file():
            content = path.read_text(encoding="utf-8")
            failures.append(FailureInfo(
                source="file",
                category="test",
                summary="Test failures from results file",
                log=content[:10000],
                failed_files=_extract_file_paths_from_log(content),
            ))

    if ci_log:
        ci_path = Path(ci_log)
        if ci_path.is_file():
            content = ci_path.read_text(encoding="utf-8")
            failures.append(FailureInfo(
                source="file",
                category=_detect_category_from_log(content),
                summary="CI failure from log file",
                log=content[:15000],
                failed_files=_extract_file_paths_from_log(content),
            ))

    return failures


def _parse_playwright_results(data: dict) -> list[FailureInfo]:
    """Parse Playwright JSON test results."""
    failures: list[FailureInfo] = []

    suites = data.get("suites", [])
    for suite in suites:
        for spec in suite.get("specs", []):
            for test in spec.get("tests", []):
                for result in test.get("results", []):
                    if result.get("status") == "failed":
                        error_msg = ""
                        for err in result.get("errors", []):
                            error_msg += err.get("message", "") + "\n"
                            error_msg += err.get("stack", "") + "\n"

                        failures.append(FailureInfo(
                            source="file",
                            category="test",
                            summary=f"FAIL: {spec.get('title', 'unknown')}",
                            log=error_msg[:5000],
                            failed_files=list(set(
                                spec.get("file", "").split("/")[-1:]
                            )),
                        ))

    return failures


def _run_local_tests(project_root: Path, config: dict[str, Any]) -> list[FailureInfo] | None:
    """Run the project's test suite locally and return failures.

    Returns:
        list[FailureInfo]: Failures found (empty list = all tests passed).
        None: Tests could not be executed (no command, missing runtime, etc.).
              Callers MUST treat None as "unverified", NOT as "fixed".
    """
    fix_config = config.get("fix", {})
    test_command = fix_config.get("test_command")

    if not test_command:
        # Auto-detect from project
        test_command = _detect_test_command(project_root)

    if not test_command:
        logger.warning("No test command configured or detected. Cannot verify fix.")
        return None

    try:
        result = subprocess.run(
            test_command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=str(project_root),
            timeout=300,  # 5 min timeout
        )
    except subprocess.TimeoutExpired:
        return [FailureInfo(
            source="local",
            category="test",
            summary="Test execution timed out (5 min)",
            log="Tests did not complete within 5 minutes.",
        )]

    if result.returncode == 0:
        return []

    output = (result.stdout + "\n" + result.stderr).strip()
    return [FailureInfo(
        source="local",
        category="test",
        summary="Local test failure",
        log=output[:15000],
        failed_files=_extract_file_paths_from_log(output),
    )]


def _detect_test_command(project_root: Path) -> str | None:
    """Auto-detect the test command for the project."""
    pkg_json = project_root / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
            scripts = pkg.get("scripts", {})
            # Prefer unit > test (E2E needs full-stack env, not available locally)
            for key in ("test:unit", "test", "test:e2e"):
                if key in scripts:
                    return f"npm run {key}"
        except json.JSONDecodeError:
            pass

    pyproject = project_root / "pyproject.toml"
    if pyproject.exists():
        return "pytest --tb=short -q"

    makefile = project_root / "Makefile"
    if makefile.exists():
        content = makefile.read_text(encoding="utf-8")
        if "test:" in content:
            return "make test"

    return None


# ---------------------------------------------------------------------------
# Context building (design doc mapping)
# ---------------------------------------------------------------------------


def _build_fix_context(
    project_root: Path,
    config: dict[str, Any],
    failures: list[FailureInfo],
) -> str:
    """Map failures to relevant design documents and build context string."""
    doc_dirs = config.get("scan", {}).get("doc_dirs", [])

    # Collect all file paths mentioned in failures
    failed_paths: set[str] = set()
    for f in failures:
        failed_paths.update(f.failed_files)

    # Find all design docs
    design_docs: list[tuple[str, str, dict]] = []  # (path, content, frontmatter)
    for doc_dir in doc_dirs:
        full_path = project_root / doc_dir
        if not full_path.exists():
            continue
        for md_file in full_path.rglob("*.md"):
            codd_data = _extract_frontmatter(md_file)
            if not codd_data or "node_id" not in codd_data:
                continue
            rel_path = md_file.relative_to(project_root).as_posix()
            content = md_file.read_text(encoding="utf-8")
            design_docs.append((rel_path, content, codd_data))

    if not design_docs:
        return ""

    # Try to match failures to specific design docs via modules/type
    relevant_docs: list[tuple[str, str]] = []

    # Strategy 1: Match by module name from failed file paths
    for fpath in failed_paths:
        for doc_path, doc_content, fm in design_docs:
            modules = fm.get("modules", [])
            doc_type = fm.get("type", "")
            # Check if any module name appears in the failed path
            for mod in modules:
                if mod.lower() in fpath.lower():
                    relevant_docs.append((doc_path, doc_content))
                    break

    # Strategy 2: Match by doc type (test docs are always relevant for test failures)
    for doc_path, doc_content, fm in design_docs:
        doc_type = fm.get("type", "")
        if doc_type in ("test", "operations"):
            relevant_docs.append((doc_path, doc_content))

    # Deduplicate
    seen: set[str] = set()
    unique_docs: list[tuple[str, str]] = []
    for path, content in relevant_docs:
        if path not in seen:
            seen.add(path)
            unique_docs.append((path, content))

    # If no specific matches, include all design docs (truncated)
    if not unique_docs:
        unique_docs = [(p, c) for p, c, _ in design_docs]

    # Build context string, truncating if too large
    context_parts: list[str] = []
    total_chars = 0
    max_chars = 30000  # ~7500 tokens

    for doc_path, doc_content in unique_docs:
        if total_chars + len(doc_content) > max_chars:
            # Truncate this doc
            remaining = max_chars - total_chars
            if remaining > 500:
                context_parts.append(
                    f"--- {doc_path} (truncated) ---\n{doc_content[:remaining]}\n--- END ---"
                )
            break
        context_parts.append(f"--- {doc_path} ---\n{doc_content}\n--- END ---")
        total_chars += len(doc_content)

    return "\n\n".join(context_parts)


# ---------------------------------------------------------------------------
# Fix prompt
# ---------------------------------------------------------------------------


def _build_fix_prompt(
    project_root: Path,
    failures: list[FailureInfo],
    design_context: str,
    config: dict[str, Any],
) -> str:
    """Build the prompt for AI to fix failures.

    The prompt includes: error logs, design docs, AND the current source
    of files mentioned in failures.  The AI returns the complete fixed
    source for each file in fenced code blocks tagged with file paths.
    """
    project_name = config.get("project", {}).get("name", project_root.name)
    language = config.get("project", {}).get("language", "unknown")

    failure_section = []
    for i, f in enumerate(failures, 1):
        failure_section.append(f"### Failure {i}: {f.summary}")
        failure_section.append(f"Category: {f.category}")
        if f.failed_files:
            failure_section.append(f"Related files: {', '.join(f.failed_files)}")
        failure_section.append(f"```\n{f.log}\n```")
        failure_section.append("")

    # Collect current source of files mentioned in failures
    source_section = _collect_source_files(project_root, failures)

    lines = [
        f"You are fixing failures in the project '{project_name}' ({language}).",
        "",
        "## Failures to fix",
        "",
        *failure_section,
        "## Current source code of relevant files",
        "",
        source_section if source_section else "(no source files found)",
        "",
        "## Design documents (for context — these define the intended behavior)",
        "",
        design_context if design_context else "(no design documents found)",
        "",
        "## Instructions",
        "",
        "1. Read the failing test/build output carefully.",
        "2. Use the design documents to understand the INTENDED behavior.",
        "3. Fix the IMPLEMENTATION code to match the design, not the other way around.",
        "   - If tests fail, fix the source code so tests pass.",
        "   - If a test expects an endpoint/method/feature that doesn't exist in code,",
        "     ADD the missing implementation as described in the design documents.",
        "     The test is correct (it matches the spec); the code is incomplete.",
        "   - If build fails (type errors, import errors), fix the source code.",
        "   - If lint fails, fix the lint issues in the source code.",
        "   - If a tool prompted interactively in CI (missing config), create the required config file.",
        "4. Do NOT modify test files unless the test itself has a bug (e.g., wrong import path).",
        "5. Do NOT modify design documents.",
        "6. Make minimal, focused changes. Don't refactor unrelated code.",
        "7. Follow the target framework's lint rules and naming conventions.",
        "   Avoid using global/reserved names (module, exports, require, etc.) as local variables.",
        "",
        "## Output format (CRITICAL)",
        "",
        "For each file you fix or create, output the COMPLETE file content in a fenced",
        "code block tagged with the language and the file path (relative to project root):",
        "",
        "```<language> <relative/path/to/file>",
        "// ... complete fixed source code ...",
        "```",
        "",
        "Example:",
        "",
        f"```{language} src/app/api/example/route.ts",
        "// complete file content here",
        "```",
        "",
        "After all code blocks, briefly explain what you fixed and why.",
    ]

    return "\n".join(lines)


def _collect_source_files(
    project_root: Path,
    failures: list[FailureInfo],
    max_chars: int = 50000,
) -> str:
    """Read current source of files mentioned in failure logs.

    When only test files are mentioned (common for E2E failures), infers
    implementation file paths from test names using naming conventions.
    """
    # Collect unique file paths from failures
    candidate_paths: list[str] = []
    test_paths: list[str] = []
    for f in failures:
        for fp in f.failed_files:
            # Normalize: strip CI runner prefixes, keep relative paths
            clean = fp
            for prefix in ["/home/runner/work/", "/github/workspace/"]:
                if clean.startswith(prefix):
                    parts = clean[len(prefix):].split("/", 2)
                    if len(parts) >= 3:
                        clean = parts[2]
                    break
            candidate_paths.append(clean)

    # Separate implementation files from test files
    impl_paths: list[str] = []
    for p in candidate_paths:
        if _is_test_path(p):
            test_paths.append(p)
        else:
            impl_paths.append(p)

    # If we only have test files, infer implementation paths from test names
    if not impl_paths and test_paths:
        impl_paths = _infer_impl_paths(project_root, test_paths)
        logger.info("Inferred %d implementation paths from %d test paths",
                     len(impl_paths), len(test_paths))

    # Also extract API endpoint paths from error logs and find route files
    api_paths = _extract_api_endpoints_from_failures(failures)
    if api_paths:
        route_files = _find_route_files_for_endpoints(project_root, api_paths)
        for rf in route_files:
            if rf not in impl_paths:
                impl_paths.append(rf)
        logger.info("Found %d route files from %d API endpoints in logs",
                     len(route_files), len(api_paths))

    # Read implementation files
    seen: set[str] = set()
    source_parts: list[str] = []
    total_chars = 0

    for rel_path in impl_paths:
        if rel_path in seen:
            continue
        seen.add(rel_path)

        target = project_root / rel_path
        if not target.is_file():
            continue

        try:
            content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        if total_chars + len(content) > max_chars:
            remaining = max_chars - total_chars
            if remaining > 500:
                rel_str = str(target.relative_to(project_root))
                source_parts.append(
                    f"```{_guess_lang(target)} {rel_str}\n{content[:remaining]}\n```\n(truncated)"
                )
            break

        rel_str = str(target.relative_to(project_root))
        source_parts.append(f"```{_guess_lang(target)} {rel_str}\n{content}\n```")
        total_chars += len(content)

    return "\n\n".join(source_parts)


def _is_test_path(path: str) -> bool:
    """Check if a path looks like a test file."""
    parts = path.replace("\\", "/").split("/")
    # Directory-based: tests/, __tests__/, test/, spec/
    if any(p in ("tests", "__tests__", "test", "spec") for p in parts):
        return True
    # File-based: *.spec.*, *.test.*, test_*
    basename = parts[-1] if parts else ""
    if ".spec." in basename or ".test." in basename or basename.startswith("test_"):
        return True
    return False


def _infer_impl_paths(project_root: Path, test_paths: list[str]) -> list[str]:
    """Infer implementation file paths from test file paths.

    Uses naming conventions to find likely implementation files:
    - tests/e2e/enrollments.spec.ts → src/app/api/enrollments/route.ts
    - tests/e2e/admin.spec.ts → src/app/api/admin/route.ts
    - tests/unit/auth.test.ts → src/auth.ts, src/services/auth/
    - tests/test_tasks.py → tasks.py, app.py, src/tasks.py
    """
    inferred: list[str] = []

    for test_path in test_paths:
        # Extract the domain name from test file
        basename = Path(test_path).stem  # e.g. "enrollments.spec" or "test_tasks"
        # Strip common test suffixes/prefixes
        domain = basename
        for suffix in (".spec", ".test", "_spec", "_test"):
            if domain.endswith(suffix):
                domain = domain[: -len(suffix)]
        if domain.startswith("test_"):
            domain = domain[5:]

        if not domain:
            continue

        # Search for matching implementation files
        candidates = _find_impl_candidates(project_root, domain)
        inferred.extend(candidates)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for p in inferred:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def _find_impl_candidates(project_root: Path, domain: str) -> list[str]:
    """Find implementation files matching a domain name."""
    candidates: list[str] = []
    domain_lower = domain.lower().replace("-", "_")

    # Strategy 1: API route files — **/api/{domain}/route.{ts,js}
    # Handles both standard (src/app/api/) and generated (src/generated/*/app/api/)
    domain_kebab = domain_lower.replace("_", "-")
    for domain_variant in {domain_lower, domain_kebab}:
        for ext in ("ts", "tsx", "js"):
            for match in project_root.glob(f"**/api/{domain_variant}/route.{ext}"):
                if match.is_file():
                    rel = str(match.relative_to(project_root))
                    if rel not in candidates:
                        candidates.append(rel)

    # Strategy 3: Generated/service files — src/**/domain*.ts
    for pattern in (
        f"src/**/*{domain_lower}*",
        f"src/**/*{domain_kebab}*",
        f"lib/**/*{domain_lower}*",
    ):
        for match in project_root.glob(pattern):
            if match.is_file() and not _is_test_path(str(match.relative_to(project_root))):
                rel = str(match.relative_to(project_root))
                if rel not in candidates:
                    candidates.append(rel)

    # Strategy 4: Python — {domain}.py, app.py in same directory
    for pattern in (
        f"**/{domain_lower}.py",
        f"**/app.py",
        f"src/**/{domain_lower}.py",
    ):
        for match in project_root.glob(pattern):
            if match.is_file() and not _is_test_path(str(match.relative_to(project_root))):
                rel = str(match.relative_to(project_root))
                if rel not in candidates:
                    candidates.append(rel)

    return candidates


def _extract_api_endpoints_from_failures(failures: list[FailureInfo]) -> list[str]:
    """Extract API endpoint paths (e.g., /api/enrollments) from error logs."""
    endpoint_re = re.compile(r"/api/[\w/-]+")
    endpoints: list[str] = []
    seen: set[str] = set()
    for f in failures:
        for match in endpoint_re.finditer(f.log):
            ep = match.group(0).rstrip("/")
            if ep not in seen:
                seen.add(ep)
                endpoints.append(ep)
    return endpoints


def _find_route_files_for_endpoints(
    project_root: Path, endpoints: list[str],
) -> list[str]:
    """Find route files matching API endpoint paths.

    E.g., /api/enrollments → **/api/enrollments/route.{ts,js}
    """
    found: list[str] = []
    for ep in endpoints:
        # Strip leading /
        ep_path = ep.lstrip("/")
        for ext in ("ts", "tsx", "js"):
            for match in project_root.glob(f"**/{ep_path}/route.{ext}"):
                if match.is_file():
                    rel = str(match.relative_to(project_root))
                    if rel not in found:
                        found.append(rel)
    return found


def _guess_lang(path: Path) -> str:
    """Guess language identifier from file extension."""
    ext_map = {
        ".ts": "typescript", ".tsx": "typescript", ".js": "javascript",
        ".jsx": "javascript", ".py": "python", ".rb": "ruby",
        ".go": "go", ".rs": "rust", ".java": "java",
        ".yml": "yaml", ".yaml": "yaml", ".json": "json",
    }
    return ext_map.get(path.suffix.lower(), "")


# ---------------------------------------------------------------------------
# Post-fix: push & CI watch
# ---------------------------------------------------------------------------


def _git_push(project_root: Path) -> bool:
    """Commit fixes and push."""
    try:
        # Check if there are changes
        status = subprocess.run(
            ["git", "diff", "--quiet"],
            cwd=str(project_root), capture_output=True,
        )
        if status.returncode == 0:
            return False  # No changes

        # Stage and commit
        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(project_root), capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "commit", "-m",
             "fix: auto-fix failures via codd fix\n\nCo-Authored-By: CoDD <noreply@codd.dev>"],
            cwd=str(project_root), capture_output=True, check=True,
        )
        result = subprocess.run(
            ["git", "push"],
            cwd=str(project_root), capture_output=True, text=True,
        )
        return result.returncode == 0
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _watch_ci(project_root: Path) -> bool | None:
    """Watch the CI run triggered by the push. Returns True if passed, False if failed, None if unavailable."""
    if not _has_gh_cli():
        return None

    try:
        result = subprocess.run(
            ["gh", "run", "watch", "--exit-status"],
            cwd=str(project_root),
            capture_output=True, text=True,
            timeout=600,  # 10 min max
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _has_gh_cli() -> bool:
    """Check if `gh` CLI is available."""
    try:
        result = subprocess.run(
            ["gh", "--version"], capture_output=True, text=True,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def _extract_file_paths_from_log(log: str) -> list[str]:
    """Extract file paths from error logs."""
    import re

    # Match common patterns: path/to/file.ts:42:10 or path/to/file.py:42
    pattern = re.compile(r'(?:^|\s)((?:[\w./-]+/)?[\w.-]+\.(?:ts|tsx|js|jsx|py|go|java|rs))(?::\d+)?', re.MULTILINE)
    matches = pattern.findall(log)

    # Deduplicate and filter
    seen: set[str] = set()
    result: list[str] = []
    for m in matches:
        if m not in seen and not m.startswith("node_modules/"):
            seen.add(m)
            result.append(m)

    return result


def _detect_category_from_log(log: str) -> str:
    """Detect failure category from log content."""
    lower = log.lower()
    # Interactive prompts in CI = missing config (tool asks "How would you like to configure...")
    if "how would you like to" in lower or "would you like to set up" in lower:
        return "config"
    if "tsc" in lower or "type error" in lower or "ts(" in lower or "ts2" in lower:
        return "typecheck"
    if "eslint" in lower or "lint" in lower:
        return "lint"
    if "build" in lower and ("error" in lower or "failed" in lower):
        return "build"
    return "test"
