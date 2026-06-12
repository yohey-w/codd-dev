#!/usr/bin/env python3
"""D14 — self-application: run CoDD's own read-only gates on codd-dev itself.

The harness eats its own cooking. This is deliberately *read-only* (M2 / the
self-hosting limit in README): it never tries to self-build. It exercises two
public, deterministic, LLM-free surfaces against the codd-dev tree:

  1. DAG checks (``codd.dag.runner.run_all_checks``) — the same engine behind
     ``codd dag verify``. A check that RAISES (crashes) is a regression =
     a finding. A check that merely returns red/amber is NOT a finding here:
     codd-dev is its own source tree, not a finished delivery, so a normal
     red verdict is the baseline. We only flag *crashes* — the harness failing
     to run is the regression we care about.
  2. Config-key validation (``codd.config_schema.validate_config_keys`` /
     ``project_config_key_warnings``) — a key that is UNKNOWN to the schema is a
     finding (the schema and the shipped config drifted apart). Advisory
     warnings about close-match typos in a real codd.yaml are reported but
     count as findings only when the key is genuinely unknown.

Exit nonzero on any finding (a crash or an unknown config key). Degrades
gracefully: a missing codd dir / unreadable config is reported, not crashed.
"""

from __future__ import annotations

from pathlib import Path

from _common import AxisResult, Finding, REPO_ROOT, ensure_repo_on_path

ensure_repo_on_path()


def run() -> AxisResult:
    result = AxisResult(axis="D14")
    root = REPO_ROOT

    # ── 1. DAG checks: a crash is the regression we hunt ─────────────────────
    checks_ran = 0
    try:
        from codd.dag.runner import run_all_checks

        try:
            results = run_all_checks(root)
            checks_ran = len(results)
            # Surface the verdict mix for visibility (NOT a finding by itself).
            passed = sum(1 for r in results if getattr(r, "passed", None) is True)
            result.stats["dag_checks_total"] = checks_ran
            result.stats["dag_checks_passed"] = passed
        except Exception as exc:  # a check CRASHED → regression
            result.findings.append(
                Finding(
                    axis="D14",
                    symptom="dag verify crashed on codd-dev",
                    detail=f"{type(exc).__name__}: {exc}",
                    subject="codd-dev",
                )
            )
    except Exception as exc:  # import failure → harness regression
        result.findings.append(
            Finding(
                axis="D14",
                symptom="could not import codd.dag.runner",
                detail=f"{type(exc).__name__}: {exc}",
                subject="codd-dev",
            )
        )

    # ── 2. Config-key validation: unknown keys = schema/config drift ─────────
    unknown_keys: list[str] = []
    try:
        from codd.config_schema import project_config_key_warnings

        warnings = project_config_key_warnings(root)
        result.stats["config_key_warnings"] = len(warnings)
        for w in warnings:
            # The validator phrases genuinely-unknown keys as
            # "unknown config key '<dotted>'". A close-match typo warning is
            # advisory only; an unknown-with-no-suggestion is the harder signal.
            if "unknown config key" in w:
                unknown_keys.append(w)
        # Any unknown key is a finding: the shipped config drifted from schema.
        for w in unknown_keys:
            result.findings.append(
                Finding(
                    axis="D14",
                    symptom="codd.yaml carries a config key the schema does not know",
                    detail=w,
                    subject="codd-dev",
                )
            )
    except Exception as exc:
        result.findings.append(
            Finding(
                axis="D14",
                symptom="config-key validation crashed",
                detail=f"{type(exc).__name__}: {exc}",
                subject="codd-dev",
            )
        )

    result.summary = (
        f"ran {checks_ran} DAG checks on codd-dev; "
        f"{len(unknown_keys)} unknown config keys"
    )
    return result


def main() -> int:
    result = run()
    result.print_report()
    return 1 if result.findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
