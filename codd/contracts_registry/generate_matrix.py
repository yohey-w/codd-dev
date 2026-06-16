"""Generate ``dogfood/contract_matrix.yaml`` from the registry.

The committed matrix is the human-readable "ledger -> 契約カバレッジ表" artifact
(GPT §5). It is GENERATED from :data:`codd.contracts_registry.REGISTRY` so it can never
silently drift; ``tests/test_contracts_registry.py`` has a meta-test asserting
the committed file matches what this generator would produce.

Run:  ``python -m codd.contracts_registry.generate_matrix``  (writes the file)
      ``python -m codd.contracts_registry.generate_matrix --check``  (diff only, exit!=0
      on drift; used in tests/CI).
"""

from __future__ import annotations

import argparse
import sys
from collections import OrderedDict
from pathlib import Path

from codd.contracts_registry.registry import REGISTRY, Contract, coverage_summary


def _contract_to_entry(c: Contract) -> "OrderedDict[str, object]":
    """Render one contract as an ordered mapping (covered vs uncovered shape)."""
    entry: "OrderedDict[str, object]" = OrderedDict()
    entry["id"] = c.id
    entry["source_node"] = c.source_node
    entry["target_node"] = c.target_node
    entry["edge_type"] = c.edge_type
    entry["dimensions"] = list(c.dimensions)
    entry["status"] = c.status
    entry["fail_mode"] = c.fail_mode
    if c.status == "uncovered":
        entry["authority"] = None
        entry["predicted_issue"] = c.predicted_issue
        entry["proposed_gate"] = c.proposed_gate
    else:
        entry["authority"] = c.authority
        entry["certification_fixtures"] = list(c.certification_fixtures)
    if c.finding_ids:
        entry["finding_ids"] = list(c.finding_ids)
    return entry


def build_document() -> "OrderedDict[str, object]":
    """Assemble the full matrix document (grouped by node family)."""
    families: "OrderedDict[str, list]" = OrderedDict()
    # Stable family order = first-seen order in the registry (covered first,
    # then outer, then uncovered — matches registry assembly), which keeps the
    # file readable and the diff stable.
    for c in REGISTRY:
        families.setdefault(c.node_family, []).append(_contract_to_entry(c))

    doc: "OrderedDict[str, object]" = OrderedDict()
    doc["generated_from"] = "codd/contracts/registry.py"
    doc["coverage_summary"] = dict(coverage_summary())
    doc["families"] = families
    return doc


def render_yaml() -> str:
    """Deterministic YAML text for the matrix (stable key order, block style)."""
    import yaml

    # Preserve insertion order (OrderedDict) in the dump.
    class _OrderedDumper(yaml.SafeDumper):
        pass

    def _ordered_representer(dumper, data):
        return dumper.represent_mapping(
            "tag:yaml.org,2002:map", data.items()
        )

    _OrderedDumper.add_representer(OrderedDict, _ordered_representer)

    body = yaml.dump(
        build_document(),
        Dumper=_OrderedDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=100,
    )
    header = (
        "# ═══════════════════════════════════════════════════════════════════\n"
        "# ACG Contract Coverage Matrix  (ledger -> 契約カバレッジ表)\n"
        "# ═══════════════════════════════════════════════════════════════════\n"
        "# AUTO-GENERATED from codd/contracts/registry.py.\n"
        "# Regenerate with: python -m codd.contracts_registry.generate_matrix\n"
        "# A meta-test (tests/test_contracts_registry.py) pins this file to the\n"
        "# registry so they can never drift. Do NOT hand-edit.\n"
        "#\n"
        "# status: covered      = enforced by `authority` AND pinned by a test\n"
        "#         uncertified  = enforced by `authority` but no test pointer\n"
        "#         uncovered    = predicted-but-unenforced (GPT §2 空セル backlog)\n"
        "# ═══════════════════════════════════════════════════════════════════\n"
    )
    return header + body


def matrix_path() -> Path:
    """``dogfood/contract_matrix.yaml`` at the repo root."""
    return Path(__file__).resolve().parents[2] / "dogfood" / "contract_matrix.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m codd.contracts_registry.generate_matrix",
        description="Generate dogfood/contract_matrix.yaml from the registry.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit non-zero if the committed file is stale.",
    )
    args = parser.parse_args(argv)

    path = matrix_path()
    expected = render_yaml()

    if args.check:
        if not path.is_file():
            print(f"missing: {path}", file=sys.stderr)
            return 1
        actual = path.read_text(encoding="utf-8")
        if actual != expected:
            print(
                "contract_matrix.yaml is STALE — run "
                "`python -m codd.contracts_registry.generate_matrix`.",
                file=sys.stderr,
            )
            return 1
        print("contract_matrix.yaml is up to date.")
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(expected, encoding="utf-8")
    print(f"wrote {path} ({len(REGISTRY)} contracts).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
