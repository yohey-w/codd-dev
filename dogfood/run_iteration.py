#!/usr/bin/env python3
"""The dogfood loop tick — run every automatable axis, update the ledger.

One invocation = one loop iteration over the SCRIPTED axes (D7, D8, D10, D11,
D14): it runs each runner, prints its report, records the run in
``dogfood/ledger.yaml`` (a new finding resets that axis's saturation counter; a
dry run advances it toward K), and prints the convergence report. The LLM axes
(D1–D6, D9) and the human/external axes (D12, D13) are run by hand and recorded
into the same ledger — this tick only automates the free, deterministic ones.

Idempotent per day: re-running on the same date does not double-advance a dry
axis (a finding always resets, though). Use ``--preview`` to run the axes and
see the would-be ledger state WITHOUT writing. Use ``--online`` to let the zoo
(D11) clone ``url:`` repos.

Exit nonzero iff a NEW finding appeared this tick.
"""
from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from _common import ensure_repo_on_path, load_ledger, save_ledger  # noqa: E402

ensure_repo_on_path()

import run_d7_chaos  # noqa: E402
import run_d8_adversarial  # noqa: E402
import run_d10_roundtrip  # noqa: E402
import run_d11_zoo  # noqa: E402
import run_d14_self  # noqa: E402

# Reporting order (cheap → cheap; D7 is the slowest, last).
SCRIPTED_ORDER = ["D14", "D8", "D10", "D11", "D7"]


def run_scripted_axes(online: bool) -> dict:
    return {
        "D14": run_d14_self.run(),
        "D8": run_d8_adversarial.run(),
        "D10": run_d10_roundtrip.run(),
        "D11": run_d11_zoo.run(online=online),
        "D7": run_d7_chaos.run(),
    }


def apply_results(ledger: dict, results: dict, today: str) -> int:
    """Fold this tick's results into the ledger. Returns the new-finding count."""
    k = int(ledger.get("saturation_k", 2))
    axes = ledger.setdefault("axes", {})
    findings_log = ledger.setdefault("findings", [])
    new_findings = 0

    for axis_id, res in results.items():
        ax = axes.setdefault(axis_id, {})
        if res.findings:
            new_findings += len(res.findings)
            for i, f in enumerate(res.findings, start=1):
                findings_log.append({
                    "id": f"F-{axis_id.lower()}-{today}-{i}",
                    "axis": axis_id,
                    "date": today,
                    "model_used": "deterministic (no LLM)",
                    "symptom": f.symptom,
                    "root_cause": "under-investigation",
                    "fix_commit": None,
                    "regression_test": "PENDING — add a regression before closing.",
                    "status": "open",
                    "derived_case_ids": [],
                    "subject": f.subject,
                    "detail": f.detail,
                })
            ax["saturation_counter"] = 0
            ax["status"] = "active"
        else:
            # Dry run: advance the counter, but only once per day (idempotent).
            if ax.get("last_run") != today:
                ax["saturation_counter"] = int(ax.get("saturation_counter", 0)) + 1
            counter = int(ax.get("saturation_counter", 0))
            ax["status"] = "saturated" if counter >= k else "active"
        ax["last_run"] = today

    return new_findings


def _is_owner_only(entry: dict) -> bool:
    """An axis/case is owner-only (not autonomously runnable) when it is flagged
    so on either the automation channel or the status channel. Such axes/cases are
    tracked but EXCLUDED from the autonomous convergence requirement: an autonomous
    agent cannot run them without fabricating the owner-supplied input (author ==
    solver), which collapses the gap they test. See dogfood/README.md."""
    return entry.get("automation") == "owner-only" or entry.get("status") == "owner-only"


def convergence(ledger: dict) -> tuple[bool, list[str], list[dict], list[str], list[dict]]:
    """Autonomous convergence: every AUTONOMOUS axis saturated AND no AUTONOMOUS
    pending case open. owner-only axes/cases are tracked separately and never gate."""
    axes = ledger.get("axes", {})
    owner_only_axes = [aid for aid, a in axes.items() if _is_owner_only(a)]
    unsaturated = [
        aid for aid, a in axes.items()
        if not _is_owner_only(a) and a.get("status") != "saturated"
    ]
    cases = ledger.get("pending_cases", [])
    owner_only_cases = [c for c in cases if _is_owner_only(c)]
    open_cases = [
        c for c in cases
        if not _is_owner_only(c) and c.get("status") in ("pending", "running")
    ]
    converged = not unsaturated and not open_cases
    return converged, unsaturated, open_cases, owner_only_axes, owner_only_cases


def main() -> int:
    parser = argparse.ArgumentParser(description="Dogfood loop tick (scripted axes).")
    parser.add_argument("--online", action="store_true", help="let D11 clone url: repos")
    parser.add_argument("--preview", action="store_true", help="run axes but do NOT write the ledger")
    args = parser.parse_args()

    today = datetime.date.today().isoformat()

    print("═══ dogfood loop tick ═══")
    results = run_scripted_axes(args.online)
    for axis_id in SCRIPTED_ORDER:
        results[axis_id].print_report()

    ledger = load_ledger()
    new_findings = apply_results(ledger, results, today)
    converged, unsaturated, open_cases, owner_only_axes, owner_only_cases = convergence(ledger)

    if not args.preview:
        save_ledger(ledger)

    print("\n═══ convergence report ═══")
    for axis_id in SCRIPTED_ORDER:
        a = ledger["axes"].get(axis_id, {})
        print(f"  {axis_id}: counter={a.get('saturation_counter')} "
              f"status={a.get('status')} last_run={a.get('last_run')}")
    print(f"  new findings this tick : {new_findings}")
    print(f"  unsaturated axes ({len(unsaturated)}): {', '.join(unsaturated) if unsaturated else '—'}")
    print(f"  open pending_cases     : {len(open_cases)}")
    print(f"  owner-only axes ({len(owner_only_axes)}, excluded from convergence): "
          f"{', '.join(owner_only_axes) if owner_only_axes else '—'}")
    print(f"  owner-only pending_cases ({len(owner_only_cases)}, excluded): "
          f"{len(owner_only_cases)}")
    print(f"  converged: {str(converged).lower()}")
    if args.preview:
        print("  (preview — ledger NOT written)")

    return 1 if new_findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
