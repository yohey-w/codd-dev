# CoDD Quality-Max Loop — ledger

`/goal` (2026-06-24): maximize CoDD quality via a **GPT-Pro-consult → improve →
GPT-Pro-consult** loop until non-owner-gated improvements are exhausted. Scope:
(1) false-green/coherence detection quality (implement every owner-gated vector
with a conservative default meaning, or explicitly defer); (2) language/FW adapter
coverage (core stays language-free; Python first, TS/Next.js maintained; new
adapters cheap). Each improvement: red-before-green → full suite → false-green
corpus regression gate → ship. Invariants: generality / anti-false-green /
anti-false-red / backward-compat / Contract Kernel. New meaning/severity/schema/
absence → conservative default decided with GPT (only the truly subjective ones
get an owner glance). **Saturation:** 2 consecutive GPT-consult cycles find no new
implementable improvement + full suite green + corpus green + generality preserved.

---

## Cycle 1 — GPT consult (owner-gated false-green vectors) — done

GPT Pro cycle-1 design (22m thinking) prioritized 11 vectors (priority_score =
false_green_impact × reachability × likelihood × diagnostic_confidence ÷
false_red_risk). Conservative defaults: red only when logically derivable from
explicit contracts; otherwise amber diagnostic / item-count visibility.

| # | vector | default | severity | score | status |
|---|---|---|---|---|---|
| 1 | vacuous_pass | 0-item PASS shown as vacuous (materiality overlay) | amber | 250 | ✅ shipped-to-main |
| 2 | extractor_silent_noop broader | invalid capability_pattern regex → verify (self-contained re-validate) | amber | 160 | ✅ shipped-to-main |
| 3 | resource_order_explicit_flow | producer-after-consumer red **only** w/ explicit op order | red(cond) | 80 | ⏳ |
| 4 | assertion_abuse | weak outcome assertion → amber (red needs owner) | amber | 75 | ⏳ |
| 5 | identity_alias_drift | explicit alias collision / shadow only | amber | 48 | ⏳ |
| 6 | reconcile-baseline gap | `propagate --baseline` acks current doc-to-doc edges | n/a | 48 | ⏳ |
| 7 | cross_artifact_partial_coverage | expected_extraction group diagnostic (no quirk touch) | amber | 45 | ⏳ |
| 8 | cardinality_partial | default representative/unknown; all→red only if declared | amber/red | 27 | ⏳ |
| 9 | stale_evidence | fingerprint-based only (never wall-clock) | amber | 27 | ⏳ |
| 10 | negative_space | explicit forbidden-evidence only (no absence guarantee) | amber/red | 8 | ⏳ |
| 11 | semantic_conflict | exact scalar-key contradiction only | amber | 6.4 | ⏳ |

Owner-glance flags (per GPT): assertion_abuse amber→red promotion; cardinality
"default all"; negative_space "absence guarantee" wording; semantic_conflict
beyond exact scalar. Default (amber) impls proceed autonomously.

### #1 vacuous_pass ✅ (2026-06-24)
- `codd/dag/materiality.py` (new): generic overlay — `is_vacuous_pass(result)` =
  pass-status + `checked_count == 0` + not skipped. No per-check/project/FW literal.
- Wired `checked_count` on `ui_coherence_for_one_to_many` (the baseline's real
  vacuous-pass case: "checked 0 relations → PASS").
- `codd dag verify` summary now lists "N check(s) PASS but verified nothing
  (vacuous): …". JSON carries `checked_count` automatically (asdict).
- red-before-green: `test_materiality.py` (4-fixture) RED pre-module (import
  error) → GREEN; CLI `test_verify_summary_flags_vacuous_pass`; full suite **5883**.
- Remaining count-wiring (resource_flow / implementation_coverage /
  user_journey / dependency_freshness) is additive follow-up — overlay already
  generic over any check that reports `checked_count`.

### #2 extractor_silent_noop broader ✅ (2026-06-24)
- `codd/dag/checks/extraction_diagnostics.py` (new): re-validates declared
  `coherence.capability_patterns` regexes; any that fail `re.compile` → amber
  `invalid_regex` (with remediation). Self-contained — does **not** touch the
  extractor's silent `except re.error: continue`; mirrors `_pattern_match_specs`
  + the builder's config accessor so the inspected set matches the extractor's.
- **amber only / never red** (a config regex typo is advisory); **skip** when no
  patterns declared (dormant — legacy projects unaffected); `checked_count` for
  materiality. No project/FW/lang literal.
- Registered in `runner.py` CHECK_MODULES.
- red-before-green: `test_extraction_diagnostics.py` (4-fixture + registration)
  RED pre-module → GREEN (5 passed); full suite **5888**.
