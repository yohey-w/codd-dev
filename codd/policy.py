"""CoDD policy — enterprise policy checker for source code.

Scans source files against configurable policy rules defined in codd.yaml.
Reports violations for: forbidden patterns, required patterns, and
file-level constraints.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from codd.bridge import load_bridge_registry
from codd.config import load_project_config
from codd.discovery import scan_exclude_patterns
from codd.path_safety import resolve_project_path


def _warn_excluded_path(kind: str, raw: str) -> None:
    """Surface a path-escape exclusion (visibility, not silent swallow).

    Policy results are counts of files checked / violations, not a binary gate
    keyed on a single path, so an out-of-root configured/changed path is
    *excluded* from scanning (its contents cannot be read as policy evidence)
    rather than crashed on. The exclusion is reported on stderr so it is never a
    pure-green silent drop. ``PolicyResult`` keeps its public shape (back-compat).
    """
    print(
        f"warning: policy: {kind} '{raw}' resolves outside the project root; "
        f"excluded from policy scan (out-of-root paths are not read as evidence).",
        file=sys.stderr,
    )


@dataclass(frozen=True)
class PolicyViolation:
    """A single policy violation found in source code."""

    rule_id: str
    severity: str  # CRITICAL, WARNING, INFO
    file: str
    line: int | None
    message: str


@dataclass
class PolicyResult:
    """Aggregate result of policy checks."""

    files_checked: int = 0
    violations: list[PolicyViolation] = field(default_factory=list)
    rules_applied: int = 0

    @property
    def critical_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == "CRITICAL")

    @property
    def warning_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == "WARNING")

    @property
    def pass_(self) -> bool:
        return self.critical_count == 0


@dataclass(frozen=True)
class PolicyRule:
    """A parsed policy rule from config."""

    id: str
    description: str
    severity: str
    kind: str  # "forbidden", "required"
    pattern: str
    glob: str  # file glob to apply to
    compiled: re.Pattern[str] | None = None


def load_policies(config: dict[str, Any]) -> list[PolicyRule]:
    """Parse policy rules from codd.yaml config."""
    raw_policies = config.get("policies", [])
    if not isinstance(raw_policies, list):
        return []

    rules: list[PolicyRule] = []
    for entry in raw_policies:
        if not isinstance(entry, dict):
            continue

        rule_id = entry.get("id", "")
        if not rule_id:
            continue

        pattern = entry.get("pattern", "")
        if not pattern:
            continue

        try:
            compiled = re.compile(pattern)
        except re.error:
            compiled = None

        rules.append(PolicyRule(
            id=rule_id,
            description=entry.get("description", ""),
            severity=entry.get("severity", "WARNING").upper(),
            kind=entry.get("kind", "forbidden"),
            pattern=pattern,
            glob=entry.get("glob", "*.py"),
            compiled=compiled,
        ))

    return rules


def _run_policy_oss(
    project_root: Path,
    *,
    changed_files: list[str] | None = None,
) -> PolicyResult:
    """Check source files against policy rules.

    If changed_files is provided, only check those files.
    Otherwise check all files under source_dirs.
    """
    project_root = project_root.resolve()
    config = load_project_config(project_root)
    rules = load_policies(config)
    result = PolicyResult(rules_applied=len(rules))

    if not rules:
        return result

    source_dirs = (config.get("scan") or {}).get("source_dirs", [])
    exclude_patterns = scan_exclude_patterns(config)

    # Collect files to check
    if changed_files:
        # ``changed_files`` is user-controllable; jail each entry so an absolute/
        # ``../`` path or an in-root symlink escaping the tree is dropped (its
        # contents would otherwise be scanned as policy evidence). ``None`` ⇒
        # out-of-root. Keep only files that resolve inside the project root; the
        # escape is reported (visibility) rather than silently swallowed.
        files_to_check = []
        for f in changed_files:
            confined = resolve_project_path(project_root, f)
            if confined is None:
                _warn_excluded_path("changed_file", str(f))
                continue
            if confined.is_file():
                files_to_check.append(confined)
    else:
        files_to_check = _collect_source_files(project_root, source_dirs, exclude_patterns)

    for file_path in files_to_check:
        relative = file_path.relative_to(project_root).as_posix()
        applicable_rules = [r for r in rules if _file_matches_glob(relative, r.glob)]
        if not applicable_rules:
            continue

        result.files_checked += 1
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        lines = content.splitlines()

        for rule in applicable_rules:
            if rule.compiled is None:
                continue

            if rule.kind == "forbidden":
                for i, line in enumerate(lines, 1):
                    if rule.compiled.search(line):
                        result.violations.append(PolicyViolation(
                            rule_id=rule.id,
                            severity=rule.severity,
                            file=relative,
                            line=i,
                            message=f"{rule.description or rule.id}: forbidden pattern matched",
                        ))

            elif rule.kind == "required":
                # Required pattern must appear at least once in the file
                if not rule.compiled.search(content):
                    result.violations.append(PolicyViolation(
                        rule_id=rule.id,
                        severity=rule.severity,
                        file=relative,
                        line=None,
                        message=f"{rule.description or rule.id}: required pattern not found",
                    ))

    return result


def run_policy(
    project_root: Path,
    *,
    changed_files: list[str] | None = None,
) -> PolicyResult:
    """Run the OSS policy pack or delegate to a registered Pro policy pack."""
    handler = load_bridge_registry().policy_handler
    if handler is not None:
        return handler(project_root, changed_files=changed_files, fallback=_run_policy_oss)
    return _run_policy_oss(project_root, changed_files=changed_files)


def format_policy_text(result: PolicyResult) -> str:
    """Format policy result as human-readable text."""
    lines: list[str] = []
    status = "PASS" if result.pass_ else "FAIL"
    lines.append(f"Policy Check: {status}")
    lines.append(f"  Files: {result.files_checked}  Rules: {result.rules_applied}")
    lines.append(f"  Critical: {result.critical_count}  Warnings: {result.warning_count}")

    if result.violations:
        lines.append("")
        for v in sorted(result.violations, key=lambda x: (x.severity != "CRITICAL", x.file, x.line or 0)):
            loc = f"{v.file}:{v.line}" if v.line else v.file
            lines.append(f"  [{v.severity}] {loc} ({v.rule_id}): {v.message}")

    return "\n".join(lines)


def _collect_source_files(
    project_root: Path,
    source_dirs: list[str],
    exclude_patterns: list[str],
) -> list[Path]:
    """Collect all source files under configured source dirs."""
    files: list[Path] = []
    for src_dir in source_dirs:
        # ``scan.source_dirs`` is user-controllable (codd.yaml); jail each entry so
        # an absolute/``../`` dir or an in-root symlink escaping the tree is dropped
        # (its files would otherwise be scanned as policy evidence). ``None`` ⇒
        # out-of-root.
        full_path = resolve_project_path(project_root, src_dir)
        if full_path is None:
            _warn_excluded_path("source_dir", str(src_dir))
            continue
        if not full_path.exists():
            continue
        for file_path in sorted(full_path.rglob("*")):
            # Re-confine each match: rglob follows symlinks, so an in-root dir may
            # hold a symlink whose target escapes the root.
            if resolve_project_path(project_root, file_path) is None:
                continue
            if not file_path.is_file():
                continue
            relative = file_path.relative_to(project_root).as_posix()
            if any(_file_matches_glob(relative, pat) for pat in exclude_patterns):
                continue
            files.append(file_path)
    return files


def _file_matches_glob(path: str, glob_pattern: str) -> bool:
    """Simple glob matching: *.py, **/*.ts, etc."""
    from fnmatch import fnmatch
    # Support both "*.py" (basename match) and "**/*.py" (full path match)
    if "/" not in glob_pattern and "**" not in glob_pattern:
        return fnmatch(path.rsplit("/", 1)[-1], glob_pattern)
    return fnmatch(path, glob_pattern)
