"""Markdown report generation for runtime smoke checks."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from codd.runtime_smoke.checks import CheckResult

if TYPE_CHECKING:
    from codd.runtime_smoke.runner import SmokeResult


def generate_markdown_section(checks: list[CheckResult], overall_passed: bool) -> str:
    """Render a Step 8 runtime smoke report section."""
    status = "PASS" if overall_passed else "FAIL"
    lines = [
        "## § Step 8 Runtime Smoke",
        "",
        f"Overall: {status}",
        "",
        "| Check | Status | Elapsed |",
        "| --- | --- | ---: |",
    ]
    for result in checks:
        lines.append(f"| {result.name} | {_status(result)} | {result.elapsed_sec:.3f}s |")
    lines.extend(["", "### Raw Output", ""])
    for result in checks:
        lines.extend(
            [
                f"#### {result.name}",
                "",
                f"- category: `{result.category or 'unknown'}`",
                f"- status: `{_status(result)}`",
                f"- elapsed: `{result.elapsed_sec:.3f}s`",
                "",
                "```text",
                result.output.rstrip() or "(no output)",
                "```",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def write_markdown_report(result: "SmokeResult", report_path: Path) -> Path:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(result.markdown_section, encoding="utf-8")
    return report_path


def _status(result: CheckResult) -> str:
    if result.skipped:
        return "SKIPPED"
    return "PASS" if result.passed else "FAIL"
