"""Self-contained HTML formatter for coverage matrix reports."""

from __future__ import annotations

from html import escape

from codd.lexicon_cli.reporter import CoverageMatrixReport


def format_coverage_report_html(report: CoverageMatrixReport) -> str:
    rows = "\n".join(
        "<tr>"
        f"<td>{escape(row.lexicon_name)}</td>"
        f"<td>{escape(row.axis_type)}</td>"
        f"<td><span class=\"status {escape(row.status)}\">{escape(row.status)}</span></td>"
        f"<td>{row.hit_count}</td>"
        "</tr>"
        for row in report.rows
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>CoDD Coverage Matrix Report</title>
<style>
body {{ font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 2rem; color: #1f2933; }}
h1 {{ font-size: 1.7rem; margin-bottom: 0.25rem; }}
.meta {{ color: #52616b; margin: 0.2rem 0; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 1.25rem; }}
th, td {{ border: 1px solid #d8dee4; padding: 0.45rem 0.6rem; text-align: left; }}
th {{ background: #f4f7f9; }}
.status {{ border-radius: 4px; padding: 0.1rem 0.35rem; background: #eef2f7; }}
.covered_text_match, .covered, .implicit {{ background: #e7f6ed; color: #125c2f; }}
.unknown, .gap {{ background: #fff4d6; color: #724c00; }}
</style>
</head>
<body>
<h1>Coverage Matrix Report</h1>
<p class="meta">Project: {escape(report.project_root)}</p>
<p class="meta">Mode: {escape(report.mode)}</p>
<p class="meta">Generated: {escape(report.generated_at)}</p>
<p class="meta">Totals: {report.totals["axes"]} axes, {report.totals["covered"]} covered signals ({report.totals["covered_pct"]:.2f}%), {report.totals["unknown"]} unknown.</p>
<table>
<thead><tr><th>Lexicon</th><th>Axis</th><th>Status</th><th>Hits</th></tr></thead>
<tbody>
{rows}
</tbody>
</table>
</body>
</html>
"""
