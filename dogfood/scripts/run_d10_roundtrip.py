#!/usr/bin/env python3
"""D10 — round-trip fidelity: code → deterministic extract → recovered inventory.

The ideal round trip is greenfield → delete docs → restore → diff, but the
greenfield/restore-with-AI legs need an LLM. This runner exercises the
DETERMINISTIC spine of that loop and is therefore free + CI-safe:

  originals  := the modules CoDD's static analysis finds in the fixture's code,
                plus the hand-authored design node inventory in its docs.
  recovered  := the module docs that ``codd extract`` (no AI) regenerates from
                that same code.
  finding    := any original code module the extractor fails to re-document
                (a coverage drop in the round trip), or a crash building the
                restoration/coverage view.

Runs on ``dogfood/fixtures/roundtrip_app`` (design docs + matching source).
No LLM.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from _common import AxisResult, Finding, FIXTURES_DIR, ensure_repo_on_path

ensure_repo_on_path()

FIXTURE = FIXTURES_DIR / "roundtrip_app"


def _declared_node_ids(project: Path) -> set[str]:
    """Hand-authored node inventory: node_ids in the fixture's docs."""
    from codd.frontmatter import read_frontmatter

    ids: set[str] = set()
    for md in project.rglob("*.md"):
        fm = read_frontmatter(md)
        if not fm:
            continue
        block = fm.get("codd", fm) if isinstance(fm, dict) else {}
        nid = block.get("node_id") if isinstance(block, dict) else None
        if nid:
            ids.add(str(nid))
    return ids


def run() -> AxisResult:
    result = AxisResult(axis="D10")

    if not FIXTURE.exists():
        result.skipped.append(f"fixture missing: {FIXTURE}")
        result.summary = "fixture missing — nothing to round-trip"
        return result

    from codd.extractor import extract_facts, run_extract
    from codd.frontmatter import read_frontmatter

    # ── originals ────────────────────────────────────────────────────────────
    facts = extract_facts(FIXTURE)
    code_modules = set(facts.modules)
    design_nodes = _declared_node_ids(FIXTURE)
    result.stats["code_modules"] = sorted(code_modules)
    result.stats["design_nodes"] = sorted(design_nodes)

    # ── round trip: regenerate module docs from code (deterministic) ─────────
    recovered_modules: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="codd-d10.") as td:
        try:
            res = run_extract(FIXTURE, None, None, td)
        except Exception as exc:
            result.findings.append(
                Finding("D10", "codd extract crashed on round-trip", f"{type(exc).__name__}: {exc}",
                        subject="roundtrip_app")
            )
            result.summary = "extract crashed during round trip"
            return result

        result.stats["extract_module_count"] = res.module_count
        for p in res.generated_files:
            fm = read_frontmatter(p)
            if not fm:
                continue
            block = fm.get("codd", fm) if isinstance(fm, dict) else {}
            nid = block.get("node_id") if isinstance(block, dict) else None
            # extracted module docs are namespaced design:extract:<module>
            if isinstance(nid, str) and nid.startswith("design:extract:"):
                recovered_modules.add(nid.rsplit(":", 1)[-1])

    result.stats["recovered_modules"] = sorted(recovered_modules)

    # ── diff: every code module must survive the round trip ──────────────────
    dropped = code_modules - recovered_modules
    for m in sorted(dropped):
        result.findings.append(
            Finding("D10", "module lost in extract round trip",
                    f"code module '{m}' was found by static analysis but not re-documented by codd extract",
                    subject="roundtrip_app")
        )

    # ── restoration/coverage view must not crash ─────────────────────────────
    try:
        from codd.restoration_report import build_restoration_report

        rep = build_restoration_report(FIXTURE)
        result.stats["restoration_artifacts"] = len(getattr(rep, "artifacts", []))
    except Exception as exc:
        result.findings.append(
            Finding("D10", "build_restoration_report crashed", f"{type(exc).__name__}: {exc}",
                    subject="roundtrip_app")
        )

    result.summary = (
        f"{len(code_modules)} code module(s), recovered {len(recovered_modules)}, "
        f"{len(design_nodes)} design node(s); {len(result.findings)} finding(s)"
    )
    return result


def main() -> int:
    result = run()
    result.print_report()
    return 1 if result.findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
