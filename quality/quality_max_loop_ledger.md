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
| 3 | resource_order_explicit_flow | producer-after-consumer red **only** w/ explicit op order | red(cond) | 80 | ✅ shipped-to-main |
| 4 | assertion_abuse | weak outcome assertion → amber (red needs owner) | amber | 75 | ✅ shipped-to-main |
| 5 | identity_alias_drift | explicit alias collision / shadow only | amber | 48 | ✅ shipped-to-main |
| 6 | reconcile-baseline gap | `propagate --baseline` acks current doc-to-doc edges | n/a | 48 | ✅ shipped-to-main (self-host amber→PASS) |
| 7 | cross_artifact_partial_coverage | expected_extraction group diagnostic (no quirk touch) | amber | 45 | ✅ shipped-to-main |
| 8 | cardinality_partial | default representative/unknown; all→red only if declared | amber/red | 27 | ✅ shipped-to-main |
| 9 | stale_evidence | fingerprint-based only (never wall-clock) | amber | 27 | ✅ shipped-to-main (forward-guard) |
| 10 | negative_space | explicit forbidden-evidence only (no absence guarantee) | amber/red | 8 | ✅ shipped-to-main |
| 11 | semantic_conflict | exact scalar-key contradiction only | amber | 6.4 | ✅ shipped-to-main |

**Cycle-1 implementation COMPLETE: 11/11 vectors shipped to main** (none deferred).
Verification going forward also uses Codex (gpt-5.5 xhigh) as an independent
parallel reviewer (owner directive 2026-06-24).

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

### #3 resource_order_explicit_flow ✅ (2026-06-24)
- `resource_flow_coherence.py`: `producer_after_consumer` **red** — fires only when
  a required consumer (critical/high journey scope, same gate as dangling) maps to a
  single operation index, the resource has ≥1 mapped non-external producer, and
  **every** such producer runs strictly later. ResourceUse gains
  operation_index/ref/mapping_status; `_operation_ref_to_index` +
  `_attach_operation_indices` read the existing `operation_flow` (via
  `requirements_meta.operation_flow_operations`) — no new schema.
- anti-false-red guards: no operation_flow → skip; external provider → skip;
  ambiguous ref → amber `ambiguous_operation_mapping`, never red; any producer
  at/before consumer → pass; dangling (producer-absent) red path unchanged. Red
  reachable only with explicit ordering.
- red-before-green proven (Fixture B red comes solely from new code); 4-fixture;
  tests/dag 251 passed.

### #4 assertion_abuse ✅ (2026-06-24)
- `user_journey_coherence.py`: `weak_outcome_assertion` **amber** — a declared
  outcome signal whose only evidence is its presence in test source text (no
  explicit assertion attribute, no generic assertion verb nearby) is flagged. Verbs
  generic (`assert`/`expect`/`verify`) — no framework matcher in core.
- amber only / never red (red promotion owner-gated). Guards: no declared signal →
  none; no e2e → defer to `no_e2e_test_for_journey`; explicit attr / source-assertion
  → pass; self-reference filtered. Existing `browser_expected_not_asserted` red kept.
- red-before-green (1 failed → 4 passed); user_journey suite 124 passed.

Both verified together: full suite **5896**, corpus gate 52.

### #5 identity_alias_drift ✅ + #7 cross_artifact_partial_coverage ✅ (2026-06-24)
- **#5** `resource_flow_coherence.py`: alias collection overwrite → collect; amber
  `duplicate_alias_target` (one alias → multiple canonicals) + `alias_shadows_canonical`
  (alias name also a declared canonical). amber only, exact-string (no fuzzy);
  conflicting aliases left unresolved (conservative). Existing alias resolution / #3
  ordering / dangling intact (frontmatter_alias 62, tests/dag 255).
- **#7** `implementation_coverage.py`: per-design coverage summaries +
  `coverage_shape_incomplete` (multi-artifact shape, test kind undeclared) +
  `cross_artifact_partial_coverage` (expected>1, 0<matched<expected). amber only;
  **defers to the existing red** (count-based partial ⊇ red set → amber suppressed
  whenever red fires; zero duplication, proven); historical glob quirk untouched.
- Both red-before-green; generality preserved. Full suite **5907**.

### #6 reconcile-baseline gap ✅ + #8 cardinality_partial ✅ (2026-06-24)
- **#6** `propagate --baseline [--dry-run|--allow-dirty|--reason]` (propagator.py
  `run_baseline_ack` + cli.py + reconciliation_ledger.py optional reason +
  dependency_freshness.py `doc_to_doc_edges` public). Acks current doc-to-doc edges
  as the reconciliation baseline; mutex with verify/commit; refuses dirty docs
  (unless --allow-dirty); doc-to-doc only; no-git-history → skipped; backward-compat
  preserved. **Dogfood closes the self-host finding**: codd-dev's
  `dependency_freshness` amber (2 never-reconciled edges) → **PASS** (77 edges
  checked); `.codd/reconciliation_ledger.json` committed = codd-dev's baseline.
- **#8** `cardinality_coverage` (new check): 1:N relations. **red only** when
  policy=all + member_signals declared + a signal unasserted (logically derived);
  default amber (`cardinality_policy_unspecified` / unverifiable-all). **Member
  universe is never inferred** (no detector-hit→red); representative / at-least-one
  pass; skip when no relations. Minimal schema (nested in existing
  aggregation_policies). 9 tests.
- Both red-before-green; generality preserved. Full suite **5921**, corpus 55.

### #9 stale_evidence ✅ + #10 negative_space ✅ + #11 semantic_contract_conflict ✅ (2026-06-24)
- **#9** `stale_evidence` (new check): amber when a recorded `source_sha256` ≠ the
  current file hash. Fingerprint-only — never reads mtime/generated_at (env diff =
  false-red). file-missing → `source_missing` amber (not red); no recorded hash →
  silent; 0 checkable → skip. Forward-guard: nothing carries `source_sha256` today,
  so it is currently dormant (activates when a writer records fingerprints).
- **#10** `negative_space` (new check): scans project-declared `forbidden_evidence`
  (scope globs + regex patterns). hit + explicit `on_violation: fail` → **red**;
  otherwise amber. Path-traversal rejected (out-of-root → amber, never red);
  0 files scanned → vacuous amber (not a clean pass); regex error → amber;
  binary → skipped. No PII/domain literal in core (patterns are project-declared).
- **#11** `semantic_contract_conflict` (new check): amber when the same
  (section, identity, scalar-key) is declared with conflicting values. scalar-only,
  declared-values-only (no default backfill), exact alias; skip when no entries.
- All red-before-green; registered in runner.py; generality preserved. Full suite
  **5944** (+23 tests), corpus green.

---

# Cycle-2 — Harness-closes-model-gap (CSUMR goal, 2026-06-24)

New `/goal`: absorb strong-model-only true-positives as harness-internalized
**issue-classes** (false-green / false-red / diagnostic-gap) so that
harness-assisted Sonnet's detection set ⊇ harness-assisted Codex/Opus union on the
classed, non-owner-gated subset. Metric **CSUMR=0** (Classed Strong-Union Miss
Rate) + mutation-survival=0 + unexpected-red=0, sustained 3 consecutive
review/dogfood rounds. Method: class-ify → red-before-green fixture (4-fixture +
held-out) → deterministic check / corpus / structured protocol. Origin: the
3-engine parallel review where Codex caught issues Sonnet missed.

### Class #1: resource_flow_operation_scope_false_red ✅ (false-red)
- **Strong-union miss**: Codex caught a BLOCKER false-red that Sonnet **and** Opus
  missed — the flagship gap this goal exists to absorb.
- **Defect**: `producer_after_consumer` compared operation indices concatenated
  **globally** across design docs → producer/consumer in *independent flows in
  separate docs* red purely on doc-sort order.
- **Fix**: the index is now `(flow_scope = owning node, local_index)`; ordering
  compares only same-scope producer/consumer; cross-flow → not comparable → no red.
  The in-order check now precedes the ambiguous bail (also resolves the Sonnet-flagged
  ambiguous-sibling false-amber).
- **Corpus**: 4-fixture = green_control + false_green_candidate + legacy (existing
  single-doc) + **new false_red_guard** (multi-doc independent flows → no red);
  **held-out** = order-invariance metamorphic (verdict invariant to doc insertion
  order). Both new tests RED pre-fix → GREEN. Full suite **5948**.
- **CSUMR effect**: the class is now a regression fixture, so *any* engine running
  the corpus catches it — the harness, not model insight, does the catching. This is
  the goal's mechanism in one concrete instance.

### Class #2: resource_flow_ambiguous_alias_false_red ✅ (false-red)
- **Strong-union miss**: Codex flagged a false-red introduced by the cycle-1 alias
  fix — an alias resolving to >1 canonical is left out of `alias_map` (unresolved),
  so a consumer using it is never canonicalized and reds as `dangling_required_consumer`
  although a producer exists for one of its targets.
- **Fix**: the dangling check suppresses the red when the consumer's resource is an
  ambiguous alias and surfaces amber `ambiguous_alias_unresolved` (with the resolved
  targets + remediation) instead — conservative, no false-red, author disambiguates.
- **Corpus**: red-before-green test (`user_id` → `users.id` + `accounts.id`, a
  producer for one target → no dangling red, amber instead). RED pre-fix → GREEN;
  full suite **5949**.

### Class #3: amber_findings_visibility_gap ✅ (diagnostic-gap)
- **Strong-union miss**: Codex caught that `_dag_result_has_findings` ignored
  `warnings`, so amber checks reporting via warnings (extraction_diagnostics,
  dead_resource, identity_alias, ambiguous_alias, cross_artifact, …) rendered as
  "PASS [amber]" and the summary undercounted WARN — the amber findings those
  cycle-1 checks compute were effectively **invisible**.
- **Fix**: `_dag_result_has_findings` now counts `warnings`; both verify summaries
  render an amber check carrying findings as WARN (not PASS), matching the count.
- **Corpus**: red-before-green unit test (`has_findings` counts warnings). full
  suite **5950**. This retroactively makes several earlier cycle-1 amber checks
  actually visible (computed but previously hidden) — a high-leverage visibility fix.

**v3.7.0 unblock status**: the 3 Codex blocker/major findings (operation-scope
false-red, ambiguous-alias false-red, amber visibility) are now fixed.

### Minor classes #4–#7 ✅ — the smaller Codex findings (all false-green, amber-only)
- **cardinality policy normalization**: `"All"/"ALL"` bypassed the `policy == "all"`
  red path; policy is now lower-cased before comparison.
- **negative_space `no_usable_patterns`**: a `forbidden_evidence` declaration with a
  valid scope but no compilable pattern was a vacuous PASS; now amber (distinct from
  `invalid_regex`).
- **semantic_contract_conflict nested scalars**: conflicts in the nested
  `aggregation_policies[].cardinality_assertion.policy` were missed (top-level-only);
  now compared via a known nested scalar-path list (in lockstep with what
  cardinality_coverage reads).
- **extraction_diagnostics `no_usable_pattern`**: `capability_patterns` declared but
  with no usable regex was a vacuous PASS; now amber (distinguished from skip and
  `invalid_regex` via usable_count/checked_count).
- All red-before-green, amber-only, generality preserved, existing behavior unchanged.
  full suite **5961**.

**All 3-engine-review findings are now internalized** (classes #1–#3 + minors #4–#7).
Next: the CSUMR validation rounds (3-engine re-review showing harness-Sonnet's
detection ⊇ harness-Codex/Opus union on the classed issues) → then v3.7.0 release.

## CSUMR validation — Round 1 (2026-06-25): NOT clean → 6 new classes internalized
The first 3-engine re-review (Codex + Sonnet + Opus) on the post-#1–7 code found **6
NEW strong-union issues** the harness was missing — the loop working (strong models
surface the next layer). All fixed red-before-green; full suite **5982**:
- **resource_flow `_entries` now reads `frontmatter` / `frontmatter.codd`** — it was
  blind to the canonical generated metadata location and silently skipped real
  contracts. Highest-leverage fix: the resource_flow checks were effectively dormant
  on real projects (this is why the zoo's resource_flow kept skipping).
- **resource_flow no-violation return is amber/warn when warnings exist** (was
  severity=info → CLI rendered PASS; the check-side complement of class #3).
- **cardinality binds assertions to detected 1:N relations** (was global → an
  unrelated policy could false-red the run or suppress a relation's amber).
- **negative_space distinguishes missing vs malformed declaration** (a malformed
  forbidden_evidence was a silent skip → now amber).
- **implementation_coverage jails path_hint under project_root** (`../outside.py`
  could satisfy an artifact — a path-traversal false-green).
- **`codd check` shows vacuous passes too** (parity with `dag verify`).
Plus a low Sonnet-only finding (negative_space double-diagnostic) noted for later.
**Round 1 = NOT clean (6 found) → saturation streak resets to 0. Rounds 2–3 pending.**

## CSUMR validation — Round 2 (2026-06-25): NOT clean → 5 more found (fixes HELD)
Codex round-2 re-review: the 13 prior fixes hold, but **5 new/remaining classed
issues** surfaced — notably `propagator.py` stale-ack (high: a `docs_classified`
upstream can change between `--verify` and `--commit`; the ledger records the
current commit, not the reviewed one → false-green), an `implementation_coverage`
path-jail edge (the round-1 fix was incomplete — `_normalize_hint` strips a leading
`/` before the jail), a cli DAG-WARN parity gap, + 2 others.
**Round 1 found 6, round 2 found 5 → the negative axis is not converging quickly;
3-clean-round saturation is many rounds away.** This convergence data is the input
to a GPT-Pro strategic decision (negative axis = harden the checker vs positive axis
= spec/contract/test completeness). Per owner directive, the strategic call is made
by GPT on MECE context, not by default — **round-2 fixes are HELD pending it.**

**Sonnet round-2** (complementary — found DIFFERENT issues than Codex, so the
strong-union > either alone): (a) cardinality `evaluated_field_ids` dedup **drops a
legit red** when two same-`field_id` policies exist softer-first — a **regression
from the round-1 relation-binding fix** (medium false-green); (b) resource_flow
`_entries` **double-counts** top-level keys (read from both `attrs[key]` and
`attrs.frontmatter[key]`) — a **double-count from the round-1 frontmatter.codd
merge** (low; inflates counts, verdict unchanged); (c) propagator corrupt-state
crash (low). **Two are regressions from my own round-1 fixes** — the verification
catching my fixes' side-effects. Round-2 union (Codex 5 + Sonnet 3, mostly disjoint)
≈ 7-8 → the negative axis is clearly NOT converging; this is the input to the
pending GPT-Pro strategic decision (negative vs positive axis).

## New /goal (2026-06-25): N-liveness gate → Axis-P coverage (GPT-decided)
GPT-Pro decided: pivot to **Axis-P** (coverage = *meaningful* green) as the main
axis, but FIRST pass a short **Axis-N operational-liveness gate** (the checker must
reliably run / read canonical metadata / surface findings — round 1 showed it was
dormant). Do NOT chase CSUMR full saturation. **Axis-M** (reachability) folded into
the N-gate. Stop line = **2 consecutive 3-engine rounds with no new P0/P1
foundational**. Then Axis-P (AI proposes gaps → decider = GPT/strong-model + harness,
**not the owner per-gap** → contract → deterministic check). Owner is not a loop step.

### Round-2 foundational fixes ✅ (N-gate work; full suite 6003)
All 6 P0/P1 findings internalized, red-before-green, generality preserved, runner.py
unchanged: cardinality dedup by full identity (keeps the stricter red); dependency_
freshness amber-when-warnings; the 3rd cli summary now uses a shared `_dag_pass_is_warn`
(all 3 summaries consistent); resource_flow `_entries` no longer double-counts
top-level keys; implementation_coverage passes the raw hint to the root-jail
(absolute-in-root only); propagator fingerprints the upstream at verify and rejects
stale acks at commit across all outcomes. (Low-severity advisories — e.g. propagator
corrupt-state robustness — deferred per the stop line.)
**N-gate progress: rounds 1-2 done. Need 2 consecutive clean rounds → round 3 next.**

## Round 3 (2026-06-25): NOT clean → 5 fixed (SYSTEMATIC, full suite 6033)
Sonnet+Codex round-3 found 5 P0/P1 foundational; all red-before-green. Fixes closed
CLASSES, not instances:
- `_dag_result_has_findings` now keys on the check's declared status (warn/fail) +
  the `findings` field — robust to field name (ci_health amber was hidden as PASS).
- new shared `metadata_access.collect_structured_entries` (attrs/frontmatter/
  frontmatter.codd + top-level dedup), applied to user_journey_coherence (C7) and
  coverage_axes (C9) — they were dormant on `frontmatter.codd`-nested declarations.
- builder `_glob_project_paths` root-jails all scans (out-of-root files never become
  nodes); edge_validity reds out-of-root absolute paths.
- `--format json` overlay marks vacuous + effective-warn (additive; raw preserved) so
  a JSON/CI consumer can't read a false-green.
- resource_flow malformed-only early-return now amber/warn (was JSON status=pass).
Round 3 NOT clean (5) → streak resets; round 4 next.

## Owner-free Axis-P design (GPT-decided 2026-06-25) — Stage-2 spec + N-gate refinement
Full: `/tmp/gpt_ownerfree_answer.md`. **Owner is never a per-item step.** Axis-P
discovers gaps owner-free (amber); **RED only from explicit/confirmed/closed-world
contracts via deterministic rules — never strong-model confidence** (that is amber
recall only). Gaps → amber Finding/AskItem(blocking=false, RECOMMENDED_PROCEEDING) in
coverage_decisions; CI/loop never waits; anyone batch-confirms later →
CONFIRMED=contract→then red-enforceable. Default-profile recs are not contracts.
**Axis-P gate**: pass when explicit/confirmed/closed-world contracts deterministically
checked + their violations red + model-discovered gaps surfaced as amber (not hidden)
+ no owner answer required for CI/merge + amber residue persisted + corpus PCUMR vs
frozen/construction-derived gold. Must NOT ask owner per gap / convert model
confidence to red / treat default recs as contracts / show pure green while amber
semantic residue exists. Real-project metric = E-PCUMR (explicit-contract coverage);
multi-engine union = Advisory Union (amber backlog).
**N-gate refinement (apply now):** acceptance = red-before-green + deterministic
regression (not Opus's say-so); 3-engine consensus = amber confidence, not red;
deferred new-semantics shown as **amber-residue=N, not pure green**. Revised pass:
"all explicit invariant checks red-before-green proven + no P0/P1 operational-liveness
remains; deferred new semantics visible as amber residue, not counted as pure green."

## Round 4 (2026-06-25): NOT clean → 3 stragglers fixed + the convergence path
3 P1 stragglers of known classes fixed (full suite 6041): env_coverage journeys
dormancy → shared `collect_structured_entries`; depends_on_consistency
records_compared=0 → skip + checked_count exposed; ci_health workflow_glob root-jail
(out-of-root → structured red, not a crash). The subagent audits revealed the
**convergence path**: the dormancy class is closed (all 22 checks audited), but the
**VACUOUS and PATH-ESCAPE classes have ~12 more stragglers** — fixing 1-2 per round
won't converge, so both classes are being closed COMPREHENSIVELY next:
- vacuous (expose checked_count + skip-when-0): deployment_completeness, edge_validity,
  implementation_coverage, task_completion, artifact_contract_check,
  user_journey_coherence, ui_coherence, environment_coverage(empty-axes).
- path-escape (root-jail config FS reads): e2e_extractor, builder `_project_path`,
  deployment/extractor, depends_on_consistency propagation_output.
Then rounds 5-6 should be clean → N-gate passes.
