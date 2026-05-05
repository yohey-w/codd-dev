"""Coverage metrics for the CoDD merge gate."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any
import warnings

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

    scenarios = _load_e2e_scenarios(scenarios_path)
    expected_test_stems = _expected_e2e_test_stems([scenario.name for scenario in scenarios])
    actual_test_stems = _actual_e2e_test_stems(tests_dir)
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

    red_results = [result for result in results if _dag_result_severity(result) == "red"]
    failed_red = [result for result in red_results if _dag_result_passed(result) is False]
    amber_findings = [
        result
        for result in results
        if _dag_result_severity(result) == "amber" and _dag_result_has_findings(result)
    ]

    total = len(red_results)
    uncovered = len(failed_red)
    covered = max(0, total - uncovered)
    pct = _coverage_pct(covered, total)
    details = [f"checks: {len(results)}", f"red_failures: {uncovered}"]
    details.extend(_format_dag_result(result) for result in failed_red[:5])
    details.extend(f"warning: {_format_dag_result(result)}" for result in amber_findings[:5])

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


def _load_e2e_scenarios(scenarios_path: Path) -> list[Any]:
    if not scenarios_path.exists():
        return []

    from codd.e2e_generator import load_scenarios_from_markdown

    return load_scenarios_from_markdown(scenarios_path).scenarios


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


def _actual_e2e_test_stems(tests_dir: Path) -> set[str]:
    if not tests_dir.exists():
        return set()

    stems: set[str] = set()
    for test_path in [*tests_dir.glob("*.spec.ts"), *tests_dir.glob("*.cy.ts")]:
        name = test_path.name
        if name.endswith(".spec.ts"):
            stems.add(name.removesuffix(".spec.ts"))
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


def _dag_result_severity(result: Any) -> str:
    return str(_dag_result_value(result, "severity") or "red")


def _dag_result_passed(result: Any) -> bool:
    return _dag_result_value(result, "passed") is not False


def _dag_result_name(result: Any) -> str:
    return str(_dag_result_value(result, "check_name") or result.__class__.__name__)


def _dag_result_has_findings(result: Any) -> bool:
    for key in (
        "violations",
        "missing_impl_files",
        "orphan_edges",
        "dangling_refs",
        "incomplete_tasks",
        "unreachable_nodes",
    ):
        if _dag_result_value(result, key):
            return True
    return False


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


def _dag_result_value(result: Any, key: str) -> Any:
    if isinstance(result, dict):
        return result.get(key)
    return getattr(result, key, None)
