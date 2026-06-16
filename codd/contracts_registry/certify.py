"""``python -m codd.contracts_registry.certify`` — report coherence-contract coverage.

This CLI is the "visualize first, don't fail" surface of the ACG Contract
Registry (GPT §5, "最小第一歩": *pointer が無い contract は CI で uncertified と
表示する。最初から fail にせず、まず可視化する*).

Behaviour:

* loads :data:`codd.contracts_registry.REGISTRY`;
* optionally cross-checks ``dogfood/contract_matrix.yaml`` (``--matrix PATH``)
  for drift between the registry and the human-readable table;
* for ``covered`` / ``uncertified`` contracts that name regression-test ids,
  optionally checks those test *files* exist under the tests/ tree
  (``--check-fixtures``; existence-only, never runs them);
* prints a summary table (total / covered / uncertified / uncovered) plus the
  uncovered list (the proactive backlog);
* **default exit 0** even with uncovered / uncertified contracts (report-only);
* ``--strict`` exits non-zero iff ANY contract is ``uncertified``
  (covered-but-no-test-pointer). ``uncovered`` cells are KNOWN backlog and do
  NOT trip ``--strict``.

It deliberately does NOT execute any test or any CoDD gate.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Sequence

from codd.contracts_registry.registry import (
    REGISTRY,
    Contract,
    contracts_by_status,
    coverage_summary,
)

# Exit codes (kept small + documented; default path is always 0).
EXIT_OK = 0
EXIT_STRICT_UNCERTIFIED = 2
EXIT_MATRIX_DRIFT = 3


def _repo_root() -> Path:
    """The codd-dev (worktree) root — two levels up from this file.

    ``codd/contracts/certify.py`` -> repo root. Used to resolve the default
    matrix path and the tests/ tree for fixture existence checks.
    """
    return Path(__file__).resolve().parents[2]


# A certification-fixture string may be a precise ``path::test_name`` id or a
# free-text pointer like ``"tests/foo.py (several cases)"``. We extract any
# ``tests/...py`` token to do an existence check; free-text without a path is
# treated as a pointer we cannot file-check (reported, never failed).
_TEST_PATH_RE = re.compile(r"(tests/[\w./-]+?\.py|tests/[\w./-]+?\.ts)")


def _fixture_test_files(fixture: str) -> list[str]:
    """Extract test file path(s) embedded in a certification-fixture string."""
    return _TEST_PATH_RE.findall(fixture)


def _check_fixture_files(
    contract: Contract, root: Path
) -> tuple[list[str], list[str]]:
    """Return (existing, missing) test files referenced by a contract.

    Existence-only; never imports or runs anything.
    """
    existing: list[str] = []
    missing: list[str] = []
    seen: set[str] = set()
    for fixture in contract.certification_fixtures:
        for rel in _fixture_test_files(fixture):
            if rel in seen:
                continue
            seen.add(rel)
            if (root / rel).is_file():
                existing.append(rel)
            else:
                missing.append(rel)
    return existing, missing


def _load_matrix_ids(matrix_path: Path) -> set[str]:
    """Load contract ids from the human-readable matrix YAML (for drift check).

    The matrix groups contracts under ``families: {<family>: [ {id: ...}, ...]}``.
    Returns the set of all contract ids found.
    """
    import yaml  # local import: only needed when --matrix is given

    data = yaml.safe_load(matrix_path.read_text(encoding="utf-8")) or {}
    ids: set[str] = set()
    families = data.get("families", {})
    if isinstance(families, dict):
        for entries in families.values():
            for entry in entries or ():
                if isinstance(entry, dict) and "id" in entry:
                    ids.add(str(entry["id"]))
    return ids


def _render_summary(
    *,
    check_fixtures: bool,
    root: Path,
    matrix_drift: tuple[set[str], set[str]] | None,
) -> tuple[str, bool]:
    """Build the report text. Returns (text, has_missing_fixture_files)."""
    grouped = contracts_by_status()
    summary = coverage_summary()
    lines: list[str] = []
    lines.append("ACG Contract Registry — coverage certification")
    lines.append("=" * 60)
    lines.append(
        f"total={summary['total']}  "
        f"covered={summary['covered']}  "
        f"uncertified={summary['uncertified']}  "
        f"uncovered={summary['uncovered']}"
    )
    lines.append("")

    has_missing_files = False

    # covered + uncertified: list with authority + (optional) fixture-file check.
    for status in ("covered", "uncertified"):
        items = grouped[status]
        if not items:
            continue
        lines.append(f"[{status}]  ({len(items)})")
        for c in sorted(items, key=lambda x: x.id):
            line = f"  {c.id}  <- {c.authority}"
            if check_fixtures:
                existing, missing = _check_fixture_files(c, root)
                if missing:
                    has_missing_files = True
                    line += f"   [MISSING test file(s): {', '.join(missing)}]"
                elif existing:
                    line += f"   [{len(existing)} test file(s) present]"
            lines.append(line)
        lines.append("")

    # uncovered: the proactive backlog (predicted_issue + proposed_gate).
    uncovered = grouped["uncovered"]
    if uncovered:
        lines.append(f"[uncovered — proactive backlog]  ({len(uncovered)})")
        for c in sorted(uncovered, key=lambda x: x.id):
            lines.append(f"  {c.id}")
            lines.append(f"      issue: {c.predicted_issue}")
            lines.append(f"      gate : {c.proposed_gate}")
        lines.append("")

    if matrix_drift is not None:
        only_registry, only_matrix = matrix_drift
        if not only_registry and not only_matrix:
            lines.append("matrix: in sync with registry (no drift).")
        else:
            lines.append("matrix: DRIFT detected")
            if only_registry:
                lines.append(
                    "  in registry but NOT in matrix: "
                    + ", ".join(sorted(only_registry))
                )
            if only_matrix:
                lines.append(
                    "  in matrix but NOT in registry: "
                    + ", ".join(sorted(only_matrix))
                )
        lines.append("")

    return "\n".join(lines), has_missing_files


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m codd.contracts_registry.certify",
        description=(
            "Report ACG coherence-contract coverage from the registry "
            "(visualize-first; report-only by default)."
        ),
    )
    parser.add_argument(
        "--matrix",
        metavar="PATH",
        default=None,
        help=(
            "cross-check this contract_matrix.yaml against the registry and "
            "report id drift (default: do not cross-check)."
        ),
    )
    parser.add_argument(
        "--check-fixtures",
        action="store_true",
        help=(
            "existence-check the regression-test FILES named by covered/"
            "uncertified contracts (never runs them)."
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "exit non-zero iff any contract is 'uncertified' (covered but no "
            "test pointer). 'uncovered' backlog cells do NOT trip strict mode."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = _repo_root()

    matrix_drift: tuple[set[str], set[str]] | None = None
    if args.matrix:
        matrix_path = Path(args.matrix)
        if not matrix_path.is_absolute():
            matrix_path = (Path.cwd() / matrix_path).resolve()
        if not matrix_path.is_file():
            print(f"error: matrix file not found: {matrix_path}", file=sys.stderr)
            return EXIT_MATRIX_DRIFT
        matrix_ids = _load_matrix_ids(matrix_path)
        registry_ids = {c.id for c in REGISTRY}
        matrix_drift = (registry_ids - matrix_ids, matrix_ids - registry_ids)

    text, _has_missing_files = _render_summary(
        check_fixtures=args.check_fixtures,
        root=root,
        matrix_drift=matrix_drift,
    )
    print(text)

    summary = coverage_summary()

    # Default: report-only, exit 0 (visualize first, don't fail).
    exit_code = EXIT_OK

    # --strict: fail ONLY on uncertified (covered-but-no-pointer). Never on
    # uncovered (known backlog).
    if args.strict and summary["uncertified"] > 0:
        print(
            f"strict: {summary['uncertified']} uncertified contract(s) "
            "(covered but no regression-test pointer) — failing.",
            file=sys.stderr,
        )
        exit_code = EXIT_STRICT_UNCERTIFIED

    # Matrix drift is a hard error regardless of --strict: the committed matrix
    # MUST mirror the registry (the no-drift invariant the meta-test pins).
    if matrix_drift is not None and (matrix_drift[0] or matrix_drift[1]):
        print(
            "error: contract_matrix.yaml has drifted from the registry "
            "(regenerate it).",
            file=sys.stderr,
        )
        exit_code = EXIT_MATRIX_DRIFT if exit_code == EXIT_OK else exit_code

    return exit_code


if __name__ == "__main__":  # pragma: no cover - thin shim
    raise SystemExit(main())
