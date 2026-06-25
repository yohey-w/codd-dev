"""Stage-2 (Axis-P) positive-corpus loader for corpus PCUMR.

This is *test* infrastructure for the Phase C2 corpus under
``tests/fixtures/stage2_axisp_corpus/``. It does NOT modify any CoDD check, CLI,
or metric. It only:

* loads each fixture's construction-derived ``gold.yaml`` (the expected
  positive-coverage gaps), and
* builds each fixture's DAG, runs the relevant deterministic checks, and
  normalizes the findings into the canonical ``(kind, dimension, subject)`` shape
  that :func:`codd.coverage_metrics.compute_pcumr` matches on.

The corpus PCUMR is then ``compute_pcumr(gold, detected)`` over the union of all
fixtures — the fraction of construction-derived gold gaps that the current
detectors do NOT find. A fixture whose gap the detector finds contributes a
*match* (no miss); a fixture whose gap the detector cannot express contributes a
*miss*, which is exactly the detection hole PCUMR is meant to surface.

Design choices (kept deliberately narrow so this stays loader-only):

* **Construction-derived gold.** Every fixture's gold is determined by a
  structural rule stated in its ``gold.yaml`` header (e.g. "declared actors minus
  actors with a journey"); no per-item human verdict is needed. The only
  owner-seeded inputs are the fixture authoring choices themselves (which actors
  exist, which resource is required), which are fixed and minimal.
* **Normalization is a fixed dispatch table**, not a model. Each relevant
  deterministic finding ``type`` maps to a canonical ``(kind, dimension,
  subject)``. The mapping is exhaustive over the finding types the corpus
  exercises; an unrecognized finding type is ignored (it cannot fabricate a gold
  match, only — at worst — fail to credit one, which keeps the metric honest).
* **No false 100%.** Empty gold is vacuous in ``compute_pcumr`` (the round-16
  lesson); the corpus is asserted non-empty so the measured miss rate is real.
"""

from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import yaml

from codd.dag.builder import build_dag, load_dag_settings
from codd.dag.runner import run_checks


CORPUS_ROOT = Path(__file__).resolve().parent / "fixtures" / "stage2_axisp_corpus"

# The deterministic checks the corpus exercises. Kept explicit so the loader runs
# only the Axis-P-relevant families (and so an unrelated check added later does
# not silently change the corpus measurement).
_CORPUS_CHECKS: tuple[str, ...] = (
    "user_journey_coherence",
    "resource_flow_coherence",
    "negative_space",
    "environment_coverage",
)


@dataclass(frozen=True)
class CorpusGap:
    """A positive-coverage gap in the canonical PCUMR match shape."""

    kind: str
    dimension: str
    subject: str

    def as_match_item(self) -> dict[str, str]:
        # The shape compute_pcumr expects (it canonicalizes subject itself).
        return {"kind": self.kind, "dimension": self.dimension, "subject": self.subject}


def list_fixtures() -> list[Path]:
    """Return the fixture directories (those that contain a ``gold.yaml``)."""
    if not CORPUS_ROOT.is_dir():
        return []
    return sorted(
        path
        for path in CORPUS_ROOT.iterdir()
        if path.is_dir() and (path / "gold.yaml").is_file()
    )


def load_gold(fixture_dir: Path) -> list[CorpusGap]:
    """Load the construction-derived gold gaps for one fixture.

    Reads ``gold.yaml``'s ``gold:`` list. Each entry must carry ``kind`` /
    ``dimension`` / ``subject``; malformed entries raise so a broken fixture is
    never silently treated as "no gold" (which would deflate the miss rate).
    """
    data = yaml.safe_load((fixture_dir / "gold.yaml").read_text(encoding="utf-8")) or {}
    raw_gold = data.get("gold")
    if not isinstance(raw_gold, list):
        raise ValueError(f"{fixture_dir.name}/gold.yaml: 'gold' must be a list")
    gaps: list[CorpusGap] = []
    for index, entry in enumerate(raw_gold):
        if not isinstance(entry, dict):
            raise ValueError(f"{fixture_dir.name}/gold.yaml: gold[{index}] must be a mapping")
        kind = str(entry.get("kind") or "").strip()
        dimension = str(entry.get("dimension") or "").strip()
        subject = str(entry.get("subject") or "").strip()
        if not (kind and dimension and subject):
            raise ValueError(
                f"{fixture_dir.name}/gold.yaml: gold[{index}] needs kind+dimension+subject"
            )
        gaps.append(CorpusGap(kind=kind, dimension=dimension, subject=subject))
    return gaps


@contextmanager
def _isolated_copy(fixture_dir: Path) -> Iterator[Path]:
    """Yield a temp copy of the fixture so building does not dirty the source.

    ``build_dag`` writes ``.codd/dag.json`` into the project root it scans. The
    corpus fixtures are committed source, so the DAG is built against a throwaway
    copy instead — the source fixture stays artifact-free and the committed tree
    never carries a stale generated dag.json.
    """
    tmp_root = Path(tempfile.mkdtemp(prefix=f"stage2_axisp_{fixture_dir.name}_"))
    try:
        work = tmp_root / fixture_dir.name
        # Skip any pre-existing build artifact so the copy is pure source.
        shutil.copytree(fixture_dir, work, ignore=shutil.ignore_patterns(".codd"))
        yield work
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def detect_gaps(fixture_dir: Path) -> list[CorpusGap]:
    """Build the fixture DAG, run the Axis-P checks, normalize findings to gaps.

    The detected list is what ``compute_pcumr`` matches gold against. It is
    deterministic for a given fixture (the checks are rule-based and the DAG is
    built from the fixture's on-disk files). The build runs against an isolated
    copy so the committed fixture is never mutated.
    """
    raw_config = yaml.safe_load((fixture_dir / "codd.yaml").read_text(encoding="utf-8")) or {}
    with _isolated_copy(fixture_dir) as work_dir:
        # Pass the fixture's raw codd.yaml as settings so its direct keys
        # (design_doc_patterns / impl_file_patterns / test_file_patterns /
        # lexicon_file / plan_task_file) flow into the DAG settings — the corpus
        # fixtures keep config in a plain codd.yaml, not a codd/ config dir.
        dag_settings = load_dag_settings(work_dir, settings=raw_config)
        dag = build_dag(work_dir, dag_settings)
        # Merge so checks see BOTH the resolved DAG settings AND the raw blocks a
        # check reads straight from config (e.g. negative_space.forbidden_evidence).
        check_settings = {**dag_settings, **raw_config}
        results = run_checks(dag, work_dir, settings=check_settings, check_names=_CORPUS_CHECKS)

    gaps: list[CorpusGap] = []
    for result in results:
        gaps.extend(_normalize_all(_findings(result)))
    return _dedupe(gaps)


def load_corpus() -> tuple[list[CorpusGap], list[CorpusGap]]:
    """Return ``(gold, detected)`` over the whole corpus (union across fixtures)."""
    gold: list[CorpusGap] = []
    detected: list[CorpusGap] = []
    for fixture_dir in list_fixtures():
        gold.extend(load_gold(fixture_dir))
        detected.extend(detect_gaps(fixture_dir))
    return _dedupe(gold), _dedupe(detected)


# --- finding -> canonical (kind, dimension, subject) normalization ----------


def _normalize_finding(finding: dict[str, Any]) -> CorpusGap | None:
    """Map one deterministic finding to a canonical positive-coverage gap.

    Fixed dispatch over the finding ``type`` (no model, no heuristics). Returns
    ``None`` for finding types the corpus does not score, so an unrecognized
    finding can never fabricate a gold match.
    """
    finding_type = str(finding.get("type") or "").strip()

    # Acceptance-signal family (C7): a declared journey lacks a plan task or an
    # e2e verification — i.e. its acceptance signal is not covered.
    if finding_type in {"no_e2e_test_for_journey", "no_plan_task_for_journey"}:
        subject = finding.get("user_journey")
        return _gap("acceptance_signal", "journey", subject)

    # Missing-journey family (C7): actors are declared but none has a journey.
    # This finding names a LIST of actors, so it is expanded per actor in
    # _expand_multi_subject (invoked from _normalize_all), not here.
    if finding_type == "actors_without_journeys":
        return None

    # Missing-producer family (resource_flow): a required consumer with no
    # producer for its resource.
    if finding_type == "dangling_required_consumer":
        return _gap("missing_producer", "resource", finding.get("resource"))

    # Forbidden / negative-space family: a declared forbidden pattern hit.
    if finding_type == "forbidden_evidence_hit":
        return _gap("forbidden", "evidence", finding.get("declaration_id"))

    # Environment / NFR family (C9): a coverage variant with no exercising test.
    if finding_type in {"missing_test_for_variant", "journey_not_executed_under_variant"}:
        return _gap("environment", "variant", finding.get("variant_id"))

    return None


def _expand_multi_subject(finding: dict[str, Any]) -> list[CorpusGap]:
    """Findings that name several subjects in one finding -> one gap per subject.

    Only ``actors_without_journeys`` currently does this: it lists every actor
    that has no journey, which maps to one ``missing_journey`` gap per actor.
    """
    finding_type = str(finding.get("type") or "").strip()
    if finding_type != "actors_without_journeys":
        return []
    actors = finding.get("actors")
    if not isinstance(actors, (list, tuple)):
        return []
    gaps: list[CorpusGap] = []
    for actor in actors:
        gap = _gap("missing_journey", "actor", actor)
        if gap is not None:
            gaps.append(gap)
    return gaps


def _findings(result: Any) -> list[dict[str, Any]]:
    """All scorable finding dicts from a check result (violations + warnings).

    Different checks expose findings under ``violations`` (C7 / C9 /
    resource_flow) or ``warnings`` (negative_space / resource_flow advisories).
    Both are read; non-mapping items are skipped.
    """
    findings: list[dict[str, Any]] = []
    for attr in ("violations", "warnings"):
        for item in getattr(result, attr, None) or []:
            if isinstance(item, dict):
                findings.append(item)
    return findings


def _normalize_all(findings: list[dict[str, Any]]) -> list[CorpusGap]:
    gaps: list[CorpusGap] = []
    for finding in findings:
        single = _normalize_finding(finding)
        if single is not None:
            gaps.append(single)
        gaps.extend(_expand_multi_subject(finding))
    return gaps


def _gap(kind: str, dimension: str, subject: Any) -> CorpusGap | None:
    text = str(subject or "").strip()
    if not text:
        return None
    return CorpusGap(kind=kind, dimension=dimension, subject=text)


def _dedupe(gaps: list[CorpusGap]) -> list[CorpusGap]:
    """Stable de-dup by canonical (kind, dimension, casefolded subject).

    Mirrors compute_pcumr's own canonicalization so two findings that differ
    only in subject case are not double-counted in detected (and a gold list
    cannot list the same gap twice).
    """
    seen: set[tuple[str, str, str]] = set()
    result: list[CorpusGap] = []
    for gap in gaps:
        key = (gap.kind.strip().casefold(), gap.dimension.strip().casefold(), gap.subject.strip().casefold())
        if key in seen:
            continue
        seen.add(key)
        result.append(gap)
    return result
