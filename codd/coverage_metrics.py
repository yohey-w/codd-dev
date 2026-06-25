"""Coverage metrics for the CoDD merge gate."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any
import warnings

from codd.dag import result_status as _result_status
from codd.path_safety import resolve_project_path
from codd.screen_flow_validator import EdgeCoverageResult


@dataclass(frozen=True)
class CoverageResult:
    """Single coverage metric result."""

    metric: str
    total: int
    covered: int
    uncovered: int
    pct: float
    threshold: float
    passed: bool
    details: list[str] = field(default_factory=list)


@dataclass
class CoverageReport:
    """Aggregated coverage gate report."""

    results: list[CoverageResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(result.passed for result in self.results)

    def add(self, result: CoverageResult) -> None:
        self.results.append(result)


def compute_e2e_coverage(project_root: Path | str, threshold: float = 100.0) -> CoverageResult:
    """Measure generated E2E test coverage for scenarios.md."""

    project_root = Path(project_root)
    scenarios_path = project_root / "docs" / "e2e" / "scenarios.md"
    tests_dir = project_root / "docs" / "e2e" / "tests"

    scenarios = _load_e2e_scenarios(scenarios_path, project_root)
    expected_test_stems = _expected_e2e_test_stems([scenario.name for scenario in scenarios])
    actual_test_stems = _actual_e2e_test_stems(tests_dir, project_root)
    covered_stems = [stem for stem in expected_test_stems if stem in actual_test_stems]
    missing_stems = [stem for stem in expected_test_stems if stem not in actual_test_stems]

    total = len(expected_test_stems)
    covered = len(covered_stems)
    uncovered = len(missing_stems)
    pct = _coverage_pct(covered, total)
    details = [
        f"scenarios: {total}",
        f"test files: {len(actual_test_stems)}",
    ]
    if missing_stems:
        details.append("missing tests: " + ", ".join(missing_stems[:5]))

    return CoverageResult(
        metric="e2e_coverage",
        total=total,
        covered=covered,
        uncovered=uncovered,
        pct=pct,
        threshold=threshold,
        passed=pct >= threshold,
        details=details,
    )


def compute_design_token_coverage(project_root: Path | str, threshold: float = 0.0) -> CoverageResult:
    """Measure design-token compliance through validator violations."""

    try:
        from codd.validator import validate_design_tokens

        violations = validate_design_tokens(project_root)
    except Exception as exc:  # pragma: no cover - defensive gate behavior
        return _exception_result("design_token_coverage", threshold, exc)

    violation_count = len(violations)
    pct = 100.0 if violation_count == 0 else 0.0
    details = [f"violations: {violation_count}"]
    details.extend(_format_design_token_violation(violation) for violation in violations[:5])

    return CoverageResult(
        metric="design_token_coverage",
        total=1,
        covered=1 if violation_count == 0 else 0,
        uncovered=violation_count,
        pct=pct,
        threshold=threshold,
        passed=pct >= threshold,
        details=details,
    )


def compute_lexicon_compliance(project_root: Path | str, threshold: float = 100.0) -> CoverageResult:
    """Measure project lexicon compliance through validator violations."""

    try:
        from codd.validator import validate_with_lexicon

        violations = validate_with_lexicon(project_root)
    except Exception as exc:  # pragma: no cover - defensive gate behavior
        return _exception_result("lexicon_compliance", threshold, exc)

    violation_count = len(violations)
    pct = 100.0 if violation_count == 0 else 0.0
    details = [f"violations: {violation_count}"]
    details.extend(_format_lexicon_violation(violation) for violation in violations[:5])

    return CoverageResult(
        metric="lexicon_compliance",
        total=1,
        covered=1 if violation_count == 0 else 0,
        uncovered=violation_count,
        pct=pct,
        threshold=threshold,
        passed=pct >= threshold,
        details=details,
    )


def compute_screen_flow_coverage(
    project_root: Path | str,
    config: dict[str, Any],
    threshold: float = 100.0,
) -> CoverageResult:
    """Measure screen-flow route drift as a coverage gate metric."""

    try:
        from codd.cli import CoddCLIError
        from codd.screen_flow_validator import validate_screen_flow

        drifts = validate_screen_flow(Path(project_root), config)
    except CoddCLIError as exc:
        return CoverageResult(
            metric="screen_flow_coverage",
            total=1,
            covered=0,
            uncovered=1,
            pct=0.0,
            threshold=threshold,
            passed=False,
            details=[f"error: {exc}"],
        )

    drift_count = len(drifts)
    pct = 100.0 if drift_count == 0 else max(0.0, 100.0 - drift_count * 10.0)
    details = [f"drift_count: {drift_count}"]
    return CoverageResult(
        metric="screen_flow_coverage",
        total=1,
        covered=1 if drift_count == 0 else 0,
        uncovered=drift_count,
        pct=pct,
        threshold=threshold,
        passed=pct >= threshold,
        details=details,
    )


def compute_dag_completeness(
    project_root: Path | str,
    config: dict[str, Any] | None = None,
    threshold: float = 100.0,
) -> CoverageResult:
    """Measure red-severity DAG completeness checks as a coverage metric."""

    try:
        from codd.dag.runner import run_all_checks

        results = run_all_checks(Path(project_root), settings=config or {})
    except Exception as exc:  # pragma: no cover - defensive gate behavior
        return _exception_result("dag_completeness", threshold, exc)

    # A SKIP result verified nothing on purpose (no input to check). Several
    # red-severity checks (e.g. node_completeness, deployment_completeness) carry
    # the dataclass default ``severity="red"`` even when they skip, returning
    # ``status="skip", passed=True``. Counting those as covered red checks is a
    # systematic merge-gate false-green: a project where every red check skipped
    # would report ``total == covered`` = 100% PASS while verifying zero checks.
    # Exclude skips from total/covered here (severity-independent guard) and
    # surface them separately in details. This is anti-false-RED: a check that
    # actually ran and passed is not a skip, so genuinely-covered cases are
    # unaffected; only "verified nothing" runs stop inflating coverage.
    skipped_results = [result for result in results if _dag_result_skipped(result)]
    red_results = [
        result
        for result in results
        if _dag_result_severity(result) == "red" and not _dag_result_skipped(result)
    ]
    failed_red = [
        result
        for result in red_results
        if _dag_result_passed(result) is False and _dag_result_status(result) != "opt_out"
    ]
    amber_findings = [
        result
        for result in results
        if _dag_result_severity(result) == "amber"
        and _dag_result_has_findings(result)
        and not _dag_result_skipped(result)
    ]
    opt_outs = [
        result
        for result in results
        if _dag_result_status(result) == "opt_out" and not _dag_result_skipped(result)
    ]

    total = len(red_results)
    uncovered = len(failed_red)
    covered = max(0, total - uncovered)
    pct = _coverage_pct(covered, total)
    details = [
        f"checks: {len(results)}",
        f"red_failures: {uncovered}",
        f"skipped: {len(skipped_results)}",
    ]
    details.extend(_format_dag_result(result) for result in failed_red[:5])
    details.extend(f"warning: {_format_dag_result(result)}" for result in amber_findings[:5])
    details.extend(f"opt_out: {_format_dag_result(result)}" for result in opt_outs[:5])
    details.extend(f"skip: {_format_dag_result(result)}" for result in skipped_results[:5])

    return CoverageResult(
        metric="dag_completeness",
        total=total,
        covered=covered,
        uncovered=uncovered,
        pct=pct,
        threshold=threshold,
        passed=pct >= threshold,
        details=details,
    )


# Declared-contract vocabulary → the deterministic check(s) that examine each
# family. This is the *explicit obligation* surface of the DAG: keys an author
# declares (``user_journeys`` / ``resource_contracts`` / ``coverage_axes`` …)
# that a deterministic check is responsible for. It is a single declarative
# table — extend it (like ``CHECK_MODULES``) when a new contract family gains a
# check, rather than scattering check names through the metric. It is
# vocabulary-/framework-/language-free: keys are CoDD contract names, not project
# or framework tokens, so the metric stays generality-safe (no per-subject string
# matching against project content). A family is "examined" when ANY of its
# checks ran (did not skip) — by construction those checks skip iff the family is
# absent, so a *declared* family whose checks all skipped is a real
# declared-but-never-examined gap (uncovered), never a silent green.
_EXPLICIT_CONTRACT_CHECKS: dict[str, frozenset[str]] = {
    "user_journeys": frozenset({"user_journey_coherence", "resource_flow_coherence"}),
    "runtime_constraints": frozenset({"user_journey_coherence"}),
    "resource_contracts": frozenset({"resource_flow_coherence"}),
    "capability_contracts": frozenset({"resource_flow_coherence"}),
    "coverage_axes": frozenset({"environment_coverage"}),
    "negative_space": frozenset({"negative_space"}),
}


def compute_explicit_pcumr(
    project_root: Path | str,
    dag: Any | None = None,
    lexicon: Any | None = None,
    threshold: float = 0.0,
    config: dict[str, Any] | None = None,
) -> CoverageResult:
    """E-PCUMR — explicit-contract coverage rate for a real project.

    ``Explicit-contract coverage rate`` = ``|explicit coverage obligations that a
    deterministic check examined (or amber-surfaced)| / |explicit obligation
    total|``. An *explicit obligation* is a declared contract entry on a DAG node
    (``user_journeys`` / ``resource_contracts`` / ``capability_contracts`` /
    ``coverage_axes`` / ``runtime_constraints`` / ``negative_space`` — the
    ``_EXPLICIT_CONTRACT_CHECKS`` vocabulary), collected generically with
    ``collect_structured_entries`` (no framework/language token inspection).

    * ``total`` = number of declared explicit obligations across all DAG nodes.
    * ``covered`` = obligations whose family was *examined*: at least one of the
      family's deterministic checks ran (``status != skip``) — covering both a
      clean deterministic pass and an amber surface (``status="warn"`` / amber
      findings are an examined, surfaced obligation, not a hidden gap).
    * ``uncovered`` = declared obligations whose family's checks all skipped
      (declared-but-never-examined) — the real explicit-coverage gap.

    round-16 lesson: with **zero** declared obligations the metric is vacuous —
    it reports ``total=0, covered=0`` and never manufactures a ``covered>0`` 100%
    PASS over nothing. ``lexicon`` is accepted for symmetry with the other
    builders and future lexicon-declared obligations; it is not required today.

    E-PCUMR is **measure-only**: the default ``threshold`` is ``0.0`` so the
    rolled-up result always reports ``passed=True`` (like
    ``design_token_coverage``). The metric *surfaces* the explicit-coverage
    fraction; it does not gate. Gating is a separate decision — a caller that
    wants a gate passes an explicit ``threshold`` (e.g. ``100.0``). This keeps
    the addition from minting a new merge/deploy RED.
    """

    del lexicon  # reserved for future lexicon-declared obligations
    project_root = Path(project_root)

    try:
        provided_dag = dag is not None
        target_dag = dag if provided_dag else _build_project_dag(project_root, config)
        obligations = _collect_explicit_obligations(target_dag)
        # When a DAG is supplied, examine that exact DAG (do not rebuild from
        # disk, which would diverge from the obligations just counted).
        examined_checks = _examined_check_names(
            project_root, config, dag=target_dag if provided_dag else None
        )
    except Exception as exc:  # pragma: no cover - defensive gate behavior
        return _exception_result("e_pcumr", threshold, exc)

    total = len(obligations)
    covered = 0
    uncovered_keys: dict[str, int] = {}
    for key, count in obligations.items():
        responsible = _EXPLICIT_CONTRACT_CHECKS.get(key, frozenset())
        if responsible & examined_checks:
            covered += count
        else:
            uncovered_keys[key] = uncovered_keys.get(key, 0) + count

    uncovered = total - covered
    pct = _coverage_pct(covered, total)
    details = [
        f"explicit_obligations: {total}",
        f"covered: {covered}",
        f"contract_keys: {', '.join(sorted(obligations)) or '(none)'}",
    ]
    if uncovered_keys:
        details.append(
            "uncovered (declared but never examined): "
            + ", ".join(f"{key}×{count}" for key, count in sorted(uncovered_keys.items()))
        )

    # Vacuous when nothing is declared: pct from _coverage_pct is 100.0 for
    # total==0, but covered stays 0 so it is never a covered>0 false green.
    return CoverageResult(
        metric="e_pcumr",
        total=total,
        covered=covered,
        uncovered=uncovered,
        pct=pct,
        threshold=threshold,
        passed=pct >= threshold,
        details=details,
    )


def compute_pcumr(
    gold_positive: list[Any],
    detected: list[Any],
    threshold: float = 0.0,
) -> CoverageResult:
    """PCUMR — Positive-Coverage Under-detection Miss Rate for a corpus.

    Pure function: ``|G_pos - D_pos| / |G_pos|`` — the fraction of gold-expected
    positive coverage gaps that the detector did NOT find. ``gold_positive`` is
    supplied by the caller (the Phase C corpus); this function does not load any
    corpus itself.

    Matching key = ``kind`` + ``dimension`` + ``canonical_subject`` (subject
    canonicalized by trim + casefold so cosmetic case/whitespace differences do
    not count as a miss). The result is reported in the shared ``CoverageResult``
    shape so the merge gate can roll it up uniformly:

    * ``total`` = ``|G_pos|`` (gold positive count).
    * ``covered`` = matched gold positives (``|G_pos ∩ D_pos|``).
    * ``uncovered`` = missed gold positives (``|G_pos - D_pos|``).
    * ``pct`` = miss rate ``uncovered/total * 100`` — a *miss* rate, so a full
      match is ``0.0`` and ``passed`` means miss rate ``<= threshold`` (default
      ``0.0``, i.e. zero misses).

    Empty gold is vacuous: ``total=0, uncovered=0`` and a ``0.0`` miss rate, never
    a fabricated signal.
    """

    gold_keys = [_pcumr_match_key(item) for item in gold_positive]
    detected_keys = {_pcumr_match_key(item) for item in detected}

    total = len(gold_keys)
    matched = sum(1 for key in gold_keys if key in detected_keys)
    missed = total - matched
    miss_rate = (missed / total * 100.0) if total else 0.0
    missed_examples = [
        "/".join(str(part) for part in key)
        for key in gold_keys
        if key not in detected_keys
    ]

    details = [
        f"gold_positive: {total}",
        f"matched: {matched}",
        f"missed: {missed}",
    ]
    if missed_examples:
        details.append("missed: " + ", ".join(missed_examples[:5]))

    return CoverageResult(
        metric="pcumr",
        total=total,
        covered=matched,
        uncovered=missed,
        pct=miss_rate,
        threshold=threshold,
        passed=miss_rate <= threshold,
        details=details,
    )


def _build_project_dag(project_root: Path, config: dict[str, Any] | None) -> Any:
    from codd.dag.builder import build_dag, load_dag_settings

    settings = load_dag_settings(project_root, config)
    return build_dag(project_root, settings)


def _collect_explicit_obligations(dag: Any) -> dict[str, int]:
    """Count declared explicit obligations per contract key across DAG nodes.

    Uses the shared ``collect_structured_entries`` so a contract authored under
    the canonical ``frontmatter.codd`` position is counted wherever it lives. It
    never inspects entry contents, so it stays language-/framework-free.
    """
    from codd.dag.metadata_access import collect_structured_entries

    counts: dict[str, int] = {}
    nodes = getattr(dag, "nodes", {}) or {}
    for node in nodes.values():
        attributes = getattr(node, "attributes", None)
        if not attributes:
            continue
        for key in _EXPLICIT_CONTRACT_CHECKS:
            entries = collect_structured_entries(attributes, key)
            if entries:
                counts[key] = counts.get(key, 0) + len(entries)
    return counts


def _examined_check_names(
    project_root: Path,
    config: dict[str, Any] | None,
    dag: Any | None = None,
) -> set[str]:
    """Names of deterministic checks that examined input (did not skip).

    A check that skipped verified nothing (its contract family was absent), so it
    does not credit coverage. A non-skip result — clean pass, red failure, or
    amber surface — means the family was examined and is therefore credited. When
    ``dag`` is provided the checks run against that exact DAG (``run_checks``);
    otherwise the project DAG is built from disk (``run_all_checks``).
    """
    if dag is not None:
        from codd.dag.runner import run_checks

        results = run_checks(dag, Path(project_root), settings=config or {})
    else:
        from codd.dag.runner import run_all_checks

        results = run_all_checks(Path(project_root), settings=config or {})
    examined: set[str] = set()
    for result in results:
        if _dag_result_skipped(result):
            continue
        examined.add(_dag_result_name(result))
    return examined


def _pcumr_match_key(item: Any) -> tuple[str, str, str]:
    """Canonical (kind, dimension, subject) match key for PCUMR.

    ``subject`` (also accepted as ``canonical_subject`` / ``subject_canonical``)
    is canonicalized by trim + casefold so cosmetic differences are not misses.
    """
    if isinstance(item, dict):
        kind = item.get("kind")
        dimension = item.get("dimension")
        subject = (
            item.get("canonical_subject")
            if item.get("canonical_subject") is not None
            else item.get("subject_canonical")
            if item.get("subject_canonical") is not None
            else item.get("subject")
        )
    else:
        kind = getattr(item, "kind", None)
        dimension = getattr(item, "dimension", None)
        subject = (
            getattr(item, "canonical_subject", None)
            or getattr(item, "subject_canonical", None)
            or getattr(item, "subject", None)
        )
    return (
        str(kind or "").strip().casefold(),
        str(dimension or "").strip().casefold(),
        str(subject or "").strip().casefold(),
    )


def check_edge_coverage_gate(result: EdgeCoverageResult, config: dict[str, Any] | None = None) -> bool:
    """Return True when transition edge coverage meets the configured threshold."""

    threshold = _edge_coverage_threshold(config)
    if result.unreachable_nodes:
        warnings.warn(
            "Screen-flow nodes not covered by any edge: "
            f"{result.unreachable_nodes}. Run 'codd extract --layer routes-edges' "
            "to generate docs/extracted/screen-transitions.yaml.",
            UserWarning,
            stacklevel=2,
        )
    if result.orphan_nodes:
        warnings.warn(
            f"Orphan nodes (no outbound edges): {result.orphan_nodes}.",
            UserWarning,
            stacklevel=2,
        )
    if result.dead_end_nodes:
        warnings.warn(
            f"Dead-end nodes (no inbound edges): {result.dead_end_nodes}.",
            UserWarning,
            stacklevel=2,
        )
    return result.coverage_ratio >= threshold


def run_coverage(
    project_root: Path | str,
    e2e_threshold: float = 100.0,
    design_token_threshold: float = 0.0,
    lexicon_threshold: float = 100.0,
    screen_flow_threshold: float = 100.0,
    config: dict[str, Any] | None = None,
) -> CoverageReport:
    """Run all coverage metrics and return an aggregated report."""

    project_root = Path(project_root)
    if config is None:
        config = _load_optional_project_config(project_root)

    report = CoverageReport()
    report.add(compute_e2e_coverage(project_root, threshold=e2e_threshold))
    report.add(compute_design_token_coverage(project_root, threshold=design_token_threshold))
    report.add(compute_lexicon_compliance(project_root, threshold=lexicon_threshold))
    report.add(compute_screen_flow_coverage(project_root, config, threshold=screen_flow_threshold))
    report.add(compute_dag_completeness(project_root, config=config))
    # E-PCUMR (explicit-contract coverage) is the sixth metric. PCUMR needs a
    # corpus gold set (Phase C) so it stays a pure function callers invoke
    # directly; it is intentionally not auto-wired here.
    report.add(compute_explicit_pcumr(project_root, config=config))
    return report


def _load_optional_project_config(project_root: Path) -> dict[str, Any]:
    try:
        from codd.config import load_project_config

        return load_project_config(project_root)
    except (FileNotFoundError, ValueError):
        return {}


def _edge_coverage_threshold(config: dict[str, Any] | None = None) -> float:
    if not config:
        return 0.5
    screen_flow_config = config.get("screen_flow", {})
    if not isinstance(screen_flow_config, dict):
        return 0.5
    return float(screen_flow_config.get("edge_coverage_threshold", 0.5))


def _load_e2e_scenarios(scenarios_path: Path, project_root: Path) -> list[Any]:
    # Re-confine the scenarios path through the shared jail before reading it.
    # The path is hardcoded in-root (docs/e2e/scenarios.md), but it may itself be
    # an in-root symlink whose target escapes the tree; resolving + confining
    # rejects that per-file symlink escape so an off-root markdown file can never
    # be consumed as the scenario source (a path-escape false-green).
    confined = resolve_project_path(project_root, scenarios_path)
    if confined is None or not confined.exists():
        return []

    from codd.e2e_generator import load_scenarios_from_markdown

    return load_scenarios_from_markdown(confined).scenarios


def _expected_e2e_test_stems(scenario_names: list[str]) -> list[str]:
    used_names: dict[str, int] = {}
    stems: list[str] = []
    for name in scenario_names:
        base = _scenario_to_test_stem(name)
        count = used_names.get(base, 0) + 1
        used_names[base] = count
        stems.append(base if count == 1 else f"{base}_{count}")
    return stems


def _scenario_to_test_stem(name: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", name, flags=re.UNICODE).strip().lower()
    slug = re.sub(r"[-\s]+", "_", slug).strip("_")
    return f"test_{slug or 'scenario'}"


def _actual_e2e_test_stems(tests_dir: Path, project_root: Path) -> set[str]:
    if not tests_dir.exists():
        return set()

    stems: set[str] = set()
    for test_path in [
        *tests_dir.glob("*.spec.ts"),
        *tests_dir.glob("*.e2e.ts"),
        *tests_dir.glob("*.cy.ts"),
    ]:
        # Re-confine each glob match through the shared jail. A symlink inside
        # docs/e2e/tests/ that points at an off-root file (e.g.
        # /tmp/outside.spec.ts) would otherwise be counted as a covering e2e
        # test — crediting coverage from a file outside the project tree
        # (a path-escape false-green). Escaping matches resolve to None and are
        # dropped, so they are never counted toward covered stems.
        if resolve_project_path(project_root, test_path) is None:
            continue
        name = test_path.name
        if name.endswith(".spec.ts"):
            stems.add(name.removesuffix(".spec.ts"))
        elif name.endswith(".e2e.ts"):
            stems.add(name.removesuffix(".e2e.ts"))
        elif name.endswith(".cy.ts"):
            stems.add(name.removesuffix(".cy.ts"))
    return stems


def _coverage_pct(covered: int, total: int) -> float:
    if total == 0:
        return 100.0
    return covered / total * 100.0


def _format_design_token_violation(violation: Any) -> str:
    file = getattr(violation, "file", "?")
    line = getattr(violation, "line", "?")
    pattern = getattr(violation, "pattern", "?")
    suggestion = getattr(violation, "suggestion", "?")
    return f"{file}:{line} - {pattern} (suggest: {suggestion})"


def _format_lexicon_violation(violation: Any) -> str:
    if isinstance(violation, dict):
        node_id = violation.get("node_id", "?")
        message = violation.get("message", violation)
        return f"{node_id} - {message}"
    return str(violation)


def _exception_result(metric: str, threshold: float, exc: Exception) -> CoverageResult:
    return CoverageResult(
        metric=metric,
        total=1,
        covered=0,
        uncovered=1,
        pct=0.0,
        threshold=threshold,
        passed=False,
        details=[f"error: {type(exc).__name__}: {exc}"],
    )


# Status / findings predicates are the canonical, status-aware versions shared
# with codd.cli and codd.deployer so all three summaries count findings (incl.
# warn-bearing amber results) identically — see codd.dag.result_status.
_dag_result_severity = _result_status.result_severity
_dag_result_passed = _result_status.result_passed
_dag_result_status = _result_status.result_status
_dag_result_has_findings = _result_status.result_has_findings


def _dag_result_skipped(result: Any) -> bool:
    """True iff the check skipped (verified nothing on purpose).

    A skip is recognised by an explicit ``status`` of ``skip``/``skipped`` or a
    truthy ``skipped`` flag. This is severity-independent on purpose: it is the
    guard that stops a red-severity SKIP (which carries ``severity="red"`` from
    its dataclass default) from being miscounted as a covered red check in the
    merge gate.
    """
    if _result_status.result_status(result) in {"skip", "skipped"}:
        return True
    return bool(_result_status.result_value(result, "skipped"))


def _dag_result_name(result: Any) -> str:
    return str(_dag_result_value(result, "check_name") or result.__class__.__name__)


def _format_dag_result(result: Any) -> str:
    details = []
    for key in (
        "missing_impl_files",
        "orphan_edges",
        "dangling_refs",
        "violations",
        "incomplete_tasks",
        "unreachable_nodes",
        "warnings",
    ):
        value = _dag_result_value(result, key)
        if not value:
            continue
        details.append(f"{key}: {value}")
    return f"{_dag_result_name(result)} ({'; '.join(details)})" if details else _dag_result_name(result)


_dag_result_value = _result_status.result_value
