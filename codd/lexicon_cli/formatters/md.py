"""Markdown formatters for lexicon CLI output."""

from __future__ import annotations

from codd.lexicon_cli.inspector import LexiconDiffResult
from codd.lexicon_cli.reporter import CoverageMatrixReport


def format_diff_md(result: LexiconDiffResult) -> str:
    lines = [
        f"# Lexicon Diff: {result.lexicon_name}",
        "",
        f"Project: {result.project_root}",
        f"Mode: {result.mode}",
        "",
        "| Axis | Status | Hits | Examples |",
        "| --- | --- | ---: | --- |",
    ]
    for axis in result.axes:
        examples = "; ".join(f"{hit.path} L{hit.line} ({hit.term})" for hit in axis.hits[:3])
        lines.append(
            f"| {_escape(axis.axis_type)} | {axis.status} | {axis.hit_count} | {_escape(examples)} |"
        )
    lines.extend(
        [
            "",
            f"Summary: {result.covered_count}/{result.total_count} axes have coverage signals.",
            "Use --with-ai for authoritative covered/implicit/gap classification.",
            "",
        ]
    )
    return "\n".join(lines)


def format_coverage_report_md(report: CoverageMatrixReport) -> str:
    lines = [
        "# Coverage Matrix Report",
        "",
        f"Project: {report.project_root}",
        f"Mode: {report.mode}",
        f"Generated: {report.generated_at}",
        "",
        "| Lexicon | Axis | Status | Hits |",
        "| --- | --- | --- | ---: |",
    ]
    for row in report.rows:
        lines.append(
            f"| {_escape(row.lexicon_name)} | {_escape(row.axis_type)} | {row.status} | {row.hit_count} |"
        )
    lines.extend(
        [
            "",
            (
                f"Totals: {report.totals['axes']} axes, {report.totals['covered']} covered signals "
                f"({report.totals['covered_pct']:.2f}%), {report.totals['unknown']} unknown."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _escape(value: str) -> str:
    return value.replace("|", "\\|")
