"""R5.1 — Test traceability for codd extract.

Maps test files to the source symbols they exercise, via import analysis
of test code. Enables 'which tests to run' and 'untested symbols' queries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codd.extractor import ProjectFacts


@dataclass
class TestCoverage:
    """Per-module test coverage summary."""
    module: str
    covered_symbols: list[str] = field(default_factory=list)
    uncovered_symbols: list[str] = field(default_factory=list)
    coverage_ratio: float = 0.0
    covering_tests: list[str] = field(default_factory=list)


def build_test_traceability(facts: ProjectFacts, project_root: Path) -> None:
    """Populate ``test_coverage`` on every module in *facts*.

    Strategy: For each module's test files, scan import lines and test
    function call patterns to identify which source symbols are exercised.
    """
    for mod in facts.modules.values():
        all_symbols = {s.name for s in mod.symbols}
        if not all_symbols:
            continue

        covered: set[str] = set()
        covering_tests: list[str] = []

        for test_detail in mod.test_details:
            covering_tests.append(test_detail.file_path)
            # Read test file and scan for symbol references
            test_path = project_root / test_detail.file_path
            try:
                test_content = test_path.read_text(errors="ignore")
            except Exception:
                continue
            # Any source symbol name that appears in the test file = covered
            for sym_name in all_symbols:
                if sym_name in test_content:
                    covered.add(sym_name)

        # Deduplicate covering_tests
        covering_tests = sorted(set(covering_tests))
        covered_list = sorted(covered & all_symbols)
        uncovered_list = sorted(all_symbols - covered)
        total = len(all_symbols)
        ratio = len(covered_list) / total if total else 0.0

        mod.test_coverage = TestCoverage(
            module=mod.name,
            covered_symbols=covered_list,
            uncovered_symbols=uncovered_list,
            coverage_ratio=round(ratio, 2),
            covering_tests=covering_tests,
        )
