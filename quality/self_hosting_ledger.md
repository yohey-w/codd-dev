# Self-hosted Coherence Gate — ledger

Milestone (`/goal`, 2026-06-24): run codd-dev's own changes through CoDD's
design → test → implement → verify loop. CoDD autonomously enforces every
red/amber derivable from existing Contract Kernel semantics; any new meaning /
severity / schema / absence decision is recorded as owner+GPT-gated. Each entry
keeps red-before-green evidence and the autonomy classification.

**Saturation:** ≥5 real codd-dev changes of differing natures through the loop
(each with red-before-green evidence) + the false-green corpus (4 shipped
vectors) passing as a regression gate + control/legacy false-red 0 + Contract
Kernel generality preserved + 2 consecutive clean discovery rounds + all 9
owner-gated vectors resolved / fixture-locked / explicitly deferred.

---

## Baseline — 2026-06-24 (CoDD verifies CoDD)

`python3 -m codd.cli dag verify` on codd-dev → exit 0 (amber advisories, deploy
allowed). Snapshot:

- **PASS**: node_completeness, edge_validity, dependency_freshness*, task_completion,
  transitive_closure*, ui_coherence_for_one_to_many, deployment_completeness,
  user_journey_coherence, ci_health, implementation_coverage, environment_coverage
- **SKIP** (now *visible* thanks to v3.5.0 skip-visibility — these verified nothing):
  - `depends_on_consistency` — propagation output not found
  - `artifact_contract` — opt-in, disabled
  - `resource_flow_coherence` — no resource/capability contracts declared on codd-dev
- **AMBER findings** (real):
  - `dependency_freshness` — 2 doc-to-doc `depends_on` edges never reconciled; no
    reconciliation ledger (`.codd/reconciliation_ledger.json` absent). Upstream
    `docs/requirements/extract-verify-requirements.md` committed after
    `docs/design/extract-architecture.md` and `docs/requirements/require-command-requirements.md`.
  - `transitive_closure` — 453+ codd/ modules unreachable from DAG roots.

**Self-hosting read:** the SKIP-visibility fix (v3.5.0) immediately paid off — it
exposed three checks that were silently green on CoDD's own repo. The two amber
findings are genuine coherence debt in CoDD's own docs/graph.

| # | nature | change | red-before-green | autonomy | status |
|---|---|---|---|---|---|
| — | verifier | (baseline) CoDD dag verify on codd-dev | 3 SKIP + 2 amber surfaced | n/a | recorded |
| 1 | verifier check | `malformed_contract` amber in resource_flow_coherence: surface declared-but-unusable contract entries (missing required field) instead of silently dropping them — Tier-1 scope of extractor_silent_noop | candidate test RED pre-impl (`test_malformed_contract_entry_is_amber` failed: no warning) → GREEN post-impl; 14 resource_flow tests + full suite 5873 passed | autonomous (visibility, amber; gate unchanged) | ✅ main |

| 2 | diagnostic | `remediation` actionable hint on `dangling_required_consumer` reds (self-repair quality) | candidate test RED pre-impl (violations lacked `remediation`) → GREEN; 15 resource_flow + full suite 5874 passed | autonomous (additive diagnostic) | ✅ main |

## Saturation tracker (2026-06-24)

| requirement | status |
|---|---|
| ≥5 real codd-dev changes, each red-before-green | **2/5** (#1 malformed_contract, #2 remediation hint) |
| false-green corpus regression gate (4 vectors) | ✅ 33 fixtures pass (`test_resource_flow_coherence.py` + `test_dag_verify_cli.py`) |
| control/legacy false-red = 0 | ✅ (all guard/control/legacy fixtures pass) |
| Contract Kernel generality preserved | ✅ (no project/FW/lang literal in resource_flow_coherence core) |
| 2 consecutive clean discovery rounds (self-host) | ⏳ |
| 9 owner-gated vectors resolved / fixture-locked / deferred | ⏳ partial — Tier-3 (semantic_conflict, negative_space) explicitly deferred; Tier-1/2 resolve as changes #2–5 land |

Re-run the corpus gate: `python3 -m pytest tests/dag/test_resource_flow_coherence.py tests/test_dag_verify_cli.py -q`

> Deferred: the `dependency_freshness` doc-drift amber (2 edges, missing
> `.codd/reconciliation_ledger.json`). `codd propagate --verify` is source→doc
> impact only ("no affected docs"); the doc-to-doc reconciliation ledger is a
> separate mechanism. Parked for a later change — establishing/acknowledging the
> ledger needs care not to rubber-stamp genuine drift.
