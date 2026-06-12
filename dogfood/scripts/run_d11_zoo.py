#!/usr/bin/env python3
"""D11 — alien-repo zoo: run CoDD's DETERMINISTIC layers over diverse repos.

For every repo in ``dogfood/zoo.yaml`` we run only the no-LLM layers
(``extract_facts`` → ``derive_iac_nfrs`` → ``frontmatter`` → restoration view)
and record any crash/exception as a finding. The zoo ships 3 local synthetic
fixtures so it runs OFFLINE and FREE; real OSS repos can be added by ``path:``
(already cloned) or ``url:`` (cloned on demand with ``--online``).

Graceful degradation: a repo that is unavailable (missing path, or a ``url:``
that cannot be cloned / ``--online`` not given) is SKIPPED with a note, never a
failure. Findings come only from real crashes on repos we actually analyzed.

No LLM.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
import warnings
from pathlib import Path

import yaml

from _common import AxisResult, Finding, REPO_ROOT, DOGFOOD_DIR, ensure_repo_on_path

ensure_repo_on_path()

ZOO_PATH = DOGFOOD_DIR / "zoo.yaml"
CLONE_TIMEOUT_S = 180


def _analyze(root: Path, subject: str, result: AxisResult) -> None:
    """Run every deterministic layer over one repo; each crash → a Finding."""
    from codd.extractor import extract_facts
    from codd.iac_nfr import derive_iac_nfrs
    from codd import frontmatter as fm

    stats: dict = {"repo": subject}

    # extract_facts is the spine; if it crashes, nothing else can run.
    try:
        with warnings.catch_warnings(record=True) as wl:
            warnings.simplefilter("always")
            facts = extract_facts(root)
        stats.update(
            files=facts.total_files,
            modules=len(facts.modules),
            infra=len(facts.infra_config),
            warnings=len(wl),
        )
    except Exception as exc:
        result.findings.append(
            Finding("D11", "extract_facts crashed", f"{type(exc).__name__}: {exc}", subject)
        )
        result.stats[subject] = {**stats, "extract": "CRASH"}
        print(f"  {subject}: extract_facts CRASH — {type(exc).__name__}: {exc}")
        return

    # IaC → NFR derivation.
    try:
        nfrs = derive_iac_nfrs(facts.infra_config)
        stats["nfrs"] = len(nfrs)
    except Exception as exc:
        result.findings.append(
            Finding("D11", "derive_iac_nfrs crashed", f"{type(exc).__name__}: {exc}", subject)
        )

    # Frontmatter must parse every markdown doc leniently (never raise).
    try:
        md = 0
        for p in root.rglob("*.md"):
            fm.read_frontmatter(p)
            md += 1
        stats["markdown"] = md
    except Exception as exc:
        result.findings.append(
            Finding("D11", "frontmatter parse crashed", f"{type(exc).__name__}: {exc}", subject)
        )

    # Restoration/coverage view — must not crash on alien input.
    try:
        from codd.restoration_report import build_restoration_report

        build_restoration_report(root)
    except Exception as exc:
        result.findings.append(
            Finding("D11", "build_restoration_report crashed", f"{type(exc).__name__}: {exc}", subject)
        )

    result.stats[subject] = stats
    print(
        f"  {subject}: files={stats.get('files','?')} modules={stats.get('modules','?')} "
        f"infra={stats.get('infra','?')} nfrs={stats.get('nfrs','?')} "
        f"md={stats.get('markdown','?')} warn={stats.get('warnings','?')}"
    )


def _resolve(entry: dict, online: bool, scratch: Path, result: AxisResult) -> Path | None:
    name = entry.get("name", "?")
    path = entry.get("path")
    url = entry.get("url")
    if path:
        p = Path(path)
        if not p.is_absolute():
            p = REPO_ROOT / path
        if p.exists():
            return p
        result.skipped.append(f"{name}: local path missing ({path})")
        return None
    if url:
        if not online:
            result.skipped.append(f"{name}: url repo skipped (pass --online to clone): {url}")
            return None
        dest = scratch / name
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", "--quiet", url, str(dest)],
                check=True, timeout=CLONE_TIMEOUT_S, capture_output=True,
            )
            return dest
        except Exception:
            result.skipped.append(f"{name}: clone failed (offline-safe skip): {url}")
            return None
    result.skipped.append(f"{name}: entry has neither path nor url")
    return None


def run(online: bool = False) -> AxisResult:
    result = AxisResult(axis="D11")
    data = yaml.safe_load(ZOO_PATH.read_text(encoding="utf-8")) if ZOO_PATH.exists() else {}
    repos = (data or {}).get("repos", []) or []

    scratch = Path(tempfile.mkdtemp(prefix="codd-zoo."))
    analyzed = 0
    try:
        for entry in repos:
            root = _resolve(entry, online, scratch, result)
            if root is None:
                continue
            _analyze(root, entry.get("name", str(root)), result)
            analyzed += 1
    finally:
        # Guarded cleanup: only ever remove our own temp dir.
        if scratch.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()):
            shutil.rmtree(scratch, ignore_errors=True)

    result.summary = (
        f"analyzed {analyzed} repo(s), {len(result.skipped)} skipped, "
        f"{len(result.findings)} finding(s)"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="D11 alien-repo zoo (deterministic, no LLM)")
    parser.add_argument("--online", action="store_true", help="also clone url: repos (depth 1)")
    args = parser.parse_args()
    result = run(online=args.online)
    result.print_report()
    return 1 if result.findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
