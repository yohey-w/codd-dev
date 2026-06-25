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

### Closure done (full suite 6060): vacuous + path-escape classes comprehensively closed
- vacuous: all 8 checks expose checked_count + return skip (no applicable input) or a
  checked_count=0 vacuous-pass (ran but 0 targets) — no clean PASS over 0 items
  remains. skip: deployment_completeness, ui_coherence, environment_coverage,
  user_journey_coherence, artifact_contract. vacuous-pass: edge_validity,
  task_completion, implementation_coverage.
- path-escape: e2e_extractor doc_dirs, builder `_project_path` (plan_task / lexicon /
  coverage-axes reads via `_jailed_project_path`), deployment/extractor documents,
  depends_on_consistency propagation_output — all root-jailed (out-of-root not read).
Round 5 (re-review) next; expect convergence toward 2 consecutive clean rounds.

## Round 5 (2026-06-25): NOT clean → 5 fixed; the 3 classes now definitively closed
- vacuous (FINAL): node_completeness (red gate), transitive_closure (amber),
  dependency_freshness (a real no-upstream-history hole) — the last legacy result
  classes lacking checked_count; now skip / expose. (ci_health fails-at-0 = N/A;
  opt_out is not a registered check; resource_flow already skips.) Every registered
  check now exposes checked_count or skips.
- dormancy (straggler): _one_to_many_detection ignored dag.lexicon_file → threaded the
  config (root-jailed) + updated ui_coherence / cardinality_coverage callers.
- visibility (straggler): _emit_verify_summary (the 3rd summary) counted skip/vacuous
  as PASS → now SKIP/VACUOUS columns (empty project now: 0 PASS / 6 SKIP / 2 VACUOUS,
  matching the N-gate refinement: vacuous/skip visible, never pure green).
full suite 6076. Round 6 next; the 3 foundational classes are now comprehensively
closed across all checks + the 3 summaries + json, so convergence is expected.

## Round 6 (2026-06-25): NOT clean → 7 fixed (P0 regression + symlink sub-vector)
- P0 (regression from round-5): depends_on_consistency violations-return omitted
  status= → JSON false-green (status:pass + severity:red). Fixed.
- visibility (3 sites): the vacuous DISPLAY list (dag verify, _emit_verify_summary,
  embedded check payload) didn't apply the _dag_pass_is_warn filter the counts use →
  amber-warn vacuous double-counted. Filtered at all 3.
- path-escape SYMLINK sub-vector (4) + dormancy (1): a repo-internal symlink resolving
  out-of-root escaped the jails — impl_coverage, artifact_contract, deployment/extractor;
  deployment/extractor journey dormancy. Fixed symlink-aware. full suite 6095 (3fadf4c).

## N-gate path-escape TERMINAL CONDITION (GPT-decided 2026-06-25) — full: /tmp/gpt_ngate_answer.md
The symlink sub-vector exposed a large FS-glob surface (102 sites). GPT decided: scope
the N-gate's path-escape to **user-path-controllable FS readers ONLY**; hardcoded
project_root fixed-pattern globs (code/catalog/profile patterns, bundled resources) are
OUT of N-gate scope → Axis-N adversarial-hardening backlog (don't reset the streak).
Machine rule: `source=external_path_like (config / frontmatter / node.path / path_hint /
lexicon_file / documents / propagation_output / coverage-axis / CLI path) AND
sink=FS read/exists/glob AND not first through the shared jail ⇒ P0/P1 candidate`.
**N-gate path-escape clean** = every user-path FS sink goes through the shared jail;
the 3 fixtures (../ , absolute out-of-root, repo-internal symlink→out) don't read the
external file or count it as PASS evidence; out-of-root → skip/warn (optional) or
missing/red (required), never pure green; 2 consecutive 3-engine rounds find no new
P0/P1 in core liveness + this user-path scope.

### path_safety closure done (full suite 6117): unified jail
New `codd/path_safety.py` (resolve_project_path / iter_project_glob /
project_relative_path: resolve → follow symlinks → relative_to(root)). Unified the 3
named jails (builder _jailed_project_path, impl_coverage _resolve_in_root,
deployment/extractor _jail_paths) onto it (same raw path → same reject/accept). Audit
routed all user-path sinks through it and found 2 NEW unjailed: propagator
wave_config.output (→ read_text/write) and screen_transition_extractor src_dirs
(→ os.walk/read_text); plus e2e doc_dirs per-file symlink gap, depends_on
propagation_output, negative_space forbidden-scope (kept its amber diagnostic).
Out-of-scope → Axis-N backlog: code-constant globs (deployment patterns, artifact
catalog globs), Path(__file__) bundled resources. Round 7 next, scoped to user-path.

## Round 7 (2026-06-25, scoped): NOT clean → 6 fixed (node.path-read sub-vector + doctor)
Scoped review (user-path-controllable only) found a new sub-vector: checks reading a
DAG **node.path** for FS access without jailing (node.path is user-controllable — e.g.
an impl_file node with path=/etc/hosts passed node_completeness; a design_doc node with
an out-of-root master-detail file gave ui_coherence credit). Fixed via path_safety:
- node_completeness (exists), task_completion (is_file), ui_coherence (_node_text),
  environment_coverage (_node_text) — escaped node.path → missing / uncredited.
- cli _read_optional_context_file (lexicon_path/design_md_path) + cli
  _configured_text_files (doctor scan.source_dirs/test_dirs) → path_safety jailed.
Audit confirmed the other node.path readers already jailed (edge_validity, cardinality,
user_journey, stale_evidence, negative_space, impl_coverage, depends_on, ci_health) or
fixed-pattern (deployment_completeness deploy.yaml candidates). full suite 6135.
Round 8 next; the user-path FS surface (config + builder + node.path + doctor + cli) is
now comprehensively jailed, so convergence expected.

## Round 8 (2026-06-25, scoped): NOT clean → 5 fixed (propagator + cli deep surface)
Finding count is dropping (6→7→6→5; Sonnet found just 1). Remaining stragglers were in
the propagator and cli layers' deeper readers:
- propagator/propagation_common: graph.path (CEG load), scan.doc_dirs (iter_design_docs
  used by _find_design_docs_by_modules), _upstream_fingerprints (verify-state paths);
  the audit proactively added _find_changed_docs + run_commit git-add jails. wave_config
  jail confirmed intact.
- cli design-doc readers: _plan_design_doc_nodes, extract_design, llm_derive — now via a
  shared _resolve_cli_project_file → path_safety; _configured_doc_files (the cli copy of
  the e2e function) jailed.
All via path_safety; anti-false-red; escaped → not read / stale / not-evidence. full
suite 6162. Round 9 next; propagator + cli now comprehensively jailed too.

## Round 9 (2026-06-25, scoped + exhaustive-sweep): NOT clean → ~11 fixed (deep modules)
The exhaustive-sweep prompt surfaced the deep-module surface: user-path FS readers
across auditors / validators / derivers / parsers / llm / fix that read config/doc/node
paths as evidence without path_safety. Fixed in 4 file-disjoint groups (each also
audited its layer): fix/candidate_selector (node.path) + require_propagate (node.path +
graph.path); operational_e2e_audit + verifiable_behavior_audit + coverage_auditor
(scan.test_dirs / test_coverage.docs / artifact_discovery.paths; replaced a local
no-jail _resolve_project_path) + coverage_execution_coherence (transitive);
validator + required_artifacts_deriver + requirement_completeness_auditor
(doc_dirs / graph.path / requirement docs); parsing/filesystem_routes (route base_dir)
+ llm/criteria_expander (design-doc + 4 audit-found readers). full suite 6225.
Finding count rose because the sweep widened to all of codd/; the user-path surface is
larger than the dag/builder/cli/propagator core. Next: a GLOBAL audit to enumerate the
COMPLETE remaining surface (brownfield / languages / stack / assembler / extractors) in
one pass, fix, then 2 clean rounds — instead of discovering it round-by-round.

## Global audit result + scope decision (2026-06-25)
The global READ-ONLY audit enumerated **~87 user-path FS readers across ~41 files** — the
whole codebase reads config/doc/node paths. Two clusters: (A) check/verify/audit
EVIDENCE readers (scanner→DAG, discovery→coherence, the coherence gates, project_types
layout, measure/policy/requirement) ≈20-30; (B) NON-check-evidence — generation
(implementer/generator), fix (fixer/repair), deploy (deployer/providers), lexicon
management (lexicon_cli/elicit), and explicit CLI inputs ≈50+. GPT's prior rule said
"counts as artifact evidence", so a scope consult (TAB 0C2CE72E) is deciding whether the
N-gate path-escape scope is (A)-only [(B)→Axis-N backlog] or all 87, plus 6 sub-questions
(CLI explicit inputs / cache_dir / sockets / package.json / shared read sinks / bespoke
jails). Cluster (A) is check-evidence regardless, so it was fixed autonomously now:
- scanner.py (RC-1, the DAG node→path map feeder), project_types._normalize_dirs (RC-6,
  strip `..`/absolute), discovery.py iter_source_files (RC-2), and the 3 coherence gates'
  _iter_py_files — all via path_safety. full suite 6259.
Awaiting GPT's scope ruling to bound cluster (B); then fix in-scope (B) + 2 clean rounds.

### GPT scope ruling (2026-06-25) — full: /tmp/gpt_ngate_scope_answer.md
N-gate path-escape = **artifact-evidence path only** (A + evidence-adjacent, ≈20-30), NOT
all 87. In-scope test: path is user/config/CLI/generated-controllable AND its
content/existence/enumeration/metadata affects check/verify/audit/coverage/policy/DAG/
coherence/gate evidence (or evidence-set selection). B (generation / fix / deploy /
lexicon-mgmt / cache / socket) is Axis-N backlog, auto-promoted to in if it later feeds
evidence.
**In-scope to close:** scanner (done), discovery (done), coherence gates (done),
project_types._normalize_dirs (done) + measure / policy / coverage /
requirement_reconciliation / operations_derive; the shared public artifact reader sinks
frontmatter.read_frontmatter / design_md / dag/extractor (jail INTERNALLY at the sink —
caller-jailed is insufficient); evidence-context CLI/config/package paths (--files /
--test-results / --ci-log / --threshold-file / --input / --history / package.json
prisma.seed) jailed when evidence-forming.
**Refinements:** (1) escaped EVIDENCE paths must FAIL CLOSED, not silent-skip (silent
exclusion is another false-green) — revise scanner/discovery/coherence accordingly;
(2) shared artifact readers jail internally; (3) bespoke / string-based jails
(reference_resolution._escapes_project, repair/loop, design_token_drift) don't count as
clean — unify onto path_safety; (4) CLI/config/package paths judged by evidence-use, not
flag name. **2-clean is judged against the evidence scope only** (B not-yet-fixed ≠ dirty
unless it connects to evidence). This converges the N-gate realistically.

### In-scope evidence-reader closure done (full suite 6332)
Closed the entire in-scope set per the ruling, in 4 groups:
- shared artifact reader sinks jail INTERNALLY (optional project_root): frontmatter.
  read_frontmatter, design_md, dag/extractor public readers — fail-closed on escape
  (FrontmatterError / error-field / ValueError / [] / None), caller-jailed kept as
  defense-in-depth.
- audit/coverage/policy evidence readers: measure, policy (escape excluded + stderr
  warning, not silent), requirement_reconciliation, operations_derive.
- fail-closed for EVIDENCE escapes: added path_safety.PathEscapeError + require_project_
  path; scanner / discovery / coherence gates now RAISE / red on an escaped evidence
  path (was silent-skip = a false-green form). bespoke jails unified: reference_
  resolution (was string-only, no symlink resolve) + repair/loop → path_safety;
  design_token_drift left (write-side auto-fix, not evidence).
- evidence-context CLI/config: --threshold-file (gate) jailed fail-closed; prisma.seed
  (package.json) jailed. (This group was implemented by a parallel tmux ashigaru after a
  subagent role-confusion wrote a stray cmd_522; verified correct and folded in; Karo
  told to close cmd_522 and stop concurrent codd-dev edits.)
Round 10 next, review SCOPED to the evidence set (B is backlog, not a reset).

## Round 10 (2026-06-25, evidence-scoped): NOT clean → 1 fixed (drift.py bespoke jail)
**Sonnet: CLEAN** (no in-scope P0/P1). **Codex: 2 P0** — both in drift.py: a LOCAL
`_resolve_project_path` (no root-confine) fed drift evidence readers (e2e.test_dir,
screen_transitions_path, document-URL drift). The global audit had mislabeled drift.py
"already jailed" (it imports path_safety for some readers but kept a bespoke local jail
for these). Per GPT's ruling (bespoke/string jails don't count as clean mitigation), the
local resolver was DELETED and all drift evidence readers routed through
path_safety.require_project_path (fail-closed: escaped evidence path raises
PathEscapeError, never silent-read/silent-empty). full suite 6352. The two engines split
on whether drift is "evidence" (Codex in / Sonnet out); fixing it is correct either way
(anti-false-green hardening, no false-red). Streak reset (round 10 had a finding);
round 11 + 12 must both be clean.

## Round 11 (2026-06-25, evidence-scoped): NOT clean → 4 fixed (per-file + fail-closed tails)
Sonnet 1 + Codex 3, all evidence-scope:
- screen_transition_extractor._iter_source_files: root jailed but each walked file not
  re-confined → an in-root dir's symlink FILE escaped (screen-flow drift evidence). Added
  per-file resolve_project_path (mirrors _iter_test_files).
- operational_e2e_audit + verifiable_behavior_audit: round-9 jailed config evidence paths
  but SILENT-SKIPPED escapes → an out-of-root test_coverage.docs / scan.test_dirs left
  the gate passing "no VB table / no tests" (false-green). Revised to FAIL CLOSED
  (declared evidence ROOT/DOC escape → PathEscapeError; per-file symlink inside an
  in-root tree still skips). --scenarios (operational E2E evidence) jailed at the
  _load_or_extract chokepoint.
full suite 6360. These closed the per-file-reconfine + audit-fail-closed sub-patterns.
Streak still 0 (round 11 had findings); round 12 + 13 must both be clean.

## Round 12 (2026-06-25, evidence-scoped): NOT clean → 1 fixed (last bespoke-jail straggler)
**Sonnet: CLEAN** (verified the remaining bespoke jails — ci_health/cardinality/
user_journey/stale_evidence — are fail-closed or empty/warn, no PASS-credit → not P0/P1).
**Codex: 1 P1** — _one_to_many_detection._configured_lexicon_path used a local bespoke
jail that SILENT-SKIPPED an escaping configured dag.lexicon_file (callers cardinality_
coverage + ui_coherence are checks) → false-green. Fixed: unified onto
path_safety.require_project_path (fail-closed on a configured-but-escaping lexicon_file);
callers catch PathEscapeError → red finding; the local jail deleted. Configured-escape =
red, unset/legacy-absent = legit skip. full suite 6367.
Convergence: Sonnet clean in rounds 10 & 12; Codex's bespoke-jail/silent-skip tail
(drift → audit → lexicon) is now exhausted. Streak still 0; round 13 + 14 must be clean.

## Round 13 (2026-06-25, evidence-scoped): NOT clean → 3 fixed (isolated niche edges)
**Sonnet: CLEAN** (3rd clean in 4 rounds). **Codex: 3 P1** (niche, isolated): (1)
coverage_metrics e2e-coverage counted an out-of-root symlink test (per-file glob match +
scenarios_path not re-confined); (2) negative_space used lstrip("/") on configured scope
paths → an absolute scope reinterpreted root-relative (missed an in-root forbidden-text
file = false-green; /tmp/x → project/tmp/x) — removed lstrip, jail via path_safety
(in-root absolute → project-relative; escaped declared scope → fail-closed red); (3)
ci_health enumerated an out-of-root workflow_glob BEFORE jailing — now validates the glob
root before enumeration (out-of-root → red without enumerating), round-4 bespoke jail
unified onto path_safety. full suite 6375. (lstrip("/") confirmed isolated to
negative_space; other lstrip uses are ./-normalize / URL / already-jailed.) Streak still
0; **round 14 onward uses 3 engines (Codex + Sonnet + Opus)** per the /goal's "3-engine
review"; need 2 consecutive clean.

## Round 14 (2026-06-25, 3-engine, evidence-scoped): NOT clean → 6 fixed
Adding the 3rd engine (Opus) surfaced more niche edges (6 total, all fixed, full suite
6399):
- Codex: (1) FALSE-RED — iter_project_glob's lstrip("/") excluded a legit absolute
  in-root pattern → fixed by rebasing absolute-in-root to project-relative (out-of-root
  still rejected); (2) coverage_metrics + deployer had their OWN status-blind
  _dag_result_has_findings copies → centralized into new codd/dag/result_status.py (one
  canonical, cli/coverage/deployer bind the same function).
- Opus: 3 fixed-filename per-file-symlink gaps (legacy lexicon defaults, deploy.yaml in
  deployment_completeness + ci_health) → reconfine each candidate via resolve_project_path
  (escape dropped; absence still a legit skip; configured lexicon stays fail-closed).
- Sonnet: vb_marker_authenticity TS/Python import-specifier escape (../../.. / ....) read
  off-root as helper source → jail both resolvers, None → unresolved_helper (fail-closed).
**Convergence concern**: 14 rounds, 3 engines still find 3-6 niche adversarial-symlink/
specifier edges/round; the per-reader tail is long vs the owner's "short gate / don't
chase saturation". GPT exit-line consult (TAB 63E64E94) pending — to decide
systematic-class-closed exit (with niche adversarial-symlink residual → Axis-N) vs literal
2-clean. Apply its ruling for round 15+.

## N-gate exit-line — Opus-decided (2026-06-25; GPT-CDP unavailable)
The GPT exit-line ruling could not be retrieved: the ChatGPT account locked mid-session
(owner can't log in from any device; ChatGPT itself is operational per status.openai.com,
so it is account-level — plausibly an unusual-activity security lock triggered by this
session's automated GPT-via-CDP consults). GPT-via-CDP is paused; **Codex (Codex CLI may
share the same OpenAI account) is also paused** to avoid worsening the lock. Per the
standing rule "GPT unavailable → Opus decides" + GPT's prior scope ruling + the /goal's
"Axis-N maintained as ongoing 20-30% adversarial review", I set the N-gate exit-line:

**Stage-1 SYSTEMATIC classes are CLOSED** (verifiable in commits through 643edc2):
dormancy (shared collect_structured_entries, all checks); vacuous (every registered check
exposes checked_count or skips); visibility (status-based, centralized
codd/dag/result_status.py, all 3 text summaries + JSON); path-escape on ALL evidence
readers via the unified codd/path_safety.py jail (config / node.path / CLI / fixed-filename
/ import-specifier), fail-closed on a declared-evidence escape; the absolute-in-root
false-RED fixed.

**Exit-line**: the N-gate passes once the SYSTEMATIC classes show no new instance in the
confirmation review. The residual is per-reader **adversarial-symlink / import-specifier**
edges that require a *malicious in-repo symlink/specifier* — not real-project liveness —
which the /goal assigns to **Axis-N ongoing adversarial-review backlog**, NOT N-gate
blockers. (machine line: an evidence reader NOT routed through path_safety, or a
silent-skip/visibility/false-RED in the shared infra = SYSTEMATIC, resets; a path that DOES
go through path_safety but a niche per-fixed-filename/specifier symlink-target case =
Axis-N, does not reset.)

**Status**: round 14 completed the systematic infra (unified jail + centralized visibility
+ false-RED fix). Confirmation = a Claude-only round 15 (Sonnet + Opus; Codex held for the
account); Codex re-confirmation deferred until the account is restored. The substantive
Stage-1 goal is essentially met; round 15 confirms no systematic gap remains.

## Round 15 (2026-06-25, Claude-only): systematic CLEAN (both engines)
**Opus: CLEAN** + **Sonnet: CLEAN** — both rigorously live-verified all 6 systematic axes
(unified path_safety jail; centralized result_status bound by cli/coverage/deployer;
3 text summaries + JSON unified precedence; fail-closed on declared-evidence escape;
absolute-in-root false-RED fixed; inline jails [edge_validity/stale_evidence/
user_journey] behaviorally-equivalent resolve+relative_to). Only Axis-N residual noted
(fixed-pattern globs in vb_marker/dependency_lock/coverage_auditor needing an adversarial
in-repo symlink; both self-limiting/fail-open). **1st systematic-clean confirmation.**

## GPT account BANNED (2026-06-25) — engine-pool + Axis-N adjustment
The ChatGPT Pro account was BANNED (owner-confirmed); root cause = this loop's automated
GPT-via-CDP operation + auto-scrape of answers (ToS/bot-abuse). **GPT consults permanently
stopped; Codex (shared-account risk) also stopped.** Engine pool for review/dogfood is now
**Claude-only (Sonnet + Opus)** — still 2 independent models (they caught each other's
blind spots across rounds, e.g. split on drift), but GPT's third diverse perspective is
gone. **Axis-N's "20-30% ongoing adversarial review" is re-scoped to Claude 2-engine**
(no GPT/Codex). See [[feedback_gpt_cdp_automation_account_risk]].

## Round 16 (2026-06-25, Claude-only): 2nd systematic-confirmation — RUNNING
Sonnet + Opus, independent/adversarial (try to find a gap round 15 missed). If both CLEAN
→ 2 consecutive systematic-clean rounds (Claude). **N-gate passage is GATED on the owner's
exit-line approval** (Opus does not self-certify completion — GPT, the prior strategic
gate, is gone, so the OWNER is now the strategic gate). Stage-2 (Axis-P) stays gated until
the owner blesses N-gate passage. Owner asked to choose: ① pass N-gate → Stage-2 / ② more
Claude rounds / ③ a different bar.

## Round 16 (2026-06-25, Claude-only): NOT clean → 1 SYSTEMATIC P1 (multi-engine vindicated)
**Opus: CLEAN. Sonnet: 1 SYSTEMATIC P1** — `coverage_metrics.compute_dag_completeness`
counted SKIP results of red-severity checks (node_completeness / deployment_completeness,
whose skip carried the dataclass-default severity="red") as *covered* → the merge-gate
metric reported dag_completeness 100% / passed while ZERO checks ran (a CI-consumed
false-green). This is a visibility-class straggler (the merge-gate metric), SYSTEMATIC,
not Axis-N. **This vindicates the owner's concern "won't Opus alone be wrong?": Opus
round-16 declared CLEAN, but Sonnet caught a real systematic false-green — multi-engine
(Sonnet+Opus) is doing real work, and self-certification by one engine would have been
wrong.** Fixed: (1) compute_dag_completeness excludes skips from covered (severity-
independent guard, reports skip count); (2) node_completeness / deployment_completeness /
depends_on_consistency skip constructors now set severity="info" (audit found all 3;
others already info/amber). Test test_severity_is_red re-pointed to a real violation
(red); added test_empty_values_skips_with_info_severity. full suite 6403.
**Streak reset (round 16 had a systematic finding); round 17 + 18 must both be
systematic-clean.** N-gate passage still gated on owner's exit-line approval.

## Round 17 (2026-06-25, Claude-only): systematic CLEAN (both engines) — 1st of restarted 2
**Opus: CLEAN. Sonnet: CLEAN.** Both ran an adversarial hunt on the exact class Sonnet
caught in round 16 (severity-based aggregation counting skips): confirmed
coverage_metrics.compute_dag_completeness is the ONLY multi-check severity→coverage
aggregator and its skip guard is complete; the other 2 text summaries + deploy gate
exclude skips via passed=False / _summary_skipped; the round-16 skip=info change masks no
real red. Both engines noted the SAME non-systematic residual: depends_on_consistency:70
(the "no propagation output" skip) still omitted status="skip"/severity="info" (carried
dataclass-default red), but it is excluded as a skip by every consumer (skipped flag /
passed=False / coverage_metrics severity-independent guard) → mis-counts nowhere, below
the SYSTEMATIC bar. Tidied anyway for consistency: depends_on:70 now sets
status="skip"+severity="info" (matching :132); test_no_propagation_output_skip_with_warn
locks it. Swept all 15 checks' skip constructors — none inherit default red now
(info/amber only; transitive_closure + ui_coherence carry class-default amber = safe).
full suite 6403. **1st systematic-clean of the restarted streak. Round 18 = 2nd
confirmation (reviews the tidied HEAD); if clean → 2 consecutive → present to owner for
exit-line approval.**
